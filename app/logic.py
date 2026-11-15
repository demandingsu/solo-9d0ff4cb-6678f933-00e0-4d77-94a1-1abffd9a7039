"""核心业务规则：领取、提交、上传改写、列表查询。

所有记录都以 (station, record_no) 联合定位：不同合作站可以使用相同的站内记录号，
描述、领取凭证与标签互不串票。

这些函数都不自己开事务，由调用方（路由或测试）放进 db.write_tx() 里执行，
保证“检查 + 写入”在同一事务内完成，跨进程并发也不会破坏约束。
"""
import csv
import io
import uuid

LABELS = ("病害", "虫害", "无异常")
ANNOTATORS = {"ann1": "标注员甲", "ann2": "标注员乙", "ann3": "标注员丙"}

MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_STATION_LEN = 120

# 上传时未填写来源站的记录归入历史站；旧库升级搬迁的存量数据也归入历史站。
DEFAULT_STATION = "历史站"

# CSV 表头别名：英文 / 中文都接受。
_NO_KEYS = ("record_no", "记录号")
_DESC_KEYS = ("description", "描述")
_STATION_KEYS = ("station", "来源站", "站点")


class LogicError(Exception):
    """业务规则拒绝（过期、改版、重复领取等），消息可直接展示给用户。"""


def normalize_station(raw) -> str:
    """来源站名：去空白；空值归入历史站；拒绝超长或控制字符。"""
    station = (raw or "").strip()
    if not station:
        return DEFAULT_STATION
    if len(station) > MAX_STATION_LEN:
        raise LogicError(f"来源站名称过长（限 {MAX_STATION_LEN} 字）")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in station):
        raise LogicError("来源站名称包含不可见控制字符")
    return station


# ---------------------------------------------------------------- 领取

