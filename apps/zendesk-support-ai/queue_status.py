"""Run INSIDE the application container, with the application UID."""
import json
import sqlite3
import common


def main():
    common.check_queue_writable()
    conn = sqlite3.connect("file:" + str(common.QUEUE_DB) + "?mode=ro", uri=True)
    try:
        print(json.dumps({"write_probe": "ok", "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                          "queues": dict(conn.execute("SELECT queue, count(*) FROM queue_items GROUP BY queue"))}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
