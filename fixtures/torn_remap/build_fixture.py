"""半成功撕裂夹具构建器（仅用标准库，SQLite 本地库）。

复现缺陷形态：重映回执成功 → 正式区已挂新键，但入站旧键行未清除，
冲突区无对应挂起；入站列表与互证卡因此各报一数。

用法：
    python -I fixtures/torn_remap/build_fixture.py
输出：
    fixtures/torn_remap/torn_remap.db
"""
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "torn_remap.db"

SCHEMA = """
CREATE TABLE map_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  effective_from TEXT NOT NULL
);
CREATE TABLE mappings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE TABLE inbound_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  arrived_at TEXT NOT NULL
);
CREATE TABLE formal_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_key TEXT NOT NULL,
  mapped_from TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE remap_audits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL,
  at TEXT NOT NULL
);
"""


def build() -> Path:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
            ("m-2026.11", "生效", "2026-11-01"),
        )
        conn.executemany(
            "INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
            [
                ("m-2026.11", "OLD-A1", "NEW-100", "一对一"),
                ("m-2026.11", "OLD-B1", "NEW-200", "多对一"),
                ("m-2026.11", "OLD-B2", "NEW-200", "多对一"),
            ],
        )
        # 入站三行，全部未清除
        conn.executemany(
            "INSERT INTO inbound_queue(old_key,status,arrived_at) VALUES (?,?,?)",
            [
                ("OLD-A1", "待重映", "2026-11-11T08:00:00"),
                ("OLD-B1", "待重映", "2026-11-11T08:05:00"),
                ("OLD-B2", "待重映", "2026-11-11T08:10:00"),
            ],
        )
        # 半成功核心：OLD-A1 的重映回执已成功，正式区挂了新键，
        # 但入站 OLD-A1 行仍在；冲突区没有 OLD-A1 的挂起。
        conn.execute(
            "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at)"
            " VALUES (?,?,?,?)",
            ("NEW-100", "OLD-A1", "m-2026.11", "2026-11-11T09:00:00"),
        )
        # 仅一条与撕裂无关的历史冲突
        conn.execute(
            "INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
            ("OLD-C9", "无映射", "挂起"),
        )
        conn.commit()
    finally:
        conn.close()
    return DB


if __name__ == "__main__":
    p = build()
    print(f"fixture written: {p}")
