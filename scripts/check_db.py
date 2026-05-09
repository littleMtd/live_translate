import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

db_path = Path(__file__).parent.parent / "logs" / "live_translate.db"
conn = sqlite3.connect(str(db_path))

cur = conn.execute("SELECT engine, model, COUNT(*) FROM translations GROUP BY engine, model")
print("=== DB Stats ===")
total = 0
for row in cur:
    print("  %s / %s: %d rows" % row)
    total += row[2]
print("  Total: %d rows" % total)

print()
print("=== Top 10 most hit ===")
cur = conn.execute("""
    SELECT source_text, target_text, engine, hit_count
    FROM translations
    ORDER BY hit_count DESC
    LIMIT 10
""")
for row in cur:
    print("  [hit=%d][%s] %s -> %s" % (row[3], row[2], row[0][:28], row[1][:28]))

print()
print("=== Recent 30 translations ===")
cur = conn.execute("""
    SELECT source_text, target_text, engine, hit_count, created_at
    FROM translations
    ORDER BY created_at DESC
    LIMIT 30
""")
for row in cur:
    print("  [%s] %s -> %s  (hit=%d)" % (row[2], row[0][:30], row[1][:30], row[3]))

conn.close()
