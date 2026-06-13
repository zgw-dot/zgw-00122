# 供应商对账批处理平台

本地部署的供应商采购单与发票对账系统，支持**批量导入方案**、预检草稿模式、批次管理、字段校验、金额容差配置、匹配/异常/确认/回滚完整流程，报表可导出供财务复核。

> ⚠️ **重要**：自 v3.0 起，文件导入支持**批量上传**（同时传采购单+发票），生成同一批次的待确认方案。确认前不写入正式表，确认后可撤销最近一次导入。原有的单文件预检入口和样例继续可用。

## 本地启动

```bash
# 1. 创建虚拟环境（推荐）
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
# source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
python app.py

# 4. 打开浏览器访问
# http://localhost:5000
```

## 样例文件格式

### 采购单文件 (CSV)

```csv
po_number,vendor_code,vendor_name,amount,currency,po_date
PO-2024-001,V001,华东科技有限公司,50000.00,CNY,2024-06-01
PO-2024-002,V001,华东科技有限公司,30000.00,CNY,2024-06-02
```

**必填列**: `po_number`, `vendor_code`, `vendor_name`, `amount`, `po_date`
**可选列**: `currency`（默认 CNY）

### 发票文件 (CSV)

```csv
invoice_number,vendor_code,vendor_name,amount,currency,invoice_date
INV-2024-001,V001,华东科技有限公司,50000.00,CNY,2024-06-05
INV-2024-002,V001,华东科技有限公司,29800.00,CNY,2024-06-06
```

**必填列**: `invoice_number`, `vendor_code`, `vendor_name`, `amount`, `invoice_date`
**可选列**: `currency`（默认 CNY）

也支持 `.xlsx` / `.xls` 格式，列名需与上述一致。

## 样例文件说明

| 文件 | 说明 |
|------|------|
| `sample/purchase_orders.csv` | 正常采购单（6条） |
| `sample/invoices.csv` | 正常发票（6条），含精确匹配、容差匹配和未匹配场景 |
| `sample/bad_missing_columns.csv` | 缺少 `vendor_name` 列，上传即报错 |
| `sample/bad_over_tolerance_po.csv` + `sample/bad_over_tolerance_invoice.csv` | 同供应商金额差异 60%，超出默认容差 |
| `sample/bad_duplicate_invoice.csv` | 同一发票号出现两次，上传即报错 |

## 完整验收流程

1. **创建批次** — 填写名称，设置容差（默认 2% / ¥100）
2. **上传文件** — 分别上传采购单和发票 CSV/XLSX
3. **执行匹配** — 系统自动按供应商编码匹配，校验金额差异
4. **查看结果** — 精确匹配、容差匹配、未匹配采购单/发票
5. **处理异常** — 逐条添加备注，确认或驳回
6. **确认入账** — 所有异常处理后确认
7. **入账** — 确认后执行入账
8. **导出报表** — 下载 CSV 对账报表供财务复核
9. **回滚** — 入账后可回滚，回滚后可重置重新匹配

### 正常验收（使用 sample/purchase_orders.csv + sample/invoices.csv）

- PO-2024-001 ↔ INV-2024-001：精确匹配（50000 = 50000）
- PO-2024-002 ↔ INV-2024-002：容差匹配（30000 vs 29800，差 200，在 2% 容差内）
- PO-2024-003 ↔ INV-2024-003：精确匹配（120000 = 120000）
- PO-2024-004 ↔ INV-2024-004：精确匹配（75000 = 75000）
- PO-2024-005 ↔ INV-2024-005：容差匹配（25000 vs 25500，差 500，在 2%/¥100 容差外，属异常）
- PO-2024-006：未匹配采购单（无发票）
- INV-2024-006：未匹配发票（无采购单）

### 失败场景

- **缺列文件**：上传 `bad_missing_columns.csv`，立即报错"缺少列: vendor_name"，批次不产生任何结果
- **超出容差**：上传 `bad_over_tolerance_po.csv` + `bad_over_tolerance_invoice.csv`，匹配后产生超容差异常
- **重复发票号**：上传 `bad_duplicate_invoice.csv`，立即报错"发票号重复"
- **重复回滚**：已回滚的批次再次点回滚，报错"该批次已回滚，不允许重复回滚"

## 批次状态流转

```
CREATED → VALIDATING → MATCHED → CONFIRMED → POSTED
                     ↘ EXCEPTION_PENDING → CONFIRMED → POSTED
                                           ↘ (回滚) ROLLED_BACK → (重置) CREATED
任意阶段失败 → FAILED → (重置) CREATED
POSTED → ROLLED_BACK → CREATED
```