def claim_record(
    conn, station: str, record_no: str, annotator: str, now: float, ttl: int
) -> dict:
    """领取一条 (来源站, 记录号) 记录，返回新凭证。

    - 已标过当前版本 / 记录已集齐两票 / 两席被他人有效领取占满 -> 拒绝；
    - 重复领取（含过期后重领）会轮换 token，旧页面凭证立即失效；
    - 凭证绑定来源站，不能用于另一来源站的同号记录。
    """
    station = normalize_station(station)
    record_no = (record_no or "").strip()
    if not record_no:
        raise LogicError("缺少记录号")
    cur = conn.execute(
        "SELECT * FROM records_current WHERE station=? AND record_no=?",
        (station, record_no),
    ).fetchone()
    if cur is None:
        raise LogicError("记录不存在，可能尚未上传")
    version = cur["version"]

    if conn.execute(
        "SELECT 1 FROM labels WHERE station=? AND record_no=? AND version=? AND annotator=?",
        (station, record_no, version, annotator),
    ).fetchone():
        raise LogicError("您已标注过该记录的当前版本")

    votes = conn.execute(
        "SELECT COUNT(*) AS c FROM labels WHERE station=? AND record_no=? AND version=?",
        (station, record_no, version),
    ).fetchone()["c"]
    if votes >= 2:
        raise LogicError("该记录已完成双人标注")

    # 他人有效领取占位：已提交者的领取视为已消费，不再占位
    others_active = conn.execute(
        """
        SELECT COUNT(*) AS c FROM claims cl
        WHERE cl.station=? AND cl.record_no=? AND cl.version=? AND cl.annotator<>?
          AND cl.expires_at>?
          AND NOT EXISTS (SELECT 1 FROM labels l
                          WHERE l.station=cl.station AND l.record_no=cl.record_no
                            AND l.version=cl.version AND l.annotator=cl.annotator)
        """,
        (station, record_no, version, annotator, now),
    ).fetchone()["c"]
    if votes + others_active >= 2:
        raise LogicError("该记录正在被其他标注员处理，请稍后再试")

    token = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO claims(token, station, record_no, version, annotator, expires_at, created_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(station, record_no, version, annotator)
        DO UPDATE SET token=excluded.token,
                      expires_at=excluded.expires_at,
                      created_at=excluded.created_at
        """,
        (token, station, record_no, version, annotator, now + ttl, now),
    )
    return {
        "token": token,
        "station": station,
        "record_no": record_no,
        "version": version,
        "description": cur["description"],
        "expires_at": now + ttl,
    }


# ---------------------------------------------------------------- 提交

def submit_label(conn, token: str, annotator: str, label: str, now: float) -> dict:
    """凭领取 token 提交标签。

    凭证自带来源站与记录号，提交只会落在这条记录上，绝不会写到另一来源站的同号记录。
    拒绝顺序：凭证无效（含重领/改写后旧页） -> 版本不符 -> 已过期；
    同人重复提交不报错也不加票。
    """
    if label not in LABELS:
        raise LogicError("非法标签")

    cl = conn.execute("SELECT * FROM claims WHERE token=?", (token,)).fetchone()
    if cl is None or cl["annotator"] != annotator:
        raise LogicError(
            "领取凭证无效：该记录可能已被改写、被重新领取，或页面已过期。"
            "请回到待标列表重新领取。"
        )

    station, record_no, version = cl["station"], cl["record_no"], cl["version"]
    cur = conn.execute(
        "SELECT * FROM records_current WHERE station=? AND record_no=?",
        (station, record_no),
    ).fetchone()
    if cur is None or cur["version"] != version:
        raise LogicError("该记录描述已被改写，旧版本页面提交被拒绝，请重新领取新版本")

    if now > cl["expires_at"]:
        raise LogicError("领取已过期（超过 60 秒有效期），本次提交被拒绝，请重新领取")

    if conn.execute(
        "SELECT 1 FROM labels WHERE station=? AND record_no=? AND version=? AND annotator=?",
        (station, record_no, version, annotator),
    ).fetchone():
        return {
            "duplicated": True,
            "station": station,
            "record_no": record_no,
            "version": version,
        }

    votes = conn.execute(
        "SELECT COUNT(*) AS c FROM labels WHERE station=? AND record_no=? AND version=?",
        (station, record_no, version),
    ).fetchone()["c"]
    if votes >= 2:
        raise LogicError("该记录已集齐两票，提交被拒绝")

    conn.execute(
        "INSERT INTO labels(station, record_no, version, annotator, label, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (station, record_no, version, annotator, label, now),
    )
    return {
        "duplicated": False,
        "station": station,
        "record_no": record_no,
        "version": version,
    }


# ---------------------------------------------------------------- 上传 / 改写

def upsert_rows(conn, rows, now: float) -> dict:
    """按 (来源站, 记录号) upsert：新增 / 改写 / 未变。

    - 同站同号描述有改动：版本 +1，并在同事务内清除该站该号的旧领取与旧标签；
    - 同站同号描述未变：保留全部领取与标注进度；
    - 不同来源站的同号记录彼此独立，本函数绝不会清掉另一站的领取或标签。
    """
    added = updated = kept = 0
    stations = set()
    for station, record_no, desc in rows:
        station = normalize_station(station)
        stations.add(station)
        cur = conn.execute(
            "SELECT * FROM records_current WHERE station=? AND record_no=?",
            (station, record_no),
        ).fetchone()
        if cur is None:
            conn.execute(
                "INSERT INTO records_current(station, record_no, version, description, updated_at) "
                "VALUES (?,?,?,?,?)",
                (station, record_no, 1, desc, now),
            )
            added += 1
        elif cur["description"] == desc:
            kept += 1  # 未改写：领取与标签全部保留
        else:
            # 改写：同事务清除本站本号的旧领取与旧标签，迟到的旧版提交随后会被版本校验挡下
            conn.execute(
                "DELETE FROM claims WHERE station=? AND record_no=?", (station, record_no)
            )
            conn.execute(
                "DELETE FROM labels WHERE station=? AND record_no=?", (station, record_no)
            )
            conn.execute(
                "UPDATE records_current SET version=?, description=?, updated_at=? "
                "WHERE station=? AND record_no=?",
                (cur["version"] + 1, desc, now, station, record_no),
            )
            updated += 1
    return {"added": added, "updated": updated, "kept": kept, "stations": sorted(stations)}


def parse_csv_bytes(raw: bytes, station: str = ""):
    """解析上传 CSV，返回 [(来源站, 记录号, 描述)]。

    station 参数是上传页填写的来源站，作为默认值：
    - CSV 有 station / 来源站 / 站点 列时按行读取，行内为空则回落到该默认值；
    - CSV 没有来源站列时，整份文件都落到该默认值；
    - 默认值也为空时归入历史站。
    """
    default_station = normalize_station(station)
    if not raw:
        raise LogicError("文件为空")
    if len(raw) > MAX_CSV_BYTES:
        raise LogicError("CSV 文件过大（限 5MB）")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LogicError("CSV 必须是 UTF-8 编码")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise LogicError("CSV 缺少表头")
    reader.fieldnames = [h.strip() for h in reader.fieldnames if h and h.strip()]
    headers = set(reader.fieldnames)
    no_key = next((k for k in _NO_KEYS if k in headers), None)
    desc_key = next((k for k in _DESC_KEYS if k in headers), None)
    station_key = next((k for k in _STATION_KEYS if k in headers), None)
    if not no_key or not desc_key:
        raise LogicError(
            "表头需包含 record_no,description（或 记录号,描述），"
            "可选 station（或 来源站）列"
        )
    rows = []
    for i, r in enumerate(reader, start=2):
        no = (r.get(no_key) or "").strip()
        desc = (r.get(desc_key) or "").strip()
        row_station = (r.get(station_key) or "").strip() if station_key else ""
        if not no and not desc and not row_station:
            continue
        if not no or not desc:
            raise LogicError(f"第 {i} 行缺少记录号或描述")
        try:
            st = normalize_station(row_station or default_station)
        except LogicError as e:
            raise LogicError(f"第 {i} 行来源站无效：{e}")
        rows.append((st, no, desc))
    if not rows:
        raise LogicError("CSV 中没有有效数据行")
    return rows


# ---------------------------------------------------------------- 列表查询

def list_pending(conn, annotator: str, now: float):
    """当前版本票数 < 2 的记录，附带当前标注员的领取/标注状态。

    注意：不返回任何他人标签内容（双盲），只返回票数。
    """
    rows = conn.execute(
        """
        SELECT c.station, c.record_no, c.version, c.description,
          (SELECT COUNT(*) FROM labels l
            WHERE l.station=c.station AND l.record_no=c.record_no
              AND l.version=c.version) AS votes,
          EXISTS(SELECT 1 FROM labels l
            WHERE l.station=c.station AND l.record_no=c.record_no
              AND l.version=c.version AND l.annotator=:a) AS i_labeled,
          (SELECT COUNT(*) FROM claims cl
            WHERE cl.station=c.station AND cl.record_no=c.record_no
              AND cl.version=c.version AND cl.annotator<>:a AND cl.expires_at>:now
              AND NOT EXISTS (SELECT 1 FROM labels l2
                              WHERE l2.station=cl.station AND l2.record_no=cl.record_no
                                AND l2.version=cl.version AND l2.annotator=cl.annotator)) AS others_active,
          (SELECT cl.token FROM claims cl
            WHERE cl.station=c.station AND cl.record_no=c.record_no
              AND cl.version=c.version AND cl.annotator=:a AND cl.expires_at>:now) AS my_token,
          (SELECT cl.expires_at FROM claims cl
            WHERE cl.station=c.station AND cl.record_no=c.record_no
              AND cl.version=c.version AND cl.annotator=:a AND cl.expires_at>:now) AS my_expires
        FROM records_current c
        WHERE (SELECT COUNT(*) FROM labels l
                WHERE l.station=c.station AND l.record_no=c.record_no
                  AND l.version=c.version) < 2
        ORDER BY c.station, c.record_no
        """,
        {"a": annotator, "now": now},
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["remaining"] = max(0, int(d["my_expires"] - now)) if d["my_expires"] else 0
        if d["i_labeled"]:
            d["state"] = "done_by_me"
        elif d["my_token"]:
            d["state"] = "claimed_by_me"
        elif d["votes"] + d["others_active"] >= 2:
            d["state"] = "busy"
        else:
            d["state"] = "open"
        out.append(d)
    return out


def list_disagreed(conn):
    """两票不一致的当前版本记录（含双方标签，用于质检复核）。"""
    recs = conn.execute(
        """
        SELECT c.station, c.record_no, c.version, c.description
        FROM records_current c
        WHERE (SELECT COUNT(*) FROM labels l
                WHERE l.station=c.station AND l.record_no=c.record_no
                  AND l.version=c.version) = 2
          AND (SELECT COUNT(DISTINCT l.label) FROM labels l
                WHERE l.station=c.station AND l.record_no=c.record_no
                  AND l.version=c.version) = 2
        ORDER BY c.station, c.record_no
        """
    ).fetchall()
    out = []
    for r in recs:
        labs = conn.execute(
            "SELECT annotator, label FROM labels "
            "WHERE station=? AND record_no=? AND version=? ORDER BY annotator",
            (r["station"], r["record_no"], r["version"]),
        ).fetchall()
        out.append(
            {
                "station": r["station"],
                "record_no": r["record_no"],
                "version": r["version"],
                "description": r["description"],
                "labels": [
                    {"name": ANNOTATORS.get(l["annotator"], l["annotator"]), "label": l["label"]}
                    for l in labs
                ],
            }
        )
    return out


def list_usable(conn):
    """两票一致的当前版本记录，即可下载的训练数据。"""
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.station, c.record_no, c.version, c.description,
              (SELECT l.label FROM labels l
                WHERE l.station=c.station AND l.record_no=c.record_no
                  AND l.version=c.version LIMIT 1) AS label
            FROM records_current c
            WHERE (SELECT COUNT(*) FROM labels l
                    WHERE l.station=c.station AND l.record_no=c.record_no
                      AND l.version=c.version) = 2
              AND (SELECT COUNT(DISTINCT l.label) FROM labels l
                    WHERE l.station=c.station AND l.record_no=c.record_no
                      AND l.version=c.version) = 1
            ORDER BY c.station, c.record_no
            """
        ).fetchall()
    ]


def stats(conn):
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN votes < 2 THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN votes = 2 AND distincts = 2 THEN 1 ELSE 0 END) AS disagreed,
          SUM(CASE WHEN votes = 2 AND distincts = 1 THEN 1 ELSE 0 END) AS usable
        FROM (
          SELECT c.station, c.record_no,
            (SELECT COUNT(*) FROM labels l
              WHERE l.station=c.station AND l.record_no=c.record_no
                AND l.version=c.version) AS votes,
            (SELECT COUNT(DISTINCT l.label) FROM labels l
              WHERE l.station=c.station AND l.record_no=c.record_no
                AND l.version=c.version) AS distincts
          FROM records_current c
        )
        """
    ).fetchone()
    return {k: (row[k] or 0) for k in ("pending", "disagreed", "usable")}
