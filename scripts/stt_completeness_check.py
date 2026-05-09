"""
Offline STT completeness checker.

Measures Character Error Rate (CER) and Coverage against a reference VTT subtitle.
Runs locally using SenseVoice (or --engine groq) — no translation API calls.

Usage:
    python scripts/stt_completeness_check.py \\
        --audio audio/LGcLBC9_RUk.wav \\
        --ref   ../subtitles/LGcLBC9_RUk.ko.vtt

    # First N seconds only
    python scripts/stt_completeness_check.py --audio ... --ref ... --duration 60

    # Compare engines
    python scripts/stt_completeness_check.py --audio ... --ref ... --engine groq

Download audio first:
    yt-dlp -x --audio-format wav --audio-quality 0 \\
           -o audio/LGcLBC9_RUk.wav "https://youtu.be/LGcLBC9_RUk"
"""
from __future__ import annotations

import argparse
import re
import sys
import os
import time
from pathlib import Path
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force UTF-8 on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import cfg

# ── VTT Parsing ──────────────────────────────────────────────────────────────

_INLINE_TAG = re.compile(r"<[^>]+>")
_TIMING_RE  = re.compile(
    r"(\d+):(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+):(\d+\.\d+)"
)


def _ts(h, m, s) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


@dataclass
class Segment:
    start: float
    end:   float
    text:  str


def parse_vtt(path: str) -> list[Segment]:
    """
    Parse a YouTube VTT file into clean (start, end, text) segments.

    YouTube VTT repeats accumulated text across cues. We extract only the NEW
    text per content cue (duration > 50ms) by taking the last text line, which
    corresponds to the newly-spoken words in that window.
    """
    content = Path(path).read_text(encoding="utf-8")
    blocks  = re.split(r"\n\n+", content.strip())

    segments: list[Segment] = []
    prev_text = ""

    for block in blocks:
        lines = [l.rstrip() for l in block.splitlines()]

        timing_line = next((l for l in lines if "-->" in l), None)
        if not timing_line:
            continue
        m = _TIMING_RE.search(timing_line)
        if not m:
            continue

        start = _ts(m.group(1), m.group(2), m.group(3))
        end   = _ts(m.group(4), m.group(5), m.group(6))

        # Skip very short end-cap cues (< 50ms)
        if end - start < 0.05:
            continue

        text_lines = [
            _INLINE_TAG.sub("", l).strip()
            for l in lines
            if "-->" not in l
            and l.strip() not in ("", " ", "WEBVTT", "Kind: captions", "Language: ko")
        ]
        if not text_lines:
            continue

        # YouTube VTT content cues: line 0 = previous accumulated text,
        # last line = new text for this cue (may have had <c> word timing).
        new_text = text_lines[-1].strip()
        if not new_text or new_text == prev_text:
            continue

        segments.append(Segment(start=start, end=end, text=new_text))
        prev_text = new_text

    return segments


def build_ref_chunks(segments: list[Segment], chunk_sec: float, total_sec: float
                     ) -> list[str]:
    """Assign each VTT segment to the audio chunk that contains its start time."""
    n_chunks = int(np.ceil(total_sec / chunk_sec))
    chunks   = [""] * n_chunks

    for seg in segments:
        idx = int(seg.start / chunk_sec)
        if idx < n_chunks:
            sep = " " if chunks[idx] else ""
            chunks[idx] += sep + seg.text

    return chunks


# ── Metrics ──────────────────────────────────────────────────────────────────

def cer(ref: str, hyp: str) -> float:
    """Character Error Rate at character level (spaces ignored)."""
    import editdistance
    ref_c = ref.replace(" ", "")
    hyp_c = hyp.replace(" ", "")
    if not ref_c:
        return 0.0
    return editdistance.eval(ref_c, hyp_c) / len(ref_c)


