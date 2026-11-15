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
        logic.upsert_rows(conn, [(f"R{i}", f"描述{i}") for i in range(5)], time.time())
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
                    info = logic.claim_record(conn, f"R{i}", a, time.time(), 60)
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


def test_concurrent_rewrite_vs_submit(env):
    """提交与改写并发：要么提交先于改写（随后被清除），要么被挡下；绝不能写进新版本。"""
    db, logic = env
    conn = db.connect()
    info = logic.claim_record(conn, "R0", "ann1", time.time(), 60)
    conn.close()
    token = info["token"]
    done = threading.Event()

    def rewriter():
        for i in range(30):
            with db.write_tx() as c:
                logic.upsert_rows(c, [("R0", f"改写版本{i}")], time.time())
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
