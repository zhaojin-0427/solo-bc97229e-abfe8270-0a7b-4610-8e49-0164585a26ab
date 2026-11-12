# Air Cargo Transfer & Scan Time Service — 航空货运中转与扫描时间服务

本服务包含两部分：

1. **扫描时间归一化**（原有能力）：把承运商导出的本地扫描时间按 IANA 时区规则归一化为 UTC，
   并在夏令时回拨（本地时间重复）/ 拨快（本地时间不存在）场景下保持转运顺序语义正确；
2. **中转补报入仓与机场停留日报**（新增能力）：接收承运商迟到的中转事实、更正和撤回，
   在 SQLite 中原子地维护最新事实与按机场、离开 UTC 日汇总的日报，供下游评估中转效率。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务启动后：

- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
  —— 可直接在页面上提交**中转补报/更正、撤回**并读取**日报**；原有 `/normalize` 示例仍可一键试用；
- 健康检查：`GET http://localhost:8000/healthz`；
- 归一化接口：`POST http://localhost:8000/normalize`；
- 中转入仓接口：`POST http://localhost:8000/transfers`；
- 机场停留日报：`GET http://localhost:8000/reports/daily`。

SQLite 文件由 Docker Compose 的具名卷 `aircargo-data` 持久化到容器内 `/data/aircargo.db`
（可用环境变量 `DATABASE_PATH` 覆盖）。容器重启后数据仍在，重传同一批数据也是安全的。

## 一、扫描时间归一化 `POST /normalize`

**请求体**

| 字段 | 说明 |
| --- | --- |
| `records` | 1–200 条扫描记录：`id`（唯一）、`local_time`（精确到秒的朴素本地时间，不得带偏移）、`timezone`（IANA 时区名） |
| `links` | 0–400 条衔接：`from_id` / `to_id`（须为已声明记录）、`min_seconds` / `max_seconds`（非负整数秒，且 min ≤ max） |

**200 响应**：`records[*].status ∈ {determined, needs_review}`；`determined` 时 `utc` 为唯一可行时标，
`needs_review` 时 `utc = null` 且 `candidates` 列出全部仍可行的 UTC 候选（升序）。

**409 响应**（不返回任何部分转换）：

- `no_legal_candidate` — 本地时间在时区规则下不存在（落入拨快跳空），`invalid_records` 列出涉事记录；
- `no_global_solution` — 衔接互相矛盾、整体无解，`conflicting_links` 给出**不可约冲突子集**
  （再删掉任意一条衔接即可恢复有解），`involved_records` 列出涉事记录。

**422 响应**：请求体不合法（未知时区、重复 ID、引用未声明记录、min > max、超过 200/400 上限等）。

### 演示：夏令时回拨消歧

2024-11-03 美国冬令时回拨，纽约 `01:30` 出现两次（05:30Z / 06:30Z）：

```bash
curl -s -X POST http://localhost:8000/normalize \
  -H 'Content-Type: application/json' \
  -d '{
    "records": [
      {"id": "ARR_JFK", "local_time": "2024-11-03T01:30:00", "timezone": "America/New_York"},
      {"id": "DEP_JFK", "local_time": "2024-11-03T02:45:00", "timezone": "America/New_York"},
      {"id": "ARR_ORD", "local_time": "2024-11-03T04:10:00", "timezone": "America/Chicago"},
      {"id": "DEP_LAX", "local_time": "2024-11-03T03:20:00", "timezone": "America/Los_Angeles"},
      {"id": "XFR_ORD", "local_time": "2024-11-03T01:30:00", "timezone": "America/Chicago"}
    ],
    "links": [
      {"from_id": "ARR_JFK", "to_id": "DEP_JFK", "min_seconds": 3600, "max_seconds": 7200},
      {"from_id": "DEP_JFK", "to_id": "ARR_ORD", "min_seconds": 7200, "max_seconds": 10800},
      {"from_id": "ARR_ORD", "to_id": "DEP_LAX", "min_seconds": 1800, "max_seconds": 7200},
      {"from_id": "ARR_JFK", "to_id": "XFR_ORD", "min_seconds": 0, "max_seconds": 86400}
    ]
  }'
```