def coverage(ref: str, hyp: str) -> float:
    """Fraction of reference characters (greedy match) found in hypothesis."""
    ref_chars = list(ref.replace(" ", ""))
    hyp_chars = list(hyp.replace(" ", ""))
    if not ref_chars:
        return 1.0
    j = found = 0
    for c in ref_chars:
        while j < len(hyp_chars) and hyp_chars[j] != c:
            j += 1
        if j < len(hyp_chars):
            found += 1
            j += 1
    return found / len(ref_chars)


# ── STT Runner ───────────────────────────────────────────────────────────────

def load_audio(path: str, target_sr: int = 16000) -> np.ndarray:
    import soundfile as sf
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    if sr != target_sr:
        try:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr)
        except ImportError:
            print(f"  WARNING: audio is {sr}Hz, expected {target_sr}Hz. "
                  "pip install librosa for auto-resampling.", file=sys.stderr)
    return audio


def run_stt_chunks(audio: np.ndarray, chunk_sec: float, engine: str
                   ) -> list[tuple[str, float]]:
    """Transcribe audio in fixed chunks. Returns list of (text, latency_sec)."""
    from modules.stt import STTEngine

    eng = STTEngine.__new__(STTEngine)
    eng._sense_voice = None
    eng._groq_client = None
    eng._use_groq = (engine == "groq")

    if engine == "groq":
        eng._init_groq()
    else:
        eng._load_sense_voice()

    sr     = cfg.audio.sample_rate
    cs     = int(chunk_sec * sr)
    result = []

    for i, offset in enumerate(range(0, len(audio), cs)):
        chunk = audio[offset : offset + cs]
        if len(chunk) < sr // 2:      # skip trailing chunk < 0.5s
            break
        t0   = time.monotonic()
        text = eng.transcribe(chunk) or ""
        lat  = time.monotonic() - t0
        result.append((text, lat))
        preview = text[:60] if text else "(empty)"
        print(f"  [{i+1:3d}] {offset/sr:6.1f}s–{min(offset+cs, len(audio))/sr:6.1f}s  "
              f"lat={lat:.2f}s  {preview}", flush=True)

    return result


def run_stt_vad(audio: np.ndarray, engine: str,
                silence_sec: float = 0.6,
                min_speech_sec: float = 0.2,
                max_segment_sec: float = 30.0) -> list[tuple[str, float]]:
    """Transcribe audio by accumulating until silence is detected.

    - `silence_sec`: seconds of continuous low-volume to consider end-of-utterance
    - `min_speech_sec`: minimum speech length to send to STT
    - `max_segment_sec`: force split if segment exceeds this length
    """
    from modules.stt import STTEngine

    eng = STTEngine.__new__(STTEngine)
    eng._sense_voice = None
    eng._groq_client = None
    eng._use_groq = (engine == "groq")

    if engine == "groq":
        eng._init_groq()
    else:
        eng._load_sense_voice()

    sr = cfg.audio.sample_rate
    frame_sec = 0.02
    frame_len = max(1, int(frame_sec * sr))

    def rms(x: np.ndarray) -> float:
        return float(np.sqrt(np.mean(x.astype("float32") ** 2)))

    results = []
    seg_buf = []
    seg_samples = 0
    silent_time = 0.0
    seg_start_time = 0.0
    i_seg = 0

    # iterate frames
    for f_idx, offset in enumerate(range(0, len(audio), frame_len)):
        frame = audio[offset: offset + frame_len]
        if frame.size == 0:
            break
        level = rms(frame)
        is_silent = level < getattr(cfg.audio, "volume_threshold", 0.001)

        if seg_samples == 0 and not is_silent:
            seg_start_time = offset / sr

        if not is_silent:
            seg_buf.append(frame)
            seg_samples += frame.size
            silent_time = 0.0
        else:
            if seg_samples > 0:
                silent_time += frame_len / sr

        # finalize if we've seen silence long enough or segment too long
        seg_duration = seg_samples / sr
        if seg_samples > 0 and (silent_time >= silence_sec or seg_duration >= max_segment_sec):
            # only send if long enough
            if seg_duration >= min_speech_sec:
                seg_audio = np.concatenate(seg_buf) if len(seg_buf) > 1 else (seg_buf[0] if seg_buf else np.array([], dtype="float32"))
                t0 = time.monotonic()
                text = eng.transcribe(seg_audio) or ""
                lat = time.monotonic() - t0
                results.append((text, lat))
                i_seg += 1
                end_time = seg_start_time + seg_duration
                preview = text[:60] if text else "(empty)"
                print(f"  [{i_seg:3d}] {seg_start_time:6.1f}s–{end_time:6.1f}s  lat={lat:.2f}s  {preview}", flush=True)

            # reset buffer
            seg_buf = []
            seg_samples = 0
            silent_time = 0.0

    # final flush
    if seg_samples > 0:
        seg_duration = seg_samples / sr
        if seg_duration >= min_speech_sec:
            seg_audio = np.concatenate(seg_buf) if len(seg_buf) > 1 else (seg_buf[0] if seg_buf else np.array([], dtype="float32"))
            t0 = time.monotonic()
            text = eng.transcribe(seg_audio) or ""
            lat = time.monotonic() - t0
            results.append((text, lat))
            i_seg += 1
            end_time = seg_start_time + seg_duration
            preview = text[:60] if text else "(empty)"
            print(f"  [{i_seg:3d}] {seg_start_time:6.1f}s–{end_time:6.1f}s  lat={lat:.2f}s  {preview}", flush=True)

    return results


