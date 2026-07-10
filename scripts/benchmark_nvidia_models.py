from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import cfg
from modules.translation_engines import _build_user_message
from modules.translation_policy import TranslationPolicy
from modules.translation_prompts import (
    _BASE_PROMPT,
    _BASE_PROMPT_TAIL,
    _QWEN_PROMPT,
    _QWEN_PROMPT_TAIL,
    get_translation_profile,
)


# Candidate pool for model-selection runs (--candidates).
# Default runs benchmark ONLY the production model from config (cfg.nvidia.model),
# so this script doubles as a health/latency check for the live engine.
CANDIDATE_MODELS = (
    "qwen/qwen3.5-122b-a10b",
    "qwen/qwen3-next-80b-a3b-instruct",
    "nvidia/riva-translate-4b-instruct-v1.1",
    "nvidia/nvidia-nemotron-nano-9b-v2",
)


def _default_models(include_candidates: bool) -> tuple[str, ...]:
    models = [cfg.nvidia.model]
    if include_candidates:
        models += [model for model in CANDIDATE_MODELS if model != cfg.nvidia.model]
    return tuple(models)

_NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_SIMPLIFIED_HINTS = set("个们这为来对吗后过还说时会没与")
_TRADITIONAL_HINTS = set("個們這為來對嗎後過還說時會沒與")
_TEMPLATE_TERMS = (
    "感謝觀看",
    "感謝收看",
    "訂閱",
    "按讚",
    "点赞",
    "观看",
)
_META_TERMS = (
    "原文",
    "可能是",
    "意思是",
    "這裡指",
    "翻譯",
    "韓語中",
    "無法辨識",
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def main() -> int:
    args = _parse_args()
    if args.rpm > 20:
        raise SystemExit("--rpm must be <= 20 for the current NVIDIA API limit")
    if args.samples < 1:
        raise SystemExit("--samples must be positive")

    event_path = args.events or _latest_event_file()
    if not event_path:
        raise SystemExit("No runtime_events_*.jsonl file found")

    texts = _select_samples(event_path, args.samples)
    if not texts:
        raise SystemExit(f"No benchmarkable samples found in {event_path}")

    output_path = args.output or _default_output_path()
    models = tuple(args.models or _default_models(args.candidates))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "event_path": str(event_path),
        "samples_requested": args.samples,
        "samples_used": len(texts),
        "rpm": args.rpm,
        "models": list(models),
        "dry_run": args.dry_run,
        "results": [],
        "summary": {},
    }

    if args.dry_run:
        report["samples"] = texts
        _write_json(output_path, report)
        print(f"Dry run: selected {len(texts)} samples -> {output_path}")
        return 0

    if not cfg.keys.nvidia:
        raise SystemExit("NVIDIA_API_KEY is not set")

    throttle = _Throttle(args.rpm)
    total_calls = len(models) * len(texts)
    call_index = 0
    for model in models:
        system_prompt = _system_prompt_for_model(model)
        for sample_index, text in enumerate(texts, start=1):
            call_index += 1
            print(
                f"[{call_index}/{total_calls}] model={model} sample={sample_index}/{len(texts)}",
                flush=True,
            )
            throttle.wait()
            result = _translate(
                model=model,
                text=text,
                system_prompt=system_prompt,
                timeout=args.timeout,
            )
            report["results"].append(
                {
                    "model": model,
                    "sample_index": sample_index,
                    "source_text": text,
                    **result,
                    "quality_flags": _quality_flags(result.get("target_text") or ""),
                }
            )
            _write_json(output_path, report)

    report["summary"] = _summarize(report["results"])
    _write_json(output_path, report)
    _print_summary(report)
    print(f"Wrote benchmark report: {output_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark NVIDIA NIM translation models on runtime samples."
    )
    parser.add_argument("--events", type=Path, help="runtime_events_YYYYMMDD.jsonl path")
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="NVIDIA model IDs to benchmark (default: cfg.nvidia.model only)",
    )
    parser.add_argument(
        "--candidates",
        action="store_true",
        help="Also benchmark the model-selection candidate pool",
    )
    parser.add_argument("--samples", type=int, default=20, help="Number of source samples")
    parser.add_argument(
        "--rpm",
        type=float,
        default=18.0,
        help="Max API calls per minute. Must be <= 20.",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-call timeout seconds")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    parser.add_argument("--dry-run", action="store_true", help="Select samples but do not call API")
    return parser.parse_args()


def _latest_event_file() -> Path | None:
    files = sorted(
        (PROJECT_ROOT / "logs").glob("runtime_events_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "logs" / f"model_benchmark_{stamp}.json"


def _select_samples(event_path: Path, limit: int) -> list[str]:
    events = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("status") != "success":
            continue
        source_text = (event.get("source_text") or "").strip()
        if not source_text:
            continue
        prepared = _prepare_source(source_text)
        if not prepared:
            continue
        events.append(
            {
                "source_text": prepared,
                "latency_ms": float(event.get("latency_ms") or 0.0),
                "flags": event.get("quality_flags") or [],
                "template": _has_template(source_text),
                "long": len(prepared) >= 90,
                "short": len(prepared) <= 30,
            }
        )

    buckets = [
        lambda item: item["template"],
        lambda item: bool(item["flags"]),
        lambda item: item["long"],
        lambda item: item["short"],
        lambda item: item["latency_ms"] >= 10000,
        lambda item: True,
    ]
    selected: list[str] = []
    seen = set()
    per_bucket = max(1, limit // len(buckets))
    for bucket in buckets:
        for item in events:
            text = item["source_text"]
            if text in seen or not bucket(item):
                continue
            selected.append(text)
            seen.add(text)
            if len(selected) >= limit:
                return selected
            if sum(1 for chosen in selected if chosen in seen) >= limit:
                return selected
            if len(selected) % per_bucket == 0:
                break

    for item in events:
        text = item["source_text"]
        if text in seen:
            continue
        selected.append(text)
        seen.add(text)
        if len(selected) >= limit:
            break
    return selected


def _prepare_source(text: str) -> str | None:
    policy = TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=2,
        max_translate_chars=cfg.translation.max_translate_chars,
    )
    return policy.prepare_input(text)


def _has_template(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "시청해주셔서 감사합니다",
            "구독과 좋아요",
            "자막 제공",
            "광고를 포함하고 있습니다",
        )
    )


def _system_prompt_for_model(model: str) -> str:
    is_qwen = "qwen" in model.lower()
    base_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT
    tail = _QWEN_PROMPT_TAIL if is_qwen else _BASE_PROMPT_TAIL
    system_prompt = base_prompt  # PromptEvolver removed 2026-06-12
    if not cfg.translation.use_profile:
        return system_prompt + tail
    profile = get_translation_profile(cfg.active_streamer_profile, qwen=is_qwen)
    if not profile:
        return system_prompt + tail
    return (
        f"{system_prompt}\n\n"
        "[Streamer Profile]\n"
        f"{profile}\n\n"
        "Apply this profile only when relevant to the input. "
        "Do not invent references that are not present."
        f"{tail}"
    )


def _translate(model: str, text: str, system_prompt: str, timeout: float) -> dict[str, Any]:
    is_qwen3 = "qwen3" in model.lower() or "qwen-3" in model.lower()
    strip_think = is_qwen3 or any(
        token in model.lower() for token in ("deepseek-v4", "deepseek-r1", "deepseek-v3")
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _build_user_message(text, incomplete=False)},
        ],
        "temperature": cfg.translation.temperature,
        "max_tokens": cfg.translation.max_tokens,
    }
    if is_qwen3:
        body["chat_template_kwargs"] = {"enable_thinking": False}

    request = urllib.request.Request(
        _NVIDIA_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.keys.nvidia}",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        content = (payload["choices"][0]["message"].get("content") or "").strip()
        if strip_think:
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return {
            "ok": True,
            "latency_ms": elapsed_ms,
            "target_text": content,
            "usage": payload.get("usage", {}),
        }
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        message = ""
        try:
            message = exc.read().decode("utf-8")
        except Exception:
            message = str(exc)
        return {
            "ok": False,
            "latency_ms": elapsed_ms,
            "target_text": "",
            "error": f"HTTP {exc.code}",
            "message": message[:500],
        }
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return {
            "ok": False,
            "latency_ms": elapsed_ms,
            "target_text": "",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }


