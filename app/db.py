"""SQLite 数据访问层。

两个应用进程共享同一个 SQLite 数据库文件：
- WAL 模式 + busy_timeout，读写不互相阻塞，写者按 BEGIN IMMEDIATE 串行化；
- 所有多步写入都包在 write_tx() 的 BEGIN IMMEDIATE 事务里，
  因此跨进程的并发领取 / 提交 / 改写要么全部落库，要么整体回滚；
- 领取期限以绝对时间戳落库，进程重启后仍然有效。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "annotate.db"
DB_PATH = os.environ.get("DB_PATH", str(_DEFAULT_DB))

SCHEMA = """
CREATE TABLE IF NOT EXISTS records_current (
    record_no   TEXT PRIMARY KEY,      -- 记录号
    version     INTEGER NOT NULL,      -- 描述每改写一次 +1
    description TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    token      TEXT PRIMARY KEY,       -- 领取凭证，提交时校验
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,       -- 领取时的记录版本
    annotator  TEXT NOT NULL,
    expires_at REAL NOT NULL,          -- 绝对过期时间（epoch 秒）
    created_at REAL NOT NULL,
    UNIQUE (record_no, version, annotator)   -- 同一标注员对同一版本只有一条领取
);

CREATE TABLE IF NOT EXISTS labels (
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    annotator  TEXT NOT NULL,
    label      TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
    created_at REAL NOT NULL,
    PRIMARY KEY (record_no, version, annotator)  -- 同人同版本至多一票，重复提交不产生新票
);

CREATE INDEX IF NOT EXISTS idx_claims_rv ON claims(record_no, version);
CREATE INDEX IF NOT EXISTS idx_labels_rv ON labels(record_no, version);
"""


def connect() -> sqlite3.Connection:
    path = DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：自动提交，事务由我们显式 BEGIN IMMEDIATE 控制
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写事务：BEGIN IMMEDIATE 先拿写锁，避免并发下锁升级失败。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
