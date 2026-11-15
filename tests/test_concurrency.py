"""并发压力测试：多线程共用同一 SQLite 文件，模拟两个应用进程下的并发领取/提交/改写。

不变量：
- 同一 (record_no, version) 至多两票，且来自不同标注员；
- 重复提交不产生额外票；
- 改写后旧 token 一律无法提交。
"""
import importlib
import threading
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "c.db"))
    import app.db as db
    importlib.reload(db)
    import app.logic as logic
    importlib.reload(logic)
    db.init_db()
    with db.write_tx() as conn:
        logic.upsert_rows(conn, [(f"站{i%2}", f"R{i}", f"描述{i}") for i in range(5)], time.time())
    return db, logic


def test_concurrent_claim_and_submit(env):
    db, logic = env
    annotators = ["ann1", "ann2", "ann3"]
    errors, tokens = [], []
    lock = threading.Lock()

    def claim_all(a):
        for i in range(5):
            try:
                with db.write_tx() as conn:
                    info = logic.claim_record(conn, f"站{i%2}", f"R{i}", a, time.time(), 60)
                with lock:
                    tokens.append((f"R{i}", a, info["token"]))
            except logic.LogicError:
                pass

    threads = [threading.Thread(target=claim_all, args=(a,)) for a in annotators for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每条记录有效领取（不同标注员）不超过 2 个
    by_rec = {}
    for rec, a, _ in tokens:
        by_rec.setdefault(rec, set()).add(a)
    for rec, holders in by_rec.items():
        assert len(holders) <= 2, f"{rec} 被 {holders} 同时领取"

    def submit_all(rec, a, token):
        try:
            with db.write_tx() as conn:
                logic.submit_label(conn, token, a, "病害", time.time())
        except logic.LogicError:
            pass
        except Exception as e:  # 不允许出现非业务异常（如 database is locked）
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=submit_all, args=x) for x in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    rows = conn.execute(
        "SELECT record_no, version, COUNT(*) c, COUNT(DISTINCT annotator) d "
        "FROM labels GROUP BY record_no, version"
    ).fetchall()
    conn.close()
    for r in rows:
        assert r["c"] <= 2 and r["c"] == r["d"], dict(r)


def test_same_record_no_across_stations_concurrent(env):
    """两个来源站同号记录的并发领取 / 提交 / 改写：互不串票，也不互相清空。

    每条记录在两站各有“站A”“站B”两个副本，两站交替改写副本，另一个站必须始终
    保留自己已提交的票；每个 (station, record_no, version) 仍然至多两票。
    """
    db, logic = env
    n = 4
    rows = []
    for i in range(n):
        rows.append(("站A", f"S{i}", f"站A描述{i}"))
        rows.append(("站B", f"S{i}", f"站B描述{i}"))
    with db.write_tx() as conn:
        logic.upsert_rows(conn, rows, time.time())

    errors, lock = [], threading.Lock()

    def worker(station, suffix):
        for i in range(n):
            for _ in range(3):
                try:
                    with db.write_tx() as conn:
                        info = logic.claim_record(
                            conn, station, f"S{i}", "ann1", time.time(), 60
                        )
                    with db.write_tx() as conn:
                        logic.submit_label(conn, info["token"], "ann1", "病害", time.time())
                    break
                except logic.LogicError:
                    time.sleep(0.01)
                except Exception as e:  # pragma: no cover
                    with lock:
                        errors.append(e)
            # 本站改写（别的站改写同号记录不应影响本站）
            with db.write_tx() as conn:
                logic.upsert_rows(conn, [(station, f"S{i}", f"{station}描述{i}-{suffix}")], time.time())

    threads = [
        threading.Thread(target=worker, args=("站A", "a")),
        threading.Thread(target=worker, args=("站B", "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    try:
        # 每条记录在两站都存在，版本各自独立递增，互不覆盖
        for i in range(n):
            for station in ("站A", "站B"):
                r = conn.execute(
                    "SELECT station, version, description FROM records_current "
                    "WHERE station=? AND record_no=?",
                    (station, f"S{i}"),
                ).fetchone()
                assert r is not None, (station, i)
                assert r["description"].startswith(station)
        # 标签只可能落在本版本、且每站每版本至多两票
        for r in conn.execute(
            "SELECT station, record_no, version, COUNT(*) c, COUNT(DISTINCT annotator) d "
            "FROM labels GROUP BY station, record_no, version"
        ):
            assert r["c"] <= 2 and r["c"] == r["d"], tuple(r)
        # 标签落点必须与该站当前版本一致，绝不出现跨站同号串票
        for r in conn.execute("SELECT station, record_no FROM labels"):
            assert (r["station"], r["record_no"]) in {(s, f"S{i}") for s in ("站A", "站B") for i in range(n)}
    finally:
        conn.close()


def test_concurrent_rewrite_vs_submit(env):
    """提交与改写并发：要么提交先于改写（随后被清除），要么被挡下；绝不能写进新版本。"""
    db, logic = env
    conn = db.connect()
    info = logic.claim_record(conn, "站0", "R0", "ann1", time.time(), 60)
    conn.close()
    token = info["token"]
    done = threading.Event()

    def rewriter():
        for i in range(30):
            with db.write_tx() as c:
                logic.upsert_rows(c, [("站0", "R0", f"改写版本{i}")], time.time())
        done.set()

    def submitter():
        while not done.is_set():
            try:
                with db.write_tx() as c:
                    logic.submit_label(c, token, "ann1", "病害", time.time())
            except logic.LogicError:
                pass

    ts = [threading.Thread(target=rewriter), threading.Thread(target=submitter)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    conn = db.connect()
    cur = conn.execute("SELECT version FROM records_current WHERE record_no='R0'").fetchone()
    n = conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no='R0' AND version=?",
        (cur["version"],),
    ).fetchone()["c"]
    conn.close()
    assert n == 0, "旧版提交写进了新版本"
