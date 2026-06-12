import csv
import io
import json
from models import (
    db, Batch, PurchaseOrder, Invoice, MatchResult, ExceptionItem,
    ToleranceHistory, AuditLog, PayableRecalcNote,
    BATCH_STATUS_VALIDATING, BATCH_STATUS_MATCHED, BATCH_STATUS_EXCEPTION,
    BATCH_STATUS_FAILED, BATCH_STATUS_CREATED,
    MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE,
    MATCH_TYPE_UNMATCHED_PO, MATCH_TYPE_UNMATCHED_INVOICE,
    EXCEPTION_MISSING_FIELD, EXCEPTION_OVER_TOLERANCE,
    EXCEPTION_DUPLICATE_INVOICE,
    EXCEPTION_STATUS_PENDING, RESULT_STATUS_PENDING, RESULT_STATUS_REJECTED,
    compute_rule_version, compute_note_content_hash,
)

PO_REQUIRED_COLUMNS = ["po_number", "vendor_code", "vendor_name", "amount", "po_date"]
INVOICE_REQUIRED_COLUMNS = ["invoice_number", "vendor_code", "vendor_name", "amount", "invoice_date"]


class ValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        self.details = errors if isinstance(errors, list) else [errors]
        super().__init__("; ".join(self.details))


def _extract_storage(file_storage, fallback_filename=None):
    """Return (read_text_fn, filename, is_excel) from either FileStorage or (text, filename) tuple."""
    if isinstance(file_storage, tuple):
        content, fname = file_storage
        fname = fallback_filename or fname or ""

        def read_text():
            return content

        return read_text, fname, fname.endswith((".xlsx", ".xls"))
    # werkzeug FileStorage or similar
    fname = getattr(file_storage, "filename", None) or fallback_filename or ""

    def read_text():
        raw = file_storage.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        return raw

    return read_text, fname, fname.endswith((".xlsx", ".xls"))


def parse_csv_file(file_storage):
    read_text, _, _ = _extract_storage(file_storage)
    raw = read_text()
    reader = csv.DictReader(io.StringIO(raw))
    rows = []
    headers = reader.fieldnames or []
    for row in reader:
        rows.append(row)
    return headers, rows


def parse_excel_file(file_storage):
    from openpyxl import load_workbook
    read_text, _, _ = _extract_storage(file_storage)
    raw = read_text()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    wb = load_workbook(filename=io.BytesIO(raw), read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h).strip().lower() if h else "" for h in next(rows_iter)]
    rows = []
    for row in rows_iter:
        d = {}
        for i, val in enumerate(row):
            if i < len(headers):
                d[headers[i]] = str(val) if val is not None else ""
        rows.append(d)
    wb.close()
    return headers, rows


def parse_file(file_storage, filename=None):
    _, fname, is_excel = _extract_storage(file_storage, fallback_filename=filename)
    if is_excel:
        return parse_excel_file(file_storage)
    return parse_csv_file(file_storage)


def validate_columns(headers, required):
    lower_headers = [h.strip().lower() for h in headers]
    missing = [c for c in required if c.lower() not in lower_headers]
    return missing


def validate_po_row(row, row_num):
    errors = []
    for col in PO_REQUIRED_COLUMNS:
        val = row.get(col, "").strip() if row.get(col) else ""
        if not val:
            errors.append(f"第{row_num}行: 缺少必填字段 '{col}'")
    if row.get("amount"):
        try:
            float(row["amount"])
        except (ValueError, TypeError):
            errors.append(f"第{row_num}行: 金额格式错误 '{row['amount']}'")
    return errors


def validate_invoice_row(row, row_num):
    errors = []
    for col in INVOICE_REQUIRED_COLUMNS:
        val = row.get(col, "").strip() if row.get(col) else ""
        if not val:
            errors.append(f"第{row_num}行: 缺少必填字段 '{col}'")
    if row.get("amount"):
        try:
            float(row["amount"])
        except (ValueError, TypeError):
            errors.append(f"第{row_num}行: 金额格式错误 '{row['amount']}'")
    return errors


