"""
Static prompt comparison tool — for testing prompt variants BEFORE a stream.

For in-stream automatic prompt evolution, see modules/prompt_evolver.py.

Usage:
    # Compare all variants defined in VARIANTS against test_cases.json
    python tools/prompt_tester.py

    # Run only specific test IDs
    python tools/prompt_tester.py --ids greeting slang_basic

    # Save results to a timestamped JSON log
    python tools/prompt_tester.py --save
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import cfg
from utils.logger import get_logger

log = get_logger("prompt_tester")

# ── Prompt variants ──────────────────────────────────────────────────────────
# Add or edit variants here. "default" always reflects the live config.

def _slang_str() -> str:
    return "、".join(f"{k}→{v}" for k, v in cfg.translation.slang.items())

VARIANTS: dict[str, str] = {
    "default": (
        f"你是韓語→{cfg.translation.target_lang}即時字幕翻譯器。\n"
        "規則：\n"
        "- 只輸出翻譯結果，不加任何說明或標點之外的文字\n"
        "- 保留語氣和情緒\n"
        f"- 俚語對照：{_slang_str()}\n"
        "- 若句子不完整(incomplete=true)，仍盡力翻譯現有內容"
    ),

    "formal": (
        f"你是韓語→{cfg.translation.target_lang}專業字幕翻譯員。\n"
        "規則：\n"
        "- 只輸出翻譯結果\n"
        "- 使用正式書面語，避免口語化\n"
        f"- 俚語對照：{_slang_str()}\n"
        "- 若句子不完整，仍翻譯現有部分"
    ),

    "casual": (
        f"你是韓語→{cfg.translation.target_lang}直播字幕助手，翻譯要貼近台灣年輕人口語。\n"
        "規則：\n"
        "- 只輸出翻譯，不加說明\n"
        "- 盡量口語化、自然，像朋友在講話\n"
        f"- 俚語對照：{_slang_str()}\n"
        "- 句子不完整時盡力翻譯"
    ),

    "concise": (
        f"將以下韓語翻譯成{cfg.translation.target_lang}字幕，只輸出譯文，越簡短越好。\n"
        f"俚語：{_slang_str()}"
    ),
}
# ─────────────────────────────────────────────────────────────────────────────


def _translate(system_prompt: str, text: str, incomplete: bool) -> tuple[str, float]:
    import anthropic
    client = anthropic.Anthropic(api_key=cfg.keys.anthropic)
    user_msg = f"{'(句子不完整) ' if incomplete else ''}{text}"
    t0 = time.monotonic()
    resp = client.messages.create(
        model=cfg.translation.model,
        max_tokens=cfg.translation.max_tokens,
        temperature=cfg.translation.temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    elapsed = time.monotonic() - t0
    return resp.content[0].text.strip(), round(elapsed, 2)


def _load_cases(ids: list[str] | None) -> list[dict]:
    path = Path(__file__).parent / "test_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases


def _col(text: str, width: int) -> str:
    text = str(text)
    if len(text) > width:
        text = text[:width - 1] + "…"
    return text.ljust(width)


def run(ids: list[str] | None, save: bool):
    cases = _load_cases(ids)
    variant_names = list(VARIANTS.keys())

    if not cfg.keys.anthropic:
        log.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print(f"\n{'═' * 100}")
    print(f"  Prompt tester  |  model: {cfg.translation.model}  |  cases: {len(cases)}  |  variants: {len(VARIANTS)}")
    print(f"{'═' * 100}\n")

    results = []

    for case in cases:
        ko = case["ko"]
        incomplete = case.get("incomplete", False)
        note = case.get("note", "")

        print(f"[{case['id']}] {note}  {'⚠ incomplete' if incomplete else ''}")
        print(f"  KO: {ko}")

        row = {"id": case["id"], "ko": ko, "note": note, "incomplete": incomplete, "variants": {}}

        for name, prompt in VARIANTS.items():
            try:
                translation, elapsed = _translate(prompt, ko, incomplete)
            except Exception as e:
                translation, elapsed = f"ERROR: {e}", 0.0
            row["variants"][name] = {"text": translation, "elapsed": elapsed}
            print(f"  [{name:8s}] ({elapsed}s) {translation}")

        results.append(row)
        print()

    if save:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(__file__).parent / f"results_{ts}.json"
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved → {out_path}")

    _print_summary(results, variant_names)


def _print_summary(results: list[dict], variant_names: list[str]):
    W_ID = 14
    W_KO = 28
    W_VAR = 26

    header = _col("id", W_ID) + _col("korean", W_KO)
    for v in variant_names:
        header += _col(v, W_VAR)
    print("─" * len(header))
    print(header)
    print("─" * len(header))

    for row in results:
        line = _col(row["id"], W_ID) + _col(row["ko"], W_KO)
        for v in variant_names:
            text = row["variants"].get(v, {}).get("text", "—")
            line += _col(text, W_VAR)
        print(line)

    print("─" * len(header))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Iterate and compare prompt variants")
    parser.add_argument("--ids", nargs="+", help="Run only these test case IDs")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    args = parser.parse_args()
    run(ids=args.ids, save=args.save)
