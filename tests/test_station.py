"""跨来源站同号记录测试。

不同合作站使用相同站内记录号时：描述、领取凭证、标签、改写互不影响；
来源站未填写时归入历史站；页面与训练 CSV 都显示来源站。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

CSV_A = "record_no,description\nX1,上游站苹果树叶片出现黑斑\nX2,上游站梨树新梢枯死\n"
CSV_B = "record_no,description\nX1,东片站葡萄果实表面白色霉层\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
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


def upload(client, text, station=""):
    r = client.post(
        "/upload",
        data={"station": station},
        files={"file": ("a.csv", text.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r


def claim_token(client, record_no, annotator, station=""):
    client.cookies.set("annotator", annotator)
    r = client.post(
        "/claim", data={"station": station, "record_no": record_no}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/work/")[1]


def submit(client, token, annotator, label, station=None):
    client.cookies.set("annotator", annotator)
    data = {"token": token, "label": label}
    if station is not None:
        data["station"] = station
    return client.post("/submit", data=data)


def labels_of(db, station, record_no):
    conn = db.connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT annotator, label, version FROM labels "
                "WHERE station=? AND record_no=? ORDER BY annotator",
                (station, record_no),
            )
        ]
    finally:
        conn.close()


def test_same_record_no_across_stations_isolated(env):
    db, logic, main, client = env
    upload(client, CSV_A, "上游站")
    upload(client, CSV_B, "东片站")

    # 同一标注员可以分别领取两个站的同号记录，互不占位
    ta = claim_token(client, "X1", "ann1", "上游站")
    tb = claim_token(client, "X1", "ann1", "东片站")

    # 两个站各收两票且标签不同：上游站一致、东片站分歧
    assert submit(client, ta, "ann1", "病害").status_code == 200
    assert submit(client, tb, "ann1", "虫害").status_code == 200
    ta2 = claim_token(client, "X1", "ann2", "上游站")
    tb2 = claim_token(client, "X1", "ann2", "东片站")
    assert submit(client, ta2, "ann2", "病害").status_code == 200
    assert submit(client, tb2, "ann2", "无异常").status_code == 200

    assert labels_of(db, "上游站", "X1") == [
        {"annotator": "ann1", "label": "病害", "version": 1},
        {"annotator": "ann2", "label": "病害", "version": 1},
    ]
    assert labels_of(db, "东片站", "X1") == [
        {"annotator": "ann1", "label": "虫害", "version": 1},
        {"annotator": "ann2", "label": "无异常", "version": 1},
    ]

    # 可用页只含上游站那条；分歧页只含东片站那条
    usable = client.get("/usable").text
    assert "上游站" in usable and "东片站" not in usable
    disagreed = client.get("/disagreed").text
    assert "东片站" in disagreed and "上游站" not in disagreed

    # 训练 CSV 带来源站列，且只有上游站那条
    body = client.get("/download").content.decode("utf-8-sig")
    lines = body.splitlines()
    assert lines[0] == "station,record_no,description,label"
    assert "上游站,X1,上游站苹果树叶片出现黑斑,病害" in lines[1:]
    assert not any(line.startswith("东片站,X1,") for line in lines)

    # 待标页显示来源站（X1 两站均已集齐两票，X2 仍在待标）
    pending = client.get("/pending").text
    assert "上游站" in pending
    assert "X2" in pending


def test_blank_station_falls_back_to_history(env):
    db, logic, main, client = env
    r = upload(client, "record_no,description\nH1,茶园发现茶小绿叶蝉\n", "")
    assert "历史站" in r.text

    # 领取时来源站留空同样归入历史站
    t = claim_token(client, "H1", "ann1", "")
    assert "历史站" in client.get(f"/work/{t}").text
    assert submit(client, t, "ann1", "虫害").status_code == 200
    assert labels_of(db, "历史站", "H1")[0]["label"] == "虫害"


def test_csv_station_column_and_row_fallback(env):
    db, logic, main, client = env
    text = (
        "station,record_no,description\n"
        "西南站,C1,柑橘叶片出现黄龙病斑\n"
        ",C2,柑橘果实未成熟脱落\n"
    )
    r = upload(client, text, "默认站")
    assert "西南站" in r.text and "默认站" in r.text
    conn = db.connect()
    rows = conn.execute(
        "SELECT station, record_no FROM records_current ORDER BY record_no"
    ).fetchall()
    conn.close()
    assert [(x["station"], x["record_no"]) for x in rows] == [
        ("西南站", "C1"),
        ("默认站", "C2"),
    ]


def test_rewrite_only_clears_own_station(env):
    db, logic, main, client = env
    upload(client, CSV_A, "上游站")
    upload(client, CSV_B, "东片站")

    ta = claim_token(client, "X1", "ann1", "上游站")
    tb = claim_token(client, "X1", "ann2", "东片站")
    submit(client, ta, "ann1", "病害")

    # 只改写上游站的 X1
    upload(client, "record_no,description\nX1,上游站苹果树叶片出现黑斑（复检改写）\n", "上游站")

    conn = db.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM labels WHERE station='上游站' AND record_no='X1'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM claims WHERE station='上游站' AND record_no='X1'"
        ).fetchone()["c"] == 0
        # 东片站的领取与描述原封不动
        assert conn.execute(
            "SELECT version, description FROM records_current "
            "WHERE station='东片站' AND record_no='X1'"
        ).fetchone()["description"].startswith("东片站葡萄")
        assert conn.execute(
            "SELECT COUNT(*) c FROM claims WHERE station='东片站' AND record_no='X1'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()

    # 上游站旧页凭证失效；东片站旧页凭证仍可提交，且只落在东片站
    assert submit(client, ta, "ann1", "病害").status_code == 400
    assert submit(client, tb, "ann2", "虫害").status_code == 200
    assert labels_of(db, "上游站", "X1") == []
    assert labels_of(db, "东片站", "X1") == [
        {"annotator": "ann2", "label": "虫害", "version": 1}
    ]


def test_token_cannot_be_redirected_to_another_station(env):
    """凭证自带来源站：伪造表单来源站或提交到另一站都会被挡下/落回本站。"""
    db, logic, main, client = env
    upload(client, CSV_A, "上游站")
    upload(client, CSV_B, "东片站")

    ta = claim_token(client, "X1", "ann1", "上游站")
    # 提交时附加另一个来源站字段，标签仍只落在凭证所属的上游站
    assert submit(client, ta, "ann1", "病害", station="东片站").status_code == 200
    assert labels_of(db, "上游站", "X1") == [
        {"annotator": "ann1", "label": "病害", "version": 1}
    ]
    assert labels_of(db, "东片站", "X1") == []

    # 领取时指定不存在的来源站 -> 记录不存在，不能读到另一站的同号描述
    client.cookies.set("annotator", "ann2")
    r = client.post(
        "/claim", data={"station": "不存在站", "record_no": "X1"}, follow_redirects=False
    )
    assert r.status_code == 303 and "/pending?kind=err" in r.headers["location"]

    # 伪造 token 只能命中它自己那站，跨站同号记录不会被写入
    assert submit(client, "deadbeef", "ann2", "虫害").status_code == 400
    assert labels_of(db, "东片站", "X1") == []


def test_pending_shows_station_and_claim_roundtrip(env):
    db, logic, main, client = env
    upload(client, CSV_A, "上游站")
    upload(client, CSV_B, "东片站")
    client.cookies.set("annotator", "ann1")
    pending = client.get("/pending").text
    assert pending.count("上游站") >= 1 and pending.count("东片站") >= 1
    # 待标页的领取表单必须携带来源站，否则会把记录领到错误站点
    assert 'name="station" value="上游站"' in pending
    assert 'name="station" value="东片站"' in pending


def test_duplicate_submit_and_two_ticket_rules_per_station(env):
    db, logic, main, client = env
    upload(client, CSV_A, "上游站")
    upload(client, CSV_B, "东片站")

    ta = claim_token(client, "X1", "ann1", "上游站")
    submit(client, ta, "ann1", "病害")
    assert "重复提交" in submit(client, ta, "ann1", "病害").text
    # 上游站 X1 已被 ann1 标记，ann2 仍可领取；ann3 在两席未满时也能领
    tb = claim_token(client, "X1", "ann1", "东片站")
    submit(client, tb, "ann1", "虫害")
    # 每站各自独立计票
    assert len(labels_of(db, "上游站", "X1")) == 1
    assert len(labels_of(db, "东片站", "X1")) == 1
