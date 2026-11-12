# Scan Time Normalizer — 航空货运扫描时间归一化 API

将承运商导出的**本地扫描时间**按 IANA 时区规则归一化为 **UTC**，并在夏令时回拨（本地时间重复）
/ 拨快（本地时间不存在）场景下保持转运顺序语义正确：

- 每条扫描记录的本地时间对应 **0、1 或 2 个合法 UTC 候选**；
- 航段/转运衔接约束两条记录之间的 UTC 耗时落在闭区间 `[min_seconds, max_seconds]`，
  衔接可共用记录，且**全部衔接必须同时满足**；
- 服务对每条记录返回**能出现在至少一套全局可行解释中的全部候选**：
  - 仅剩一个时标 → `determined`（确定），直接给出 `utc`；
  - 仍有多个 → `needs_review`（待核对），返回全部候选，**不擅自选取**；
- 任一记录无合法候选，或衔接整体无解 → **HTTP 409** 并明确原因，**不返回部分转换**。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务启动后：

- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
  —— 内置三个可一键试用的示例（夏令时回拨消歧 / 不存在本地时间 409 / 衔接冲突 409）；
- 健康检查：`GET http://localhost:8000/healthz`；
- 归一化接口：`POST http://localhost:8000/normalize`。

## 接口说明

### `POST /normalize`

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

## 实现原理

1. **候选生成**（`app/solver.py: utc_candidates`）：对本地时间分别以 `fold=0/1` 附着时区后转 UTC，
   再回读校验——回拨重复得到 2 个候选，拨快跳空得到 0 个，常规时间 1 个。
2. **全局可行性 = 2-SAT**：每条含 2 个候选的记录对应一个布尔变量；每条衔接把"越界的候选组合"
   翻译成子句（禁止该组合）。2-SAT 可用蕴含图 + 强连通分量在线性时间内判定。
3. **逐候选过滤**：对每个候选施加单元子句假设后重判可满足性，保留下来的即为
   "能出现在至少一套全局可行解释中"的候选——精确求解，非启发式。
4. **冲突定位**：整体无解时，对衔接做贪心删除得到不可约冲突子集，作为 409 的明确原因。

规模上限（200 记录 / 400 衔接）下，最多约 400 次小规模 2-SAT 判定，响应为毫秒级。

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload          # 本地起服务
python -m pytest tests/ -q             # 19 个用例：消歧/传播/冲突/校验/上限
```

## 目录结构

```
app/
  main.py      # FastAPI 入口、路由、409 错误映射、Swagger 示例
  schemas.py   # 请求/响应模型与输入校验（时区、精度、引用完整性、上限）
  solver.py    # UTC 候选生成 + 2-SAT 全局可行性 + 不可约冲突子集
tests/test_api.py
Dockerfile / docker-compose.yml / requirements.txt
```