# ── Report ───────────────────────────────────────────────────────────────────

@dataclass
class ChunkResult:
    idx:     int
    start:   float
    ref:     str
    hyp:     str
    cer_val: float
    cov_val: float
    latency: float


def print_report(results: list[ChunkResult], chunk_sec: float, engine: str):
    all_ref = "".join(r.ref.replace(" ", "") for r in results)
    all_hyp = "".join(r.hyp.replace(" ", "") for r in results)

    overall_cer = cer(all_ref, all_hyp)
    overall_cov = coverage(all_ref, all_hyp)
    avg_lat     = sum(r.latency for r in results) / max(len(results), 1)

    has_ref   = [r for r in results if r.ref]
    bad_chunks = sorted(has_ref, key=lambda x: x.cer_val, reverse=True)[:8]
    empty_hyp  = sum(1 for r in has_ref if not r.hyp)

    print("\n" + "═" * 62)
    print("  STT COMPLETENESS REPORT")
    print("═" * 62)
    print(f"  Engine       : {engine}")
    print(f"  Chunk size   : {chunk_sec}s  (config chunk_seconds={cfg.audio.chunk_seconds})")
    print(f"  Chunks total : {len(results)}  (with ref: {len(has_ref)})")
    print(f"  Avg latency  : {avg_lat:.2f}s / chunk")
    print()
    print(f"  Overall CER      : {overall_cer:.1%}   (target < 20%)")
    print(f"  Overall Coverage : {overall_cov:.1%}   (target > 80%)")
    print()

    if bad_chunks:
        print("  ── Worst chunks (highest CER) ──────────────────────")
        for r in bad_chunks:
            if not r.ref:
                continue
            print(f"  [{r.start:6.1f}s]  CER={r.cer_val:.0%}  cov={r.cov_val:.0%}")
            print(f"    REF: {r.ref[:72]}")
            print(f"    HYP: {(r.hyp or '(empty)')[:72]}")
        print()

    print("  ── Parameter suggestions ───────────────────────────")

    ok = True

    if empty_hyp > len(has_ref) * 0.15:
        ok = False
        pct = empty_hyp / len(has_ref)
        print(f"  ⚠  {empty_hyp}/{len(has_ref)} ref-chunks returned empty  ({pct:.0%})")
        print(f"     Possible causes:")
        print(f"       1. chunk_seconds={chunk_sec:.0f} too short → raise to {int(chunk_sec)+2}")
        print(f"       2. volume_threshold={cfg.audio.volume_threshold} too high → lower to 0.005")
        print(f"       3. Audio format/sample-rate mismatch")

    if overall_cer > 0.30:
        ok = False
        print(f"  ⚠  CER {overall_cer:.1%} > 30% — high error rate")
        if engine == "sensevoice":
            print("       → re-run with --engine groq to check if SenseVoice is the bottleneck")
        else:
            print("       → audio quality issue or language mismatch")

    if overall_cov < 0.75:
        ok = False
        print(f"  ⚠  Coverage {overall_cov:.1%} < 75% — words being cut off at chunk boundaries")
        new_chunk = min(int(chunk_sec) + 2, 6)
        print(f"       → try: cfg.audio.chunk_seconds = {new_chunk}")
        new_batch = cfg.stt.batch_size_s + 30
        print(f"       → or:  cfg.stt.batch_size_s   = {new_batch}")

    if avg_lat > chunk_sec:
        ok = False
        print(f"  ⚠  Avg latency {avg_lat:.2f}s > chunk size {chunk_sec:.0f}s → pipeline falls behind")
        print(f"       → ensure cfg.stt.sensevoice_device = 'cuda'")
        print(f"       → or use --engine groq (cloud, usually < 1s)")

    if ok:
        print("  ✓  STT quality looks healthy")
        print("     Next step: test translation with scripts/debug_pipeline.py --stage translate")

    print("═" * 62)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure STT CER and coverage against a reference VTT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--audio",    required=True, help="Path to WAV audio file")
    parser.add_argument("--ref",      required=True, help="Path to .vtt subtitle file")
    parser.add_argument("--engine",   choices=["sensevoice", "groq"], default="sensevoice")
    parser.add_argument("--duration", type=float, default=None,
                        help="Only test first N seconds (default: full file)")
    parser.add_argument("--chunk",    type=float, default=float(cfg.audio.chunk_seconds),
                        help=f"Chunk size in seconds (default: {cfg.audio.chunk_seconds})")
    args = parser.parse_args()

    if not Path(args.audio).exists():
        print(f"\nError: audio file not found: {args.audio}")
        print("Download with:")
        print("  yt-dlp -x --audio-format wav --audio-quality 0 \\")
        print("         -o audio/LGcLBC9_RUk.wav https://youtu.be/LGcLBC9_RUk")
        sys.exit(1)

    print(f"\n  Loading audio : {args.audio}")
    audio = load_audio(args.audio, cfg.audio.sample_rate)

    if args.duration:
        audio = audio[:int(args.duration * cfg.audio.sample_rate)]

    total_sec = len(audio) / cfg.audio.sample_rate
    print(f"  Audio length  : {total_sec:.1f}s")

    print(f"\n  Parsing ref   : {args.ref}")
    segments   = parse_vtt(args.ref)
    ref_segs   = [s for s in segments if s.start < total_sec]
    ref_chunks = build_ref_chunks(ref_segs, args.chunk, total_sec)
    ref_chars  = sum(len(s.text.replace(" ", "")) for s in ref_segs)
    print(f"  Ref segments  : {len(ref_segs)}  (total chars: {ref_chars})")

    n_chunks = int(total_sec / args.chunk)
    print(f"\n  Running STT ({args.engine}) — {n_chunks} chunks × {args.chunk}s …\n")
    hyp_pairs = run_stt_chunks(audio, args.chunk, args.engine)

    results = []
    for i, (hyp_text, lat) in enumerate(hyp_pairs):
        ref_text = ref_chunks[i] if i < len(ref_chunks) else ""
        results.append(ChunkResult(
            idx=i, start=i * args.chunk,
            ref=ref_text, hyp=hyp_text,
            cer_val=cer(ref_text, hyp_text),
            cov_val=coverage(ref_text, hyp_text),
            latency=lat,
        ))

    print_report(results, args.chunk, args.engine)


if __name__ == "__main__":
    main()
