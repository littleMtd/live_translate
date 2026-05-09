import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

db_path = Path(__file__).parent.parent / "logs" / "live_translate.db"
conn = sqlite3.connect(str(db_path))

cur = conn.execute("DELETE FROM translations WHERE target_text = 'fallback result'")
print("Deleted %d test artifact rows" % cur.rowcount)

cur = conn.execute("SELECT COUNT(*) FROM translations")
print("Remaining rows: %d" % cur.fetchone()[0])

conn.commit()
conn.close()
