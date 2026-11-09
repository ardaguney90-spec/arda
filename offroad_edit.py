#!/usr/bin/env python3
"""
Offroad Edit Bot
================

Bir klasördeki tüm videoları ÇEKİM SIRASINA göre dizer ve:

  1. Hepsini geçiş efektleriyle tek bir MP4'te BİRLEŞTİRİR (ffmpeg).
  2. Premiere Pro'ya import edilecek bir timeline (FCP7 XML) üretir:
     klipler sırayla dizilir, aralarına Cross Dissolve konur ve her kesime
     "AtomX: <geçiş adı>" marker'ı eklenir -> AtomX panelinden o geçişi
     marker'ın olduğu yere sürükleyip bırakman yeterli.
  3. Her klipteki en hareketli/extreme anı bulur (zıplama, çamur sıçratma, rampa)
     ve en iyi anları otomatik SLOW-MOTION yapar. Premiere timeline'ında da
     "SLOW-MO" marker'ı olarak işaretlenir.
  4. Sıralama listesini (edit_plan.csv) yazar, kontrol edebilesin diye.

Kullanım:
    python3 offroad_edit.py VIDEO_KLASORU [seçenekler]

Örnek:
    python3 offroad_edit.py ~/Videolar/kaz_daglari_turu \
        --title "Kaz Dağları Offroad" --clip-len 6 --music muzik.mp3
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".3gp", ".insv", ".lrv"}

# ffmpeg xfade geçişleri – offroad/aksiyon havasına uyanlar.
# Her birinin yanında Premiere'de AtomX'ten seçebileceğin benzer geçiş önerisi var.
TRANSITIONS = [
    ("zoomin",     "Zoom In / Punch Zoom"),
    ("slideleft",  "Slide / Whip Pan Left"),
    ("circleopen", "Shape Reveal Circle"),
    ("smoothleft", "Smooth Slide Left"),
    ("fadewhite",  "Flash / Light Leak"),
    ("radial",     "Spin / Rotate"),
    ("wiperight",  "Wipe Right"),
    ("slideup",    "Whip Pan Up"),
    ("pixelize",   "Glitch"),
    ("dissolve",   "Cross Dissolve / Film Burn"),
]


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def need(tool: str) -> None:
    if not shutil.which(tool):
        sys.exit(f"HATA: '{tool}' bulunamadı. ffmpeg kur: https://ffmpeg.org/download.html")


def parse_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# Telefon / kamera dosya adlarındaki tarih: VID_20240512_103455, PXL_20240512_103455123,
# 20240512_103455, IMG_20240512-103455 ...
NAME_DT = re.compile(r"(20\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})")


@dataclass
class Clip:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    shot_at: datetime
    time_source: str
    rotation: int = 0
    # render sırasında doldurulur
    start: float = 0.0
    length: float = 0.0          # zaman çizelgesindeki süre (slow-mo dahil)
    src_len: float = 0.0         # kaynaktan kullanılan süre
    motion: list = field(default_factory=list)   # (zaman, hareket skoru)
    peak_t: float = 0.0
    peak_score: float = 0.0
    slow: tuple | None = None    # (kaynak başlangıç sn, kaynak süre sn, hız 0-1)
    caption: str = ""            # ekranda altyazı (fragmandaki konuşmalar)
    gain: float = 1.0            # müzik varken klip sesinin seviyesi çarpanı


def probe(path: Path) -> Clip | None:
    try:
        out = run(["ffprobe", "-v", "error", "-print_format", "json",
                   "-show_format", "-show_streams", str(path)]).stdout
    except subprocess.CalledProcessError:
        print(f"  ! okunamadı, atlanıyor: {path.name}")
        return None
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    duration = float(info["format"].get("duration") or v.get("duration") or 0)
    if duration <= 0:
        return None

    num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0

    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    rotation = int(v.get("tags", {}).get("rotate", rotation))
    w, h = int(v["width"]), int(v["height"])
    if abs(rotation) % 180 == 90:
        w, h = h, w

    # Çekim zamanı: 1) metadata  2) dosya adı  3) dosya değişim tarihi
    tags = {**info["format"].get("tags", {}), **v.get("tags", {})}
    shot, src = None, ""
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        for k, val in tags.items():
            if k.lower() == key:
                shot = parse_dt(val)
                if shot and shot.year > 2000:
                    src = f"metadata:{key}"
                    break
                shot = None
        if shot:
            break
    if not shot:
        m = NAME_DT.search(path.stem)
        if m:
            try:
                shot = datetime(*map(int, m.groups())).astimezone(timezone.utc)
                src = "dosya adı"
            except ValueError:
                shot = None
    if not shot:
        shot = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        src = "dosya tarihi"

    return Clip(path=path.resolve(), duration=duration, width=w, height=h, fps=fps,
                has_audio=has_audio, shot_at=shot, time_source=src, rotation=rotation)


def gopro_key(p: Path) -> tuple:
    """GoPro bölünmüş dosyalar (GX010123, GX020123) aynı çekimin parçalarıdır."""
    m = re.match(r"^G[HXPL](\d{2})(\d{4})$", p.stem, re.I)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    return (0, 0)


def collect(folder: Path, recursive: bool) -> list[Clip]:
    files = folder.rglob("*") if recursive else folder.iterdir()
    paths = sorted(p for p in files if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                   and not p.name.startswith("."))
    clips = []
    for p in paths:
        c = probe(p)
        if c:
            clips.append(c)
    clips.sort(key=lambda c: (c.shot_at, gopro_key(c.path), c.path.name))
    return clips


# --------------------------------------------------------------------------- #
# Kesim planı
# --------------------------------------------------------------------------- #
def analyze_motion(c: Clip, window: float) -> None:
    """Klibin her anı için hareket skoru çıkarır, en yoğun anı (peak) bulur."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(c.path), "-an",
           "-vf", "fps=10,scale=160:-2,select='gte(scene,0)',metadata=print:file=-",
           "-f", "null", "-"]
    try:
        out = run(cmd).stdout
    except subprocess.CalledProcessError:
        return
    pts, scores = [], []
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            pts.append(float(m.group(1)))
        m = re.search(r"scene_score=([\d.]+)", line)
        if m:
            scores.append(float(m.group(1)))
    data = list(zip(pts, scores))
    if not data:
        return
    c.motion = data
    w = max(1, int(window * 10))
    best_i, best = 0, -1.0
    for i in range(len(data)):
        seg = [s for _, s in data[i:i + w]]
        avg = sum(seg) / len(seg)
        if avg > best:
            best, best_i = avg, i
    c.peak_t = data[best_i][0] + min(window, data[-1][0] - data[best_i][0]) / 2
    c.peak_score = best