实际返回（节选）：

```json
{
  "status": "ok",
  "summary": {"total": 5, "determined": 4, "needs_review": 1},
  "records": [
    {"id": "ARR_JFK", "status": "determined", "utc": "2024-11-03T06:30:00Z", "candidates": ["2024-11-03T06:30:00Z"], "...": "..."},
    {"id": "XFR_ORD", "status": "needs_review", "utc": null, "candidates": ["2024-11-03T06:30:00Z", "2024-11-03T07:30:00Z"], "...": "..."}
  ]
}
```

`ARR_JFK` 的两个回拨候选中，`05:30Z` 会使到 `DEP_JFK`（07:45Z）的地面耗时达 2h15m、
超出 1–2h 窗口，被全局约束排除，故唯一确定为 `06:30Z`；`XFR_ORD` 的两个候选都满足全部
衔接，保持 `needs_review`，交由人工核对。

## 二、中转补报入仓 `POST /transfers`

别名（便于既有系统接入）：`POST /ingest`、`POST /ingest/batch`、`POST /batch`、
`POST /transfers/ingest`、`POST /transfers/batch`。

请求体是一个批次，也可以直接提交 JSON 数组；兼容 `records` / `items` / `entries` /
`transfers` / `reports` / `late_reports` / `corrections` / `retractions` 等外层键。
**每批 1–100 条，批内 `id` 必须唯一**，否则返回 422。

### 补报 / 更正（事实条目）

| 字段 | 说明 |
| --- | --- |
| `id` | 中转 ID，非空字符串 |
| `revision` | **正整数**修订号；高修订替换低修订 |
| `airport` | 机场代码/名称 |
| `arrival_utc` | 到达时刻，**UTC 整秒**，例如 `2024-11-03T06:30:00Z` |
| `departure_utc` | 离开时刻，**UTC 整秒**，不得早于 `arrival_utc` |

时间应取自 `/normalize` 已确定（`determined`）的 UTC 结果。带非零偏移、朴素时间、
微秒精度或 `departure_utc < arrival_utc` 的条目会被 422 拒绝。

### 撤回（只含 ID 和修订号）

```json
{"id": "XFR_1002", "revision": 2}
```

也可显式写 `{"id": "...", "revision": 2, "retracted": true}` 或
`{"id": "...", "revision": 2, "action": "retract"}`；撤回条目不得携带机场/时间字段。

### 修订与幂等规则

- **高修订替换低修订**；低修订到达时返回 `ignored`，不会改动事实或日报。
- **同 ID、同修订、同内容**：返回 `deduplicated`，不重复计入日报；进程重启后重传同一批仍安全。
- **同 ID、同修订、异内容**：整批拒绝，HTTP **409**，事务回滚，本批任何条目都不生效。
- 撤回会在库中保留**撤回修订墓碑**。撤回后迟到的旧修订一律 `ignored`，不会复活记录；
  只有真正更高的修订才可重新补报该中转。
- 更正机场或跨 UTC 日改时：先扣除旧 `(airport, 离开UTC日)` 组的次数与停留秒数，
  再计入新组，且只更新本批实际影响的机场日。
- 停留秒数 = `departure_utc - arrival_utc`（整数秒）。

### 成功响应（200）

```json
{
  "status": "ok",
  "summary": {
    "received": 2,
    "applied": 1,
    "deduplicated": 1,
    "duplicates": 1,
    "ignored": 0,
    "retracted": 0
  },
  "results": [
    {"id": "XFR_1001", "revision": 1, "action": "report", "outcome": "applied"},
    {"id": "XFR_1002", "revision": 1, "action": "report", "outcome": "deduplicated"}
  ]
}
```

`outcome ∈ {applied, deduplicated, ignored}`；`action ∈ {report, retract}`。

### 冲突响应（409，整批不生效）

```json
{
  "status": "rejected",
  "error": "revision_conflict",
  "message": "The batch was rejected in full ...",
  "conflicts": [
    {
      "id": "XFR_1001",
      "revision": 1,
      "stored_revision": 1,
      "stored_status": "active",
      "reason": "same id and revision but different content",
      "incoming": {"kind": "report", "revision": 1, "airport": "LAX", "...": "..."},
      "stored": {"kind": "report", "revision": 1, "airport": "JFK", "...": "..."}
    }
  ]
}
```

