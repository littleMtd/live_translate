import sqlite3
from pathlib import Path

db_path = Path("logs/live_translate.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT source_text, target_text, engine, model, created_at FROM translations ORDER BY created_at DESC LIMIT 20")
records = cursor.fetchall()
print(f"Total records shown: {len(records)}\n")
print("=" * 120)

for i, row in enumerate(records, 1):
    print(f"\n【Record {i}】")
    print(f"KO Input: {row['source_text'][:90]}")
    print(f"ZH Output: {row['target_text'][:90]}")
    print(f"Engine: {row['engine']} | Model: {row['model']}")
    print(f"Created: {row['created_at']}")
    print("-" * 120)

conn.close()
