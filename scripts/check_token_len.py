import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

db_path = Path(__file__).parent.parent / "logs" / "live_translate.db"
conn = sqlite3.connect(str(db_path))

rows = conn.execute(
    "SELECT source_text, target_text FROM translations ORDER BY length(target_text) DESC"
).fetchall()
conn.close()

print("=== Translation length (chars) ===")
print("%-6s %-6s  %s" % ("src", "tgt", "target_text"))
print("-" * 72)
for src, tgt in rows:
    print("%-6d %-6d  %s" % (len(src), len(tgt), tgt[:60]))

lengths = [len(tgt) for _, tgt in rows]
if lengths:
    print()
    print("max=%d  avg=%.1f  p90=%d" % (
        max(lengths),
        sum(lengths) / len(lengths),
        sorted(lengths)[int(len(lengths) * 0.9)],
    ))

# Spot potential truncations: no ending punctuation
suspicious = [(s, t) for s, t in rows if t and t[-1] not in "。！？…」)）~～"]
if suspicious:
    print()
    print("=== Possible truncations (no ending punct) ===")
    for src, tgt in suspicious:
        print("  [%d chars] %s" % (len(tgt), tgt))
