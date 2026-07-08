from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "scratch" / "ocr_gate_scout"


@dataclass
class BoxObservation:
    engine: str
    frame_index: int
    timestamp_s: float
    text: str
    confidence: float
    bbox_xywh: tuple[int, int, int, int]
    frame_width: int | None
    frame_height: int | None
    region: str = ""


@dataclass
class OCREvent:
    event_id: str
    text: str
    normalized_text: str
    first_seen: float
    last_seen: float
    first_frame_index: int
    last_frame_index: int
    bbox_xywh: tuple[int, int, int, int]
    frame_width: int | None
    frame_height: int | None
    region: str = ""
    confidences: list[float] = field(default_factory=list)
    observation_count: int = 0

    def update(self, obs: BoxObservation) -> None:
        self.last_seen = max(self.last_seen, obs.timestamp_s)
        self.last_frame_index = obs.frame_index
        self.confidences.append(obs.confidence)
        self.observation_count += 1
        if len(normalize_text(obs.text)) > len(self.normalized_text):
            self.text = obs.text
            self.normalized_text = normalize_text(obs.text)
        self.bbox_xywh = obs.bbox_xywh
        self.frame_width = obs.frame_width
        self.frame_height = obs.frame_height
        if obs.region:
            self.region = obs.region

    @property
    def dwell_s(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    @property
    def avg_confidence(self) -> float:
        return statistics.mean(self.confidences) if self.confidences else 0.0

    @property
    def max_confidence(self) -> float:
        return max(self.confidences) if self.confidences else 0.0


@dataclass(frozen=True)
class TranscriptSegment:
    start_s: float
    end_s: float
    text: str
    source: str = ""


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_text(text: str) -> str:
    chars: list[str] = []
    for char in unicodedata.normalize("NFKC", text).lower():
        category = unicodedata.category(char)
        if category.startswith("L") or category.startswith("N"):
            chars.append(char)
    return "".join(chars)


def contains_hangul(text: str) -> bool:
    return any("\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff" for char in text)


AMOUNT_RE = re.compile(r"(?:[₩$]\s*)?\d[\d,.\s]*(?:원|만원|천원|억|달러|won|krw)?", re.IGNORECASE)


def contains_amount(text: str) -> bool:
    return bool(AMOUNT_RE.search(text))


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 2 and a in b:
        return 1.0
    if len(b) >= 2 and b in a:
        return len(b) / len(a)
    return SequenceMatcher(None, a, b).ratio()


def load_gate1_boxes(
    path: Path,
    *,
    winner_engine: str,
    min_confidence: float,
) -> tuple[list[BoxObservation], dict[str, int]]:
    observations: list[BoxObservation] = []
    counts = {
        "total_rows": 0,
        "winner_engine_rows": 0,
        "low_confidence_excluded": 0,
        "accepted_high_confidence": 0,
    }
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            counts["total_rows"] += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_no}: {exc}") from exc
            engine = str(row.get("engine") or "").lower()
            if engine != winner_engine:
                continue
            counts["winner_engine_rows"] += 1
            confidence = parse_float(row.get("confidence"), 0.0)
            if confidence < min_confidence:
                counts["low_confidence_excluded"] += 1
                continue
            bbox_raw = row.get("bbox_xywh") or [0, 0, 1, 1]
            if not isinstance(bbox_raw, list) or len(bbox_raw) < 4:
                bbox_raw = [0, 0, 1, 1]
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            observations.append(
                BoxObservation(
                    engine=engine,
                    frame_index=parse_int(row.get("frame_index"), 0),
                    timestamp_s=parse_float(row.get("timestamp_s"), 0.0),
                    text=text,
                    confidence=confidence,
                    bbox_xywh=(
                        parse_int(bbox_raw[0], 0),
                        parse_int(bbox_raw[1], 0),
                        max(1, parse_int(bbox_raw[2], 1)),
                        max(1, parse_int(bbox_raw[3], 1)),
                    ),
                    frame_width=parse_int(row.get("frame_width"), 0) or None,
                    frame_height=parse_int(row.get("frame_height"), 0) or None,
                    region=str(row.get("region") or row.get("roi") or ""),
                )
            )
            counts["accepted_high_confidence"] += 1
    observations.sort(key=lambda obs: (obs.timestamp_s, obs.frame_index, obs.text))
    return observations, counts


