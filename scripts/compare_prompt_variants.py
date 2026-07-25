from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import cfg
from modules.activity_context import activity_prompt_capsule
from modules.translation_engines import (
    NvidiaEngine,
    get_last_engine_api_diagnostics,
    get_last_token_usage,
)
from modules.translation_prompts import (
    _QWEN_PROMPT_TAIL,
    _build_qwen_legacy_prompt,
    _build_qwen_optimized_prompt,
    get_translation_profile,
)
from modules.translator import _looks_like_meta_garbage_output
from utils.runtime_events import translation_quality

DEFAULT_OUTPUT = PROJECT_ROOT / "scratch" / "analysis" / "prompt_v2_comparison_20260712.json"

# Frozen with the 2026-07-12 legacy prompt baseline. Reading ``git show HEAD``
# worked only while the v2 changes were uncommitted; after commit, HEAD would
# silently become the new compact profile and invalidate future comparisons.
_LEGACY_PROFILE_SNAPSHOTS = {
    "url": """[Fixed proper-noun glossary - Qwen]
UR:L/유아렐/유아엘=UR:L. URL=UR:L only in clear group context; keep URL for a web address. YOU ARE LINKED=YOU ARE LINKED.
모카/랑코/마냥/솜먕: keep the Korean stage names exactly.
오아=오아; 바밍=바밍.
결속아이돌/결속 아이돌=결속아이돌; 샌드박스네트워크/샌드박스 네트워크=Sandbox Network; 플럭서스/㈜플럭서스=Fluxus.
Chemical Love/Again/Wish Me Love: keep official English titles. 조금 더 가까이 (모카): keep official Korean title. 사계(四季)=「四季」企劃.

【UR:L / 유아렐 (Qwen 專化)】
Four-member virtual idol group managed by Sandbox Network, active primarily on SOOP, originating from 결속아이돌. The group name abbreviates YOU ARE LINKED and intentionally echoes URL/link. 오아 helped the project as a partner. Members: 모카, 랑코, 마냥, 솜먕. Confirmed release terms: Chemical Love, Again, Wish Me Love, 조금 더 가까이 (모카). No official fandom name is recorded in current project data.

1. 團體成員
input:유아렐은 모카 랑코 마냥 솜먕 네 명이에요
output:UR:L由모카、랑코、마냥、솜먕四人組成

2. 團體名消歧
input:URL 신곡 링크 URL 보내줘
output:把UR:L新歌的網址傳給我

3. 데뷔 싱글
input:Chemical Love랑 Again 계속 듣는 중이야
output:我一直在聽《Chemical Love》和《Again》

4. 봄 시즌 곡
input:Wish Me Love 뮤비가 새로 올라왔어요
output:《Wish Me Love》的MV上線了

5. 모카 솔로
input:모카의 조금 더 가까이 들어봤어?
output:聽過모카的《조금 더 가까이》嗎？

6. 사계 프로젝트
input:사계 프로젝트에서 멤버 솔로곡도 나온대
output:聽說「四季」企劃也會推出成員個人歌曲

7. SOOP 합방
input:오늘 랑코 마냥 솜먕이 숲에서 합방해요
output:랑코、마냥和솜먕今天會在SOOP一起直播""",
}

CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "coherent_english",
        "source": "I am Iron Man.",
        "require_cjk": True,
        "forbid": ("留空", "無輸出"),
    },
    {
        "id": "coherent_english_sentence",
        "source": "Today was really fun, thank you everyone.",
        "require_cjk": True,
        "forbid": ("留空", "無輸出"),
    },
    {
        "id": "coherent_japanese",
        "source": "今日はとても楽しかったです",
        "require_cjk": True,
        "forbid_japanese": True,
    },
    {
        "id": "unknown_token_in_sentence",
        "source": "서버에서 띠부시레 아이템을 찾았어",
        "contains": ("띠부시레",),
        "require_cjk": True,
    },
    {
        "id": "unknown_sound_word_no_kana",
        "source": "지노와아!",
        "contains": ("지노와아",),
        "forbid_japanese": True,
    },
    {
        "id": "amount_man_unit",
        "source": "만 5천원 후원 감사합니다",
        "contains_any": ("15,000", "15000", "1萬5千", "一萬五千"),
    },
    {
        "id": "url_group_and_address",
        "source": "URL 신곡 링크 URL 보내줘",
        "contains": ("UR:L",),
        "contains_any": ("URL", "網址"),
    },
    {
        "id": "url_members_and_song",
        "source": "모카랑 솜먕이 Wish Me Love 얘기했어",
        "contains": ("모카", "솜먕", "Wish Me Love"),
        "require_cjk": True,
    },
)


