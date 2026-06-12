# 供应商对账批处理平台

本地部署的供应商采购单与发票对账系统，支持**预检草稿模式**、批次管理、字段校验、金额容差配置、匹配/异常/确认/回滚完整流程，报表可导出供财务复核。

> ⚠️ **重要**：自 v2.0 起，文件导入采用「先预检、后确认」的两步模式。上传文件后不会立即写入正式数据，需在预检报告确认无误后点击「确认导入」才会生效。取消或丢弃草稿不会影响原有数据。

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
