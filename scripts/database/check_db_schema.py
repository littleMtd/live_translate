import sqlite3
from pathlib import Path

db_path = Path('logs/live_translate.db')
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# 获取表的schema
cursor.execute("SELECT sql FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:')
for table in tables:
    print(table[0])
    print()

# 查询最近的记录
cursor.execute("SELECT * FROM translations LIMIT 1")
cols = [description[0] for description in cursor.description]
print(f"Columns: {cols}")
print()

# 查询最近的翻译
cursor.execute("""
SELECT * FROM translations 
ORDER BY rowid DESC 
LIMIT 20
""")

rows = cursor.fetchall()
for row in rows:
    print(row)

conn.close()
