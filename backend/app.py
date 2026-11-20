
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 乙路径：入站清除与正式插入必须同库一次事务（BEGIN IMMEDIATE）提交，
# 缺一方则两侧回滚，保持撕裂前。详见 docs/半成功切断说明.md
BUSY_TIMEOUT_MS = 5000


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def record_audit(old_key: str, outcome: str, detail: str):
    """审计留痕独立短事务，绝不与主写路径混用连接，避免污染回滚边界。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO remap_audits(old_key,outcome,detail,at) VALUES (?,?,?,?)",
            (old_key, outcome, detail, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbound_queue(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          arrived_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS remap_audits(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          outcome TEXT NOT NULL,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))
        # 入站回填：新建库给三条待重映行；旧库（缺入站表）按正式区现状对齐，
        # 已在正式区的旧键标 已重映，避免制造新的撕裂。
        if conn.execute("SELECT COUNT(*) c FROM inbound_queue").fetchone()["c"] == 0:
            known = conn.execute("SELECT old_key, MIN(arrived_at) a FROM (SELECT old_key, '2026-11-11T08:00:00' AS arrived_at FROM mappings) GROUP BY old_key").fetchall()
            formal_from = {r["mapped_from"] for r in conn.execute("SELECT DISTINCT mapped_from FROM formal_entities")}
            for r in known:
                status = "已重映" if r["old_key"] in formal_from else "待重映"
                conn.execute("INSERT INTO inbound_queue(old_key,status,arrived_at) VALUES (?,?,?)",
                             (r["old_key"], status, r["a"]))
        conn.commit()


@app.on_event("startup")
def _s(): init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}


@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}


@app.get("/api/inbound")
def inbound(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM inbound_queue ORDER BY id").fetchall()]}


@app.get("/api/audits")
def audits(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM remap_audits ORDER BY id DESC").fetchall()]}


class RemapIn(BaseModel):
    old_key: str


class HalfSuccessDeviation(Exception):
    """偏离半成功切断说明：无法保证入站清除与正式插入同败同成，必须拒写。"""


@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    """乙路径唯一重映写入口：同库 BEGIN IMMEDIATE 一次事务，入站清除与正式
    插入同败同成；后到同键只记争用；故障整体回滚至撕裂前。"""
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        # 先占库级写锁，两路抢映在此串行
        conn.execute("BEGIN IMMEDIATE")

        inbound_row = conn.execute(
            "SELECT id, status FROM inbound_queue WHERE old_key=?", (body.old_key,)).fetchone()
        if inbound_row is not None and inbound_row["status"] == "已重映":
            # 后到者：入站行已被先到者清除，只记争用，正式零增、冲突不挂起
            conn.execute(
                "INSERT INTO remap_audits(old_key,outcome,detail,at) VALUES (?,?,?,?)",
                (body.old_key, "争用", "同键重映争用，后到请求未写入", now))
            conn.commit()
            raise HTTPException(409, "同键重映争用：该旧键已被处理，后到请求只记争用")
        if inbound_row is None:
            # 首次入站：同事务登记为待重映，再走映射判定（无映射则进冲突区）
            conn.execute(
                "INSERT INTO inbound_queue(old_key,status,arrived_at) VALUES (?,?,?)",
                (body.old_key, "待重映", now))

        ver = conn.execute(
            "SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver:
            conn.execute(
                "INSERT INTO remap_audits(old_key,outcome,detail,at) VALUES (?,?,?,?)",
                (body.old_key, "冲突", "无生效映射版本", now))
            conn.commit()
            raise HTTPException(404, "无生效映射版本")

        m = conn.execute(
            "SELECT * FROM mappings WHERE version=? AND old_key=?",
            (ver["version"], body.old_key)).fetchone()
        if not m:
            # 无映射：入站不动、正式零增，冲突区挂起
            conn.execute(
                "INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                (body.old_key, "无映射", "挂起"))
            conn.execute(
                "INSERT INTO remap_audits(old_key,outcome,detail,at) VALUES (?,?,?,?)",
                (body.old_key, "冲突", "无映射，进冲突队列", now))
            conn.commit()
            raise HTTPException(409, "无映射，已进冲突队列")

        # 同一事务：先清入站（校验恰清一行），再插正式区
        cur = conn.execute(
            "UPDATE inbound_queue SET status='已重映' WHERE old_key=? AND status='待重映'",
            (body.old_key,))
        if cur.rowcount != 1:
            # 偏离乙路径：清除与插入无法配对，拒写并记 半成功偏了
            raise HalfSuccessDeviation("入站清除影响行数=%d（应为1），拒写" % cur.rowcount)
        conn.execute(
            "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
            (m["new_key"], body.old_key, ver["version"], now))
        cur_audit = conn.execute(
            "INSERT INTO remap_audits(old_key,outcome,detail,at) VALUES (?,?,?,?)",
            (body.old_key, "成功", "同事务清除入站并写入正式区", now))
        conn.commit()
        return {
            "old_key": body.old_key,
            "entity_key": m["new_key"],
            "version": ver["version"],
            "outcome": "成功",
            "audit_id": cur_audit.lastrowid,
        }
    except HTTPException:
        # 409/404 分支已自行提交留痕，事务边界已闭合
        raise
    except HalfSuccessDeviation as e:
        # 偏离切断说明：拒写，两侧回滚，专档留痕 半成功偏了
        conn.rollback()
        record_audit(body.old_key, "半成功偏了", "拒写并两侧回滚：%s" % e)
        raise HTTPException(500, "半成功偏了：已拒写并整体回滚，保持撕裂前")
    except sqlite3.Error as e:
        # 任一写入失败：两侧回滚，正式零增、入站保持 待重映，回到撕裂前
        conn.rollback()
        record_audit(body.old_key, "回滚", "事务失败两侧回滚：%s" % e)
        raise HTTPException(500, "重映事务失败，入站与正式区已整体回滚")
    finally:
        conn.close()


@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}


@app.get("/api/reconciliation")
def reconciliation(authorization: str | None = Header(default=None)):
    """互证卡：由库内同批数据派生，禁止靠展示过滤掩盖撕裂。"""
    require_user(authorization)
    with connect() as c:
        formal_from = {r["mapped_from"] for r in c.execute(
            "SELECT DISTINCT mapped_from FROM formal_entities")}
        pending = {r["old_key"] for r in c.execute(
            "SELECT old_key FROM inbound_queue WHERE status='待重映'")}
        cleared = {r["old_key"] for r in c.execute(
            "SELECT old_key FROM inbound_queue WHERE status='已重映'")}
        conflict_pending = {r["old_key"] for r in c.execute(
            "SELECT old_key FROM conflicts WHERE status='挂起'")}
    torn = sorted(formal_from & pending)
    orphan_cleared = sorted(cleared - formal_from)
    return {
        "aligned": not torn and not orphan_cleared,
        "torn": torn,
        "orphan_cleared": orphan_cleared,
        "formal_count": len(formal_from),
        "inbound_pending_count": len(pending),
        "conflict_pending_count": len(conflict_pending),
    }


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