def plan(clips: list[Clip], clip_len: float | None, trans: float, skip_start: float) -> None:
    """Her klipten hangi kısmın kullanılacağını belirler."""
    min_len = trans * 2 + 0.5
    for c in clips:
        usable_from = min(skip_start, max(0.0, c.duration - min_len))
        avail = c.duration - usable_from
        if clip_len and avail > clip_len:
            if c.motion:
                # En hareketli anı ortalayacak şekilde kes
                st = c.peak_t - clip_len / 2
            else:
                st = usable_from + (avail - clip_len) / 2
            c.start = min(max(st, usable_from), c.duration - clip_len)
            c.src_len = clip_len
        else:
            c.start = usable_from
            c.src_len = avail
        c.length = c.src_len
    for c in clips:
        if c.length < min_len:
            print(f"  ! {c.path.name} çok kısa ({c.length:.1f}s), geçiş süresi kısaltılabilir.")


def plan_slowmo(clips: list[Clip], *, count: int, seg: float, speed: float,
                manual: dict[str, list[tuple[float, float]]], trans: float) -> None:
    """Slow-motion uygulanacak anları seçer (manuel liste veya en extreme anlar)."""
    def apply(c: Clip, at: float, dur: float) -> None:
        lo, hi = c.start + trans, c.start + c.src_len - trans
        dur = min(dur, hi - lo)
        if dur < 0.4:
            return
        st = min(max(at, lo), hi - dur)
        c.slow = (st, dur, speed)
        c.length = c.src_len + dur * (1 / speed - 1)

    if manual:
        for c in clips:
            for at, dur in manual.get(c.path.name, [])[:1]:
                apply(c, at, dur)
        return
    ranked = sorted((c for c in clips if c.motion), key=lambda c: c.peak_score, reverse=True)
    for c in ranked[:count]:
        apply(c, c.peak_t - seg / 2, seg)