## 容差配置留痕

每次修改容差配置都会写入 `tolerance_history` 表，记录修改前的百分比、绝对值和规则版本哈希。匹配时使用的 `rule_version` 随匹配结果保存，保证同一批次的规则版本可追溯。

## 数据持久性

- 使用 SQLite 数据库（`reconciliation.db`），服务重启后数据完整保留
- 回滚后应付合计与导出的对账报表保持一致
- 容差变更历史、操作日志均持久化
- 应付重算说明持久化存储，服务重启后历史版本完整可查

## 应付重算说明

当财务修改异常处理意见、回滚后重新匹配，或调整容差再跑一次时，系统自动生成应付重算说明，清晰记录应付金额变化的原因。

### 字段说明

| 字段 | 说明 |
|------|------|
| `version` | 版本号，每次有效变更自增 |
| `current_total` | 本次应付合计 |
| `previous_total` | 上一次应付合计（v1 为 null） |
| `amount_diff` | 差额 = current_total - previous_total |
| `change_source` | 变化来源（MATCH / EXCEPTION_REMARK / EXCEPTION_RESOLVE / UPDATE_TOLERANCE / CONFIRM / POST / ROLLBACK / RESET / MANUAL） |
| `change_summary` | 变化摘要，人类可读文本 |
| `po_numbers` | 涉及的采购单号列表 |
| `invoice_numbers` | 涉及的发票号列表 |
| `rule_version` | 对应规则版本哈希 |

### 去重与版本控制

- 基于「异常状态 + 备注 + 匹配结果 + 规则版本」计算 SHA-256 哈希作为内容指纹
- 相同内容重复生成时不新增记录（`is_new: false`）
- 只要异常状态、备注、匹配结果或规则版本任一变化，即生成新版本
- 每次生成新版本自动写入操作日志（`RECALC_NOTE_V{n}`），包含变化摘要

### API 接口

```bash
# 查询某批次全部历史说明（按版本升序）
GET /api/batches/{batch_id}/recalc-notes

# 查询最新版本说明
GET /api/batches/{batch_id}/recalc-notes/latest

# 按 ID 查询指定版本
GET /api/batches/{batch_id}/recalc-notes/{note_id}

# 手动触发生成（自动去重）
POST /api/batches/{batch_id}/recalc-notes/generate
Body: {"change_source": "MANUAL", "operator": "finance_user"}
```

### 导出报表

导出 CSV 的「汇总信息」区自动附带：
- 重算说明版本
- 重算说明摘要
- 重算来源
- 上一次应付合计（如存在）
- 应付差额（如存在）

导出金额严格与批次详情接口 `/api/batches/{id}` 的 `summary.payable_total` 对齐。

### 回滚与重置

- 已回滚或重置的批次可查看全部历史说明（版本列表完整保留）
- 回滚/重置操作本身也会生成新的说明版本，但旧版本不会被覆盖或修改
- 导出始终取当前最新版本，不会串用回滚前的旧数据

## 预检草稿 API

> **推荐使用预检模式**：所有文件导入请走预检草稿链路，确保数据安全。

### 1. 上传预检（采购单）

```bash
POST /api/batches/{batch_id}/precheck-po
Content-Type: multipart/form-data

# 参数
# file: CSV/XLSX 文件
# operator: 操作人（可选，默认 web_user）

# 返回示例
{
  "id": 1,
  "batch_id": 10,
  "file_type": "PO",
  "filename": "purchase_orders.csv",
  "status": "PENDING",
  "row_count": 6,
  "valid_row_count": 6,
  "error_count": 0,
  "warning_count": 2,
  "tolerance_pct": 2.0,
  "tolerance_abs": 100.0,
  "rule_version": "76ef9f65a123...",
  "is_new": true,
  "conflict": null,
  "precheck_report": {
    "summary": { "error_count": 0, "warning_count": 2, "valid_rows": 6 },
    "missing_columns": [],
    "issues": [...]
  },
  "issues": [...]
}
```

### 2. 上传预检（发票）

```bash
POST /api/batches/{batch_id}/precheck-invoice
Content-Type: multipart/form-data

# 参数同采购单
```

### 3. 查询草稿列表

```bash
GET /api/batches/{batch_id}/drafts
GET /api/batches/{batch_id}/drafts?file_type=PO
GET /api/batches/{batch_id}/drafts?status=PENDING
GET /api/batches/{batch_id}/drafts?file_type=INVOICE&status=DISCARDED

# 返回: { "drafts": [...] }
```

