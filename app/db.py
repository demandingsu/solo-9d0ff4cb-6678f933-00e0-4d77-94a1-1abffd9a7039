"""SQLite 数据访问层。

两个应用进程共享同一个 SQLite 数据库文件：
- WAL 模式 + busy_timeout，读写不互相阻塞，写者按 BEGIN IMMEDIATE 串行化；
- 所有多步写入都包在 write_tx() 的 BEGIN IMMEDIATE 事务里，
  因此跨进程的并发领取 / 提交 / 改写要么全部落库，要么整体回滚；
- 领取期限以绝对时间戳落库，进程重启后仍然有效。

记录以 (station, record_no) 联合定位：不同合作站可以使用相同的站内记录号，
互不串票。旧版数据库只有 record_no，升级时会把存量数据整体归入“历史站”，
并在同一个 BEGIN IMMEDIATE 事务里完成建表、搬迁、删旧表、改名的全过程；
事务失败则回滚到升级前的结构与数据，两个进程同时启动也只会迁移一次。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "annotate.db"
DB_PATH = os.environ.get("DB_PATH", str(_DEFAULT_DB))

# 旧版数据（只有一个站内记录号）在升级后统一归入这个来源站。
LEGACY_STATION = "历史站"

_TABLES = ("records_current", "claims", "labels")

# 新版结构：station 与 record_no 共同构成记录身份。
SCHEMA = """
CREATE TABLE IF NOT EXISTS records_current (
    station     TEXT NOT NULL,          -- 来源站
    record_no   TEXT NOT NULL,          -- 站内记录号
    version     INTEGER NOT NULL,       -- 描述每改写一次 +1
    description TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (station, record_no)
);

CREATE TABLE IF NOT EXISTS claims (
    token      TEXT PRIMARY KEY,        -- 领取凭证，提交时校验
    station    TEXT NOT NULL,
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,        -- 领取时的记录版本
    annotator  TEXT NOT NULL,
    expires_at REAL NOT NULL,           -- 绝对过期时间（epoch 秒）
    created_at REAL NOT NULL,
    UNIQUE (station, record_no, version, annotator)  -- 同站同记录同版本同人一条领取
);

CREATE TABLE IF NOT EXISTS labels (
    station    TEXT NOT NULL,
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    annotator  TEXT NOT NULL,
    label      TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
    created_at REAL NOT NULL,
    PRIMARY KEY (station, record_no, version, annotator)  -- 同人同站同版本至多一票
);

CREATE INDEX IF NOT EXISTS idx_claims_rv ON claims(station, record_no, version);
CREATE INDEX IF NOT EXISTS idx_labels_rv ON labels(station, record_no, version);
"""

# 旧版结构（record_no 单列主键），仅用于识别与迁移。
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS records_current (
    record_no   TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    description TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    token      TEXT PRIMARY KEY,
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    annotator  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (record_no, version, annotator)
);
CREATE TABLE IF NOT EXISTS labels (
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    annotator  TEXT NOT NULL,
    label      TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
    created_at REAL NOT NULL,
    PRIMARY KEY (record_no, version, annotator)
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list:
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把旧版单键结构升级为 (station, record_no) 联合键。

    调用方已经持有 BEGIN IMMEDIATE 写锁，因此两个应用进程同时启动时，
    后到者会等前一个事务提交后再检查；看到新结构就不会重复迁移。
    整个搬迁（建新表 -> 复制数据 -> 删旧表 -> 改名 -> 建索引）都在同一事务内，
    任何一步失败都会整体回滚，旧库保持原样。
    """
    # 存量记录、存量领取（含其 token）和存量标签全部保留，并归入历史站。
    conn.execute("DROP TABLE IF EXISTS records_current_migrated")
    conn.execute("DROP TABLE IF EXISTS claims_migrated")
    conn.execute("DROP TABLE IF EXISTS labels_migrated")

    conn.execute(
        """
        CREATE TABLE records_current_migrated (
            station TEXT NOT NULL, record_no TEXT NOT NULL, version INTEGER NOT NULL,
            description TEXT NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (station, record_no)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO records_current_migrated
            (station, record_no, version, description, updated_at)
        SELECT ?, record_no, version, description, updated_at FROM records_current
        """,
        (LEGACY_STATION,),
    )

    conn.execute(
        """
        CREATE TABLE claims_migrated (
            token TEXT PRIMARY KEY, station TEXT NOT NULL, record_no TEXT NOT NULL,
            version INTEGER NOT NULL, annotator TEXT NOT NULL,
            expires_at REAL NOT NULL, created_at REAL NOT NULL,
            UNIQUE (station, record_no, version, annotator)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO claims_migrated
            (token, station, record_no, version, annotator, expires_at, created_at)
        SELECT token, ?, record_no, version, annotator, expires_at, created_at
        FROM claims
        """,
        (LEGACY_STATION,),
    )

    conn.execute(
        """
        CREATE TABLE labels_migrated (
            station TEXT NOT NULL, record_no TEXT NOT NULL, version INTEGER NOT NULL,
            annotator TEXT NOT NULL,
            label TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
            created_at REAL NOT NULL,
            PRIMARY KEY (station, record_no, version, annotator)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO labels_migrated
            (station, record_no, version, annotator, label, created_at)
        SELECT ?, record_no, version, annotator, label, created_at FROM labels
        """,
        (LEGACY_STATION,),
    )

    conn.execute("DROP TABLE claims")
    conn.execute("DROP TABLE labels")
    conn.execute("DROP TABLE records_current")
    conn.execute("ALTER TABLE records_current_migrated RENAME TO records_current")
    conn.execute("ALTER TABLE claims_migrated RENAME TO claims")
    conn.execute("ALTER TABLE labels_migrated RENAME TO labels")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "records_current"):
        if "station" not in _columns(conn, "records_current"):
            # 确认另外两张旧表也在，避免对结构不完整的库做部分迁移。
            if not _table_exists(conn, "claims") or not _table_exists(conn, "labels"):
                raise RuntimeError("数据库结构不完整：缺少 claims 或 labels 表")
            _migrate_legacy(conn)
        elif "station" not in _columns(conn, "claims") or "station" not in _columns(
            conn, "labels"
        ):
            raise RuntimeError("数据库结构不一致：records_current 已升级，但 claims/labels 未升级")

    # 每条语句单独执行（不能用 executescript：它会先隐式 COMMIT，破坏原子性）。
    for statement in SCHEMA.strip().split(";"):
        if statement.strip():
            conn.execute(statement)


def init_db() -> None:
    """建表 / 升级。整个过程是一个 BEGIN IMMEDIATE 事务。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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