### 原子性与并发

- 每批事实与日报更新在**同一个 SQLite `BEGIN IMMEDIATE` 事务**中提交或回滚；
- 使用 WAL + `synchronous=FULL`：进程中断、容器重启或提交失败时，事实与日报一起保持旧状态；
- 并发提交由 SQLite 写锁串行化（跨进程同样有效），不会丢失更新或重复计数；
- 重传安全：状态由持久化的最新修订与内容指纹判定，而非依赖请求批次号。

### 演示

```bash
# 补报两条中转
curl -s -X POST http://localhost:8000/transfers \
  -H 'Content-Type: application/json' \
  -d '{
    "records": [
      {"id": "XFR_1001", "revision": 1, "airport": "JFK",
       "arrival_utc": "2024-11-03T06:30:00Z", "departure_utc": "2024-11-03T08:00:00Z"},
      {"id": "XFR_1002", "revision": 1, "airport": "ORD",
       "arrival_utc": "2024-11-03T10:10:00Z", "departure_utc": "2024-11-03T11:40:00Z"}
    ]
  }'

# 更正 XFR_1001 的机场并跨日改时（修订号 2）
curl -s -X POST http://localhost:8000/transfers \
  -H 'Content-Type: application/json' \
  -d '{"records": [{"id": "XFR_1001", "revision": 2, "airport": "LHR",
       "arrival_utc": "2024-11-03T06:30:00Z", "departure_utc": "2024-11-04T02:00:00Z"}]}'

# 撤回 XFR_1002（只含 ID 和修订号）
curl -s -X POST http://localhost:8000/transfers \
  -H 'Content-Type: application/json' \
  -d '{"records": [{"id": "XFR_1002", "revision": 2}]}'

# 读取日报
curl -s http://localhost:8000/reports/daily
```

## 三、机场停留日报 `GET /reports/daily`

别名：`GET /reports`、`GET /report/daily`、`GET /daily-report`、`GET /daily`、
`GET /api/reports/daily` 等。

按 **`airport` + 离开时刻的 UTC 日期**汇总当前有效中转：

| 字段 | 说明 |
| --- | --- |
| `airport` | 机场 |
| `departure_date` | 离开时刻的 UTC 日期（`YYYY-MM-DD`） |
| `transfer_count` | 该组当前有效中转次数 |
| `dwell_seconds` | 该组停留总秒数（`departure - arrival` 之和） |

可选查询参数：`airport`（精确匹配）、`date_from` / `date_to`（含端点）、
`date`（精确某天）；也接受 `start_date` / `end_date` / `from` / `to` / `departure_date`。

响应：

```json
{
  "status": "ok",
  "rows": [
    {"airport": "JFK", "departure_date": "2024-11-03", "transfer_count": 1, "dwell_seconds": 5400}
  ],
  "totals": {"transfer_count": 1, "dwell_seconds": 5400}
}
```

**读取一致性**：日报读取在一个 SQLite 读事务中完成（WAL 快照），因此一次读取绝不会
混合某一批提交前与提交后的数据；`totals` 由同一快照的行计算得出。

## 四、本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload          # 本地起服务
python -m pytest tests/ -q             # 37 个用例：归一化、入仓、幂等、撤回、原子性、并发、日报
```

本地默认数据库为 `./data/aircargo.db`，可用 `DATABASE_PATH=/path/to.db` 指定。

## 目录结构

```
app/
  main.py      # FastAPI 入口、路由、409 错误映射、Swagger 示例
  schemas.py   # 归一化 + 入仓/日报请求响应模型与输入校验
  solver.py    # UTC 候选生成 + 2-SAT 全局可行性 + 不可约冲突子集
  db.py        # SQLite 原子入仓、修订/撤回墓碑、日报增量维护与快照读取
tests/
  test_api.py       # 归一化接口测试
  test_transfers.py # 入仓、更正、撤回、幂等、原子性、并发与日报测试
Dockerfile / docker-compose.yml / requirements.txt
```
