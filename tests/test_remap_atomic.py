"""半成功撕裂切断测试（本地 SQLite 夹具）。

闸门顺序：
1. test_fixture_reproduces_half_success_torn 先复现撕裂；
2. 半成功失败测（注入正式写入故障，必须两侧回滚）先红；
3. 双路抢映失败测（缺争用留痕即红）先红；
4. 实现乙路径后全绿，五处（正式区/入站列表/冲突区/互证卡/重映回执）对齐。
"""
import shutil
import sqlite3
import threading
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app, init_db

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BUILDER = ROOT / "fixtures" / "torn_remap" / "build_fixture.py"


def _load_fixture_builder():
    spec = spec_from_file_location("torn_fixture_builder", FIXTURE_BUILDER)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    c.headers.update({"Authorization": "Bearer " + r.json()["token"]})
    return c


def _raw_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---- 第 1 步：夹具复现半成功撕裂 ---------------------------------------

def test_fixture_reproduces_half_success_torn():
    db = _load_fixture_builder().build()
    conn = _raw_conn(db)
    try:
        pending = {r["old_key"] for r in conn.execute(
            "SELECT old_key FROM inbound_queue WHERE status='待重映'")}
        formal = {r["mapped_from"] for r in conn.execute(
            "SELECT mapped_from FROM formal_entities")}
        conflicts = {r["old_key"] for r in conn.execute(
            "SELECT old_key FROM conflicts WHERE status='挂起'")}
        torn = pending & formal
        assert torn == {"OLD-A1"}, "夹具必须复现：正式已挂、入站未清"
        assert torn & conflicts == set(), "撕裂键在冲突区不得有挂起"
    finally:
        conn.close()


def test_fixture_torn_state_is_flagged_by_reconciliation(tmp_path, monkeypatch):
    db = _load_fixture_builder().build()
    dst = tmp_path / "torn.db"
    shutil.copy(db, dst)
    monkeypatch.setattr("app.DB_PATH", dst)
    init_db()
    c = TestClient(app)
    h = {"Authorization": "Bearer " + c.post(
        "/api/login", json={"username": "admin", "password": "123456"}).json()["token"]}
    rec = c.get("/api/reconciliation", headers=h).json()
    assert rec["aligned"] is False
    assert "OLD-A1" in rec["torn"]


# ---- 第 2 步：半成功失败测（红 → 实现后绿） -----------------------------

def test_remap_rolls_back_both_sides_when_formal_insert_fails(client, tmp_path):
    db_path = tmp_path / "t.db"
    with _raw_conn(db_path) as raw:
        before = raw.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"]
        raw.execute(
            "CREATE TRIGGER fail_formal BEFORE INSERT ON formal_entities "
            "WHEN NEW.mapped_from='OLD-B1' "
            "BEGIN SELECT RAISE(ABORT,'injected formal insert failure'); END")
        raw.commit()

    r = client.post("/api/remap", json={"old_key": "OLD-B1"})
    assert r.status_code == 500, "正式写入失败必须整体失败，不得返回成功回执"

    with _raw_conn(db_path) as raw:
        # 正式区零增
        assert raw.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"] == before
        # 入站行未清除，回到撕裂前
        row = raw.execute(
            "SELECT status FROM inbound_queue WHERE old_key='OLD-B1'").fetchone()
        assert row is not None and row["status"] == "待重映"
        # 冲突区不得借机挂起
        assert raw.execute(
            "SELECT COUNT(*) c FROM conflicts WHERE old_key='OLD-B1'").fetchone()["c"] == 0

    rec = client.get("/api/reconciliation").json()
    assert rec["aligned"] is True
    assert rec["torn"] == []
    # 回滚须留痕
    audits = client.get("/api/audits").json()["items"]
    assert any(a["old_key"] == "OLD-B1" and a["outcome"] == "回滚" for a in audits)

    # 故障解除后，同入口重映必须同事务成功，五处对齐
    with _raw_conn(db_path) as raw:
        raw.execute("DROP TRIGGER fail_formal")
        raw.commit()
    ok = client.post("/api/remap", json={"old_key": "OLD-B1"})
    assert ok.status_code == 200
    receipt = ok.json()
    assert receipt["outcome"] == "成功"
    assert receipt["old_key"] == "OLD-B1" and receipt["entity_key"] == "NEW-200"
    assert "audit_id" in receipt

    with _raw_conn(db_path) as raw:
        assert raw.execute(
            "SELECT status FROM inbound_queue WHERE old_key='OLD-B1'").fetchone()["status"] == "已重映"
        assert raw.execute(
            "SELECT COUNT(*) c FROM formal_entities WHERE mapped_from='OLD-B1'").fetchone()["c"] == 1

    rec2 = client.get("/api/reconciliation").json()
    assert rec2["aligned"] is True
    assert rec2["torn"] == [] and rec2["orphan_cleared"] == []


