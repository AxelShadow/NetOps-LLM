import sqlite3

conn = sqlite3.connect("netops.db")
cur = conn.cursor()
for name, ddl in [
    ("source", "ALTER TABLE devices ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'manual'"),
    ("zabbix_hostid", "ALTER TABLE devices ADD COLUMN zabbix_hostid VARCHAR(32)"),
    ("group_name", "ALTER TABLE devices ADD COLUMN group_name VARCHAR(128) NOT NULL DEFAULT ''"),
]:
    try:
        cur.execute(ddl)
        print(f"добавлен столбец {name}")
    except sqlite3.OperationalError as e:
        print(f"{name}: {e}")
conn.commit()
conn.close()
print("готово")
