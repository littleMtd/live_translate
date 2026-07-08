from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "scratch" / "ocr_gate_scout"


@dataclass(frozen=True)
class FrameRef:
    frame_index: int
    timestamp_s: float
    path: Path
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class OCRBox:
    engine: str
    frame_index: int
    timestamp_s: float
    frame_path: str
    frame_width: int | None
    frame_height: int | None
    text: str
    confidence: float
    bbox_xywh: tuple[int, int, int, int]
    high_confidence: bool
    text_len: int
    hangul_char_count: int
    hangul_char_ratio: float


def run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], cmd_name: str) -> None:
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{cmd_name} failed with exit code {result.returncode}: {details}")


def find_executable(explicit: str | None, env_name: str, names: list[str]) -> str | None:
    if explicit:
        return explicit
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def resolve_tesseract(explicit: str | None) -> str | None:
    found = find_executable(explicit, "TESSERACT_CMD", ["tesseract", "tesseract.exe"])
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def resolve_ffprobe(ffmpeg_cmd: str) -> str | None:
    explicit = os.environ.get("FFPROBE_CMD")
    if explicit:
        return explicit
    sibling = Path(ffmpeg_cmd).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def parse_thresholds(raw: str, engines: list[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {"default": 70.0}
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                thresholds["default"] = float(item)
                continue
            key, value = item.split("=", 1)
            thresholds[key.strip().lower()] = float(value.strip())
    return {engine: thresholds.get(engine, thresholds["default"]) for engine in engines}


def hangul_count(text: str) -> int:
    return sum(1 for char in text if "\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff")


def text_stats(text: str) -> tuple[int, int, float]:
    stripped = "".join(char for char in text if not char.isspace())
    count = hangul_count(stripped)
    return len(stripped), count, (count / len(stripped) if stripped else 0.0)


def confidence_distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "p90": None}
    ordered = sorted(values)
    p90_index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.9) - 1)
    return {
        "min": round(ordered[0], 3),
        "median": round(statistics.median(ordered), 3),
        "p90": round(ordered[p90_index], 3),
    }


def probe_image_size(ffprobe_cmd: str | None, image_path: Path) -> tuple[int | None, int | None]:
    if not ffprobe_cmd:
        return None, None
    result = run(
        [
            ffprobe_cmd,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(image_path),
        ]
    )
    if result.returncode != 0:
        return None, None
    raw = result.stdout.strip().splitlines()
    if not raw or "x" not in raw[0]:
        return None, None
    width_raw, height_raw = raw[0].split("x", 1)
    try:
        return int(width_raw), int(height_raw)
    except ValueError:
        return None, None


def extract_frames(
    recording: Path,
    frames_dir: Path,
    *,
    ffmpeg_cmd: str,
    ffprobe_cmd: str | None,
    interval_sec: float,
    max_frames: int | None,
) -> list[FrameRef]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()
    fps = 1.0 / interval_sec
    cmd = [
        ffmpeg_cmd,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(recording),
        "-vf",
        f"fps={fps:.8f}",
        "-q:v",
        "2",
    ]
    if max_frames:
        cmd.extend(["-frames:v", str(max_frames)])
    cmd.append(str(frames_dir / "frame_%06d.jpg"))
    result = run(cmd)
    require_success(result, "ffmpeg frame extraction")
    frames: list[FrameRef] = []
    for index, path in enumerate(sorted(frames_dir.glob("frame_*.jpg"))):
        width, height = probe_image_size(ffprobe_cmd, path)
        frames.append(
            FrameRef(
                frame_index=index,
                timestamp_s=round(index * interval_sec, 3),
                path=path,
                width=width,
                height=height,
            )
        )
    if not frames:
        raise RuntimeError("ffmpeg produced no frames")
    return frames


