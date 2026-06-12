import csv
import io
import json
from datetime import datetime, timezone
from models import (
    db, Batch, PurchaseOrder, Invoice, MatchResult, ExceptionItem,
    ToleranceHistory, AuditLog, PayableRecalcNote, NoteComparison,
    BATCH_STATUS_VALIDATING, BATCH_STATUS_MATCHED, BATCH_STATUS_EXCEPTION,
    BATCH_STATUS_FAILED, BATCH_STATUS_CREATED,
    MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE,
    MATCH_TYPE_UNMATCHED_PO, MATCH_TYPE_UNMATCHED_INVOICE,
    EXCEPTION_MISSING_FIELD, EXCEPTION_OVER_TOLERANCE,
    EXCEPTION_DUPLICATE_INVOICE,
    EXCEPTION_STATUS_PENDING, RESULT_STATUS_PENDING, RESULT_STATUS_REJECTED,
    REVIEW_STATUS_PENDING, REVIEW_STATUS_CONFIRMED, REVIEW_STATUS_IGNORED,
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


def _get_doc_sets_from_snapshot(snapshot):
    """从快照中提取采购单和发票的集合（用于对比增删）"""
    po_set = set()
    inv_set = set()
    mr_map = {}
    exc_map = {}
    mr_id_to_docs = {}

    if not snapshot:
        return po_set, inv_set, mr_map, exc_map, mr_id_to_docs

    try:
        snap = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
    except (json.JSONDecodeError, TypeError):
        return po_set, inv_set, mr_map, exc_map, mr_id_to_docs

    for mr_id, mr in snap.get("match_results", {}).items():
        po_num = mr.get("po_number")
        inv_num = mr.get("invoice_number")
        if po_num:
            po_set.add(po_num)
        if inv_num:
            inv_set.add(inv_num)
        key = f"{po_num}:{inv_num}"
        mr_map[key] = mr
        mr_id_to_docs[str(mr.get("po_id") or "")] = (po_num, inv_num)
        mr_id_to_docs[str(mr.get("invoice_id") or "")] = (po_num, inv_num)
        mr_id_to_docs[str(mr_id)] = (po_num, inv_num)

    for exc_id, exc in snap.get("exceptions", {}).items():
        exc_map[exc_id] = exc

    return po_set, inv_set, mr_map, exc_map, mr_id_to_docs


def _find_changed_docs(a_map, b_map, a_exc_map, b_exc_map, a_mr_id_to_docs, b_mr_id_to_docs, doc_type="po"):
    """找出在两个版本中都存在但状态/备注/类型等发生变化的单据"""
    changed = set()
    num_key = "po_number" if doc_type == "po" else "invoice_number"

    for key in a_map:
        if key in b_map:
            a_item = a_map[key]
            b_item = b_map[key]
            if a_item != b_item:
                num = a_item.get(num_key) or b_item.get(num_key)
                if num:
                    changed.add(num)

    all_exc_ids = set(a_exc_map.keys()) | set(b_exc_map.keys())
    for exc_id in all_exc_ids:
        a_exc = a_exc_map.get(exc_id)
        b_exc = b_exc_map.get(exc_id)
        if a_exc != b_exc:
            exc = b_exc or a_exc
            mr_id = str(exc.get("match_result_id") or "")
            docs = a_mr_id_to_docs.get(mr_id) or b_mr_id_to_docs.get(mr_id)
            if docs:
                po_num, inv_num = docs
                if doc_type == "po" and po_num:
                    changed.add(po_num)
                elif doc_type == "invoice" and inv_num:
                    changed.add(inv_num)

    return sorted(changed)


def compare_notes(batch_id, note_a_id, note_b_id, operator="system"):
    """
    比较同一批次内两个应付重算说明版本的差异。
    
    返回 (comparison_obj, error_message)。
    如果有错误，error_message 不为 None，comparison_obj 为 None。
    """
    batch = Batch.query.get(batch_id)
    if not batch:
        return None, "批次不存在"

    note_a = PayableRecalcNote.query.get(note_a_id)
    if not note_a:
        return None, f"版本 {note_a_id} 不存在"

    note_b = PayableRecalcNote.query.get(note_b_id)
    if not note_b:
        return None, f"版本 {note_b_id} 不存在"

    if note_a.batch_id != batch_id:
        return None, f"版本 {note_a_id} 不属于批次 {batch_id}"

    if note_b.batch_id != batch_id:
        return None, f"版本 {note_b_id} 不属于批次 {batch_id}"

    if note_a_id == note_b_id:
        return None, "不能对比同一版本"

    a_po, a_inv, a_map, a_exc_map, a_mr_id_to_docs = _get_doc_sets_from_snapshot(note_a.result_snapshot)
    b_po, b_inv, b_map, b_exc_map, b_mr_id_to_docs = _get_doc_sets_from_snapshot(note_b.result_snapshot)

    po_added = sorted(b_po - a_po)
    po_removed = sorted(a_po - b_po)
    po_changed = _find_changed_docs(a_map, b_map, a_exc_map, b_exc_map, a_mr_id_to_docs, b_mr_id_to_docs, doc_type="po")

    invoice_added = sorted(b_inv - a_inv)
    invoice_removed = sorted(a_inv - b_inv)
    invoice_changed = _find_changed_docs(a_map, b_map, a_exc_map, b_exc_map, a_mr_id_to_docs, b_mr_id_to_docs, doc_type="invoice")

    amount_diff = round(note_b.current_total - note_a.current_total, 2)

    change_sources = []
    if po_added:
        change_sources.append(f"采购单新增{len(po_added)}条")
    if po_removed:
        change_sources.append(f"采购单移除{len(po_removed)}条")
    if po_changed:
        change_sources.append(f"采购单变更{len(po_changed)}条")
    if invoice_added:
        change_sources.append(f"发票新增{len(invoice_added)}条")
    if invoice_removed:
        change_sources.append(f"发票移除{len(invoice_removed)}条")
    if invoice_changed:
        change_sources.append(f"发票变更{len(invoice_changed)}条")
    if note_a.rule_version != note_b.rule_version:
        change_sources.append(f"规则版本变更")
    change_source = "; ".join(change_sources) if change_sources else "无变化"

    summary_parts = []
    if amount_diff > 0:
        summary_parts.append(f"应付合计增加 {abs(amount_diff):.2f}")
    elif amount_diff < 0:
        summary_parts.append(f"应付合计减少 {abs(amount_diff):.2f}")
    else:
        summary_parts.append("应付合计不变")
    summary_parts.append(f"(v{note_a.version} → v{note_b.version})")
    if change_sources:
        summary_parts.append(f"变化来源: {change_source}")
    comparison_summary = " ".join(summary_parts)

    detail = json.dumps({
        "note_a": {
            "version": note_a.version,
            "current_total": note_a.current_total,
            "rule_version": note_a.rule_version,
            "change_source": note_a.change_source,
        },
        "note_b": {
            "version": note_b.version,
            "current_total": note_b.current_total,
            "rule_version": note_b.rule_version,
            "change_source": note_b.change_source,
        },
        "diff": {
            "po_added": po_added,
            "po_removed": po_removed,
            "po_changed": po_changed,
            "invoice_added": invoice_added,
            "invoice_removed": invoice_removed,
            "invoice_changed": invoice_changed,
        },
    }, ensure_ascii=False)

    comparison = NoteComparison(
        batch_id=batch_id,
        note_a_id=note_a_id,
        note_b_id=note_b_id,
        note_a_version=note_a.version,
        note_b_version=note_b.version,
        amount_diff=amount_diff,
        change_source=change_source,
        po_added=json.dumps(po_added, ensure_ascii=False),
        po_removed=json.dumps(po_removed, ensure_ascii=False),
        po_changed=json.dumps(po_changed, ensure_ascii=False),
        invoice_added=json.dumps(invoice_added, ensure_ascii=False),
        invoice_removed=json.dumps(invoice_removed, ensure_ascii=False),
        invoice_changed=json.dumps(invoice_changed, ensure_ascii=False),
        rule_version_a=note_a.rule_version,
        rule_version_b=note_b.rule_version,
        operator=operator,
        comparison_summary=comparison_summary,
        detail=detail,
    )
    db.session.add(comparison)

    log = AuditLog(
        batch_id=batch_id,
        action="COMPARE_NOTES",
        detail=f"对比应付说明 v{note_a.version} vs v{note_b.version}: {comparison_summary}",
        operator=operator,
    )
    db.session.add(log)
    db.session.commit()

    return comparison, None


def get_latest_comparison(batch_id):
    """获取指定批次最新的版本对比结果"""
    return (
        NoteComparison.query.filter_by(batch_id=batch_id)
        .order_by(NoteComparison.created_at.desc())
        .first()
    )


def list_comparisons(batch_id):
    """列出指定批次所有版本对比结果（按时间降序）"""
    return (
        NoteComparison.query.filter_by(batch_id=batch_id)
        .order_by(NoteComparison.created_at.desc())
        .all()
    )


def get_comparison(comparison_id):
    """按ID获取版本对比结果"""
    return NoteComparison.query.get(comparison_id)


def list_comparisons_with_filter(batch_id, review_status=None):
    """列出指定批次的版本对比结果，支持按复核状态筛选"""
    query = NoteComparison.query.filter_by(batch_id=batch_id)
    if review_status:
        query = query.filter_by(review_status=review_status)
    return query.order_by(NoteComparison.created_at.desc()).all()


def get_latest_confirmed_comparison(batch_id):
    """获取指定批次最近一次已确认的版本对比"""
    return (
        NoteComparison.query.filter_by(batch_id=batch_id, review_status=REVIEW_STATUS_CONFIRMED)
        .order_by(NoteComparison.reviewed_at.desc())
        .first()
    )


def update_comparison_review(comparison_id, review_status, review_remark=None, operator="user"):
    """
    更新对比记录的复核状态和备注。

    返回 (comparison_obj, error_message)。
    冲突场景：
    - 对比记录不存在 → 400
    - 已确认后再确认 → 400（重复确认）
    - 已忽略后再确认 → 400（需先恢复待复核？这里按需求直接返回冲突）
    """
    comparison = NoteComparison.query.get(comparison_id)
    if not comparison:
        return None, "对比记录不存在"

    if review_status == REVIEW_STATUS_CONFIRMED:
        if comparison.review_status == REVIEW_STATUS_CONFIRMED:
            return None, "该对比记录已确认，不允许重复确认"
        if comparison.review_status == REVIEW_STATUS_IGNORED:
            return None, "该对比记录已忽略，不允许直接确认"

    comparison.review_status = review_status
    comparison.review_remark = review_remark
    comparison.reviewed_by = operator
    comparison.reviewed_at = datetime.now(timezone.utc)

    status_label = {
        REVIEW_STATUS_PENDING: "待复核",
        REVIEW_STATUS_CONFIRMED: "已确认",
        REVIEW_STATUS_IGNORED: "已忽略",
    }.get(review_status, review_status)

    log = AuditLog(
        batch_id=comparison.batch_id,
        action=f"REVIEW_COMPARISON_{review_status}",
        detail=f"对比记录 #{comparison_id} 复核状态更新为 {status_label}"
               + (f"，备注: {review_remark}" if review_remark else ""),
        operator=operator,
    )
    db.session.add(log)
    db.session.commit()

    return comparison, None
