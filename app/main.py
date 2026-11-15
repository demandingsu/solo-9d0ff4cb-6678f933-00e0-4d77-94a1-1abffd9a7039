"""FastAPI 路由层：页面渲染 + 表单处理。

无会话状态（标注员身份在 cookie、领取凭证在 URL/表单里），
因此两个进程前面挂任意负载均衡都能正确工作，状态全部落在共享 SQLite。
"""
import csv
import io
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, logic

BASE_DIR = Path(__file__).resolve().parent
CLAIM_TTL = int(os.environ.get("CLAIM_TTL_SECONDS", "60"))
INSTANCE = os.environ.get("INSTANCE_NAME") or os.uname().nodename
SEED_CSV = os.environ.get("SEED_CSV", str(BASE_DIR.parent / "sample_data.csv"))

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 空库时导入内置样例，便于开箱演示；两个进程同时启动也只会有一个真正写入
    if os.environ.get("SEED_ON_EMPTY", "1") == "1" and os.path.exists(SEED_CSV):
        with open(SEED_CSV, "rb") as f:
            raw = f.read()
        with db.write_tx() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM records_current").fetchone()["c"]
            if n == 0:
                logic.upsert_rows(conn, logic.parse_csv_bytes(raw), time.time())
    yield


app = FastAPI(title="农技语料双人盲标", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- 工具

def current_annotator(request: Request) -> str:
    a = request.cookies.get("annotator", "ann1")
    return a if a in logic.ANNOTATORS else "ann1"


def ctx(request: Request, **kw):
    base = {
        "request": request,
        "annotator": current_annotator(request),
        "annotator_name": logic.ANNOTATORS[current_annotator(request)],
        "annotators": logic.ANNOTATORS,
        "instance": INSTANCE,
        "ttl": CLAIM_TTL,
        "msg": request.query_params.get("msg"),
        "kind": request.query_params.get("kind", "ok"),
    }
    base.update(kw)
    return base


def back(msg: str, kind: str = "err", where: str = "/pending") -> RedirectResponse:
    return RedirectResponse(f"{where}?kind={kind}&msg={quote(str(msg))}", status_code=303)


def result_page(request: Request, ok: bool, title: str, message: str, status: int = 200):
    return templates.TemplateResponse(
        "result.html",
        ctx(request, ok=ok, title=title, message=message),
        status_code=status,
    )


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/pending", status_code=303)


@app.get("/pending", response_class=HTMLResponse)
def pending(request: Request):
    a = current_annotator(request)
    conn = db.connect()
    try:
        rows = logic.list_pending(conn, a, time.time())
        counts = logic.stats(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "pending.html", ctx(request, rows=rows, counts=counts, active="pending")
    )


@app.get("/disagreed", response_class=HTMLResponse)
def disagreed(request: Request):
    conn = db.connect()
    try:
        rows = logic.list_disagreed(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "disagreed.html", ctx(request, rows=rows, active="disagreed")
    )


@app.get("/usable", response_class=HTMLResponse)
def usable(request: Request):
    conn = db.connect()
    try:
        rows = logic.list_usable(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "usable.html", ctx(request, rows=rows, active="usable")
    )


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse("upload.html", ctx(request, active="upload"))


@app.get("/work/{token}", response_class=HTMLResponse)
def work(request: Request, token: str):
    a = current_annotator(request)
    conn = db.connect()
    try:
        cl = conn.execute("SELECT * FROM claims WHERE token=?", (token,)).fetchone()
        if cl is None or cl["annotator"] != a:
            return result_page(
                request, False, "领取凭证无效",
                "该领取不存在、已被重新领取，或记录已被改写。请回到待标列表重新领取。",
                status=400,
            )
        cur = conn.execute(
            "SELECT * FROM records_current WHERE record_no=?", (cl["record_no"],)
        ).fetchone()
        if cur is None or cur["version"] != cl["version"]:
            return result_page(
                request, False, "记录已改版",
                "该记录描述已被改写，请回到待标列表重新领取新版本。",
                status=400,
            )
        remaining = max(0, int(cl["expires_at"] - time.time()))
        return templates.TemplateResponse(
            "work.html",
            ctx(
                request,
                claim=dict(cl),
                record=dict(cur),
                remaining=remaining,
                labels=logic.LABELS,
                active="pending",
            ),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------- 动作

@app.post("/claim")
def claim(request: Request, record_no: str = Form(...)):
    a = current_annotator(request)
    try:
        with db.write_tx() as conn:
            info = logic.claim_record(conn, record_no, a, time.time(), CLAIM_TTL)
    except logic.LogicError as e:
        return back(str(e))
    return RedirectResponse(f"/work/{info['token']}", status_code=303)


@app.post("/submit", response_class=HTMLResponse)
def submit(request: Request, token: str = Form(...), label: str = Form(...)):
    a = current_annotator(request)
    try:
        with db.write_tx() as conn:
            res = logic.submit_label(conn, token, a, label, time.time())
    except logic.LogicError as e:
        return result_page(request, False, "提交被拒绝", str(e), status=400)
    if res["duplicated"]:
        return result_page(
            request, True, "重复提交已忽略",
            "您之前已提交过该记录（当前版本）的标注，本次提交未增加票数。",
        )
    return result_page(request, True, "提交成功", f"已记录您的标注：{label}")


@app.post("/upload", response_class=HTMLResponse)
async def upload_do(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    try:
        rows = logic.parse_csv_bytes(raw)
        with db.write_tx() as conn:
            s = logic.upsert_rows(conn, rows, time.time())
    except logic.LogicError as e:
        return templates.TemplateResponse(
            "upload.html", ctx(request, active="upload", error=str(e)), status_code=400
        )
    return templates.TemplateResponse(
        "upload.html", ctx(request, active="upload", stats=s)
    )


@app.get("/download")
def download():
    conn = db.connect()
    try:
        rows = logic.list_usable(conn)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["record_no", "description", "label"])
    for r in rows:
        w.writerow([r["record_no"], r["description"], r["label"]])
    data = "﻿" + buf.getvalue()  # BOM，Excel 直接打开不乱码
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="train_labels.csv"'},
    )


@app.get("/switch")
def switch(to: str = "ann1", next: str = "/pending"):
    if to not in logic.ANNOTATORS:
        to = "ann1"
    if not next.startswith("/"):
        next = "/pending"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie("annotator", to)
    return resp


@app.get("/sample.csv")
def sample():
    return FileResponse(SEED_CSV, media_type="text/csv; charset=utf-8", filename="sample.csv")


@app.get("/health")
def health():
    return {"ok": True, "instance": INSTANCE}