def normalize_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def tesseract_boxes(
    frame: FrameRef,
    *,
    tesseract_cmd: str,
    lang: str,
    psm: str,
    threshold: float,
) -> list[OCRBox]:
    result = run([tesseract_cmd, str(frame.path), "stdout", "-l", lang, "--psm", psm, "tsv"])
    require_success(result, "tesseract")
    reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t")
    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        if row.get("level") != "5":
            continue
        confidence = normalize_confidence(row.get("conf"))
        if confidence <= 0:
            continue
        key = (
            row.get("page_num", ""),
            row.get("block_num", ""),
            row.get("par_num", ""),
            row.get("line_num", ""),
        )
        grouped[key].append(row)

    boxes: list[OCRBox] = []
    for rows in grouped.values():
        words: list[str] = []
        confidences: list[float] = []
        lefts: list[int] = []
        tops: list[int] = []
        rights: list[int] = []
        bottoms: list[int] = []
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                left = int(float(row.get("left") or 0))
                top = int(float(row.get("top") or 0))
                width = int(float(row.get("width") or 0))
                height = int(float(row.get("height") or 0))
            except ValueError:
                continue
            if width <= 0 or height <= 0:
                continue
            words.append(text)
            confidences.append(normalize_confidence(row.get("conf")))
            lefts.append(left)
            tops.append(top)
            rights.append(left + width)
            bottoms.append(top + height)
        if not words:
            continue
        text = " ".join(words).strip()
        confidence = sum(confidences) / len(confidences)
        text_len, hangul_chars, hangul_ratio = text_stats(text)
        left = min(lefts)
        top = min(tops)
        right = max(rights)
        bottom = max(bottoms)
        boxes.append(
            OCRBox(
                engine="tesseract",
                frame_index=frame.frame_index,
                timestamp_s=frame.timestamp_s,
                frame_path=str(frame.path),
                frame_width=frame.width,
                frame_height=frame.height,
                text=text,
                confidence=round(confidence, 3),
                bbox_xywh=(left, top, max(1, right - left), max(1, bottom - top)),
                high_confidence=confidence >= threshold,
                text_len=text_len,
                hangul_char_count=hangul_chars,
                hangul_char_ratio=round(hangul_ratio, 4),
            )
        )
    return boxes


