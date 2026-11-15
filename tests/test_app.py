"""业务规则与页面流程测试。

时间相关用例直接调用 logic 层并注入 now，无需真实等待 60 秒。
"""
import importlib
import io

import pytest
from fastapi.testclient import TestClient

CSV1 = "record_no,description\nR1,玉米叶片出现病斑\nR2,稻穗颈部枯死\nR3,植株生长正常\n"
CSV1_CHANGED = "record_no,description\nR1,玉米叶片出现大面积褐色病斑（改写）\nR2,稻穗颈部枯死\nR3,植株生长正常\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEED_ON_EMPTY", "0")
    import app.db as db
    importlib.reload(db)
    import app.logic as logic
    importlib.reload(logic)
    import app.main as main
    importlib.reload(main)
    db.init_db()
    with TestClient(main.app) as client:
        yield db, logic, main, client


def upload(client, text=CSV1):
    return client.post(
        "/upload", files={"file": ("a.csv", text.encode("utf-8"), "text/csv")}
    )


def claim_token(client, record_no, annotator):
    client.cookies.set("annotator", annotator)
    r = client.post("/claim", data={"record_no": record_no}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/work/")[1]


def submit(client, token, annotator, label):
    client.cookies.set("annotator", annotator)
    return client.post("/submit", data={"token": token, "label": label})


# ------------------------------------------------------------- 页面流程

def test_full_flow_to_download(env):
    db, logic, main, client = env
    assert upload(client).status_code == 200

    t1 = claim_token(client, "R1", "ann1")
    r = client.get(f"/work/{t1}")
    assert r.status_code == 200 and "玉米叶片出现病斑" in r.text

    assert submit(client, t1, "ann1", "病害").status_code == 200
    t2 = claim_token(client, "R1", "ann2")
    assert submit(client, t2, "ann2", "病害").status_code == 200

    r = client.get("/usable")
    assert "R1" in r.text and "病害" in r.text

    r = client.get("/download")
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "R1,玉米叶片出现病斑,病害" in body
    assert "R2" not in body  # 未完成的记录不进训练集


def test_blind_pending_hides_labels(env):
    """提交前服务端不返回他人标签：待标页只显示票数。"""
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    submit(client, t1, "ann1", "虫害")
    client.cookies.set("annotator", "ann2")
    r = client.get("/pending")
    assert "R1" in r.text and "1/2" in r.text
    # R1 所在行不得出现他人已提交的标签
    row = r.text.split("R1", 1)[1].split("</tr>", 1)[0]
    assert "虫害" not in row


def test_expired_submit_rejected_and_reclaim(env):
    db, logic, main, client = env
    upload(client)
    conn = db.connect()
    try:
        info = logic.claim_record(conn, "R1", "ann1", now=1000.0, ttl=60)
        with pytest.raises(logic.LogicError, match="过期"):
            logic.submit_label(conn, info["token"], "ann1", "病害", now=1061.0)
        # 过期后可重领：拿到新 token
        info2 = logic.claim_record(conn, "R1", "ann1", now=1062.0, ttl=60)
        assert info2["token"] != info["token"]
        # 旧页（旧 token）即使本人重领后也拒绝
        with pytest.raises(logic.LogicError, match="凭证无效"):
            logic.submit_label(conn, info["token"], "ann1", "病害", now=1063.0)
        # 新页在有效期内可提交
        res = logic.submit_label(conn, info2["token"], "ann1", "病害", now=1064.0)
        assert res["duplicated"] is False
    finally:
        conn.close()


def test_duplicate_submit_adds_no_vote(env):
    db, logic, main, client = env
    upload(client)
    t = claim_token(client, "R1", "ann1")
    assert submit(client, t, "ann1", "病害").status_code == 200
    r = submit(client, t, "ann1", "病害")
    assert r.status_code == 200 and "重复提交" in r.text
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"]
    conn.close()
    assert n == 1


def test_third_annotator_blocked(env):
    db, logic, main, client = env
    upload(client)
    claim_token(client, "R1", "ann1")
    claim_token(client, "R1", "ann2")
    # 两席被占，第三人不能领取
    client.cookies.set("annotator", "ann3")
    r = client.post("/claim", data={"record_no": "R1"}, follow_redirects=False)
    assert "err" in r.headers["location"]
    # 前两票完成后第三人同样不能领取
    t1 = claim_token(client, "R2", "ann1")
    t2 = claim_token(client, "R2", "ann2")
    submit(client, t1, "ann1", "病害")
    submit(client, t2, "ann2", "病害")
    r = client.post("/claim", data={"record_no": "R2"}, follow_redirects=False)
    assert "err" in r.headers["location"]


def test_disagreement_page_and_download_excluded(env):
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    t2 = claim_token(client, "R1", "ann2")
    submit(client, t1, "ann1", "病害")
    submit(client, t2, "ann2", "虫害")
    r = client.get("/disagreed")
    assert "R1" in r.text and "病害" in r.text and "虫害" in r.text
    assert "R1" not in client.get("/download").content.decode("utf-8-sig")


# ------------------------------------------------------------- 改写规则

def test_rewrite_clears_and_blocks_late_submit(env):
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    submit(client, t1, "ann1", "病害")          # 旧版本一票
    t2 = claim_token(client, "R1", "ann2")      # ann2 持有有效领取

    r = upload(client, CSV1_CHANGED)            # R1 描述改写
    assert "改写 <b>1</b>" in r.text and "未变保留 <b>2</b>" in r.text

    conn = db.connect()
    cur = conn.execute("SELECT * FROM records_current WHERE record_no='R1'").fetchone()
    assert cur["version"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM claims WHERE record_no='R1'").fetchone()["c"] == 0
    conn.close()

    # 旧页提交（迟到）不得写入新版
    r = submit(client, t2, "ann2", "虫害")
    assert r.status_code == 400 and "被拒绝" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"] == 0
    conn.close()

    # 未改写的 R2 进度保留：之前 ann1 没标过，重新验证票数语义
    t3 = claim_token(client, "R2", "ann1")
    submit(client, t3, "ann1", "病害")
    upload(client, CSV1_CHANGED)  # 再次重传，R2 仍未变
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no='R2' AND version=1"
    ).fetchone()["c"] == 1
    conn.close()


def test_chinese_header_csv(env):
    db, logic, main, client = env
    r = upload(client, "记录号,描述\nC1,茶园发现茶小绿叶蝉\n")
    assert r.status_code == 200 and "新增 <b>1</b>" in r.text
