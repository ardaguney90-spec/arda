#!/usr/bin/env python3
"""
Bip Modülü – küfürlü / gereksiz konuşmaları bipler.

1. Videonun sesini yazıya döker (faster-whisper, Türkçe, kelime kelime zamanlı).
2. kufur_listesi.txt'deki kelimeleri bulur ve otomatik işaretler.
3. İşaretli aralıkları 1 kHz "biiip" ile (ya da sadece sessize alarak) kapatır.
   Görüntüye dokunulmaz, sadece ses yeniden kodlanır; çekim tarihi korunur.

Panel bu modülü kullanır; komut satırından da çalışır:
    python3 bleep.py video.mp4             # yazıya dök + küfürleri biple
    python3 bleep.py KLASOR --model medium # klasördeki tüm videolar
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDLIST = HERE / "kufur_listesi.txt"
PAD = 0.06          # bip, kelimenin biraz öncesinden başlayıp biraz sonrasına taşsın
_models: dict = {}


# --------------------------------------------------------------------------- #
# Türkçe normalize + küfür listesi
# --------------------------------------------------------------------------- #
def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def norm(word: str) -> str:
    return re.sub(r"[^\wçğıöşü]", "", tr_lower(word))


def load_wordlist(path: Path = WORDLIST) -> tuple[set[str], tuple[str, ...]]:
    """Satır başına bir kelime. Sonunda * olan kelime 'ile başlayan' demek (siktir*)."""
    exact, prefix = set(), []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            w = line.split("#")[0].strip()
            if not w:
                continue
            if w.endswith("*"):
                prefix.append(norm(w[:-1]))
            else:
                exact.add(norm(w))
    return exact, tuple(p for p in prefix if p)


def is_bad(word: str, wl=None) -> bool:
    exact, prefix = wl or load_wordlist()
    w = norm(word)
    return bool(w) and (w in exact or w.startswith(prefix))


# --------------------------------------------------------------------------- #
# Yazıya dökme
# --------------------------------------------------------------------------- #
def transcribe(video: Path, model_size: str = "small", language: str = "tr",
               progress=None) -> dict:
    """Döner: {"language", "duration", "segments":[{start,end,text,words:[{s,e,w}]}]}"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper kurulu değil. Kur:  pip install faster-whisper")
    if model_size not in _models:
        _models[model_size] = WhisperModel(model_size, device="auto", compute_type="int8")
    model = _models[model_size]
    segs, info = model.transcribe(str(video), language=language, word_timestamps=True,
                                  vad_filter=True, beam_size=5)
    out = []
    for seg in segs:
        words = [{"s": round(w.start, 3), "e": round(w.end, 3), "w": w.word.strip()}
                 for w in (seg.words or [])]
        out.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                    "text": seg.text.strip(), "words": words})
        if progress and info.duration:
            progress(min(1.0, seg.end / info.duration))
    return {"language": info.language, "duration": info.duration, "segments": out}


def auto_bleeps(transcript: dict, wl=None) -> list[dict]:
    """Küfür listesine uyan kelimeler için bip aralıkları üretir."""
    wl = wl or load_wordlist()
    res = []
    for seg in transcript.get("segments", []):
        for w in seg["words"]:
            if is_bad(w["w"], wl):
                res.append({"start": w["s"], "end": w["e"], "mode": "bip",
                            "text": w["w"], "auto": True})
    return res


# --------------------------------------------------------------------------- #
# Bip uygulama
# --------------------------------------------------------------------------- #
def merge(segments: list[dict], pad: float = PAD) -> list[tuple[float, float, str]]:
    items = sorted(((max(0.0, s["start"] - pad), s["end"] + pad, s.get("mode", "bip"))
                    for s in segments if s["end"] > s["start"]), key=lambda x: x[0])
    out: list[list] = []
    for a, b, m in items:
        if out and a <= out[-1][1] and out[-1][2] == m:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, m])
    return [tuple(x) for x in out]


def _expr(ranges) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in ranges) or "0"


def apply_bleeps(video: Path, segments: list[dict], out: Path, beep_hz: int = 1000,
                 beep_vol: float = 0.25) -> Path:
    """segments: [{start, end, mode: 'bip' | 'sessiz'}]  ->  out (video kopyalanır, ses yeniden)."""
    ranges = merge(segments)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip() or 0)
    has_audio = bool(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.strip())

    beeps = [r for r in ranges if r[2] == "bip"]
    mute = _expr(ranges)
    inputs = ["-i", str(video)]
    if not has_audio:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    src = "1:a" if not has_audio else "0:a"
    fc = (f"[{src}]aresample=48000,aformat=channel_layouts=stereo,"
          f"volume=0:enable='{mute}'[m];"
          f"aevalsrc=exprs='{beep_vol}*sin(2*PI*{beep_hz}*t)':s=48000:c=stereo:d={dur:.3f},"
          f"volume=0:enable='eq(0,{_expr(beeps)})'[b];"
          f"[m][b]amix=inputs=2:duration=first:normalize=0[a]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-map_metadata", "0", "-movflags", "+faststart+use_metadata_tags", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Bip uygulanamadı:\n" + r.stderr[-1500:])
    return out


# --------------------------------------------------------------------------- #
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Videolardaki küfürleri otomatik biple")
    ap.add_argument("path", type=Path, help="Video ya da klasör")
    ap.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--mode", default="bip", choices=["bip", "sessiz"])
    a = ap.parse_args()
    vids = [a.path] if a.path.is_file() else sorted(
        p for p in a.path.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv"})
    wl = load_wordlist()
    for v in vids:
        print(f"▶ {v.name}: yazıya dökülüyor...")
        tr = transcribe(v, a.model)
        segs = auto_bleeps(tr, wl)
        for s in segs:
            s["mode"] = a.mode
        js = v.with_suffix(".transcript.json")
        js.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
        if not segs:
            print("  küfür bulunamadı.")
            continue
        out = v.parent / "_bip" / v.name
        apply_bleeps(v, segs, out)
        print(f"  {len(segs)} yer biplendi -> {out}  ({', '.join(s['text'] for s in segs)})")


if __name__ == "__main__":
    sys.exit(main())