### 4. 获取最新草稿

```bash
GET /api/batches/{batch_id}/drafts/latest
GET /api/batches/{batch_id}/drafts/latest?file_type=PO
GET /api/batches/{batch_id}/drafts/latest?file_type=INVOICE

# 返回: { "draft": {...} } 或 { "draft": null }
```

### 5. 获取单个草稿详情

```bash
GET /api/batches/{batch_id}/drafts/{draft_id}

# 返回: { "draft": {...} }
```

### 6. 确认草稿（写入正式数据）

```bash
POST /api/batches/{batch_id}/drafts/{draft_id}/confirm
Content-Type: application/json
{ "operator": "finance_user" }

# 返回: { "success": true, "imported_count": 6 }
```

### 7. 丢弃草稿（不写入数据）

```bash
POST /api/batches/{batch_id}/drafts/{draft_id}/discard
Content-Type: application/json
{ "operator": "finance_user" }

# 返回: { "success": true }
```

### 旧 API（已废弃）

> ⚠️ `POST /api/batches/{batch_id}/upload-po` 和 `POST /api/batches/{batch_id}/upload-invoice` 为旧版直接导入接口，**不经过预检**。保留用于兼容旧脚本，新流程请使用预检 API。

## 可复现验收步骤（预检模式）

以下步骤可直接复制到终端执行，验证完整预检链路：

```bash
# 1. 启动服务
python app.py &

# 2. 创建批次
curl -X POST http://localhost:5000/api/batches \
  -H "Content-Type: application/json" \
  -d '{"name":"验收批次-001","tolerance_pct":2.0,"tolerance_abs":100}'

# 3. 上传采购单预检
curl -X POST http://localhost:5000/api/batches/1/precheck-po \
  -F "file=@sample/purchase_orders.csv" \
  -F "operator=test_user"

# 4. 查看最新草稿
curl http://localhost:5000/api/batches/1/drafts/latest?file_type=PO

# 5. 确认导入采购单
curl -X POST http://localhost:5000/api/batches/1/drafts/1/confirm \
  -H "Content-Type: application/json" \
  -d '{"operator":"finance_user"}'

# 6. 上传发票预检
curl -X POST http://localhost:5000/api/batches/1/precheck-invoice \
  -F "file=@sample/invoices.csv" \
  -F "operator=test_user"

# 7. 确认导入发票
curl -X POST http://localhost:5000/api/batches/1/drafts/2/confirm \
  -H "Content-Type: application/json" \
  -d '{"operator":"finance_user"}'

# 8. 执行匹配
curl -X POST http://localhost:5000/api/batches/1/match

# 9. 查看结果
curl http://localhost:5000/api/batches/1/results
```

**失败场景验证**：

```bash
# 验证：缺列文件预检报错，不写入正式数据
curl -X POST http://localhost:5000/api/batches/1/precheck-po \
  -F "file=@sample/bad_missing_columns.csv"
# 预期返回: error_count > 0，issues 包含 "缺少必填列"

# 验证：重复发票预检报错
curl -X POST http://localhost:5000/api/batches/1/precheck-invoice \
  -F "file=@sample/bad_duplicate_invoice.csv"
# 预期返回: error_count > 0，issues 包含 "发票号重复"

# 验证：取消后数据不变
# 先预检 → 再 discard → 检查 PurchaseOrder 表数量不变
```

## 导入复核台（v2.1 新增）

> 采购单/发票上传后**不止显示通过/失败**，还会与当前批次正式数据、上一版草稿、跨批次全局发票做**三层 diff 对比**，给出「新增 / 覆盖 / 跳过 / 冲突」四宫格分类 + 人类可读复核摘要，由财务人工复核后再点「确认导入」。

### 复核台操作流程

```
上传文件
   │
   ▼
预检 + 三层 diff 分析
   │  ├─ vs_official        → 对比当前批次正式数据 → 新增/覆盖/跳过
   │  ├─ vs_previous_draft  → 对比同批次同类型上一版草稿 → 增/删/改
   │  └─ cross_batch_conflicts → 跨批次全局查重复发票 → 冲突(CONFLICT)
   ▼
显示复核分析面板  ← 状态: PENDING / CONFLICT
   │
   ├─ ✅ 确认导入  → 写入正式表 + 记录 confirmed_by/confirmed_at
   ├─ 🟡 取消（保留原数据）→ 草稿置 CANCELLED，正式数据不变
   └─ ⚫ 丢弃草稿  → 草稿置 DISCARDED（同类型重新上传时系统自动 discard 旧版）
```

### 草稿状态流转图