def center_distance_ratio(event: OCREvent, obs: BoxObservation) -> float | None:
    width = obs.frame_width or event.frame_width
    height = obs.frame_height or event.frame_height
    if not width or not height:
        return None
    x1, y1, w1, h1 = event.bbox_xywh
    x2, y2, w2, h2 = obs.bbox_xywh
    c1 = (x1 + w1 / 2.0, y1 + h1 / 2.0)
    c2 = (x2 + w2 / 2.0, y2 + h2 / 2.0)
    distance = ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5
    diagonal = (width**2 + height**2) ** 0.5
    return distance / diagonal if diagonal else None


def dedupe_events(
    observations: list[BoxObservation],
    *,
    max_gap_s: float,
    text_similarity_threshold: float,
    max_center_shift: float,
) -> list[OCREvent]:
    active: list[OCREvent] = []
    completed: list[OCREvent] = []
    next_id = 1
    for obs in observations:
        still_active: list[OCREvent] = []
        for event in active:
            if obs.timestamp_s - event.last_seen > max_gap_s:
                completed.append(event)
            else:
                still_active.append(event)
        active = still_active

        norm = normalize_text(obs.text)
        if not norm:
            continue
        best_event: OCREvent | None = None
        best_score = 0.0
        for event in active:
            score = similarity(norm, event.normalized_text)
            if score < text_similarity_threshold:
                continue
            shift = center_distance_ratio(event, obs)
            if shift is not None and shift > max_center_shift:
                continue
            if score > best_score:
                best_event = event
                best_score = score
        if best_event is not None:
            best_event.update(obs)
            continue
        event = OCREvent(
            event_id=f"OCR{next_id:05d}",
            text=obs.text,
            normalized_text=norm,
            first_seen=obs.timestamp_s,
            last_seen=obs.timestamp_s,
            first_frame_index=obs.frame_index,
            last_frame_index=obs.frame_index,
            bbox_xywh=obs.bbox_xywh,
            frame_width=obs.frame_width,
            frame_height=obs.frame_height,
            region=obs.region,
            confidences=[obs.confidence],
            observation_count=1,
        )
        next_id += 1
        active.append(event)
    completed.extend(active)
    completed.sort(key=lambda event: (event.first_seen, event.event_id))
    return completed


def parse_iso_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def srt_time_to_seconds(raw: str) -> float:
    hours, minutes, rest = raw.strip().replace(",", ".").split(":", 2)
    seconds = float(rest)
    return int(hours) * 3600 + int(minutes) * 60 + seconds


def parse_srt(path: Path) -> list[TranscriptSegment]:
    text = path.read_text(encoding="utf-8-sig")
    segments: list[TranscriptSegment] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines) or "-->" not in lines[timing_index]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[timing_index].split("-->", 1)]
        body = " ".join(lines[timing_index + 1 :]).strip()
        if not body:
            continue
        segments.append(
            TranscriptSegment(
                start_s=srt_time_to_seconds(start_raw),
                end_s=srt_time_to_seconds(end_raw),
                text=body,
                source="srt",
            )
        )
    return segments


