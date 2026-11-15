# 农技语料双人盲标系统

FastAPI + SQLite 的田间病虫害描述双人盲标网页应用。每条记录由**两名不同标注员**
独立标为 **病害 / 虫害 / 无异常**，两票一致才进入训练集下载。

## 功能规则

| 规则 | 实现 |
| --- | --- |
| 上传语料 | CSV（UTF-8），表头 `record_no,description`（或 `记录号,描述`） |
| 三名标注员 | 内置 标注员甲/乙/丙，页面右上角一键切换（cookie 记录身份） |
| 双盲 | 提交前服务端不返回他人标签：待标页只显示票数，标注页只有描述 |
| 领取时效 | 每次领取有效 **60 秒**（`CLAIM_TTL_SECONDS` 可调），过期可重新领取 |
| 重领作废旧页 | 重新领取会轮换凭证 token，**即使同一人**，旧页面提交一律拒绝 |
| 重复提交 | 同人同版本重复提交不加票（数据库主键 + 事务内判重） |
| 描述改写 | 同号描述有改动 → 版本 +1，**同事务清除旧领取与旧标签**，迟到提交无法写入新版 |
| 未变重传 | 同号描述未变 → 保留全部领取与标注进度 |
| 训练集下载 | 仅含**当前版本两票一致**的记录：`record_no,description,label` |
| 双进程共享 | 两个应用进程挂载同一 SQLite 文件（WAL），并发领取/提交/改写不产生重复票、不放行旧版 |
| 重启保留 | 标签与领取期限（绝对时间戳）全部落库，容器重启不丢 |

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8080
```

compose 启动三个容器：

- `app1` / `app2`：两个相同的应用进程，挂载同一命名卷 `dbdata`，共享 `/data/annotate.db`；
- `gateway`：nginx 轮询反向代理（`:8080`）。刷新页面，页脚的"服务进程"会在
  app1 / app2 间切换——领取在 app1、提交落在 app2 也照常工作。

首次启动空库时自动导入仓库内置样例 `sample_data.csv`（8 条田间描述）。

## 浏览器演示

### 1. 双人盲标 → 下载训练集
1. 右上角切换到 **标注员甲**，待标页点「领取并标注」，60 秒倒计时内点「病害」；
2. 切换到 **标注员乙**，领取同一条（页面看不到甲的标签），也点「病害」；
3. 「可用」页出现该记录，「下载训练集」得到 `train_labels.csv`；
   若两人标签不同，记录进入「分歧」页且不会下载。

### 2. 超时重领
1. 任一人领取一条记录，**不提交**，等倒计时归零（页面按钮自动禁用）；
2. 强行提交 → 「领取已过期，本次提交被拒绝」；
3. 回到待标页重新领取（拿到新凭证），**回到旧页面提交 → 仍被拒绝**（凭证无效）；
4. 用新页面提交 → 成功；再点一次 → 「重复提交已忽略」，票数不增加。

> 想缩短等待，可在 `docker-compose.yml` 把 `CLAIM_TTL_SECONDS` 改成 `"10"` 再启动。

### 3. 改写后旧页提交
1. 标注员甲领取记录 `T-0005`（停留在标注页，先不提交）；
2. 把 `sample_data.csv` 里 `T-0005` 的描述改几个字，在「上传 CSV」页上传
   → 提示"改写 1 条（旧领取与标签已清除）"；
3. 回到甲的旧标注页提交 → 「领取凭证无效 / 提交被拒绝」，新版记录不会被旧页写入；
4. 原样再传一次同一文件 → 提示"未变保留"，已有进度原样保留。

### 4. 双进程与重启
- 刷新页面看页脚进程名在 app1/app2 间切换，所有状态都在共享库中；
- `docker compose restart app1 app2` 后：已标记录仍在，未过期的领取仍可提交
  （期限按绝对时间计算，重启不重置）。

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000     # http://localhost:8000
.venv/bin/pytest tests/ -q                              # 10 个用例：规则 + 并发
```

双进程本地验证（共享同一 DB 文件）：

```bash
DB_PATH=/tmp/shared.db INSTANCE_NAME=app1 .venv/bin/uvicorn app.main:app --port 8001 &
DB_PATH=/tmp/shared.db INSTANCE_NAME=app2 .venv/bin/uvicorn app.main:app --port 8002 &
```

## 架构与并发设计

```
app/                 应用包
  main.py            FastAPI 路由（页面渲染 + 表单动作）
  logic.py           业务规则：领取 / 提交 / 上传改写 / 列表查询
  db.py              SQLite 连接（WAL + busy_timeout）与 BEGIN IMMEDIATE 写事务
  templates/ static/ 服务端渲染页面
sample_data.csv      内置样例（空库自动导入）
docker-compose.yml   app1 + app2（共享卷）+ nginx 网关
tests/               规则测试与多线程并发测试
```

- **一致性**：所有"检查 + 写入"都在 `BEGIN IMMEDIATE` 事务内完成，SQLite 写锁
  使两个进程的并发操作串行化；`labels` 主键 `(record_no, version, annotator)`
  兜底重复票，提交前再校验"当前版本票数 < 2"，第三人无法加票。
- **领取**：`claims` 表 `UNIQUE(record_no, version, annotator)`，重复领取
  `UPSERT` 轮换 token——旧页凭证随之失效；提交时校验 token → 版本 → 过期时间。
- **改写**：清领取、清标签、版本 +1 在同一事务；旧 token 随领取一起被删，
  即使并发也只会出现"提交先落库随后被清除"或"提交被挡下"，绝不写进新版。
- **盲标**：标注页/待标页不输出任何他人标签，仅显示票数；分歧页只在两票齐后可见。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/annotate.db`（容器内 `/data/annotate.db`） | SQLite 文件路径 |
| `CLAIM_TTL_SECONDS` | `60` | 领取有效期（秒） |
| `SEED_ON_EMPTY` | `1` | 空库时是否导入样例 |
| `SEED_CSV` | `sample_data.csv` | 样例 CSV 路径 |
| `INSTANCE_NAME` | 主机名 | 页脚显示的进程名 |

## CSV 格式

```csv
record_no,description
T-0001,玉米叶片出现黄褐色梭形条斑，病斑边缘有黄色晕圈，疑似大斑病
```

- 新记录号 → 新增（v1）；同号描述改变 → 版本 +1 并清旧数据；同号描述不变 → 跳过。
- 同一文件内重复记录号按顺序处理（后者视为改写）。
- 下载的训练集带 UTF-8 BOM，Excel 直接打开不乱码。