def _legacy_profile_snapshot(profile_id: str | None = None) -> str:
    profile_id = profile_id or cfg.active_streamer_profile
    return _LEGACY_PROFILE_SNAPSHOTS.get(
        profile_id,
        get_translation_profile(profile_id, qwen=True),
    )


def _composed_prompt(builder: Callable[[], str], *, legacy_profile: bool = False) -> str:
    prompt = builder()
    if cfg.translation.use_profile:
        profile = (
            _legacy_profile_snapshot(cfg.active_streamer_profile)
            if legacy_profile
            else get_translation_profile(cfg.active_streamer_profile, qwen=True)
        )
        if profile:
            prompt += "\n\n" + profile
    activity = activity_prompt_capsule(
        getattr(cfg.translation, "current_activity", "")
    )
    if activity:
        prompt += "\n\n" + activity
    return prompt + _QWEN_PROMPT_TAIL


def _has_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in text)


def _has_japanese(text: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" for char in text)


def evaluate_output(case: dict[str, Any], output: str | None) -> dict[str, Any]:
    text = str(output or "").strip()
    failures = []
    if not text:
        failures.append("empty_output")
    if text and _looks_like_meta_garbage_output(text):
        failures.append("meta_or_placeholder_output")
    for required in case.get("contains", ()):
        if required not in text:
            failures.append(f"missing:{required}")
    choices = tuple(case.get("contains_any", ()))
    if choices and not any(choice in text for choice in choices):
        failures.append("missing_any:" + "|".join(choices))
    for forbidden in case.get("forbid", ()):
        if forbidden in text:
            failures.append(f"forbidden:{forbidden}")
    if case.get("require_cjk") and not _has_cjk(text):
        failures.append("missing_cjk")
    if case.get("forbid_japanese") and _has_japanese(text):
        failures.append("unexpected_japanese")
    return {"passed": not failures, "failures": failures}


def build_report(
    generate: Callable[[str, str], str | None] | None = None,
) -> dict[str, Any]:
    prompts = {
        "legacy": _composed_prompt(_build_qwen_legacy_prompt, legacy_profile=True),
        "v2": _composed_prompt(_build_qwen_optimized_prompt),
    }
    report: dict[str, Any] = {
        "schema": 1,
        "profile": cfg.active_streamer_profile,
        "model": cfg.nvidia.model,
        "translation_mode": cfg.translation.translation_mode,
        "prompt_chars": {name: len(prompt) for name, prompt in prompts.items()},
        "prompt_lines": {name: len(prompt.splitlines()) for name, prompt in prompts.items()},
        "reduction_ratio": round(1 - len(prompts["v2"]) / len(prompts["legacy"]), 3),
        "executed": generate is not None,
        "cases": [],
    }
    for case in CASES:
        row = {"id": case["id"], "source": case["source"], "variants": {}}
        for variant, prompt in prompts.items():
            started = time.monotonic()
            output = generate(case["source"], prompt) if generate else None
            elapsed_ms = round((time.monotonic() - started) * 1000, 1) if generate else None
            result: dict[str, Any] = {
                "output": output,
                "elapsed_ms": elapsed_ms,
                "evaluation": evaluate_output(case, output) if generate else None,
            }
            if generate:
                result["quality"] = translation_quality(case["source"], output)
                result["api_diagnostics"] = get_last_engine_api_diagnostics()
                result["token_usage"] = get_last_token_usage()
            row["variants"][variant] = result
        report["cases"].append(row)
    if generate:
        report["pass_counts"] = {
            variant: sum(
                bool(row["variants"][variant]["evaluation"]["passed"])
                for row in report["cases"]
            )
            for variant in prompts
        }
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare legacy and compact-v2 Qwen prompts.")
    parser.add_argument("--execute", action="store_true", help="Call the configured NVIDIA API.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    generate = None
    if args.execute:
        engine = NvidiaEngine()
        if not engine.available:
            print("NVIDIA engine unavailable", file=sys.stderr)
            return 2
        # This is an offline quality comparison, not the live latency path.
        # Avoid scoring a transient 5s network miss as a prompt regression.
        engine._timeout = max(float(getattr(engine, "_timeout", 0) or 0), 15.0)
        engine._retry_transient_errors = True

        def generate(source: str, prompt: str) -> str | None:
            return engine.translate(source, prompt, False, history=[])

    report = build_report(generate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "prompt_chars": report["prompt_chars"],
        "reduction_ratio": report["reduction_ratio"],
        "pass_counts": report.get("pass_counts"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
