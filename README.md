# 演出录音播放版税结算服务

Python + FastAPI + PostgreSQL 实现的播放版税结算服务，支持：

- 作品与权利人登记，分成协议登记（适用起止时间、多名权利人及各自比例）；
  - 同一作品协议区间不得重叠（"每作品事务咨询锁 + 区间重叠检测"强制，**无需任何 contrib 扩展**）；
  - 每笔协议比例须合计 **100%**（应用层 + 数据库延迟约束触发器双重校验）。
- 播放记录导入：调用方唯一编号、作品、发生时间、非负整数金额（最小货币单位）。
  - 同编号同内容 **幂等**；同编号异内容 **拒绝（409）**；
  - 更正必须引用原编号（`corrects_id`）并使用新编号；**分叉、循环更正拒绝**；
    原记录留痕，仅更正链 **链尾** 参与核算。
- 按播放 **发生时间** 匹配协议；缺协议的播放列入 **待处理清单**，不计入应付。
- 逐笔按比例分摊到最小货币单位：先向下取整，余数按 **小数部分降序、权利人编号升序** 补足，逐笔总额严格守恒。
- 按期间出具权利人应付额与待处理清单：
  - 已出具单据 **不可覆盖**；
  - 后补协议或播放更正后，再出具按 **新应付额 − 此前单据累计额** 出具 **追补单**，差额 **可正可负**；
  - 应付无变化不产生新单据；同一期间并发出具经咨询锁串行化，**不会产生重复单据**。
- **逐笔归因证据（财务核对）**：每次出具同步冻结"每位权利人的每笔金额来自哪些播放"：
  - 按 **更正链原始编号** 归集，固化参与核算的 **链尾编号、作品、发生时间、匹配协议、比例与逐笔分摊**；
  - 追补单逐笔记录相对上次出具的差额：`added`（新增）/ `removed`（撤销，**负额逐笔保留不轧差**），
    跨期更正、协议后补造成的撤销与新增均可分辨；
  - 归因记录 **一经冻结不可修改/删除**（触发器强制），历史单据不随日后播放或协议变化；
    待处理播放不计入应付、不产生证据；
  - 按 **单据 + 权利人分页** 查询归因记录与逐权利人合计，
    合计 **恒等于该单据原有明细行**（`reconciles=true`）；
  - 升级前已存在、无逐笔证据的存量单据自动标记 **不可追溯**（`traceable=false`）；
    其追补中无法归因的旧账差额单列为 `legacy`（无播放字段），**绝不用当前播放冒充历史**；
  - 并发出具不会重复生成证据；应付无变化不生成新单据，也不生成归因。
- **按权利人付款核对（财务实付）**：
  - 权利人当前余额 = **全部期间已出具单据明细的正负金额累计 − 已登记付款**；
    试算（preview）不出具、待处理播放不入应付，**均不进余额**；
  - 登记付款须提供权利人编号、外部付款流水号与**正整数分额**，
    仅当金额 **不超过当时的正余额** 才入账，返回付款记录与登记后余额；
  - 同一流水号同内容重试 **幂等返回原记录**（`status=duplicate`，HTTP 200），
    异内容（权利人或金额不同）返回 **409 `payment_ref_conflict`**；
    权利人不存在 404、金额非正整数 422、余额不为正或超额 **409 `payment_exceeds_balance`**，
    拒绝时**不留任何付款**；
  - 跨期更正产生负额追补后 **不撤销历史付款**：余额变负，留待后续正额抵扣，
    **余额不为正时禁止付款**；付款记录一经登记不可修改/删除（触发器强制）；
  - 按权利人查询单据增减、付款流水与余额（`/rightsholders/{code}/ledger`）；
  - 付款与出单/其他付款并发时，以「流水号事务咨询锁 + 权利人行锁（出单侧按编号升序加锁）」
    保证**校验与入账一致、绝不超付、无死锁**。