def read_slowmo_file(path: Path) -> dict[str, list[tuple[float, float]]]:
    """Satır biçimi:  dosya_adi.mp4  12.5  [2.0]   (saniye, isteğe bağlı süre)"""
    res: dict[str, list[tuple[float, float]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        nums = []
        while parts and re.fullmatch(r"[\d.:]+", parts[-1]) and len(nums) < 2:
            v = parts.pop()
            if ":" in v:
                mm, ss = v.split(":")
                nums.insert(0, int(mm) * 60 + float(ss))
            else:
                nums.insert(0, float(v))
        if not nums:
            continue
        res.setdefault(" ".join(parts), []).append((nums[0], nums[1] if len(nums) > 1 else 2.0))
    return res


def atempo_chain(speed: float) -> str:
    f, out = speed, []
    while f < 0.5:
        out.append("atempo=0.5")
        f /= 0.5
    out.append(f"atempo={f:.4f}")
    return ",".join(out)


def pick_transitions(n: int, style: str) -> list[tuple[str, str]]:
    if style == "mix":
        return [TRANSITIONS[i % len(TRANSITIONS)] for i in range(n)]
    found = next((t for t in TRANSITIONS if t[0] == style), (style, style))
    return [found] * n


def caption_filter(text: str, tmpdir: Path, i: int) -> str:
    f = tmpdir / f"cap{i}.txt"
    f.write_text(text, encoding="utf-8")
    path = f.as_posix().replace(":", "\\:")
    return (f"drawtext=textfile='{path}':fontcolor=white:fontsize=h/18:"
            f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h-text_h-h/10")


def top_moments(c: Clip, window: float, k: int) -> list[tuple[float, float]]:
    """Klipteki birbirine çakışmayan en hareketli k an: [(merkez_sn, skor)]."""
    if not c.motion:
        return []
    w = max(1, int(window * 10))
    scored = []
    for i in range(0, max(1, len(c.motion) - w + 1)):
        seg = c.motion[i:i + w]
        scored.append((seg[0][0] + window / 2, sum(x for _, x in seg) / len(seg)))
    scored.sort(key=lambda x: x[1], reverse=True)
    picked: list[tuple[float, float]] = []
    for t, sc in scored:
        if all(abs(t - p) >= window * 1.5 for p, _ in picked):
            picked.append((t, sc))
        if len(picked) >= k:
            break
    return picked


def build_teaser(clips: list[Clip], *, length: float = 30.0, cut: float = 2.0,
                 trans: float = 0.25, quotes: list[tuple[Clip, float, float, str]] | None = None
                 ) -> list[Clip]:
    """Açılış fragmanı: en extreme anlardan hızlı kesimler + seçilen konuşma kesitleri.

    quotes: [(klip, başlangıç_sn, bitiş_sn, yazı)] – konuşma kesitleri (altyazılı, ses yüksek).
    Döner: render()'a verilebilecek kısa klip listesi (çekim sırasına göre).
    """
    items: list[tuple[float, Clip]] = []   # (sıralama anahtarı, klip parçası)
    budget = length
    for q_clip, qs, qe, text in (quotes or []):
        qs, qe = max(0.0, qs - 0.15), min(q_clip.duration, qe + 0.25)
        qe = min(qe, qs + 7.0)
        if qe - qs < 0.8 or budget - (qe - qs - trans) < cut:
            continue
        part = replace(q_clip, start=qs, src_len=qe - qs, length=qe - qs, motion=[], slow=None,
                       caption=text, gain=3.0)
        items.append((q_clip.shot_at.timestamp() + qs, part))
        budget -= part.length - trans

    per_clip = [(c, top_moments(c, cut, 3)) for c in clips]
    cand = sorted(((m[0][1], c, m[0][0]) for c, m in per_clip if m), key=lambda x: -x[0])
    cand += sorted(((sc, c, t) for c, m in per_clip for t, sc in m[1:]), key=lambda x: -x[0])
    for sc, c, t in cand:   # önce her klibin en iyi anı, sonra ikinci/üçüncü anlar
        if budget < cut * 0.6:
            break
        dur = min(cut, budget + trans, c.duration)
        st = min(max(0.0, t - dur / 2), max(0.0, c.duration - dur))
        if any(p.path == c.path and abs(p.start - st) < dur for _, p in items):
            continue
        part = replace(c, start=st, src_len=dur, length=dur, motion=[], slow=None, caption="", gain=1.0)
        items.append((c.shot_at.timestamp() + st, part))
        budget -= dur - trans
    # hiç hareket analizi yoksa: her klibin ortasından
    if not cand and not items:
        for c in clips:
            if budget < cut * 0.6:
                break
            dur = min(cut, c.duration)
            st = max(0.0, c.duration / 2 - dur / 2)
            items.append((c.shot_at.timestamp() + st,
                          replace(c, start=st, src_len=dur, length=dur, motion=[], slow=None)))
            budget -= dur - trans
    items.sort(key=lambda x: x[0])
    return [p for _, p in items]


def concat_videos(parts: list[Path], out: Path) -> None:
    """Aynı ayarlarla render edilmiş videoları yeniden kodlamadan uç uca ekler."""
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                        "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart",
                        str(out)], capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("Birleştirme (concat) başarısız:\n" + r.stderr[-1500:])


# --------------------------------------------------------------------------- #
# 1) ffmpeg ile birleştirme
# --------------------------------------------------------------------------- #
def render(clips: list[Clip], out: Path, *, width: int, height: int, fps: int,
           trans: float, style: str, music: Path | None, music_vol: float,
           keep_clip_audio: bool, title: str | None, crf: int, preset: str,
           smooth_slowmo: bool, progress=None) -> float:
    """progress: isteğe bağlı fonksiyon(0..1) – panel ilerleme çubuğu için."""
    n = len(clips)
    transitions = pick_transitions(max(n - 1, 0), style)
    inputs: list[str] = []
    fc: list[str] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="offroad_"))

    for c in clips:
        inputs += ["-ss", f"{c.start:.3f}", "-t", f"{c.src_len:.3f}", "-i", str(c.path)]
    idx = n
    audio_idx = []
    for i, c in enumerate(clips):
        if c.has_audio:
            audio_idx.append(f"{i}:a")
        else:  # sessiz klip için boş ses üret
            inputs += ["-f", "lavfi", "-t", f"{c.src_len:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_idx.append(f"{idx}:a")
            idx += 1
    music_input = None
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_input = idx

    # Görüntü: hepsini aynı çözünürlük/fps'e getir. Dikey videolar için
    # arkaya bulanık kopyası konur (siyah bant yerine).
    def clip_gain(c: Clip) -> float:
        # Müzik yoksa klip sesi olduğu gibi. Müzik varsa motor sesi kısık (ya da kapalı),
        # konuşma kesitleri (altyazılı olanlar) her zaman duyulur.
        if not music:
            return 1.0
        if c.caption:
            return 1.1
        return 0.35 * c.gain if keep_clip_audio else 0.0

    for i, c in enumerate(clips):
        afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if c.slow:
            s0 = c.slow[0] - c.start
            s1 = s0 + c.slow[1]
            k = 1 / c.slow[2]
            interp = (f",minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1"
                      if smooth_slowmo and c.fps < fps * k * 0.9 else "")
            fc.append(
                f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,split=3[{i}p0][{i}p1][{i}p2];"
                f"[{i}p0]trim=0:{s0:.3f},setpts=PTS-STARTPTS,fps={fps}[{i}q0];"
                f"[{i}p1]trim={s0:.3f}:{s1:.3f},setpts=(PTS-STARTPTS)*{k:.4f}{interp},fps={fps}[{i}q1];"
                f"[{i}p2]trim={s1:.3f},setpts=PTS-STARTPTS,fps={fps}[{i}q2];"
                f"[{i}q0][{i}q1][{i}q2]concat=n=3:v=1:a=0[{i}raw]")
            fc.append(
                f"[{audio_idx[i]}]asetpts=PTS-STARTPTS,{afmt},asplit=3[{i}b0][{i}b1][{i}b2];"
                f"[{i}b0]atrim=0:{s0:.3f},asetpts=PTS-STARTPTS[{i}c0];"
                f"[{i}b1]atrim={s0:.3f}:{s1:.3f},asetpts=PTS-STARTPTS,{atempo_chain(c.slow[2])}[{i}c1];"
                f"[{i}b2]atrim={s1:.3f},asetpts=PTS-STARTPTS[{i}c2];"
                f"[{i}c0][{i}c1][{i}c2]concat=n=3:v=0:a=1,volume={clip_gain(c):.3f}[a{i}]")
        else:
            fc.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps}[{i}raw]")
            fc.append(f"[{audio_idx[i]}]asetpts=PTS-STARTPTS,{afmt},volume={clip_gain(c):.3f}[a{i}]")
        fc.append(
            f"[{i}raw]split[{i}fg0][{i}bg0];"
            f"[{i}bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:5,eq=brightness=-0.08[{i}bg];"
            f"[{i}fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[{i}fg];"
            f"[{i}bg][{i}fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,fps={fps},settb=1/{fps}"
            + (f",{caption_filter(c.caption, tmpdir, i)}" if c.caption else "") + f"[v{i}]"
        )

    # xfade zinciri
    vlast, alast = "v0", "a0"
    offset = clips[0].length
    starts = [0.0]
    for i in range(1, n):
        t = min(trans, clips[i - 1].length / 2, clips[i].length / 2)
        offset -= t
        starts.append(offset)
        name = transitions[i - 1][0]
        fc.append(f"[{vlast}][v{i}]xfade=transition={name}:duration={t:.3f}:offset={offset:.3f}[vx{i}]")
        fc.append(f"[{alast}][a{i}]acrossfade=d={t:.3f}:c1=tri:c2=tri[ax{i}]")
        vlast, alast = f"vx{i}", f"ax{i}"
        offset += clips[i].length
    total = offset

    # Başlık + açılış/kapanış kararması
    vf = []
    if title:
        safe = title.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
        vf.append(
            f"drawtext=text='{safe}':fontcolor=white:fontsize=h/12:borderw=4:bordercolor=black@0.6:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"alpha='if(lt(t,0.5),t/0.5,if(lt(t,3),1,if(lt(t,3.8),(3.8-t)/0.8,0)))':enable='lt(t,4)'"
        )
    vf.append(f"fade=t=in:st=0:d=0.8,fade=t=out:st={max(total - 1.2, 0):.3f}:d=1.2")
    fc.append(f"[{vlast}]{','.join(vf)}[vout]")

    if music_input is not None:
        fc.append(f"[{music_input}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                  f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={music_vol}[mus]")
        # konuşma kesitlerinde müziği kıs (ducking)
        duck = "+".join(f"between(t,{starts[i]:.3f},{starts[i] + c.length:.3f})"
                        for i, c in enumerate(clips) if c.caption)
        if duck:
            fc.append(f"[mus]volume=0.2:enable='{duck}'[musd]")
        mus = "musd" if duck else "mus"
        if keep_clip_audio or duck:
            fc.append(f"[{alast}][{mus}]amix=inputs=2:duration=first:normalize=0[amix]")
            alast = "amix"
        else:
            alast = mus
    fc.append(f"[{alast}]afade=t=in:st=0:d=0.8,afade=t=out:st={max(total - 2, 0):.3f}:d=2[aout]")

    script = out.with_suffix(".filtergraph.txt")
    script.write_text(";\n".join(fc))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           *(["-progress", "pipe:1", "-nostats"] if progress else ["-stats"]), *inputs,
           "-filter_complex_script", str(script),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-t", f"{total:.3f}", str(out)]
    print(f"\n▶ Render başlıyor: {n} klip, ~{total:.0f} sn -> {out.name}")
    if progress:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                progress(min(1.0, int(line.strip()[12:]) / 1e6 / max(total, 0.1)))
        err = proc.stderr.read()
        rc = proc.wait()
    else:
        rc, err = subprocess.run(cmd).returncode, ""
    if rc != 0:
        raise RuntimeError(f"ffmpeg render başarısız (filtre grafiği: {script})\n{err[-2000:]}")
    script.unlink(missing_ok=True)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"✔ Birleştirilmiş video hazır: {out}")
    return total