def check_duplicate_invoices(rows):
    seen = {}
    duplicates = []
    for i, row in enumerate(rows, 2):
        inv_num = row.get("invoice_number", "").strip()
        if inv_num in seen:
            duplicates.append(f"发票号重复: '{inv_num}' 出现在第{seen[inv_num]}行和第{i}行")
        else:
            seen[inv_num] = i
    return duplicates


def import_purchase_orders(batch, rows):
    for i, row in enumerate(rows, 2):
        po = PurchaseOrder(
            batch_id=batch.id,
            po_number=row.get("po_number", "").strip(),
            vendor_code=row.get("vendor_code", "").strip(),
            vendor_name=row.get("vendor_name", "").strip(),
            amount=float(row.get("amount", 0)),
            currency=row.get("currency", "CNY").strip(),
            po_date=row.get("po_date", "").strip(),
            raw_data=json.dumps(row, ensure_ascii=False),
        )
        db.session.add(po)


def import_invoices(batch, rows):
    for i, row in enumerate(rows, 2):
        inv = Invoice(
            batch_id=batch.id,
            invoice_number=row.get("invoice_number", "").strip(),
            vendor_code=row.get("vendor_code", "").strip(),
            vendor_name=row.get("vendor_name", "").strip(),
            amount=float(row.get("amount", 0)),
            currency=row.get("currency", "CNY").strip(),
            invoice_date=row.get("invoice_date", "").strip(),
            raw_data=json.dumps(row, ensure_ascii=False),
        )
        db.session.add(inv)