- **银行回单批次导入与付款核验（财务对账）**：
  - 按批次导入回单：批次编号 + 回单行（回单编号、外部付款流水号、权利人编号、正整数分额）；
    批次同内容重试 **幂等返回原批次**（`status=duplicate`，HTTP 200）；
    批次号或回单编号 **异内容复用**、批内重复流水号、无效字段均 **整批拒绝**，
    不留任何回单；
  - 跨批次同一付款流水号 **仅首次成功导入的回单参与核验**，其余标为 **重复回单**；
  - 有效回单按流水号对照已登记付款，区分 **匹配 / 付款未登记 / 权利人不符 / 金额不符**，
    两项均不符 **同时列出**；导入时 **冻结逐行结论**（第 1 版）；
  - 付款迟到后可 **复核**：结论变化才生成 **新版本**，结论不变不新增版本；
    **旧版本与原始回单始终可查**；
  - 每版输出 **状态计数及差异行**；付款与复核并发时，一版内全部结论来自
    **同一已提交状态**（单语句快照）；核验 **不改写付款、余额或单据**。

## 一键启动

要求 Docker 20.10+（带 Compose v2）。

```bash
docker compose up --build -d
```

首次启动会自动：

1. 启动 PostgreSQL 16 并建立数据卷；
2. 等待数据库健康检查通过；
3. 构建并启动 API 容器，自动执行建表脚本（约束、触发器，无需额外扩展）。

查看日志：

```bash
docker compose logs -f api
```

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷
```

## 地址与端口

| 服务 | 默认地址 |
| --- | --- |
| API 基地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（交互式文档） | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| PostgreSQL | localhost:5432 |

## 配置（可选）

均通过环境变量覆盖，docker-compose 已内置默认值；可在项目根目录放 `.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | API 映射到宿主机的端口 |
| `POSTGRES_PORT` | `5432` | 数据库映射到宿主机的端口 |
| `POSTGRES_USER` | `royalty` | 数据库用户 |
| `POSTGRES_PASSWORD` | `royalty` | 数据库密码 |
| `POSTGRES_DB` | `royalty` | 数据库名 |
| `DATABASE_URL`（仅 API 容器） | `postgresql://royalty:royalty@db:5432/royalty` | 数据库连接串 |

本地裸机运行（不使用 Docker）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 让 DATABASE_URL 指向本地 PG（无需安装任何 contrib 扩展）
export DATABASE_URL="postgresql://royalty:royalty@localhost:5432/royalty"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 金额一律使用 **非负整数、最小货币单位**（如"分"），避免浮点误差；比例以字符串小数传输（如 `"62.5"`），最多 4 位小数（0.0001%）。
> 所有时间必须带时区（如 `2026-02-10T20:00:00+08:00`），内部统一按 UTC 比较；协议区间为半开 `[from, to)`，相邻区间允许，未填 `valid_to` 表示长期有效。

## HTTP API 一览

### 登记

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 登记作品（同编号幂等，标题冲突 409） |
| GET | `/works` | 作品列表 |
| POST | `/rightsholders` | 登记权利人（建协议时也会自动建档） |
| GET | `/rightsholders` | 权利人列表 |
| POST | `/agreements` | 登记分成协议（区间重叠 409；比例合计≠100% 422） |
| GET | `/agreements?work_code=` | 协议列表（可按作品过滤） |
| GET | `/agreements/{id}` | 协议详情 |

### 播放记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plays/import` | 导入/更正单条；幂等返回 `status=duplicate`，异内容 409，分叉 409 |
| POST | `/plays/bulk-import` | 批量导入（整批一个事务，任一失败整批拒绝） |
| GET | `/plays/{import_id}/chain` | 更正链（从原始记录到链尾，标注 `chain_tail`） |

### 结算单据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/statements/issue` | 按期间出具；首次初次单，其后差额追补单（无变化不出新单）；同步冻结逐笔归因 |
| GET | `/statements/preview?start=&end=` | 不出具，试算当前应付、待处理、潜在差额 |
| GET | `/statements?start=&end=` | 列出该期间全部单据及明细（含 `traceable` 可追溯标记） |
| GET | `/statements/{batch_id}/attributions` | 按单据分页查询逐笔归因记录与逐权利人合计（可按 `holder_code` 过滤） |

