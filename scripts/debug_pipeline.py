"""
偵錯工具 — 直接輸入韓文文字，跳過音訊/STT 階段進行測試。

使用方式：
  python scripts/debug_pipeline.py "안녕하세요, 오늘 방송입니다"
  python scripts/debug_pipeline.py --stage split  "안녕하세요"
  python scripts/debug_pipeline.py --stage translate "안녕하세요"
  python scripts/debug_pipeline.py --no-api "진짜 대박이다"
  echo "안녕하세요" | python scripts/debug_pipeline.py

階段說明：
  all        斷句 → 翻譯（預設）
  split      僅執行斷句器（顯示完整/不完整的判斷結果）
  translate  僅執行翻譯器（跳過斷句，直接送出文字）

參數說明：
  --no-api   略過實際 API 呼叫，僅顯示會送出的 prompt 內容
  --repeat N 將輸入重複 N 次，每次間隔 1 秒（用於壓力測試斷句器）
"""

import argparse
import queue
import sys
import os
import time
import threading

# 強制 Windows 主控台使用 UTF-8（預設可能是 cp950/cp1252）
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import cfg
from modules.sentence_splitter import _is_complete, start as splitter_start


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms"


def _print_divider(title: str):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print('─' * 50)


# ── stage: split ─────────────────────────────────────────────────────────────

def run_split(tokens: list[str], repeat: int = 1):
    """顯示斷句器如何切分輸入的 token。"""
    _print_divider("STAGE: sentence_splitter")

    text_q: queue.Queue = queue.Queue()
    sentence_q: queue.Queue = queue.Queue(maxsize=50)
    stop = threading.Event()

    splitter_start(text_q, sentence_q, stop)

    t0 = time.monotonic()
    for _ in range(repeat):
        for token in tokens:
            text_q.put(token)
            print(f"  [{_fmt_ms(time.monotonic() - t0):>6}] token → {token!r}  "
                  f"  complete={_is_complete(token)}")
            time.sleep(1)

    # 等待最多 force_cut + 2 秒，讓斷句器把緩衝區清空
    deadline = time.monotonic() + cfg.splitter.force_cut_seconds + 2
    results = []
    while time.monotonic() < deadline:
        try:
            results.append(sentence_q.get_nowait())
        except queue.Empty:
            time.sleep(0.1)

    stop.set()

    print()
    if results:
        for r in results:
            flag = " [INCOMPLETE]" if r.incomplete else ""
            print(f"  → sentence: {r.text!r}{flag}")
    else:
        print("  （未產生任何句子 — 斷句器仍在累積？）")

    return results


# ── stage: translate ─────────────────────────────────────────────────────────

def run_translate(text: str, incomplete: bool = False, no_api: bool = False):
    """翻譯單一句子，並顯示 prompt 與結果。"""
    _print_divider("STAGE: translator")

    from modules.translator import Translator, _compose_system_prompt

    if no_api:
        # 顯示會送出的內容，但不實際呼叫 API（與 runtime 同一組裝路徑）
        system = _compose_system_prompt()
        flag = "(句子不完整，請盡力翻譯)" if incomplete else ""
        user_msg = f"[待翻譯]{flag}: {text}"

        print("  [模擬執行 — 不呼叫 API]")
        print()
        print("  系統提示詞（SYSTEM PROMPT）：")
        for line in system.splitlines():
            print(f"    {line}")
        print()
        print(f"  使用者訊息（USER MESSAGE）：{user_msg}")
        return None

    t0 = time.monotonic()
    print(f"  input:      {text!r}")
    print(f"  incomplete: {incomplete}")

    translator = Translator()
    result = translator.translate(text, incomplete)
    elapsed = time.monotonic() - t0

    print(f"  result:     {result!r}")
    print(f"  latency:    {_fmt_ms(elapsed)}")
    return result


# ── stage: all ───────────────────────────────────────────────────────────────

def run_all(tokens: list[str], repeat: int = 1, no_api: bool = False):
    """完整執行斷句 → 翻譯流程。"""
    sentences = run_split(tokens, repeat=repeat)
    if not sentences:
        print("\n  沒有可翻譯的句子。")
        return

    for item in sentences:
        run_translate(item.text, incomplete=item.incomplete, no_api=no_api)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="偵錯韓語字幕流程（跳過音訊/STT）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("text", nargs="?", help="要處理的韓文文字（或從 stdin 讀入）")
    parser.add_argument("--stage", choices=["all", "split", "translate"], default="all")
    parser.add_argument("--no-api", action="store_true", help="模擬執行 — 印出 prompt，不呼叫 API")
    parser.add_argument("--incomplete", action="store_true", help="標記為不完整句子")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="將 token 重複 N 次（壓力測試斷句器）")
    args = parser.parse_args()

    # 從參數或 stdin 讀取文字
    if args.text:
        raw = args.text
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        parser.print_help()
        sys.exit(1)

    # 以空白/換行切分輸入，模擬多個 STT token
    tokens = raw.split()
    if not tokens:
        print("錯誤：輸入為空")
        sys.exit(1)

    print(f"\n  輸入 token：{tokens}")
    print(f"  階段：{args.stage}  |  模擬執行：{args.no_api}  |  重複次數：{args.repeat}")

    if args.stage == "split":
        run_split(tokens, repeat=args.repeat)
    elif args.stage == "translate":
        run_translate(raw, incomplete=args.incomplete, no_api=args.no_api)
    elif args.stage == "all":
        run_all(tokens, repeat=args.repeat, no_api=args.no_api)

    print()


if __name__ == "__main__":
    main()