def row_text(row: dict[str, Any]) -> str:
    for key in ("text", "source_text", "transcript", "sentence", "utterance"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_jsonl_transcript(path: Path) -> list[TranscriptSegment]:
    direct: list[TranscriptSegment] = []
    timed_records: list[tuple[datetime, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_no}: {exc}") from exc
            if not isinstance(row, dict):
                continue
            event_type = str(row.get("event_type") or "")
            if event_type and event_type not in {"translation", "sentence", "transcript"}:
                continue
            text = row_text(row)
            if not text:
                continue
            start_value = row.get("start_s", row.get("start", row.get("start_sec")))
            end_value = row.get("end_s", row.get("end", row.get("end_sec")))
            if start_value is not None or end_value is not None:
                start_s = parse_float(start_value, parse_float(end_value, 0.0))
                end_s = parse_float(end_value, start_s)
                direct.append(TranscriptSegment(start_s=start_s, end_s=max(start_s, end_s), text=text, source="jsonl"))
                continue
            created_at = parse_iso_time(row.get("created_at"))
            if created_at is not None:
                timed_records.append((created_at, row))
    if direct:
        return sorted(direct, key=lambda item: (item.start_s, item.end_s))
    if not timed_records:
        return []
    first_time = min(record[0] for record in timed_records)
    segments: list[TranscriptSegment] = []
    for created_at, row in sorted(timed_records, key=lambda item: item[0]):
        text = row_text(row)
        end_s = (created_at - first_time).total_seconds()
        duration = parse_float(row.get("audio_seconds"), 0.0)
        start_s = max(0.0, end_s - duration) if duration > 0 else end_s
        segments.append(TranscriptSegment(start_s=start_s, end_s=max(start_s, end_s), text=text, source="runtime_jsonl"))
    return segments


def parse_csv_transcript(path: Path) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            text = row_text(row)
            if not text:
                continue
            start_s = parse_float(row.get("start_s", row.get("start", row.get("start_sec"))), 0.0)
            end_s = parse_float(row.get("end_s", row.get("end", row.get("end_sec"))), start_s)
            segments.append(TranscriptSegment(start_s=start_s, end_s=max(start_s, end_s), text=text, source="csv"))
    return sorted(segments, key=lambda item: (item.start_s, item.end_s))


def parse_transcript(path: Path, fmt: str) -> list[TranscriptSegment]:
    selected = fmt
    if fmt == "auto":
        suffix = path.suffix.lower()
        if suffix == ".srt":
            selected = "srt"
        elif suffix == ".csv":
            selected = "csv"
        else:
            selected = "jsonl"
    if selected == "srt":
        return parse_srt(path)
    if selected == "csv":
        return parse_csv_transcript(path)
    if selected in {"jsonl", "runtime"}:
        return parse_jsonl_transcript(path)
    raise ValueError(f"unsupported transcript format: {fmt}")


def match_event(
    event: OCREvent,
    transcript: list[TranscriptSegment],
    *,
    window_s: float,
    threshold: float,
) -> dict[str, Any]:
    candidates = [
        segment
        for segment in transcript
        if segment.end_s >= event.first_seen - window_s and segment.start_s <= event.last_seen + window_s
    ]
    best_score = 0.0
    best_segment: TranscriptSegment | None = None
    for segment in candidates:
        score = similarity(event.normalized_text, normalize_text(segment.text))
        if score > best_score:
            best_score = score
            best_segment = segment
    if candidates:
        joined_text = " ".join(segment.text for segment in candidates)
        joined_score = similarity(event.normalized_text, normalize_text(joined_text))
        if joined_score > best_score:
            best_score = joined_score
            best_segment = TranscriptSegment(
                start_s=candidates[0].start_s,
                end_s=candidates[-1].end_s,
                text=joined_text,
                source="window_join",
            )
    classification = "SPOKEN" if best_score >= threshold else "EXCLUSIVE"
    return {
        "classification": classification,
        "match_score": round(best_score, 4),
        "matched_transcript_snippet": best_segment.text[:240] if best_segment else "",
        "matched_transcript_start_s": round(best_segment.start_s, 3) if best_segment else None,
        "matched_transcript_end_s": round(best_segment.end_s, 3) if best_segment else None,
    }


def length_bucket(text: str) -> str:
    size = len(normalize_text(text))
    if size <= 5:
        return "len_0_5"
    if size <= 15:
        return "len_6_15"
    return "len_16_plus"


def event_to_record(event: OCREvent, match: dict[str, Any]) -> dict[str, Any]:
    record = {
        "event_id": event.event_id,
        "first_seen": round(event.first_seen, 3),
        "last_seen": round(event.last_seen, 3),
        "dwell_s": round(event.dwell_s, 3),
        "region": event.region or None,
        "ocr_text": event.text,
        "normalized_text": event.normalized_text,
        "confidence": round(event.avg_confidence, 3),
        "max_confidence": round(event.max_confidence, 3),
        "observation_count": event.observation_count,
        "first_frame_index": event.first_frame_index,
        "last_frame_index": event.last_frame_index,
        "bbox_xywh": list(event.bbox_xywh),
        "length_bucket": length_bucket(event.text),
        "contains_hangul": contains_hangul(event.text),
        "contains_amount": contains_amount(event.text),
    }
    record.update(match)
    return record


def summarize(records: list[dict[str, Any]], *, box_counts: dict[str, int], args: argparse.Namespace) -> dict[str, Any]:
    total = len(records)
    spoken = sum(1 for row in records if row["classification"] == "SPOKEN")
    exclusive = sum(1 for row in records if row["classification"] == "EXCLUSIVE")

    def breakdown(key: str) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for row in records:
            value = str(row.get(key))
            item = output.setdefault(value, {"total": 0, "spoken": 0, "exclusive": 0, "exclusivity_rate": 0.0})
            item["total"] += 1
            if row["classification"] == "SPOKEN":
                item["spoken"] += 1
            else:
                item["exclusive"] += 1
        for item in output.values():
            item["exclusivity_rate"] = round(item["exclusive"] / item["total"], 4) if item["total"] else 0.0
        return dict(sorted(output.items()))

    return {
        "recording": str(args.recording.resolve()),
        "gate1_boxes": str(args.gate1_boxes.resolve()),
        "transcript": str(args.transcript.resolve()),
        "winner_engine": args.winner_engine,
        "min_confidence": args.min_confidence,
        "match_window_s": args.window_s,
        "similarity_threshold": args.similarity_threshold,
        "dedup_gap_s": args.dedup_gap_s,
        "dedup_text_similarity": args.dedup_text_similarity,
        "box_counts": box_counts,
        "total_ocr_events": total,
        "spoken_events": spoken,
        "exclusive_events": exclusive,
        "exclusivity_rate": round(exclusive / total, 4) if total else 0.0,
        "breakdown_by_length_bucket": breakdown("length_bucket"),
        "breakdown_by_contains_hangul": breakdown("contains_hangul"),
        "breakdown_by_contains_amount": breakdown("contains_amount"),
        "breakdown_by_region": breakdown("region"),
    }


def write_readout(path: Path, summary: dict[str, Any], *, details_path: Path, summary_path: Path) -> None:
    total = int(summary["total_ocr_events"])
    exclusive = int(summary["exclusive_events"])
    if total == 0:
        status = "KILL_CANDIDATE_NO_HIGH_CONFIDENCE_OCR_EVENTS"
    elif exclusive == 0:
        status = "KILL_CANDIDATE_ZERO_EXCLUSIVE_EVENTS"
    else:
        status = "HAS_EXCLUSIVE_EVENTS_NO_PASS_DECISION"
    lines = [
        "Gate 2 OCR exclusivity readout",
        f"recording={summary['recording']}",
        f"winner_engine={summary['winner_engine']}",
        f"min_confidence={summary['min_confidence']}",
        f"match_window_s={summary['match_window_s']}",
        f"similarity_threshold={summary['similarity_threshold']}",
        f"dedup_gap_s={summary['dedup_gap_s']}",
        f"gate2_status={status}",
        f"accepted_high_confidence_boxes={summary['box_counts']['accepted_high_confidence']}",
        f"low_confidence_excluded={summary['box_counts']['low_confidence_excluded']}",
        f"total_ocr_events={summary['total_ocr_events']}",
        f"spoken_events={summary['spoken_events']}",
        f"exclusive_events={summary['exclusive_events']}",
        f"exclusivity_rate={summary['exclusivity_rate']}",
        f"summary_json={summary_path}",
        f"details_jsonl={details_path}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Throwaway Gate 2 OCR exclusivity proxy for live-stream screen translation."
    )
    parser.add_argument("recording", type=Path, help="Same recording used for Gate 1.")
    parser.add_argument("--gate1-boxes", type=Path, required=True, help="gate1_boxes.jsonl from gate1_ocr_scout.py.")
    parser.add_argument("--winner-engine", required=True, help="Gate 1 winner engine. Required by spec.")
    parser.add_argument(
        "--min-confidence",
        type=float,
        required=True,
        help="Gate 1 high-confidence threshold for the winner engine. Low-confidence boxes are excluded.",
    )
    parser.add_argument("--transcript", type=Path, required=True, help="Timestamped transcript JSONL/CSV/SRT.")
    parser.add_argument("--transcript-format", choices=["auto", "jsonl", "runtime", "csv", "srt"], default="auto")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--window-s", type=float, default=8.0)
    parser.add_argument("--similarity-threshold", type=float, default=0.65)
    parser.add_argument("--dedup-gap-s", type=float, default=4.0)
    parser.add_argument("--dedup-text-similarity", type=float, default=0.92)
    parser.add_argument("--max-center-shift", type=float, default=0.20)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    recording = args.recording.resolve()
    if not recording.is_file():
        raise SystemExit(f"recording not found: {recording}")
    if not args.gate1_boxes.is_file():
        raise SystemExit(f"gate1 boxes not found: {args.gate1_boxes}")
    if not args.transcript.is_file():
        raise SystemExit(f"transcript not found: {args.transcript}")
    winner_engine = args.winner_engine.strip().lower()
    if not winner_engine:
        raise SystemExit("--winner-engine is required")
    if args.min_confidence < 0:
        raise SystemExit("--min-confidence must be non-negative")

    observations, box_counts = load_gate1_boxes(
        args.gate1_boxes,
        winner_engine=winner_engine,
        min_confidence=args.min_confidence,
    )
    if box_counts["winner_engine_rows"] == 0:
        raise SystemExit(f"winner engine {winner_engine!r} has no rows in {args.gate1_boxes}")

    transcript = parse_transcript(args.transcript, args.transcript_format)
    if not transcript:
        raise SystemExit(
            "no transcript segments found; provide CSV/JSONL/SRT with text and timestamps, "
            "or a runtime JSONL with translation source_text"
        )

    events = dedupe_events(
        observations,
        max_gap_s=args.dedup_gap_s,
        text_similarity_threshold=args.dedup_text_similarity,
        max_center_shift=args.max_center_shift,
    )
    records: list[dict[str, Any]] = []
    for event in events:
        match = match_event(
            event,
            transcript,
            window_s=args.window_s,
            threshold=args.similarity_threshold,
        )
        records.append(event_to_record(event, match))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir or DEFAULT_OUTPUT_ROOT / f"gate2_{recording.stem}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    details_path = out_dir / "gate2_events.jsonl"
    with details_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    summary = summarize(records, box_counts=box_counts, args=args)
    summary_path = out_dir / "gate2_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readout_path = out_dir / "gate2_readout.txt"
    write_readout(readout_path, summary, details_path=details_path, summary_path=summary_path)

    print(f"wrote {out_dir}")
    print(f"summary: {summary_path}")
    print(f"details: {details_path}")
    print(f"readout: {readout_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