def perform_matching(batch):
    tolerance_pct = batch.tolerance_pct
    tolerance_abs = batch.tolerance_abs
    rule_version = compute_rule_version(tolerance_pct, tolerance_abs)
    batch.rule_version = rule_version

    pos = PurchaseOrder.query.filter_by(batch_id=batch.id).all()
    invoices = Invoice.query.filter_by(batch_id=batch.id).all()

    inv_by_vendor = {}
    for inv in invoices:
        inv_by_vendor.setdefault(inv.vendor_code, []).append(inv)

    matched_inv_ids = set()
    has_exceptions = False

    for po in pos:
        vendor_invs = inv_by_vendor.get(po.vendor_code, [])
        best_match = None
        best_diff = float("inf")
        best_match_type = None

        for inv in vendor_invs:
            if inv.id in matched_inv_ids:
                continue
            diff = abs(po.amount - inv.amount)
            if po.amount == inv.amount:
                if best_match_type != MATCH_TYPE_EXACT:
                    best_match = inv
                    best_diff = diff
                    best_match_type = MATCH_TYPE_EXACT
            elif diff <= tolerance_abs or (po.amount > 0 and diff / po.amount * 100 <= tolerance_pct):
                if best_match_type != MATCH_TYPE_EXACT and diff < best_diff:
                    best_match = inv
                    best_diff = diff
                    best_match_type = MATCH_TYPE_TOLERANCE

        if best_match:
            matched_inv_ids.add(best_match.id)
            is_exc = best_match_type == MATCH_TYPE_TOLERANCE
            mr = MatchResult(
                batch_id=batch.id,
                po_id=po.id,
                invoice_id=best_match.id,
                match_type=best_match_type,
                po_amount=po.amount,
                invoice_amount=best_match.amount,
                amount_diff=best_diff,
                is_exception=is_exc,
                exception_type=EXCEPTION_OVER_TOLERANCE if is_exc else None,
                status=RESULT_STATUS_PENDING,
                rule_version=rule_version,
            )
            db.session.add(mr)
            db.session.flush()
            if is_exc:
                has_exceptions = True
                ei = ExceptionItem(
                    batch_id=batch.id,
                    match_result_id=mr.id,
                    exception_type=EXCEPTION_OVER_TOLERANCE,
                    detail=f"采购单{po.po_number}与发票{best_match.invoice_number}金额差异{best_diff:.2f}，"
                           f"采购金额{po.amount}，发票金额{best_match.amount}",
                    status=EXCEPTION_STATUS_PENDING,
                )
                db.session.add(ei)
        else:
            remaining_invs = [inv for inv in vendor_invs if inv.id not in matched_inv_ids]
            fallback_inv = None
            fallback_diff = float("inf")
            for inv in remaining_invs:
                diff = abs(po.amount - inv.amount)
                if diff < fallback_diff:
                    fallback_inv = inv
                    fallback_diff = diff
            if fallback_inv is not None:
                matched_inv_ids.add(fallback_inv.id)
                mr = MatchResult(
                    batch_id=batch.id,
                    po_id=po.id,
                    invoice_id=fallback_inv.id,
                    match_type=MATCH_TYPE_OVER_TOLERANCE,
                    po_amount=po.amount,
                    invoice_amount=fallback_inv.amount,
                    amount_diff=fallback_diff,
                    is_exception=True,
                    exception_type=EXCEPTION_OVER_TOLERANCE,
                    status=RESULT_STATUS_PENDING,
                    rule_version=rule_version,
                )
                db.session.add(mr)
                db.session.flush()
                has_exceptions = True
                ei = ExceptionItem(
                    batch_id=batch.id,
                    match_result_id=mr.id,
                    exception_type=EXCEPTION_OVER_TOLERANCE,
                    detail=f"采购单{po.po_number}与发票{fallback_inv.invoice_number}金额差异{fallback_diff:.2f}，"
                           f"超出容差（采购金额{po.amount}，发票金额{fallback_inv.amount}）",
                    status=EXCEPTION_STATUS_PENDING,
                )
                db.session.add(ei)
            else:
                mr = MatchResult(
                    batch_id=batch.id,
                    po_id=po.id,
                    invoice_id=None,
                    match_type=MATCH_TYPE_UNMATCHED_PO,
                    po_amount=po.amount,
                    invoice_amount=None,
                    amount_diff=None,
                    is_exception=True,
                    exception_type=EXCEPTION_OVER_TOLERANCE,
                    status=RESULT_STATUS_PENDING,
                    rule_version=rule_version,
                )
                db.session.add(mr)
                db.session.flush()
                has_exceptions = True
                ei = ExceptionItem(
                    batch_id=batch.id,
                    match_result_id=mr.id,
                    exception_type=EXCEPTION_OVER_TOLERANCE,
                    detail=f"采购单{po.po_number}(供应商{po.vendor_code})无匹配发票",
                    status=EXCEPTION_STATUS_PENDING,
                )
                db.session.add(ei)

    for inv in invoices:
        if inv.id not in matched_inv_ids:
            mr = MatchResult(
                batch_id=batch.id,
                po_id=None,
                invoice_id=inv.id,
                match_type=MATCH_TYPE_UNMATCHED_INVOICE,
                po_amount=None,
                invoice_amount=inv.amount,
                amount_diff=None,
                is_exception=True,
                exception_type=EXCEPTION_OVER_TOLERANCE,
                status=RESULT_STATUS_PENDING,
                rule_version=rule_version,
            )
            db.session.add(mr)
            db.session.flush()
            has_exceptions = True
            ei = ExceptionItem(
                batch_id=batch.id,
                match_result_id=mr.id,
                exception_type=EXCEPTION_OVER_TOLERANCE,
                detail=f"发票{inv.invoice_number}(供应商{inv.vendor_code})无匹配采购单",
                status=EXCEPTION_STATUS_PENDING,
            )
            db.session.add(ei)

    return has_exceptions


