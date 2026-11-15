"""旧库升级测试：已有 SQLite 一次性归入历史站，保留领取与标签，且升级原子生效。

覆盖：
- 旧结构（record_no 单键）升级后数据完整搬到历史站，token 仍可用；
- 升级只发生一次，新结构再次 init_db 不重复搬迁；
- 升级中途失败时整体回滚，旧库结构与数据保持原样；
- 两个进程（并发线程 / 独立进程）同时启动 init_db，升级只完整生效一次；
- 升级期间旧页凭证只能写到历史站，不能打到另一个站的同号记录。
"""
import importlib
import multiprocessing
import os
import sqlite3
import threading
import time

import pytest

# 旧版结构与种子数据：与 app/db.py 里的 _LEGACY_SCHEMA 保持一致。
LEGACY_SQL = """
CREATE TABLE records_current (
    record_no TEXT PRIMARY KEY, version INTEGER NOT NULL,
    description TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE claims (
    token TEXT PRIMARY KEY, record_no TEXT NOT NULL, version INTEGER NOT NULL,
    annotator TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
    UNIQUE (record_no, version, annotator)
);
CREATE TABLE labels (
    record_no TEXT NOT NULL, version INTEGER NOT NULL, annotator TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
    created_at REAL NOT NULL,
    PRIMARY KEY (record_no, version, annotator)
);
CREATE INDEX idx_claims_rv ON claims(record_no, version);
CREATE INDEX idx_labels_rv ON labels(record_no, version);
"""


def make_legacy_db(path, now=None):
    """建一个旧版库：L1 有 ann1 的标签 + ann2 的有效领取，L2 未标。"""
    now = time.time() if now is None else now
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SQL)
        conn.executemany(
            "INSERT INTO records_current VALUES (?,?,?,?)",
            [
                ("L1", 3, "旧站玉米叶片病斑", now - 100),
                ("L2", 1, "旧站稻穗枯死", now - 100),
            ],
        )
        conn.execute(
            "INSERT INTO claims VALUES (?,?,?,?,?,?)",
            ("legacy-token-ann2", "L1", 3, "ann2", now + 3600, now - 50),
        )
        conn.execute(
            "INSERT INTO claims VALUES (?,?,?,?,?,?)",
            ("legacy-token-ann1", "L2", 1, "ann1", now + 3600, now - 50),
        )
        conn.execute(
            "INSERT INTO labels VALUES (?,?,?,?,?)",
            ("L1", 3, "ann1", "病害", now - 40),
        )
        conn.commit()
    finally:
        conn.close()


def load_modules(monkeypatch, path):
    monkeypatch.setenv("DB_PATH", str(path))
    import app.db as db
    importlib.reload(db)
    import app.logic as logic
    importlib.reload(logic)
    return db, logic


def column_names(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def test_legacy_upgrade_moves_data_to_history_station(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    now = time.time()
    make_legacy_db(path, now)
    db, logic = load_modules(monkeypatch, path)

    db.init_db()

    conn = db.connect()
    try:
        assert "station" in column_names(conn, "records_current")
        assert "station" in column_names(conn, "claims")
        assert "station" in column_names(conn, "labels")
        recs = conn.execute(
            "SELECT station, record_no, version, description FROM records_current "
            "ORDER BY record_no"
        ).fetchall()
        assert [(r["station"], r["record_no"], r["version"]) for r in recs] == [
            ("历史站", "L1", 3),
            ("历史站", "L2", 1),
        ]
        assert recs[0]["description"] == "旧站玉米叶片病斑"
        labels = conn.execute("SELECT * FROM labels").fetchall()
        assert [(r["station"], r["record_no"], r["version"], r["annotator"], r["label"])
                for r in labels] == [("历史站", "L1", 3, "ann1", "病害")]
        claims = conn.execute(
            "SELECT token, station, record_no, version, annotator FROM claims ORDER BY token"
        ).fetchall()
        assert {(r["token"], r["station"], r["record_no"], r["annotator"]) for r in claims} == {
            ("legacy-token-ann2", "历史站", "L1", "ann2"),
            ("legacy-token-ann1", "历史站", "L2", "ann1"),
        }
        # 临时迁移表不残留
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_migrated'"
        ).fetchall()
        assert leftovers == []
        # 旧索引已随旧表删除，新索引存在
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert {"idx_claims_rv", "idx_labels_rv"} <= indexes
    finally:
        conn.close()


def test_upgraded_legacy_claim_and_label_still_work(tmp_path, monkeypatch):
    path = tmp_path / "legacy2.db"
    now = time.time()
    make_legacy_db(path, now)
    db, logic = load_modules(monkeypatch, path)
    db.init_db()

    # 旧 token 仍然有效，能继续提交并落成历史站上的第二票
    with db.write_tx() as conn:
        res = logic.submit_label(conn, "legacy-token-ann2", "ann2", "病害", now)
    assert res == {"duplicated": False, "station": "历史站", "record_no": "L1", "version": 3}

    conn = db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM labels WHERE station='历史站' AND record_no='L1'"
        ).fetchone()["c"] == 2
        usable = logic.list_usable(conn)
    finally:
        conn.close()
    assert [(u["station"], u["record_no"], u["label"]) for u in usable] == [
        ("历史站", "L1", "病害")
    ]


