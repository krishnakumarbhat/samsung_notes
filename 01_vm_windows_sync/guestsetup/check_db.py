import sqlite3
import sys

db_path = r"C:\Users\notes\AppData\Local\Packages\SAMSUNGELECTRONICSCO.LTD.SamsungAccount_3c1yjt4zspk6g\LocalState\SamsungAccountInfo.db"

try:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Get all tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cur.fetchall()
    print(f"Tables: {tables}")

    for table in tables:
        tname = table[0]
        print(f"\n=== Table: {tname} ===")
        try:
            cur.execute(f"PRAGMA table_info({tname})")
            cols = cur.fetchall()
            print(f"Columns: {[c[1] for c in cols]}")
            cur.execute(f"SELECT * FROM {tname} LIMIT 5")
            rows = cur.fetchall()
            for r in rows:
                # Truncate long values
                print([str(v)[:100] if v else v for v in r])
        except Exception as e:
            print(f"Error: {e}")

    conn.close()
except Exception as e:
    print(f"Failed: {e}")