def process_batch(batch_id):
    batch = Batch.query.get(batch_id)
    if not batch:
        raise ValidationError(["批次不存在"])

    if not batch.can_transition(BATCH_STATUS_VALIDATING):
        raise ValidationError([f"批次状态'{batch.status}'不允许执行匹配操作"])

    batch.status = BATCH_STATUS_VALIDATING
    db.session.flush()

    all_errors = []
    try:
        if not batch.purchase_orders:
            all_errors.append("未上传采购单文件")
        if not batch.invoices:
            all_errors.append("未上传发票文件")

        if all_errors:
            raise ValidationError(all_errors)

        has_exceptions = perform_matching(batch)

        log = AuditLog(
            batch_id=batch.id,
            action="MATCH",
            detail=f"匹配完成，规则版本{batch.rule_version}",
        )
        db.session.add(log)

        if has_exceptions:
            batch.status = BATCH_STATUS_EXCEPTION
        else:
            batch.status = BATCH_STATUS_MATCHED

        db.session.commit()

        generate_payable_recalc_note(batch.id, change_source="MATCH", operator="system")

        return {"success": True, "has_exceptions": has_exceptions}

    except ValidationError:
        batch.status = BATCH_STATUS_FAILED
        log = AuditLog(batch_id=batch.id, action="MATCH_FAILED", detail="; ".join(all_errors))
        db.session.add(log)
        db.session.commit()
        raise
    except Exception as e:
        db.session.rollback()
        batch = Batch.query.get(batch_id)
        batch.status = BATCH_STATUS_FAILED
        log = AuditLog(batch_id=batch.id, action="MATCH_FAILED", detail=str(e))
        db.session.add(log)
        db.session.commit()
        raise ValidationError([str(e)])


def validate_and_import_po(batch_or_id, file_storage_or_content, filename=None):
    batch = batch_or_id if isinstance(batch_or_id, Batch) else Batch.query.get(batch_or_id)
    if isinstance(file_storage_or_content, str):
        file_storage = (file_storage_or_content, filename or batch.po_filename or "po.csv")
    else:
        file_storage = file_storage_or_content
    _, fname, _ = _extract_storage(file_storage, fallback_filename=filename)
    headers, rows = parse_file(file_storage, filename=filename)
    missing = validate_columns(headers, PO_REQUIRED_COLUMNS)
    if missing:
        raise ValidationError([f"采购单文件缺少列: {', '.join(missing)}"])

    row_errors = []
    for i, row in enumerate(rows, 2):
        row_errors.extend(validate_po_row(row, i))

    if row_errors:
        raise ValidationError(row_errors)

    PurchaseOrder.query.filter_by(batch_id=batch.id).delete()
    import_purchase_orders(batch, rows)
    batch.po_filename = fname
    log = AuditLog(batch_id=batch.id, action="UPLOAD_PO", detail=f"上传采购单 {fname}，共{len(rows)}条")
    db.session.add(log)
    db.session.commit()
    return len(rows)


def validate_and_import_invoice(batch_or_id, file_storage_or_content, filename=None):
    batch = batch_or_id if isinstance(batch_or_id, Batch) else Batch.query.get(batch_or_id)
    if isinstance(file_storage_or_content, str):
        file_storage = (file_storage_or_content, filename or batch.invoice_filename or "inv.csv")
    else:
        file_storage = file_storage_or_content
    _, fname, _ = _extract_storage(file_storage, fallback_filename=filename)
    headers, rows = parse_file(file_storage, filename=filename)
    missing = validate_columns(headers, INVOICE_REQUIRED_COLUMNS)
    if missing:
        raise ValidationError([f"发票文件缺少列: {', '.join(missing)}"])

    row_errors = []
    for i, row in enumerate(rows, 2):
        row_errors.extend(validate_invoice_row(row, i))

    dup_errors = check_duplicate_invoices(rows)
    row_errors.extend(dup_errors)

    if row_errors:
        raise ValidationError(row_errors)

    Invoice.query.filter_by(batch_id=batch.id).delete()
    import_invoices(batch, rows)
    batch.invoice_filename = fname
    log = AuditLog(batch_id=batch.id, action="UPLOAD_INVOICE", detail=f"上传发票 {fname}，共{len(rows)}条")
    db.session.add(log)
    db.session.commit()
    return len(rows)


