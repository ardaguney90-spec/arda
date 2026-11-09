#!/usr/bin/env python3
"""
Offroad Edit Paneli
===================

Tarayıcıda açılan video paneli:
  * Videoları sürükle-bırak ile yükle
  * Yukarıda ÇEKİM SIRASI şeridi: sürükleyerek sırayı değiştir, istemediğini çıkar
  * Herhangi bir videoya tıklayıp izle (ya da hepsini sırayla izle)
  * Konuşmaları yazıya dök -> küfürler otomatik kırmızı/bip; kelimeye tıklayıp bip ekle/kaldır,
    gereksiz konuşma satırını sessize al, güzel konuşmaları ⭐ ile açılış fragmanına ekle
  * "Videoyu Oluştur": bipler uygulanır, açılış fragmanı + geçişli ana video + Premiere XML

Çalıştır:
    python3 panel.py                 # ./proje klasörünü kullanır
    python3 panel.py ~/Offroad/2026-kaz-daglari --port 8000
Sonra tarayıcıda http://localhost:8000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import bleep
import offroad_edit as oe

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

DEFAULT_SETTINGS = {
    "title": "", "teaser": 30, "clip_len": "", "res": "1920x1080", "transition": "mix",
    "trans_dur": 0.6, "slowmo": -1, "slowmo_speed": 0.4, "slowmo_len": 2.0,
    "music": "", "keep_clip_audio": True, "quality": "iyi", "skip_start": 0.0,
    "whisper_model": "small",
}


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.videos = root / "videolar"
        self.music = root / "muzik"
        self.out = root / "cikti"
        self.cache = root / ".cache"
        for d in (self.videos, self.music, self.out, self.cache / "thumbs",
                  self.cache / "transcripts", self.cache / "bip"):
            d.mkdir(parents=True, exist_ok=True)
        self.state_file = root / "panel.json"
        self.lock = threading.RLock()
        self.meta: dict[str, tuple[float, oe.Clip]] = {}
        self.jobs: dict[str, dict] = {}
        self.whisper_lock = threading.Lock()
        self.state = {"order": [], "manual_order": False, "excluded": [], "bleeps": {},
                      "quotes": {}, "settings": dict(DEFAULT_SETTINGS)}
        if self.state_file.exists():
            try:
                self.state.update(json.loads(self.state_file.read_text(encoding="utf-8")))
                self.state["settings"] = {**DEFAULT_SETTINGS, **self.state["settings"]}
            except json.JSONDecodeError:
                pass
        self.sync()

    # ---------------------------------------------------------------- state
    def save(self) -> None:
        with self.lock:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_file)

    def clip(self, name: str) -> oe.Clip | None:
        p = self.videos / name
        if not p.exists():
            return None
        mt = p.stat().st_mtime
        cached = self.meta.get(name)
        if cached and cached[0] == mt:
            return cached[1]
        c = oe.probe(p)
        if c:
            self.meta[name] = (mt, c)
        return c

    def sync(self) -> None:
        """Klasördeki dosyalarla sıralama listesini eşitler."""
        with self.lock:
            files = [p.name for p in sorted(self.videos.iterdir())
                     if p.is_file() and p.suffix.lower() in oe.VIDEO_EXTS and not p.name.startswith(".")]
            clips = {n: self.clip(n) for n in files}
            files = [n for n in files if clips[n]]
            order = [n for n in self.state["order"] if n in files]
            key = lambda n: (clips[n].shot_at, oe.gopro_key(clips[n].path), n)
            new = [n for n in files if n not in order]
            if not self.state["manual_order"]:
                order = sorted(files, key=key)
            else:
                for n in sorted(new, key=key):   # yeni geleni zamanına göre araya sok
                    pos = len(order)
                    for i, o in enumerate(order):
                        if key(o) > key(n):
                            pos = i
                            break
                    order.insert(pos, n)
            self.state["order"] = order
            self.state["excluded"] = [n for n in self.state["excluded"] if n in files]
            self.save()

    def transcript_path(self, name: str) -> Path:
        return self.cache / "transcripts" / f"{name}.json"

    def public_state(self) -> dict:
        with self.lock:
            vids = []
            for n in self.state["order"]:
                c = self.clip(n)
                if not c:
                    continue
                vids.append({
                    "name": n, "duration": round(c.duration, 2), "width": c.width, "height": c.height,
                    "fps": round(c.fps, 2), "has_audio": c.has_audio,
                    "shot_at": c.shot_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "time_source": c.time_source, "size": c.path.stat().st_size,
                    "transcribed": self.transcript_path(n).exists(),
                    "bleeps": len(self.state["bleeps"].get(n, [])),
                    "quotes": len(self.state["quotes"].get(n, [])),
                    "excluded": n in self.state["excluded"],
                })
            music = [p.name for p in sorted(self.music.iterdir()) if p.suffix.lower() in AUDIO_EXTS]
            outputs = [p.name for p in sorted(self.out.iterdir(), key=lambda p: -p.stat().st_mtime)
                       if p.is_file()]
            return {"videos": vids, "manual_order": self.state["manual_order"], "music": music,
                    "settings": self.state["settings"], "outputs": outputs,
                    "jobs": sorted(self.jobs.values(), key=lambda j: -j["created"])[:20],
                    "project": str(self.root)}

    # ---------------------------------------------------------------- jobs
    def start_job(self, kind: str, label: str, fn) -> dict:
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "status": "running",
               "progress": 0.0, "message": "Başlıyor...", "result": None, "created": time.time()}
        self.jobs[job["id"]] = job

        def runner():
            try:
                job["result"] = fn(job)
                job["status"], job["progress"] = "done", 1.0
                job["message"] = job.get("message_done") or "Bitti"
            except Exception as e:   # noqa: BLE001
                traceback.print_exc()
                job["status"], job["message"] = "error", str(e)
        threading.Thread(target=runner, daemon=True).start()
        return job

    def transcribe_job(self, name: str, model: str) -> dict:
        def fn(job):
            job["message"] = "Sıra bekleniyor..." if self.whisper_lock.locked() else "Model yükleniyor..."
            with self.whisper_lock:
                job["message"] = "Konuşmalar yazıya dökülüyor..."

                def prog(x):
                    job["progress"] = x
                tr = bleep.transcribe(self.videos / name, model, progress=prog)
            self.transcript_path(name).write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
            with self.lock:
                old = [b for b in self.state["bleeps"].get(name, []) if not b.get("auto")]
                auto = bleep.auto_bleeps(tr)
                self.state["bleeps"][name] = auto + old
                self.save()
            job["message_done"] = f"{len(auto)} küfür bulundu ve biplendi"
            return {"name": name, "auto": len(auto)}
        return self.start_job("transcribe", f"Yazıya dök: {name}", fn)

    def bleeped_path(self, name: str) -> Path:
        """Bipler uygulanmış kopya (önbellekli). Bip yoksa orijinal dosya."""
        segs = self.state["bleeps"].get(name, [])
        src = self.videos / name
        if not segs:
            return src
        h = hashlib.sha1(json.dumps([[s["start"], s["end"], s.get("mode")] for s in segs]).encode()
                         + str(src.stat().st_mtime).encode()).hexdigest()[:10]
        out = self.cache / "bip" / f"{Path(name).stem}.{h}{src.suffix}"
        if not out.exists():
            for old in (self.cache / "bip").glob(f"{Path(name).stem}.*{src.suffix}"):
                old.unlink(missing_ok=True)
            bleep.apply_bleeps(src, segs, out)
        return out

    def render_job(self) -> dict:
        st = dict(self.state["settings"])

        def fn(job):
            with self.lock:
                names = [n for n in self.state["order"] if n not in self.state["excluded"]]
                quotes_state = json.loads(json.dumps(self.state["quotes"]))
            if not names:
                raise RuntimeError("Birleştirilecek video yok.")
            clips = []
            for i, n in enumerate(names):
                job["message"] = f"Bipler uygulanıyor ({i + 1}/{len(names)})"
                job["progress"] = 0.05 * i / len(names)
                c = replace(self.clip(n))
                c.path = self.bleeped_path(n).resolve()
                clips.append(c)

            clip_len = float(st["clip_len"]) if str(st["clip_len"]).strip() else None
            slow_n = int(st["slowmo"]) if int(st["slowmo"]) >= 0 else max(1, len(clips) // 3)
            teaser = float(st["teaser"] or 0)
            for i, c in enumerate(clips):
                job["message"] = f"Extreme anlar aranıyor ({i + 1}/{len(clips)})"
                job["progress"] = 0.05 + 0.2 * i / len(clips)
                if slow_n or clip_len or teaser:
                    oe.analyze_motion(c, float(st["slowmo_len"]))
            trans = float(st["trans_dur"])
            oe.plan(clips, clip_len, trans, float(st["skip_start"] or 0))
            if slow_n:
                oe.plan_slowmo(clips, count=slow_n, seg=float(st["slowmo_len"]),
                               speed=float(st["slowmo_speed"]), manual={}, trans=trans)

            w, h = map(int, st["res"].split("x"))
            music = self.music / st["music"] if st["music"] else None
            if music and not music.exists():
                music = None
            fast = st["quality"] != "iyi"
            common = dict(width=w, height=h, fps=30, music=music, music_vol=0.9,
                          keep_clip_audio=bool(st["keep_clip_audio"]), crf=23 if fast else 20,
                          preset="veryfast" if fast else "medium", smooth_slowmo=False)
            title = st["title"].strip() or None
            safe = re.sub(r"[^\w\-]+", "_", title or "offroad_edit").strip("_") or "offroad_edit"
            final = self.out / f"{safe}.mp4"

            def stage(lo, hi, msg):
                def p(x):
                    job["progress"] = lo + (hi - lo) * x
                    job["message"] = f"{msg} %{int(x * 100)}"
                return p

            if teaser:
                by_name = {n: c for n, c in zip(names, clips)}
                quotes = [(by_name[n], q["start"], q["end"], q["text"])
                          for n in names for q in quotes_state.get(n, [])]
                tclips = oe.build_teaser(clips, length=teaser, quotes=quotes)
                t_out, m_out = self.out / f".{safe}_fragman.mp4", self.out / f".{safe}_ana.mp4"
                oe.render(tclips, t_out, trans=0.25, style="mix", title=None,
                          progress=stage(0.25, 0.4, "Açılış fragmanı oluşturuluyor"), **common)
                oe.render(clips, m_out, trans=trans, style=st["transition"], title=title,
                          progress=stage(0.4, 0.97, "Ana video oluşturuluyor"), **common)
                job["message"] = "Birleştiriliyor..."
                oe.concat_videos([t_out, m_out], final)
                t_out.unlink(missing_ok=True)
                m_out.unlink(missing_ok=True)
            else:
                oe.render(clips, final, trans=trans, style=st["transition"], title=title,
                          progress=stage(0.25, 0.97, "Video oluşturuluyor"), **common)
            oe.premiere_xml(clips, self.out / f"{safe}_premiere.xml", name=title or safe,
                            width=w, height=h, fps=30, trans=trans, style=st["transition"])
            oe.write_plan(clips, self.out / f"{safe}_edit_plan.csv", st["transition"])
            job["message_done"] = "Video hazır!"
            return {"video": final.name, "xml": f"{safe}_premiere.xml", "csv": f"{safe}_edit_plan.csv"}
        return self.start_job("render", "Videoyu oluştur", fn)

    def thumb(self, name: str) -> Path | None:
        c = self.clip(name)
        if not c:
            return None
        t = self.cache / "thumbs" / f"{name}.{int(c.path.stat().st_mtime)}.jpg"
        if not t.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.5, c.duration / 2):.2f}",
                            "-i", str(c.path), "-frames:v", "1", "-vf", "scale=320:-2", str(t)])
        return t if t.exists() else None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def safe_name(name: str) -> str:
    name = Path(unquote(name)).name.strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    if not name or name in {".", ".."}:
        raise ValueError("geçersiz dosya adı")
    return name


class Handler(BaseHTTPRequestHandler):
    project: Project = None   # type: ignore
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # sessiz
        pass

    # ---------- yardımcılar
    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path: Path, download: bool = False):
        if not path or not path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".mov", ".m4v"}:
            ctype = "video/mp4"   # tarayıcılar H.264 .mov'u böyle daha iyi oynatır
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{path.name}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        P = self.project
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send_file(STATIC / "panel.html")
            if u.path == "/api/state":
                return self.send_json(P.public_state())
            if u.path == "/api/transcript":
                name = safe_name(q["name"])
                tp = P.transcript_path(name)
                tr = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
                return self.send_json({"transcript": tr, "bleeps": P.state["bleeps"].get(name, []),
                                       "quotes": P.state["quotes"].get(name, [])})
            if u.path.startswith("/thumb/"):
                return self.send_file(P.thumb(safe_name(u.path[7:])))
            m = re.match(r"^/files/(videolar|muzik|cikti)/(.+)$", u.path)
            if m:
                base = {"videolar": P.videos, "muzik": P.music, "cikti": P.out}[m.group(1)]
                return self.send_file(base / safe_name(m.group(2)), download="dl" in q)
            self.send_error(HTTPStatus.NOT_FOUND)
        except (KeyError, ValueError) as e:
            self.send_json({"error": str(e)}, 400)

    # ---------- PUT (yükleme)
    def do_PUT(self):
        P = self.project
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path != "/api/upload":
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            name = safe_name(q["name"])
        except (KeyError, ValueError) as e:
            return self.send_json({"error": str(e)}, 400)
        ext = Path(name).suffix.lower()
        kind = q.get("kind", "video")
        if kind == "music":
            if ext not in AUDIO_EXTS:
                return self.send_json({"error": "Müzik dosyası değil"}, 400)
            dest = P.music / name
        else:
            if ext not in oe.VIDEO_EXTS:
                return self.send_json({"error": f"Desteklenmeyen video türü: {ext}"}, 400)
            dest = P.videos / name
        left = int(self.headers.get("Content-Length") or 0)
        tmp = dest.with_name(f".{dest.name}.part")
        with tmp.open("wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left > 0:
            tmp.unlink(missing_ok=True)
            return self.send_json({"error": "Yükleme yarıda kesildi"}, 400)
        tmp.replace(dest)
        lm = q.get("lastModified")
        if lm and lm.isdigit():   # telefondaki dosya tarihini koru (sıralama için yedek bilgi)
            t = int(lm) / 1000
            os.utime(dest, (t, t))
        if kind == "music":
            with P.lock:
                P.state["settings"]["music"] = name
                P.save()
        else:
            P.meta.pop(name, None)
            if not P.clip(name):
                dest.unlink(missing_ok=True)
                return self.send_json({"error": f"{name} okunamadı (bozuk ya da video değil)"}, 400)
            P.sync()
        self.send_json({"ok": True, "name": name})

    # ---------- POST
    def do_POST(self):
        P = self.project
        u = urlparse(self.path)
        try:
            d = self.read_json()
            st = P.state
            if u.path == "/api/order":
                with P.lock:
                    names = set(st["order"])
                    st["order"] = [n for n in d["order"] if n in names] + \
                                  [n for n in st["order"] if n not in d["order"]]
                    st["manual_order"] = True
                    P.save()
            elif u.path == "/api/sort":
                with P.lock:
                    st["manual_order"] = False
                P.sync()
            elif u.path == "/api/exclude":
                with P.lock:
                    ex = set(st["excluded"])
                    (ex.add if d["excluded"] else ex.discard)(d["name"])
                    st["excluded"] = sorted(ex)
                    P.save()
            elif u.path == "/api/delete":
                name = safe_name(d["name"])
                base = P.music if d.get("kind") == "music" else P.videos
                (base / name).unlink(missing_ok=True)
                if d.get("kind") != "music":
                    P.transcript_path(name).unlink(missing_ok=True)
                    with P.lock:
                        st["bleeps"].pop(name, None)
                        st["quotes"].pop(name, None)
                P.sync()
            elif u.path == "/api/edits":
                name = safe_name(d["name"])
                with P.lock:
                    st["bleeps"][name] = [
                        {"start": float(b["start"]), "end": float(b["end"]),
                         "mode": "sessiz" if b.get("mode") == "sessiz" else "bip",
                         "text": str(b.get("text", ""))[:200], "auto": bool(b.get("auto"))}
                        for b in d.get("bleeps", [])]
                    st["quotes"][name] = [
                        {"start": float(x["start"]), "end": float(x["end"]),
                         "text": str(x.get("text", ""))[:200]} for x in d.get("quotes", [])]
                    P.save()
            elif u.path == "/api/settings":
                with P.lock:
                    for k, v in d.items():
                        if k in DEFAULT_SETTINGS:
                            st["settings"][k] = v
                    P.save()
            elif u.path == "/api/transcribe":
                name = safe_name(d["name"])
                if any(j["kind"] == "transcribe" and j["status"] == "running" and
                       j["result"] is None and j["label"].endswith(name) for j in P.jobs.values()):
                    return self.send_json({"error": "Zaten yazıya dökülüyor"}, 409)
                job = P.transcribe_job(name, d.get("model") or st["settings"]["whisper_model"])
                return self.send_json({"ok": True, "job": job})
            elif u.path == "/api/render":
                if any(j["kind"] == "render" and j["status"] == "running" for j in P.jobs.values()):
                    return self.send_json({"error": "Zaten bir video oluşturuluyor"}, 409)
                return self.send_json({"ok": True, "job": P.render_job()})
            else:
                return self.send_error(HTTPStatus.NOT_FOUND)
            self.send_json({"ok": True})
        except (KeyError, ValueError, TypeError) as e:
            self.send_json({"error": str(e)}, 400)


def main() -> None:
    ap = argparse.ArgumentParser(description="Offroad Edit Paneli")
    ap.add_argument("project", nargs="?", type=Path, default=Path("proje"),
                    help="Proje klasörü (videolar/, muzik/, cikti/ burada tutulur)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 yaparsan aynı Wi-Fi'deki telefonlar da yükleyebilir")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"HATA: '{tool}' bulunamadı. ffmpeg kur: https://ffmpeg.org/download.html")
    Handler.project = Project(a.project.resolve())
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://localhost:{a.port}"
    print(f"🚙 Offroad Edit Paneli: {url}\n   Proje klasörü: {a.project.resolve()}\n   Kapatmak için Ctrl+C")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatıldı.")


if __name__ == "__main__":
    main()