### 付款核对

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments` | 登记已完成付款；新登记 201，同流水号同内容重试 200 返回原记录 |
| GET | `/rightsholders/{holder_code}/ledger` | 按权利人查询单据增减、付款流水及当前余额（不存在 404） |

### 银行回单核验

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/receipts/import` | 按批次导入回单并冻结第 1 版核验结论；同内容重试 200 返回原批次；异内容复用 409；批内重复/无效字段 422 整批拒绝 |
| POST | `/receipts/{batch_no}/reverify` | 按当前已登记付款复核；结论变化生成新版本（201），不变不新增（200 返回最新版） |
| GET | `/receipts/{batch_no}` | 批次原始回单行（含重复回单标记）与最新版本号 |
| GET | `/receipts/{batch_no}/versions` | 全部核验版本及各版状态计数（旧版本始终可查） |
| GET | `/receipts/{batch_no}/versions/{version}` | 指定版本的状态计数及差异行（结论已冻结） |

`POST /payments` 请求体：

```json
{ "holder_code": "RS-A", "external_ref": "BANK-20260301-0001", "amount": 19 }
```

- `amount` 为正整数、最小货币单位（分）；`external_ref` 为外部付款流水号，全局唯一。
- 成功响应（含登记后余额）：

```json
{
  "payment_id": 1,
  "holder_code": "RS-A",
  "external_ref": "BANK-20260301-0001",
  "amount": 19,
  "registered_at": "2026-03-01T10:00:00Z",
  "status": "created",
  "balance_after": 0
}
```

- 幂等重试时 `status="duplicate"`、HTTP 200，`payment_id` 与原记录相同，
  `balance_after` 为**当前**余额；不产生第二笔付款。
- 错误：同流水号异内容 → 409 `payment_ref_conflict`（响应 `detail` 回显原权利人/金额）；
  余额不为正或超额 → 409 `payment_exceeds_balance`（`detail.balance` 为当时余额）；
  权利人不存在 → 404 `holder_not_found`；金额为 0、负数或非整数 → 422。
  上述拒绝均**不写入**任何付款。

`GET /rightsholders/{holder_code}/ledger` 响应要点：

- `billed_total`：该权利人全部期间已出具单据明细（初次单与正负追补单）有符号累计；
- `paid_total`：已登记付款累计；`balance = billed_total − paid_total`（可为负）；
- `statements[]`：每张涉及该权利人的单据（`batch_id`、`kind`、期间、`amount` 带正负号、`issued_at`），
  按出具次序；`payments[]`：付款流水（`payment_id`、`external_ref`、`amount`、`registered_at`）；
- 试算与待处理播放不在内；负余额表示"历史付款暂未被后续正额抵扣完"，仍可核对。

期间同样是半开区间 `[start, end)`，与协议区间语义一致。

#### 银行回单批次导入与核验

`POST /receipts/import` 请求体：

```json
{
  "batch_no": "RC-20260301-01",
  "receipts": [
    {"receipt_no": "RC-1", "external_ref": "BANK-20260301-0001", "holder_code": "RS-A", "amount": 19},
    {"receipt_no": "RC-2", "external_ref": "BANK-20260302-0002", "holder_code": "RS-B", "amount": 9}
  ]
}
```

- 回单行字段：`receipt_no`（回单编号）、`external_ref`（外部付款流水号）、
  `holder_code`（权利人编号，回单所载，不要求已建档）、`amount`（正整数分额）；
  空编号、非正整数金额等无效字段 **422 整批拒绝**，不留任何回单。
- 导入响应（201，`status=created`）含原始回单行（`is_duplicate` 标记）与
  导入时冻结的第 1 版核验结论 `verification`：