def _compute_payable_total(batch):
    matched = [r for r in batch.match_results if r.match_type in (
        MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE)]
    return round(sum(r.invoice_amount or 0 for r in matched if r.status != RESULT_STATUS_REJECTED), 2)


def _build_result_snapshot(batch):
    """构建匹配结果快照，用于版本间差异对比"""
    mr_snap = {}
    for mr in batch.match_results:
        mr_snap[str(mr.id)] = {
            "po_id": mr.po_id,
            "invoice_id": mr.invoice_id,
            "po_number": mr.po.po_number if mr.po else None,
            "invoice_number": mr.invoice.invoice_number if mr.invoice else None,
            "match_type": mr.match_type,
            "status": mr.status,
            "exception_type": mr.exception_type,
            "remarks": mr.remarks or "",
            "rule_version": mr.rule_version,
            "is_exception": mr.is_exception,
        }
    exc_snap = {}
    for exc in batch.exception_items:
        exc_snap[str(exc.id)] = {
            "match_result_id": exc.match_result_id,
            "exception_type": exc.exception_type,
            "status": exc.status,
            "remarks": exc.remarks or "",
        }
    return {
        "rule_version": batch.rule_version,
        "batch_status": batch.status,
        "match_results": mr_snap,
        "exceptions": exc_snap,
    }


def _collect_affected_documents(batch, prev_note):
    """收集当前版本相对于上一版涉及变化的采购单和发票号。
    - 首次生成（prev_note is None）：返回整批所有单据
    - 后续版本：只返回相对上一版有变化的单据
    """
    po_set = set()
    inv_set = set()

    if prev_note is None or not prev_note.result_snapshot:
        for mr in batch.match_results:
            if mr.po:
                po_set.add(mr.po.po_number)
            if mr.invoice:
                inv_set.add(mr.invoice.invoice_number)
        return sorted(po_set), sorted(inv_set)

    try:
        prev_snap = json.loads(prev_note.result_snapshot)
    except (json.JSONDecodeError, TypeError):
        for mr in batch.match_results:
            if mr.po:
                po_set.add(mr.po.po_number)
            if mr.invoice:
                inv_set.add(mr.invoice.invoice_number)
        return sorted(po_set), sorted(inv_set)

    curr_snap = _build_result_snapshot(batch)
    prev_mr = prev_snap.get("match_results", {})
    curr_mr = curr_snap.get("match_results", {})

    all_mr_ids = set(prev_mr.keys()) | set(curr_mr.keys())
    for mr_id in all_mr_ids:
        prev_item = prev_mr.get(mr_id)
        curr_item = curr_mr.get(mr_id)
        if prev_item != curr_item:
            item = curr_item or prev_item
            if item.get("po_number"):
                po_set.add(item["po_number"])
            if item.get("invoice_number"):
                inv_set.add(item["invoice_number"])

    prev_exc = prev_snap.get("exceptions", {})
    curr_exc = curr_snap.get("exceptions", {})
    all_exc_ids = set(prev_exc.keys()) | set(curr_exc.keys())
    for exc_id in all_exc_ids:
        prev_item = prev_exc.get(exc_id)
        curr_item = curr_exc.get(exc_id)
        if prev_item != curr_item:
            item = curr_item or prev_item
            mr_id = str(item.get("match_result_id")) if item.get("match_result_id") else None
            if mr_id and mr_id in curr_mr:
                if curr_mr[mr_id].get("po_number"):
                    po_set.add(curr_mr[mr_id]["po_number"])
                if curr_mr[mr_id].get("invoice_number"):
                    inv_set.add(curr_mr[mr_id]["invoice_number"])
            elif mr_id and mr_id in prev_mr:
                if prev_mr[mr_id].get("po_number"):
                    po_set.add(prev_mr[mr_id]["po_number"])
                if prev_mr[mr_id].get("invoice_number"):
                    inv_set.add(prev_mr[mr_id]["invoice_number"])

    return sorted(po_set), sorted(inv_set)