class _Throttle:
    def __init__(self, rpm: float):
        self._interval = 60.0 / rpm
        self._next_at = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if self._next_at > now:
            time.sleep(self._next_at - now)
        self._next_at = time.monotonic() + self._interval


def _quality_flags(text: str) -> list[str]:
    flags = []
    if not text:
        flags.append("empty")
    if text.lower().startswith("input:"):
        flags.append("input_label")
    if text and not _CJK_RE.search(text):
        flags.append("no_cjk")
    if any(term in text for term in _TEMPLATE_TERMS):
        flags.append("template_leak")
    if any(term in text for term in _META_TERMS):
        flags.append("meta_commentary")
    if "[UNK:" in text:
        flags.append("unk_marker")
    simplified = sum(1 for char in text if char in _SIMPLIFIED_HINTS)
    traditional = sum(1 for char in text if char in _TRADITIONAL_HINTS)
    if simplified > traditional and simplified >= 2:
        flags.append("possible_simplified")
    return flags


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_model.setdefault(result["model"], []).append(result)

    summary = {}
    for model, rows in by_model.items():
        ok_rows = [row for row in rows if row.get("ok")]
        latencies = sorted(float(row.get("latency_ms") or 0.0) for row in ok_rows)
        flags: dict[str, int] = {}
        for row in rows:
            for flag in row.get("quality_flags") or []:
                flags[flag] = flags.get(flag, 0) + 1
        summary[model] = {
            "calls": len(rows),
            "ok": len(ok_rows),
            "failed": len(rows) - len(ok_rows),
            "latency_ms": _latency_summary(latencies),
            "quality_flags": dict(sorted(flags.items(), key=lambda item: (-item[1], item[0]))),
        }
    return summary


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "avg": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}

    def percentile(p: float) -> float:
        index = min(len(values) - 1, max(0, round((len(values) - 1) * p)))
        return round(values[index], 2)

    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
    }


def _print_summary(report: dict[str, Any]) -> None:
    print("\nSummary:")
    for model, summary in report.get("summary", {}).items():
        print(
            f"- {model}: ok={summary['ok']}/{summary['calls']} "
            f"latency={summary['latency_ms']} flags={summary['quality_flags']}"
        )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