```
PENDING ──确认导入──▶ CONFIRMED
   │  ▲
   │  │  同类型重新上传 & 内容相同 → 复用旧草稿
   │  │
   │  └──同类型重新上传 & 内容不同──┐
   │                               │
   └─系统自动 discard──▶ DISCARDED ◀┘
   │
   └─用户点 取消 ────▶ CANCELLED
   │
跨批次重复发票 ──────▶ CONFLICT ──用户取消/丢弃──▶ CANCELLED / DISCARDED
                                 └──（不可直接确认，需先处理重复）
```

### 新增 API 接口

#### 8. 取消草稿（v2.1 新增）

语义：用户主动放弃导入，**原正式数据保持不变**。与 `discard` 的区别：
- `cancel` → 用户主动点「取消」，状态置 `CANCELLED`
- `discard` → 系统因新草稿替代旧草稿 或 用户点「丢弃」，状态置 `DISCARDED`

```bash
POST /api/batches/{batch_id}/drafts/{draft_id}/cancel
Content-Type: application/json
{ "operator": "finance_user" }

# 返回: { "success": true, "note": "已取消，原正式数据保持不变" }
```

**跨批次阻断**：若 `batch_id` 与草稿实际归属批次不一致，直接返回 400 `草稿不属于该批次，跨批次草稿误确认被阻断`。

### 预检返回体 v2.1 扩展字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | str | `PENDING` / `CONFLICT`（跨批次重复） / `CONFIRMED` / `DISCARDED` / `CANCELLED` |
| `diff_analysis` | object | 三层 diff 分析结果（见下方） |
| `diff_analysis.vs_official` | object | `{add_count, overwrite_count, skip_count, conflict_count, add_rows[], overwrite_rows[], skip_rows[], conflict_rows[]}` |
| `diff_analysis.vs_previous_draft` | object | 同上一版草稿的差异：`{prev_draft_id, prev_filename, added_vs_prev[], removed_vs_prev[], modified_vs_prev[]}` |
| `diff_analysis.cross_batch_conflicts` | object | `{invoice_duplicates: [{invoice_number, vendor, existing_batch_id, existing_batch_name, amount}]}` |
| `diff_analysis.summary_text` | str | 人类可读复核摘要，如「新增 6 条；覆盖 2 条；跳过 1 条」 |
| `conflict_reason` | str | `None` 或 冲突原因（如「检测到 3 条跨批次重复发票...」「被新草稿 #5 取代」） |
| `review_summary` | str | 复核摘要（= diff_analysis.summary_text），导出 CSV 用 |
| `confirmed_by` | str | 确认人（仅 CONFIRMED 状态有值） |
| `confirmed_at` | ISO 时间 | 确认时间（仅 CONFIRMED 状态有值） |
| `supersedes_draft_id` | int | 本草稿取代了哪一版旧草稿 |
| `superseded_by_draft_id` | int | 本草稿被哪一版新草稿取代（仅 DISCARDED 状态有值） |

### 确认导入的阻断规则（四层校验）

| 层级 | 触发条件 | 行为 |
|------|----------|------|
| ① | 草稿状态非 `PENDING` / `CONFLICT`（如 CANCELLED、DISCARDED） | 返回 400 `草稿状态为 XXX，不允许确认` |
| ② | 跨批次重复发票（cross_batch_conflicts 非空） | 返回 400 `草稿存在跨批次重复发票，禁止直接确认，请先处理发票重复` |
| ③ | 文件缺少必填列（missing_columns 非空） | 返回 400 `文件缺少必要列: vendor_code, ...` |
| ④ | 文件存在格式错误（error_count > 0） | 返回 400 `文件存在 N 条格式错误，请修正后重新上传` |

### 导出 CSV 汇总区扩展（v2.1 新增）

在原有「汇总信息」区块底部，新增「最近预检复核摘要」子区块：

```
最近预检复核摘要  |  [采购单]新增7条 @fin_user_1； [发票]新增6条 @fin_user_2
采购单草稿       |  #2 po_v2.csv @fin_user_1 (2025-01-15 10:30)
采购单复核明细   |  新增 7 条
发票草稿         |  #4 inv.csv @fin_user_2 (2025-01-15 10:32)
发票复核明细     |  新增 6 条
```

> 取各自 file_type 最近一条 status=CONFIRMED 的草稿展示。

---

## 验收检查清单（导入复核台）