class PaddleEngine:
    def __init__(self, *, lang: str):
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError as exc:
            raise RuntimeError("paddleocr is not installed in this environment") from exc

        try:
            self._ocr = PaddleOCR(
                lang=lang,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
            self._mode = "auto"
        except TypeError:
            self._ocr = PaddleOCR(use_angle_cls=True, lang=lang)
            self._mode = "ocr"

    def boxes(self, frame: FrameRef, *, threshold: float) -> list[OCRBox]:
        if self._mode == "ocr":
            raw = self._ocr.ocr(str(frame.path), cls=True)
        else:
            raw = self._ocr.ocr(str(frame.path))
        return paddle_result_to_boxes(raw, frame, threshold=threshold)


def _paddle_rows(raw: Any) -> list[tuple[Any, str, float]]:
    rows: list[tuple[Any, str, float]] = []
    if raw is None:
        return rows
    if isinstance(raw, dict):
        texts = raw.get("rec_texts") or raw.get("texts") or []
        scores = raw.get("rec_scores") or raw.get("scores") or []
        polys = raw.get("rec_polys") or raw.get("dt_polys") or raw.get("boxes") or []
        for poly, text, score in zip(polys, texts, scores):
            rows.append((poly, str(text or ""), normalize_confidence(score)))
        return rows
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rows.extend(_paddle_rows(item))
                continue
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            poly = item[0]
            payload = item[1]
            if isinstance(payload, (list, tuple)) and len(payload) >= 2:
                rows.append((poly, str(payload[0] or ""), normalize_confidence(payload[1])))
    return rows


def polygon_to_xywh(poly: Any) -> tuple[int, int, int, int] | None:
    points: list[tuple[float, float]] = []
    try:
        for point in poly:
            if len(point) < 2:
                continue
            points.append((float(point[0]), float(point[1])))
    except TypeError:
        return None
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left = int(min(xs))
    top = int(min(ys))
    right = int(max(xs))
    bottom = int(max(ys))
    return left, top, max(1, right - left), max(1, bottom - top)


def paddle_result_to_boxes(raw: Any, frame: FrameRef, *, threshold: float) -> list[OCRBox]:
    boxes: list[OCRBox] = []
    for poly, text, confidence in _paddle_rows(raw):
        text = text.strip()
        if not text:
            continue
        bbox = polygon_to_xywh(poly)
        if bbox is None:
            continue
        text_len, hangul_chars, hangul_ratio = text_stats(text)
        boxes.append(
            OCRBox(
                engine="paddle",
                frame_index=frame.frame_index,
                timestamp_s=frame.timestamp_s,
                frame_path=str(frame.path),
                frame_width=frame.width,
                frame_height=frame.height,
                text=text,
                confidence=round(confidence, 3),
                bbox_xywh=bbox,
                high_confidence=confidence >= threshold,
                text_len=text_len,
                hangul_char_count=hangul_chars,
                hangul_char_ratio=round(hangul_ratio, 4),
            )
        )
    return boxes


def box_to_dict(box: OCRBox) -> dict[str, Any]:
    return {
        "engine": box.engine,
        "frame_index": box.frame_index,
        "timestamp_s": box.timestamp_s,
        "frame_path": box.frame_path,
        "frame_width": box.frame_width,
        "frame_height": box.frame_height,
        "text": box.text,
        "confidence": box.confidence,
        "bbox_xywh": list(box.bbox_xywh),
        "high_confidence": box.high_confidence,
        "text_len": box.text_len,
        "hangul_char_count": box.hangul_char_count,
        "hangul_char_ratio": box.hangul_char_ratio,
    }


def build_summary(
    boxes: list[OCRBox],
    *,
    frame_count: int,
    thresholds: dict[str, float],
    engine_errors: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    engines = sorted(set(thresholds) | {box.engine for box in boxes} | set(engine_errors))
    for engine in engines:
        engine_boxes = [box for box in boxes if box.engine == engine]
        confidences = [box.confidence for box in engine_boxes]
        high_boxes = [box for box in engine_boxes if box.high_confidence]
        total_chars = sum(box.text_len for box in engine_boxes)
        hangul_chars = sum(box.hangul_char_count for box in engine_boxes)
        dist = confidence_distribution(confidences)
        rows.append(
            {
                "engine": engine,
                "status": "error" if engine in engine_errors else "ok",
                "error": engine_errors.get(engine, ""),
                "confidence_threshold": thresholds.get(engine),
                "frame_count": frame_count,
                "box_count": len(engine_boxes),
                "avg_boxes_per_frame": round(len(engine_boxes) / frame_count, 3) if frame_count else 0,
                "high_confidence_box_count": len(high_boxes),
                "high_confidence_box_ratio": round(len(high_boxes) / len(engine_boxes), 4) if engine_boxes else 0,
                "confidence_min": dist["min"],
                "confidence_median": dist["median"],
                "confidence_p90": dist["p90"],
                "avg_text_len": round(sum(box.text_len for box in engine_boxes) / len(engine_boxes), 3)
                if engine_boxes
                else 0,
                "hangul_char_ratio": round(hangul_chars / total_chars, 4) if total_chars else 0,
                "boxes_with_hangul_ratio": round(
                    sum(1 for box in engine_boxes if box.hangul_char_count > 0) / len(engine_boxes),
                    4,
                )
                if engine_boxes
                else 0,
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def choose_eyeball_boxes(boxes: list[OCRBox], *, sample_size: int, seed: int) -> list[OCRBox]:
    rng = random.Random(seed)
    candidates = [box for box in boxes if box.high_confidence and box.text.strip()]
    hangul_candidates = [box for box in candidates if box.hangul_char_count > 0]
    if hangul_candidates:
        candidates = hangul_candidates
    by_engine: dict[str, list[OCRBox]] = defaultdict(list)
    for box in candidates:
        by_engine[box.engine].append(box)
    for rows in by_engine.values():
        rng.shuffle(rows)
    engines = sorted(by_engine)
    selected: list[OCRBox] = []
    while len(selected) < sample_size and any(by_engine.values()):
        for engine in engines:
            if len(selected) >= sample_size:
                break
            if by_engine[engine]:
                selected.append(by_engine[engine].pop())
    return selected


def crop_box(
    box: OCRBox,
    *,
    crop_path: Path,
    ffmpeg_cmd: str,
    pad: int = 8,
) -> Path:
    frame = Path(box.frame_path)
    x, y, width, height = box.bbox_xywh
    frame_width = box.frame_width
    frame_height = box.frame_height
    x = max(0, x - pad)
    y = max(0, y - pad)
    width = width + pad * 2
    height = height + pad * 2
    if frame_width is not None:
        width = min(width, max(1, frame_width - x))
    if frame_height is not None:
        height = min(height, max(1, frame_height - y))
    if width <= 1 or height <= 1:
        shutil.copyfile(frame, crop_path)
        return crop_path
    result = run(
        [
            ffmpeg_cmd,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(frame),
            "-vf",
            f"crop={width}:{height}:{x}:{y}",
            "-frames:v",
            "1",
            str(crop_path),
        ]
    )
    if result.returncode != 0:
        shutil.copyfile(frame, crop_path)
    return crop_path


def write_eyeball_page(
    out_dir: Path,
    boxes: list[OCRBox],
    *,
    ffmpeg_cmd: str,
    sample_size: int,
    seed: int,
) -> tuple[Path, Path]:
    crops_dir = out_dir / "eyeball_crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    selected = choose_eyeball_boxes(boxes, sample_size=sample_size, seed=seed)
    items: list[dict[str, Any]] = []
    for index, box in enumerate(selected, start=1):
        crop_name = f"item_{index:03d}_{box.engine}_f{box.frame_index:06d}.jpg"
        crop_path = crop_box(box, crop_path=crops_dir / crop_name, ffmpeg_cmd=ffmpeg_cmd)
        item = box_to_dict(box)
        item.update(
            {
                "item_id": f"E{index:03d}",
                "crop_path": str(crop_path.relative_to(out_dir)),
                "human_label": "",
            }
        )
        items.append(item)
    items_path = out_dir / "gate1_eyeball_items.json"
    items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path = out_dir / "gate1_eyeball.html"
    html_path.write_text(render_eyeball_html(items), encoding="utf-8")
    return html_path, items_path


def render_eyeball_html(items: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in items:
        item_json = html.escape(json.dumps(item, ensure_ascii=False), quote=True)
        text = html.escape(str(item["text"]))
        cards.append(
            f"""
<section class="card" data-item='{item_json}'>
  <div class="meta">{html.escape(item['item_id'])} · {html.escape(item['engine'])} · frame {item['frame_index']} · {item['timestamp_s']}s · conf {item['confidence']}</div>
  <img src="{html.escape(item['crop_path'])}" alt="crop">
  <pre>{text}</pre>
  <div class="buttons">
    <button data-label="correct">讀對</button>
    <button data-label="partial">半對</button>
    <button data-label="noise">雜訊</button>
  </div>
</section>"""
        )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>Gate 1 OCR Eyeball</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f6f6f6; color: #111; }}
.toolbar {{ position: sticky; top: 0; background: #fff; border: 1px solid #ccc; padding: 12px; margin-bottom: 16px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }}
.card {{ background: #fff; border: 1px solid #ccc; padding: 10px; border-radius: 4px; }}
.meta {{ font-size: 12px; color: #555; margin-bottom: 8px; }}
img {{ max-width: 100%; background: #222; display: block; margin-bottom: 8px; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; min-height: 42px; }}
button {{ margin-right: 6px; padding: 6px 10px; }}
button.selected {{ outline: 3px solid #111; }}
textarea {{ width: 100%; height: 220px; margin-top: 8px; }}
</style>
</head>
<body>
<div class="toolbar">
  <button id="export">Export annotations JSON</button>
  <span id="status"></span>
  <textarea id="output" placeholder="annotations export appears here"></textarea>
</div>
<main class="grid">
{''.join(cards)}
</main>
<script>
const key = "gate1_ocr_eyeball_annotations";
const state = JSON.parse(localStorage.getItem(key) || "{{}}");
function save() {{ localStorage.setItem(key, JSON.stringify(state)); }}
function refresh(card) {{
  const item = JSON.parse(card.dataset.item);
  const label = state[item.item_id] || "";
  card.querySelectorAll("button[data-label]").forEach(button => {{
    button.classList.toggle("selected", button.dataset.label === label);
  }});
}}
document.querySelectorAll(".card").forEach(card => {{
  refresh(card);
  card.querySelectorAll("button[data-label]").forEach(button => {{
    button.addEventListener("click", () => {{
      const item = JSON.parse(card.dataset.item);
      state[item.item_id] = button.dataset.label;
      save();
      refresh(card);
    }});
  }});
}});
document.getElementById("export").addEventListener("click", () => {{
  const rows = [...document.querySelectorAll(".card")].map(card => {{
    const item = JSON.parse(card.dataset.item);
    item.human_label = state[item.item_id] || "";
    return item;
  }});
  document.getElementById("output").value = JSON.stringify({{items: rows}}, null, 2);
  const done = rows.filter(row => row.human_label).length;
  document.getElementById("status").textContent = `${{done}} / ${{rows.length}} labeled`;
}});
</script>
</body>
</html>
"""


def write_readout(
    path: Path,
    *,
    recording: Path,
    frames: list[FrameRef],
    summary_rows: list[dict[str, Any]],
    thresholds: dict[str, float],
    boxes_path: Path,
    summary_csv: Path,
    eyeball_html: Path,
) -> None:
    total_high = sum(int(row["high_confidence_box_count"]) for row in summary_rows if row["status"] == "ok")
    status = "KILL_CANDIDATE_NO_HIGH_CONFIDENCE_BOXES" if total_high == 0 else "NEEDS_EYEBALL_NO_PASS_DECISION"
    lines = [
        "Gate 1 OCR scout readout",
        f"recording={recording}",
        f"frame_count={len(frames)}",
        f"thresholds={thresholds}",
        f"gate1_status={status}",
        "winner_engine=TBD_BY_USER_EYEBALL",
        "",
        "engine table:",
    ]
    for row in summary_rows:
        lines.append(
            "  {engine}: status={status} frames={frame_count} boxes={box_count} "
            "high={high_confidence_box_count} high_ratio={high_confidence_box_ratio} "
            "conf_median={confidence_median} conf_p90={confidence_p90} hangul_ratio={hangul_char_ratio} "
            "error={error}".format(**row)
        )
    lines.extend(
        [
            "",
            f"boxes_jsonl={boxes_path}",
            f"summary_csv={summary_csv}",
            f"eyeball_html={eyeball_html}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Throwaway Gate 1 OCR extractability scout for live-stream screen translation."
    )
    parser.add_argument("recording", type=Path, help="Recording/video file to sample.")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--interval-sec", type=float, default=2.0)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--engines", default="tesseract,paddle", help="Comma-separated: tesseract,paddle.")
    parser.add_argument("--high-confidence", default="tesseract=70,paddle=70")
    parser.add_argument("--eyeball-samples", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260630)
    parser.add_argument("--ffmpeg-cmd", default=None)
    parser.add_argument("--tesseract-cmd", default=None)
    parser.add_argument("--tesseract-lang", default="kor+eng")
    parser.add_argument("--tesseract-psm", default="6")
    parser.add_argument("--paddle-lang", default="korean")
    parser.add_argument(
        "--allow-missing-engines",
        action="store_true",
        help="Write error rows for unavailable engines instead of aborting.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    recording = args.recording.resolve()
    if not recording.is_file():
        raise SystemExit(f"recording not found: {recording}")
    if args.interval_sec <= 0:
        raise SystemExit("--interval-sec must be positive")
    engines = [item.strip().lower() for item in args.engines.split(",") if item.strip()]
    unsupported = sorted(set(engines) - {"tesseract", "paddle"})
    if unsupported:
        raise SystemExit(f"unsupported engine(s): {', '.join(unsupported)}")
    if not engines:
        raise SystemExit("at least one engine is required")

    ffmpeg_cmd = find_executable(args.ffmpeg_cmd, "FFMPEG_CMD", ["ffmpeg", "ffmpeg.exe"])
    if not ffmpeg_cmd:
        raise SystemExit("ffmpeg not found; set --ffmpeg-cmd or FFMPEG_CMD")
    ffprobe_cmd = resolve_ffprobe(ffmpeg_cmd)
    thresholds = parse_thresholds(args.high_confidence, engines)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir or DEFAULT_OUTPUT_ROOT / f"gate1_{recording.stem}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = extract_frames(
        recording,
        out_dir / "frames",
        ffmpeg_cmd=ffmpeg_cmd,
        ffprobe_cmd=ffprobe_cmd,
        interval_sec=args.interval_sec,
        max_frames=args.max_frames,
    )

    engine_errors: dict[str, str] = {}
    boxes: list[OCRBox] = []

    tesseract_cmd = resolve_tesseract(args.tesseract_cmd) if "tesseract" in engines else None
    paddle_engine: PaddleEngine | None = None

    for engine in engines:
        try:
            if engine == "tesseract":
                if not tesseract_cmd:
                    raise RuntimeError("tesseract not found; set --tesseract-cmd or TESSERACT_CMD")
            elif engine == "paddle":
                paddle_engine = PaddleEngine(lang=args.paddle_lang)
        except Exception as exc:
            if not args.allow_missing_engines:
                raise SystemExit(str(exc)) from exc
            engine_errors[engine] = str(exc)

    for frame in frames:
        if "tesseract" in engines and "tesseract" not in engine_errors and tesseract_cmd:
            try:
                boxes.extend(
                    tesseract_boxes(
                        frame,
                        tesseract_cmd=tesseract_cmd,
                        lang=args.tesseract_lang,
                        psm=args.tesseract_psm,
                        threshold=thresholds["tesseract"],
                    )
                )
            except Exception as exc:
                engine_errors.setdefault("tesseract", str(exc))
                if not args.allow_missing_engines:
                    raise
        if "paddle" in engines and "paddle" not in engine_errors and paddle_engine:
            try:
                boxes.extend(paddle_engine.boxes(frame, threshold=thresholds["paddle"]))
            except Exception as exc:
                engine_errors.setdefault("paddle", str(exc))
                if not args.allow_missing_engines:
                    raise

    boxes_path = out_dir / "gate1_boxes.jsonl"
    with boxes_path.open("w", encoding="utf-8") as handle:
        for box in boxes:
            handle.write(json.dumps(box_to_dict(box), ensure_ascii=False, separators=(",", ":")) + "\n")

    summary_rows = build_summary(boxes, frame_count=len(frames), thresholds=thresholds, engine_errors=engine_errors)
    summary_json = out_dir / "gate1_engine_summary.json"
    summary_csv = out_dir / "gate1_engine_summary.csv"
    summary_json.write_text(json.dumps(summary_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(summary_csv, summary_rows)
    eyeball_html, _items_path = write_eyeball_page(
        out_dir,
        boxes,
        ffmpeg_cmd=ffmpeg_cmd,
        sample_size=args.eyeball_samples,
        seed=args.seed,
    )
    readout = out_dir / "gate1_readout.txt"
    write_readout(
        readout,
        recording=recording,
        frames=frames,
        summary_rows=summary_rows,
        thresholds=thresholds,
        boxes_path=boxes_path,
        summary_csv=summary_csv,
        eyeball_html=eyeball_html,
    )

    print(f"wrote {out_dir}")
    print(f"summary: {summary_csv}")
    print(f"eyeball: {eyeball_html}")
    print(f"readout: {readout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