# --------------------------------------------------------------------------- #
# 2) Premiere Pro XML (File > Import ile açılır)
# --------------------------------------------------------------------------- #
def premiere_xml(clips: list[Clip], out: Path, *, name: str, width: int, height: int,
                 fps: int, trans: float, style: str) -> None:
    transitions = pick_transitions(max(len(clips) - 1, 0), style)
    tf = max(2, int(round(trans * fps)))
    half = tf // 2
    rate = f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"

    def fr(sec: float) -> int:
        return int(round(sec * fps))

    def pathurl(p: Path) -> str:
        s = p.as_posix()
        if re.match(r"^[A-Za-z]:/", s):   # Windows yolu
            s = "/" + s
        return "file://localhost" + quote(s)

    v_items, a_items, markers = [], [], []
    pos = 0
    for i, c in enumerate(clips):
        src_dur = fr(c.duration)
        # geçişlerin ortalanabilmesi için her iki uçta "handle" bırak
        cin = max(fr(c.start), half if i > 0 else 0)
        cout = min(fr(c.start + c.src_len), src_dur - (half if i < len(clips) - 1 else 0))
        if cout - cin < tf + 2:
            cin, cout = 0, src_dur
        length = cout - cin
        start, end = pos, pos + length
        fid = f"file-{i + 1}"
        file_el = (
            f'<file id="{fid}"><name>{escape(c.path.name)}</name>'
            f"<pathurl>{escape(pathurl(c.path))}</pathurl>{rate}<duration>{src_dur}</duration>"
            f"<media><video><samplecharacteristics>{rate}<width>{c.width}</width>"
            f"<height>{c.height}</height></samplecharacteristics></video>"
            + ("<audio><channelcount>2</channelcount></audio>" if c.has_audio else "")
            + "</media></file>"
        )
        common = (f"<name>{escape(c.path.name)}</name><enabled>TRUE</enabled>"
                  f"<duration>{src_dur}</duration>{rate}<start>{start}</start><end>{end}</end>"
                  f"<in>{cin}</in><out>{cout}</out>")
        link = (f'<link><linkclipref>v-{i+1}</linkclipref><mediatype>video</mediatype></link>'
                f'<link><linkclipref>a-{i+1}</linkclipref><mediatype>audio</mediatype></link>'
                if c.has_audio else "")
        v_items.append(f'<clipitem id="v-{i+1}">{common}{file_el}{link}</clipitem>')
        if c.has_audio:
            a_items.append(f'<clipitem id="a-{i+1}">{common}<file id="{fid}"/>'
                           f"<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex>"
                           f"</sourcetrack>{link}</clipitem>")
        if i < len(clips) - 1:
            tname = transitions[i][1]
            v_items.append(
                f"<transitionitem>{rate}<start>{end - half}</start><end>{end - half + tf}</end>"
                f"<alignment>center</alignment><effect><name>Cross Dissolve</name>"
                f"<effectid>Cross Dissolve</effectid><effectcategory>Dissolve</effectcategory>"
                f"<effecttype>transition</effecttype><mediatype>video</mediatype></effect>"
                f"</transitionitem>")
            markers.append(
                f"<marker><name>AtomX: {escape(tname)}</name>"
                f"<comment>{escape(c.path.name)} -&gt; {escape(clips[i+1].path.name)}</comment>"
                f"<in>{end}</in><out>-1</out></marker>")
        if c.slow:
            at = start + fr(c.slow[0]) - cin
            markers.append(
                f"<marker><name>SLOW-MO %{int(c.slow[2] * 100)}</name>"
                f"<comment>{escape(c.path.name)}: {c.slow[1]:.1f} sn ağırlat "
                f"(Time Remapping / Speed {int(c.slow[2] * 100)}%)</comment>"
                f"<in>{at}</in><out>{at + fr(c.slow[1])}</out></marker>")
        pos = end

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="seq-1">
<name>{escape(name)}</name>
<duration>{pos}</duration>
{rate}
<media>
<video>
<format><samplecharacteristics>{rate}<width>{width}</width><height>{height}</height>
<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>
</samplecharacteristics></format>
<track>
{chr(10).join(v_items)}
</track>
</video>
<audio>
<track>
{chr(10).join(a_items)}
</track>
</audio>
</media>
{chr(10).join(markers)}
</sequence>
</xmeml>
"""
    out.write_text(xml, encoding="utf-8")
    print(f"✔ Premiere Pro timeline'ı hazır: {out}")


def write_plan(clips: list[Clip], out: Path, style: str) -> None:
    transitions = pick_transitions(len(clips), style)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sira", "dosya", "cekim_zamani", "zaman_kaynagi", "sure_sn",
                    "kullanilan_bas_sn", "kullanilan_sure_sn", "hareket_skoru", "slowmo",
                    "sonraki_gecis", "atomx_onerisi"])
        for i, c in enumerate(clips):
            nxt = transitions[i] if i < len(clips) - 1 else ("", "")
            w.writerow([i + 1, c.path.name, c.shot_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                        c.time_source, f"{c.duration:.1f}", f"{c.start:.1f}", f"{c.src_len:.1f}",
                        f"{c.peak_score:.3f}",
                        (f"{c.slow[0]:.1f}s +{c.slow[1]:.1f}s @%{int(c.slow[2]*100)}" if c.slow else ""),
                        nxt[0], nxt[1]])
    print(f"✔ Sıralama listesi: {out}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Offroad grup videolarını çekim sırasına göre birleştiren bot")
    ap.add_argument("folder", type=Path, help="Videoların olduğu klasör")
    ap.add_argument("-o", "--out", type=Path, help="Çıktı klasörü (varsayılan: KLASOR/_edit)")
    ap.add_argument("--title", default=None, help="Açılışta görünecek başlık, ör. 'Kaz Dağları Offroad'")
    ap.add_argument("--clip-len", type=float, default=None,
                    help="Her klipten en fazla kaç saniye kullanılsın (varsayılan: tamamı)")
    ap.add_argument("--skip-start", type=float, default=0.0,
                    help="Her klibin başından atılacak saniye (kayıt tuşuna basma sarsıntısı)")
    ap.add_argument("--transition", default="mix",
                    help="Geçiş tipi: mix (karışık) veya xfade adı (zoomin, slideleft, dissolve...)")
    ap.add_argument("--trans-dur", type=float, default=0.6, help="Geçiş süresi (sn)")
    ap.add_argument("--music", type=Path, help="Arka plan müziği (mp3/wav)")
    ap.add_argument("--music-vol", type=float, default=0.9)
    ap.add_argument("--keep-clip-audio", action="store_true",
                    help="Müzik varken klip seslerini (motor sesi) kısık şekilde koru")
    ap.add_argument("--res", default="1920x1080", help="Çıkış çözünürlüğü (dikey için 1080x1920)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", type=int, default=20, help="Kalite (düşük = daha iyi, 18-23)")
    ap.add_argument("--preset", default="medium", help="x264 hızı: veryfast/fast/medium/slow")
    ap.add_argument("--slowmo", type=int, default=None, metavar="N",
                    help="En extreme N ana otomatik slow-motion uygula (varsayılan: klip sayısı/3, 0=kapalı)")
    ap.add_argument("--slowmo-file", type=Path,
                    help="Elle slow-mo listesi: her satır 'dosya.mp4 SANIYE [SURE]'")
    ap.add_argument("--slowmo-speed", type=float, default=0.4,
                    help="Slow-mo hızı (0.4 = %%40 hız, 2.5x yavaş). 60/120fps çekimde en akıcı olur")
    ap.add_argument("--slowmo-len", type=float, default=2.0, help="Ağırlatılacak anın kaynak süresi (sn)")
    ap.add_argument("--smooth-slowmo", action="store_true",
                    help="Düşük fps çekimlerde ara kare üret (çok daha akıcı ama render yavaşlar)")
    ap.add_argument("--no-analyze", action="store_true",
                    help="Hareket analizini atla (hızlı, ama otomatik slow-mo/en iyi an seçimi olmaz)")
    ap.add_argument("--teaser", type=float, default=0, metavar="SN",
                    help="Başa SN saniyelik açılış fragmanı ekle (en extreme anlar), ör. --teaser 30")
    ap.add_argument("--recursive", action="store_true", help="Alt klasörleri de tara")
    ap.add_argument("--no-render", action="store_true", help="Sadece Premiere XML + liste üret")
    ap.add_argument("--no-xml", action="store_true", help="Premiere XML üretme")
    a = ap.parse_args()

    need("ffmpeg"); need("ffprobe")
    if not a.folder.is_dir():
        sys.exit(f"HATA: klasör yok: {a.folder}")
    width, height = map(int, a.res.lower().split("x"))
    out_dir = a.out or (a.folder / "_edit")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = a.title or a.folder.resolve().name

    print(f"▶ Videolar taranıyor: {a.folder}")
    clips = [c for c in collect(a.folder, a.recursive) if out_dir.resolve() not in c.path.parents]
    if not clips:
        sys.exit("HATA: klasörde video bulunamadı.")
    manual = read_slowmo_file(a.slowmo_file) if a.slowmo_file else {}
    slow_n = a.slowmo if a.slowmo is not None else max(1, len(clips) // 3)
    if not a.no_analyze and (slow_n > 0 or a.clip_len or a.teaser) and not manual:
        print("▶ Hareket analizi (extreme anları buluyor)...")
        for c in clips:
            analyze_motion(c, a.slowmo_len)
    plan(clips, a.clip_len, a.trans_dur, a.skip_start)
    if slow_n > 0 or manual:
        plan_slowmo(clips, count=slow_n, seg=a.slowmo_len, speed=a.slowmo_speed,
                    manual=manual, trans=a.trans_dur)

    print(f"\n  Çekim sırası ({len(clips)} klip):")
    for i, c in enumerate(clips, 1):
        print(f"  {i:>3}. {c.shot_at.astimezone():%d.%m %H:%M:%S}  {c.path.name:<32} "
              f"{c.length:6.1f}s  ({c.time_source})"
              + (f"  🐢 slow-mo @ {c.slow[0]:.1f}s" if c.slow else ""))

    safe = re.sub(r"[^\w\-]+", "_", name).strip("_") or "offroad"
    write_plan(clips, out_dir / f"{safe}_edit_plan.csv", a.transition)
    if not a.no_xml:
        premiere_xml(clips, out_dir / f"{safe}_premiere.xml", name=name, width=width,
                     height=height, fps=a.fps, trans=a.trans_dur, style=a.transition)
    if not a.no_render:
        common = dict(width=width, height=height, fps=a.fps, music=a.music, music_vol=a.music_vol,
                      keep_clip_audio=a.keep_clip_audio, crf=a.crf, preset=a.preset,
                      smooth_slowmo=a.smooth_slowmo)
        final = out_dir / f"{safe}.mp4"
        try:
            if a.teaser:
                teaser = build_teaser(clips, length=a.teaser)
                print(f"\n▶ Açılış fragmanı: {len(teaser)} kesim")
                t_out, m_out = out_dir / f"{safe}_fragman.mp4", out_dir / f"{safe}_ana.mp4"
                render(teaser, t_out, trans=0.25, style="mix", title=None, **common)
                render(clips, m_out, trans=a.trans_dur, style=a.transition, title=a.title, **common)
                concat_videos([t_out, m_out], final)
                m_out.unlink(missing_ok=True)
                print(f"✔ Fragman + ana video: {final}")
            else:
                render(clips, final, trans=a.trans_dur, style=a.transition, title=a.title, **common)
        except RuntimeError as e:
            sys.exit(f"HATA: {e}")


if __name__ == "__main__":
    main()