def test_init_db_is_idempotent_after_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "legacy3.db"
    make_legacy_db(path)
    db, logic = load_modules(monkeypatch, path)
    db.init_db()
    # 归入历史站后再上传一条同号数据，不应把历史站记录“升级”成新的站
    db.init_db()
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT station, record_no, version FROM records_current ORDER BY record_no"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("历史站", "L1", 3), ("历史站", "L2", 1)]
    finally:
        conn.close()


def test_failed_upgrade_rolls_back_completely(tmp_path, monkeypatch):
    """迁移中途抛错：旧表、旧数据、旧索引原样保留，没有半成品新表。"""
    path = tmp_path / "legacy4.db"
    make_legacy_db(path)
    db, logic = load_modules(monkeypatch, path)

    original = db._migrate_legacy
    calls = {"n": 0}

    def boom(conn):
        original(conn)
        calls["n"] += 1
        raise RuntimeError("模拟升级中途失败")

    monkeypatch.setattr(db, "_migrate_legacy", boom)
    with pytest.raises(RuntimeError, match="模拟升级中途失败"):
        db.init_db()
    assert calls["n"] == 1

    monkeypatch.setattr(db, "_migrate_legacy", original)
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "records_current" in tables and "claims" in tables and "labels" in tables
        assert not any(t.endswith("_migrated") for t in tables)
        assert "station" not in [r[1] for r in conn.execute("PRAGMA table_info(records_current)")]
        assert conn.execute("SELECT COUNT(*) FROM records_current").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_claims_rv'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # 回滚后再次正常启动即可完成升级
    db.init_db()
    conn = db.connect()
    try:
        assert {r["station"] for r in conn.execute("SELECT station FROM records_current")} == {"历史站"}
    finally:
        conn.close()


def test_concurrent_init_db_upgrades_once(tmp_path, monkeypatch):
    """两个线程同时启动（模拟两个应用进程）：升级只生效一次，数据不重复不丢失。"""
    path = tmp_path / "legacy5.db"
    make_legacy_db(path)
    db, logic = load_modules(monkeypatch, path)

    errors, ready = [], threading.Barrier(6)

    def boot():
        try:
            ready.wait(timeout=10)
            db.init_db()
        except Exception as e:  # pragma: no cover - 失败时输出便于定位
            errors.append(e)

    threads = [threading.Thread(target=boot) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM records_current").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM records_current WHERE station<>'历史站'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 独立进程级验证

def _boot_process(db_path, station_to_add, q):
    # 必须放在子进程内设置并重载：spawn 启动的进程会重新导入 app.db，
    # 模块级的 DB_PATH 在导入时就已确定。
    os.environ["DB_PATH"] = db_path
    import app.db as db
    import app.logic as logic

    importlib.reload(db)
    importlib.reload(logic)
    try:
        db.init_db()
        if station_to_add:
            with db.write_tx() as conn:
                logic.upsert_rows(conn, [(station_to_add, "L1", "另一站点的同号描述")], time.time())
        with db.write_tx() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"]
        q.put(("ok", n))
    except Exception as e:
        q.put(("err", repr(e)))


def test_two_processes_upgrade_concurrently(tmp_path):
    """两个真实进程交错启动/上传：升级完整生效，历史站旧 token 不会写到别的站。"""
    path = tmp_path / "legacy6.db"
    now = time.time()
    make_legacy_db(path, now)

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_boot_process, args=(str(path), None, q)),
        ctx.Process(target=_boot_process, args=(str(path), "新站", q)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    results = sorted(q.get(timeout=10) for _ in range(2))
    assert [r[0] for r in results] == ["ok", "ok"]

    import app.db as db
    import app.logic as logic

    os.environ["DB_PATH"] = str(path)
    importlib.reload(db)
    importlib.reload(logic)

    conn = db.connect()
    try:
        by_station = {
            r["station"]: r["record_no"]
            for r in conn.execute("SELECT station, record_no FROM records_current")
        }
        # 历史站的两条旧记录 + 新站的一条同号记录，互不覆盖且没有重复搬迁
        assert conn.execute(
            "SELECT COUNT(*) c FROM records_current"
        ).fetchone()["c"] == 3
        assert conn.execute(
            "SELECT COUNT(*) c FROM records_current WHERE station='历史站'"
        ).fetchone()["c"] == 2
        assert "新站" in by_station
        # 旧 token 只属于历史站
        cl = conn.execute(
            "SELECT station, record_no FROM claims WHERE token='legacy-token-ann2'"
        ).fetchone()
        assert (cl["station"], cl["record_no"]) == ("历史站", "L1")
    finally:
        conn.close()

    # 旧页凭证照常提交，只落在历史站
    with db.write_tx() as conn:
        res = logic.submit_label(conn, "legacy-token-ann2", "ann2", "虫害", time.time())
    assert res["station"] == "历史站"
    conn = db.connect()
    try:
        assert conn.execute(
            "SELECT label FROM labels WHERE station='历史站' AND record_no='L1' AND annotator='ann2'"
        ).fetchone()["label"] == "虫害"
        assert conn.execute(
            "SELECT COUNT(*) c FROM labels WHERE station='新站'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