def _build_change_summary(batch, prev_note, current_total):
    """生成变化摘要文本"""
    parts = []
    if prev_note is None:
        parts.append(f"首次生成应付说明，应付合计 {current_total:.2f}")
    else:
        diff = round(current_total - prev_note.current_total, 2)
        if diff != 0:
            direction = "增加" if diff > 0 else "减少"
            parts.append(f"应付合计{direction} {abs(diff):.2f}（{prev_note.current_total:.2f} → {current_total:.2f}）")
        else:
            parts.append(f"应付合计不变（{current_total:.2f}）")
        if prev_note.rule_version != batch.rule_version:
            parts.append(f"规则版本变更: {prev_note.rule_version[:8]} → {batch.rule_version[:8]}")
    if not parts:
        parts.append("匹配结果状态或异常处理意见变更")
    return "; ".join(parts)


def generate_payable_recalc_note(batch_id, change_source=None, operator="system"):
    """
    生成应付重算说明。
    - 如果内容哈希与最新版本一致，不生成新记录（去重）
    - 否则生成新版本，并将变化摘要写入操作日志
    - 返回 (note_obj, is_new)
    """
    batch = Batch.query.get(batch_id)
    if not batch:
        return None, False

    content_hash = compute_note_content_hash(batch)

    latest_note = (
        PayableRecalcNote.query.filter_by(batch_id=batch_id)
        .order_by(PayableRecalcNote.version.desc())
        .first()
    )

    if latest_note and latest_note.content_hash == content_hash:
        return latest_note, False

    current_total = _compute_payable_total(batch)
    previous_total = latest_note.current_total if latest_note else None
    amount_diff = round(current_total - previous_total, 2) if previous_total is not None else None

    po_numbers, invoice_numbers = _collect_affected_documents(batch, latest_note)
    change_summary = _build_change_summary(batch, latest_note, current_total)

    new_version = (latest_note.version + 1) if latest_note else 1
    snapshot = _build_result_snapshot(batch)

    note = PayableRecalcNote(
        batch_id=batch_id,
        version=new_version,
        current_total=current_total,
        previous_total=previous_total,
        amount_diff=amount_diff,
        change_source=change_source,
        change_summary=change_summary,
        po_numbers=json.dumps(po_numbers, ensure_ascii=False),
        invoice_numbers=json.dumps(invoice_numbers, ensure_ascii=False),
        rule_version=batch.rule_version,
        content_hash=content_hash,
        result_snapshot=json.dumps(snapshot, ensure_ascii=False),
    )
    db.session.add(note)

    action = "RECALC_NOTE_V1" if new_version == 1 else f"RECALC_NOTE_V{new_version}"
    log = AuditLog(
        batch_id=batch_id,
        action=action,
        detail=f"应付重算说明 v{new_version}: {change_summary}",
        operator=operator,
    )
    db.session.add(log)
    db.session.commit()
    return note, True


def get_latest_recalc_note(batch_id):
    """获取指定批次最新的应付重算说明"""
    return (
        PayableRecalcNote.query.filter_by(batch_id=batch_id)
        .order_by(PayableRecalcNote.version.desc())
        .first()
    )


def list_recalc_notes(batch_id):
    """列出指定批次所有应付重算说明（按版本升序）"""
    return (
        PayableRecalcNote.query.filter_by(batch_id=batch_id)
        .order_by(PayableRecalcNote.version.asc())
        .all()
    )