| # | 验收项 | 验证位置 | 预期结果 |
|---|--------|----------|----------|
| 1 | 同批次连续上传两版同类型文件 | 步骤4（superseded） | 新草稿 `supersedes_draft_id` 指向旧稿，旧稿 `status=DISCARDED` 且含 `conflict_reason`，正式数据 **未被改写** |
| 2 | 服务重启持久化 | 步骤13（文件 SQLite） | 草稿/状态/`confirmed_by`/`confirmed_at`/`superseded_by_*`/审计日志 **全部保留** |
| 3 | 确认阻断：重复发票 | 步骤9（CONFLICT→确认） | 返回 400，草稿不写入，跨批次正式数据完好 |
| 4 | 确认阻断：缺列 / 格式错误 | 步骤10 | 返回 400，正式数据不变 |
| 5 | 确认阻断：跨批次草稿误确认 | 步骤11（bid/did 不匹配） | 返回 400「草稿不属于该批次，跨批次草稿误确认被阻断」 |
| 6 | 取消草稿后原正式数据仍可匹配 | 步骤6+18 | `Invoice`/`PurchaseOrder` 正式表行数不变，匹配结果数不变 |
| 7 | 导出 CSV 含预检复核摘要 | 步骤12 | CSV 中存在「最近预检复核摘要」「采购单草稿」「采购单复核明细」「发票草稿」「发票复核明细」5 行 |
| 8 | 原有预检入口 & 样例保留可用 | 步骤16 | `/precheck-po`/`/precheck-invoice` 仍 200，`sample/*.csv` 正常上传预检 |

---

## 一键验收脚本

### 方案 A：测试客户端跑 18 步（推荐，零依赖）

```bash
python verify_precheck_flow.py
```

覆盖：预检 → diff 四宫格 → superseded 链路 → 取消/确认 → 跨批次重复发票 CONFLICT → 缺列/跨批次阻断 → 导出摘要 → 文件 SQLite 重启持久化 → 匹配 → 原入口保留。

### 方案 B：pytest 回归（68 条）

```bash
python -m pytest tests/ -v
# 其中 5 条导入复核台专用用例：
#   test_import_review_desk_full_flow            完整链路（含 superseded）
#   test_confirm_blocked_by_cross_batch_dup_inv  跨批次重复发票阻断
#   test_confirm_blocked_by_missing_columns      缺列/格式错误阻断
#   test_cancel_keeps_official_data              取消后正式数据不变
#   test_export_csv_includes_review_summary      CSV 汇总含复核摘要
```

### 方案 C：requests 端到端（需先起服务）

```bash
# 终端 1
python app.py

# 终端 2
python -c "
import requests
BASE = 'http://localhost:5000'
# 1. 创建批次
b = requests.post(BASE+'/api/batches', json={'name':'E2E-导入复核台'}).json()
bid = b['id']
# 2. 预检 PO
with open('sample/purchase_orders.csv','rb') as f:
    d1 = requests.post(BASE+f'/api/batches/{bid}/precheck-po',
        files={'file':('po.csv',f)}, data={'operator':'e2e_u'}).json()
assert d1['status']=='PENDING' and d1['diff_analysis']['vs_official']['add_count']>0
# 3. 取消
r = requests.post(BASE+f'/api/batches/{bid}/drafts/{d1[\"id\"]}/cancel', json={'operator':'e2e_u'})
assert r.status_code==200 and r.json()['note'].startswith('已取消')
# 4. 重新预检并确认 PO
with open('sample/purchase_orders.csv','rb') as f:
    d2 = requests.post(BASE+f'/api/batches/{bid}/precheck-po',
        files={'file':('po.csv',f)}, data={'operator':'e2e_u'}).json()
c = requests.post(BASE+f'/api/batches/{bid}/drafts/{d2[\"id\"]}/confirm', json={'operator':'e2e_u'})
assert c.status_code==200 and c.json()['confirmed_by']=='e2e_u'
# 5. 预检 + 确认发票
with open('sample/invoices.csv','rb') as f:
    di = requests.post(BASE+f'/api/batches/{bid}/precheck-invoice',
        files={'file':('inv.csv',f)}, data={'operator':'e2e_u2'}).json()
requests.post(BASE+f'/api/batches/{bid}/drafts/{di[\"id\"]}/confirm', json={'operator':'e2e_u2'})
# 6. 匹配
requests.post(BASE+f'/api/batches/{bid}/match')
# 7. 导出 CSV
exp = requests.get(BASE+f'/api/batches/{bid}/export').text
assert '最近预检复核摘要' in exp
assert '采购单草稿' in exp and '@e2e_u' in exp
print('✅ E2E 预检→取消→确认→匹配→导出 全链路通过')
"
```