```json
{
  "batch_no": "RC-20260301-01",
  "status": "created",
  "created_at": "2026-03-02T02:00:00Z",
  "receipts": [
    {"receipt_no": "RC-1", "external_ref": "BANK-20260301-0001",
     "holder_code": "RS-A", "amount": 19, "is_duplicate": false}
  ],
  "verification": {
    "batch_no": "RC-20260301-01",
    "version": 1,
    "created_at": "2026-03-02T02:00:00Z",
    "counts": {"matched": 1, "payment_not_registered": 0,
               "mismatched": 1, "duplicate": 0, "total": 2},
    "differences": [
      {"receipt_no": "RC-2", "external_ref": "BANK-20260302-0002",
       "status": "mismatched", "differences": ["holder_mismatch"],
       "receipt_holder_code": "RS-B", "receipt_amount": 9,
       "payment_holder_code": "RS-C", "payment_amount": 9}
    ]
  }
}
```

- 逐行结论 `status`：`matched`（流水号命中已登记付款且权利人、金额均一致）、
  `payment_not_registered`（付款未登记）、`mismatched`（`differences` 列出
  `holder_mismatch` / `amount_mismatch`，**两项均不符同时列出**）、
  `duplicate`（跨批次同一流水号的重复回单，不参与核验）；
  差异行附回单与付款双方快照（`receipt_*` / `payment_*`）。
- 幂等与拒绝：批次同内容重试 **200 返回原批次**（`status=duplicate`，含第 1 版结论）；
  批次号异内容复用 **409 `receipt_batch_conflict`**；
  回单编号异内容复用 **409 `receipt_no_conflict`**（`detail` 回显已导入内容）；
  批内重复流水号 / 重复回单编号 **422**；上述拒绝均 **整批不留痕**。
- 跨批次同一流水号：仅 **首次成功导入** 的回单参与核验；后续批次中同号回单
  标为 `is_duplicate=true`、结论 `duplicate`（同内容复用回单编号亦按此处理）。

`POST /receipts/{batch_no}/reverify`（付款迟到后复核）：

- 按 **当前已登记付款** 重算逐行结论：与最新版不同才冻结为新版本
  （201，`status=created`）；结论不变不新增版本
  （200，`status=unchanged`，返回现有最新版）；
- 付款与复核并发时，一版内全部结论来自 **同一已提交状态**（单语句快照）；
- 复核只新增核验版本，**绝不改写付款、余额或单据**；批次不存在 404。

查询（旧版本与原始回单始终可查）：

- `GET /receipts/{batch_no}`：原始回单行（含 `is_duplicate`）与 `latest_version`；
- `GET /receipts/{batch_no}/versions`：全部版本及各版状态计数；
- `GET /receipts/{batch_no}/versions/{version}`：指定版本的状态计数及差异行；
  版本不存在 404 `receipt_version_not_found`。

#### 逐笔归因查询参数

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `batch_id` | 路径 | 单据编号；不存在返回 `404 statement_not_found` |
| `holder_code` | query | 可选，只看某位权利人；权利人不存在返回 `404 holder_not_found` |
| `page` | query | 页码，从 1 开始（默认 1；<1 返回 422） |
| `page_size` | query | 每页条数，默认 50，范围 1–500（越界 422） |

响应要点：

- `records[]`：按"权利人 → 原始编号 → 性质（removed 先于 added）"稳定排序，字段含
  `root_import_id`（更正链原始编号）、`tail_import_id`（链尾）、`work_code`、
  `occurred_at`、`agreement_id`、`tail_amount`、`share_percent`、`amount`；
- `change_type`：`added`=新增（非负）、`removed`=撤销（负额）、`legacy`=不可追溯旧账差额（无播放字段）；
- `holder_totals[]`：每位权利人 `attributed_total`（归因合计）、
  `statement_line_amount`（原单据明细行金额）与 `reconciles`（两者是否相等）；
- `traceable=false`：存量不可追溯单据，`records` 为空、`reconciles` 恒为 false。

## 调用示例

一键演示脚本（请先启动服务），覆盖幂等、异内容拒绝、区间重叠、更正链/分叉拒绝、
后补协议、逐笔分摊、初次单与正负追补单、待处理清单等全部场景：

```bash
bash examples/demo.sh
```

手工调用（以下为关键几步）：

