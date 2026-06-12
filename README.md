# 供应商对账批处理平台

本地部署的供应商采购单与发票对账系统，支持批次管理、字段校验、金额容差配置、匹配/异常/确认/回滚完整流程，报表可导出供财务复核。

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