# ---- 第 3 步：双路抢映失败测（缺争用留痕即红） --------------------------

def test_concurrent_remap_same_old_key_has_contention_lock(client, tmp_path):
    token = client.headers["Authorization"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        local = TestClient(app, raise_server_exceptions=False)
        local.headers.update({"Authorization": token})
        barrier.wait()
        r = local.post("/api/remap", json={"old_key": "OLD-B2"})
        results.append((r.status_code, r.json() if r.content else {}))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409], f"两路抢映须一成一争用，实际：{statuses}"
    winner = [b for s, b in results if s == 200]
    loser = [b for s, b in results if s == 409]
    assert winner[0]["outcome"] == "成功"
    assert "争用" in loser[0]["detail"]

    with _raw_conn(tmp_path / "t.db") as raw:
        # 正式区只允许一行，入站只清除一次，冲突区不挂起
        assert raw.execute(
            "SELECT COUNT(*) c FROM formal_entities WHERE mapped_from='OLD-B2'").fetchone()["c"] == 1
        assert raw.execute(
            "SELECT status FROM inbound_queue WHERE old_key='OLD-B2'").fetchone()["status"] == "已重映"
        assert raw.execute(
            "SELECT COUNT(*) c FROM conflicts WHERE old_key='OLD-B2'").fetchone()["c"] == 0
        rows = raw.execute(
            "SELECT outcome, COUNT(*) c FROM remap_audits WHERE old_key='OLD-B2' GROUP BY outcome"
        ).fetchall()
        outcomes = {r["outcome"]: r["c"] for r in rows}
        assert outcomes.get("成功") == 1
        assert outcomes.get("争用") == 1, "后到者必须只记争用留痕"

    rec = client.get("/api/reconciliation").json()
    assert rec["aligned"] is True


# ---- 偏离切断说明：拒写并记 半成功偏了 -----------------------------------

def test_deviation_from_cutoff_spec_is_rejected_and_logged(client, tmp_path):
    db_path = tmp_path / "t.db"
    with _raw_conn(db_path) as raw:
        before = raw.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"]
        # 让入站清除语句静默影响 0 行，破坏"清除与插入配对"，触发偏离守卫
        raw.execute(
            "CREATE TRIGGER deviate_inbound BEFORE UPDATE ON inbound_queue "
            "WHEN NEW.old_key='OLD-B1' "
            "BEGIN SELECT RAISE(IGNORE); END")
        raw.commit()

    r = client.post("/api/remap", json={"old_key": "OLD-B1"})
    assert r.status_code == 500
    assert "半成功偏了" in r.json()["detail"]

    with _raw_conn(db_path) as raw:
        assert raw.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"] == before
        assert raw.execute(
            "SELECT status FROM inbound_queue WHERE old_key='OLD-B1'").fetchone()["status"] == "待重映"

    audits = client.get("/api/audits").json()["items"]
    assert any(a["old_key"] == "OLD-B1" and a["outcome"] == "半成功偏了" for a in audits)
    assert client.get("/api/reconciliation").json()["aligned"] is True

    # 触发器移除后，同入口重映恢复正常，五处对齐
    with _raw_conn(db_path) as raw:
        raw.execute("DROP TRIGGER deviate_inbound")
        raw.commit()
    ok = client.post("/api/remap", json={"old_key": "OLD-B1"})
    assert ok.status_code == 200 and ok.json()["outcome"] == "成功"
    assert client.get("/api/reconciliation").json()["aligned"] is True


# ---- 五处对齐的常规路径 -------------------------------------------------

def test_successful_remap_aligns_all_five_views(client):
    r = client.post("/api/remap", json={"old_key": "OLD-B1"})
    assert r.status_code == 200
    receipt = r.json()
    assert receipt["entity_key"] == "NEW-200"

    formal = {x["mapped_from"] for x in client.get("/api/formal").json()["items"]}
    inbound = {x["old_key"]: x["status"] for x in client.get("/api/inbound").json()["items"]}
    conflicts = {x["old_key"] for x in client.get("/api/conflicts").json()["items"]}
    rec = client.get("/api/reconciliation").json()

    assert "OLD-B1" in formal
    assert inbound["OLD-B1"] == "已重映"
    assert "OLD-B1" not in conflicts
    assert rec["aligned"] is True
    assert rec["torn"] == [] and rec["orphan_cleared"] == []


def test_no_mapping_goes_conflict_without_touching_inbound(client):
    r = client.post("/api/remap", json={"old_key": "OLD-ZZ"})
    assert r.status_code == 409
    conflicts = client.get("/api/conflicts").json()["items"]
    assert any(x["old_key"] == "OLD-ZZ" and x["status"] == "挂起" for x in conflicts)
    rec = client.get("/api/reconciliation").json()
    assert rec["aligned"] is True