```bash
# 1. 登记作品
curl -sS -X POST http://localhost:8000/works \
  -H 'Content-Type: application/json' \
  -d '{"code":"W-LIVE-001","title":"仲夏之夜现场录音"}'

# 2. 登记分成协议（合计 100%）
curl -sS -X POST http://localhost:8000/agreements \
  -H 'Content-Type: application/json' \
  -d '{
    "work_code":"W-LIVE-001",
    "valid_from":"2026-01-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[
      {"holder_code":"RS-A","percent":"62.5"},
      {"holder_code":"RS-B","percent":"30"},
      {"holder_code":"RS-C","percent":"7.5"}
    ]
  }'

# 3. 导入播放（100 分）
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'

# 4. 更正：新编号 + corrects_id 引用原编号
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R1","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-1001"}'

# 5. 出具 2026 年 2 月单据（80 分 => 50/24/6）
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

# 6. 再次更正为 30 分后重新出具 => 追补单差额 -31/-15/-4
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R2","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":30,
       "corrects_id":"PLAY-1001-R1"}'
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'
```

出具后，财务按单据核对每笔金额来自哪些播放（归因记录分页查询）：

```bash
# 初次单 batch_id=1：查看权利人 RS-A 的逐笔归因（第 1 页，每页 20 条）
curl -sS "http://localhost:8000/statements/1/attributions?holder_code=RS-A&page=1&page_size=20"

# 追补单 batch_id=2：撤销(removed，负数)与新增(added)分列、负额保留；
# holder_totals 中 attributed_total 恒等于该单原明细行金额（reconciles=true）
curl -sS "http://localhost:8000/statements/2/attributions" | python3 -m json.tool

# 不存在的单据明确报错
curl -sS -i "http://localhost:8000/statements/999999/attributions"   # 404 statement_not_found
```

财务按权利人核对单据与实付（第 5、6 步后 RS-A 单据累计 19 分）：

```bash
# 登记付款 19 分（不超过正余额）=> 201，balance_after=0
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}'

# 同流水号同内容重试 => 200 status=duplicate，返回原记录，不重复入账
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}'

# 超额（余额已为 0）=> 409 payment_exceeds_balance，不留付款
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0002","amount":1}'

# 同流水号异内容 => 409 payment_ref_conflict；权利人不存在 => 404；金额非正整数 => 422
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":10}'
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"NOBODY","external_ref":"X","amount":1}'

# 对账单：单据增减（含负追补）、付款流水、billed/paid/balance
curl -sS "http://localhost:8000/rightsholders/RS-A/ledger" | python3 -m json.tool
```

财务按批次导入银行回单，核验已登记付款（承上：BANK-20260301-0001 已付 RS-A 19 分）：

```bash
# 1) 再登记两笔付款：RS-B 9 分（回单将错记权利人）、RS-C 2 分（回单迟到后补登）
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260302-0002","amount":9}'

# 2) 导入批次 RC-20260301-01：匹配 / 权利人不符 / 付款未登记 各一行
curl -sS -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{
    "batch_no":"RC-20260301-01",
    "receipts":[
      {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
      {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9},
      {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2}
    ]
  }' | python3 -m json.tool
# => 201，verification(version=1)：counts matched=1 / mismatched=1(RC-2 holder_mismatch)
#    / payment_not_registered=1(RC-3)

# 3) 同内容重试 => 200 status=duplicate 返回原批次；异内容复用批次号 => 409
curl -sS -i -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-01","receipts":[
      {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
      {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9},
      {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2}]}' | head -1
curl -sS -i -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-01","receipts":[
      {"receipt_no":"RC-9","external_ref":"BANK-X","holder_code":"RS-A","amount":1}]}' | head -1

# 4) 付款迟到：补登 RS-C 2 分后复核 => 生成第 2 版（RC-3 转为 matched）
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-C","external_ref":"BANK-20260303-0003","amount":2}'
curl -sS -X POST http://localhost:8000/receipts/RC-20260301-01/reverify | python3 -m json.tool
# 结论不变再复核 => 200 status=unchanged，不新增版本
curl -sS -X POST http://localhost:8000/receipts/RC-20260301-01/reverify | python3 -m json.tool

# 5) 旧版本与原始回单始终可查
curl -sS "http://localhost:8000/receipts/RC-20260301-01/versions" | python3 -m json.tool
curl -sS "http://localhost:8000/receipts/RC-20260301-01/versions/1" | python3 -m json.tool
curl -sS "http://localhost:8000/receipts/RC-20260301-01" | python3 -m json.tool

# 6) 跨批次同一流水号：后续批次同号回单标为重复回单，不参与核验
curl -sS -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-02","receipts":[
      {"receipt_no":"RC-4","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19}
    ]}' | python3 -m json.tool
# => RC-4 is_duplicate=true，结论 status=duplicate
```

## 分摊示例

金额 100，比例 62.5% / 30% / 7.5%：

- 精确值 62.5 / 30 / 7.5 → 先取整 62 / 30 / 7，余 1；
- 小数部分 `RS-A`、`RS-C` 同为 0.5（`RS-B` 为 0），按权利人编号升序补 1 给 `RS-A`；
- 结果 **63 / 30 / 7 = 100**。

金额 80：50 / 24 / 6（精确整除，无余数）；金额 30：18.75 / 9 / 2.25，
取整 18/9/2 余 1，小数部分最大的 `RS-A`（0.75）补 1 → **19/9/2**。每笔之和恒等于原金额。

## 测试

核心分摊算法的单元测试（无需数据库）：

```bash
python3 -m unittest discover -s tests -v
```

端到端集成测试（用 [`pgserver`](https://pypi.org/project/pgserver/) 自动拉起便携 PostgreSQL，
**无需 Docker / root**，覆盖登记、幂等、重叠、更正链、待处理、初次/追补单、并发出具等全链路；
逐笔归因证据（冻结/撤销负额/分页对账/不可变/存量不可追溯与 legacy 旧账/错误码）另有一套；
付款核对（余额口径/幂等与冲突/超额拒绝不留痕/负额追补不撤销与后续抵扣/
试算待处理不入账/付款与出单及其他付款并发不超付）再有一套；
银行回单核验（整批拒绝/幂等重试/异内容复用/跨批次重复回单/冻结结论/迟到付款复核版本化/
并发复核与并发导入/付款与复核并发快照一致/核验不改写账务）又有一套：

```bash
pip install pgserver httpx
PYTHONPATH=. python3 tests/integration_pgserver.py       # 原有全链路
PYTHONPATH=. python3 tests/integration_attribution.py    # 逐笔归因证据
PYTHONPATH=. python3 tests/integration_payments.py       # 付款登记与权利人对账
PYTHONPATH=. python3 tests/integration_receipts.py       # 银行回单批次导入与付款核验
```

## 目录结构

```
app/
  main.py                 # FastAPI 入口、生命周期、异常映射
  config.py               # 环境配置
  db.py                   # 连接池与幂等建表脚本（全部约束在此）
  allocation.py           # 逐笔按比例分摊（最大余数法 + 编号次序）
  schemas.py              # Pydantic 模型与输入校验
  statements_service.py   # 期间匹配、重算、初次/追补出具、并发互斥、逐笔归因冻结与分页对账
  payments_service.py     # 付款登记（流水号锁/余额校验/幂等冲突）、权利人对账单、出单侧权利人行锁
  receipts_service.py     # 回单批次导入（幂等/整批拒绝/跨批次去重）、核验结论冻结与复核版本化
  routers/
    catalog.py            # 作品 / 权利人 / 协议（含协议区间互斥）
    plays.py              # 播放导入、幂等、更正链
    statements.py         # 单据出具、试算、查询、逐笔归因分页查询
    payments.py           # 付款登记、按权利人对账
    receipts.py           # 回单批次导入、复核、版本查询
examples/demo.sh          # 端到端 curl 示例
tests/                    # 单元测试与四套 pgserver 端到端集成测试
Dockerfile  docker-compose.yml  requirements.txt
```
