import json
import hashlib
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

BATCH_STATUS_CREATED = "CREATED"
BATCH_STATUS_VALIDATING = "VALIDATING"
BATCH_STATUS_MATCHED = "MATCHED"
BATCH_STATUS_EXCEPTION = "EXCEPTION_PENDING"
BATCH_STATUS_CONFIRMED = "CONFIRMED"
BATCH_STATUS_POSTED = "POSTED"
BATCH_STATUS_ROLLED_BACK = "ROLLED_BACK"
BATCH_STATUS_FAILED = "FAILED"

VALID_TRANSITIONS = {
    BATCH_STATUS_CREATED: [BATCH_STATUS_VALIDATING, BATCH_STATUS_FAILED],
    BATCH_STATUS_VALIDATING: [BATCH_STATUS_MATCHED, BATCH_STATUS_EXCEPTION, BATCH_STATUS_FAILED],
    BATCH_STATUS_MATCHED: [BATCH_STATUS_EXCEPTION, BATCH_STATUS_CONFIRMED, BATCH_STATUS_FAILED],
    BATCH_STATUS_EXCEPTION: [BATCH_STATUS_CONFIRMED, BATCH_STATUS_FAILED],
    BATCH_STATUS_CONFIRMED: [BATCH_STATUS_POSTED, BATCH_STATUS_FAILED],
    BATCH_STATUS_POSTED: [BATCH_STATUS_ROLLED_BACK],
    BATCH_STATUS_ROLLED_BACK: [BATCH_STATUS_CREATED],
    BATCH_STATUS_FAILED: [BATCH_STATUS_CREATED],
}

MATCH_TYPE_EXACT = "EXACT"
MATCH_TYPE_TOLERANCE = "TOLERANCE"
MATCH_TYPE_OVER_TOLERANCE = "OVER_TOLERANCE"
MATCH_TYPE_UNMATCHED_PO = "UNMATCHED_PO"
MATCH_TYPE_UNMATCHED_INVOICE = "UNMATCHED_INVOICE"

EXCEPTION_MISSING_FIELD = "MISSING_FIELD"
EXCEPTION_OVER_TOLERANCE = "OVER_TOLERANCE"
EXCEPTION_DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
EXCEPTION_DUPLICATE_ROLLBACK = "DUPLICATE_ROLLBACK"

EXCEPTION_STATUS_PENDING = "PENDING"
EXCEPTION_STATUS_RESOLVED = "RESOLVED"
EXCEPTION_STATUS_REJECTED = "REJECTED"

RESULT_STATUS_PENDING = "PENDING"
RESULT_STATUS_CONFIRMED = "CONFIRMED"
RESULT_STATUS_REJECTED = "REJECTED"

REVIEW_STATUS_PENDING = "PENDING"
REVIEW_STATUS_CONFIRMED = "CONFIRMED"
REVIEW_STATUS_IGNORED = "IGNORED"

DRAFT_STATUS_PENDING = "PENDING"
DRAFT_STATUS_CONFIRMED = "CONFIRMED"
DRAFT_STATUS_DISCARDED = "DISCARDED"
DRAFT_FILE_TYPE_PO = "PO"
DRAFT_FILE_TYPE_INVOICE = "INVOICE"

PRECHECK_ERROR = "ERROR"
PRECHECK_WARNING = "WARNING"
PRECHECK_INFO = "INFO"


def compute_rule_version(tolerance_pct, tolerance_abs):
    raw = f"{tolerance_pct}:{tolerance_abs}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


class Batch(db.Model):
    __tablename__ = "batches"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(30), nullable=False, default=BATCH_STATUS_CREATED)
    tolerance_pct = db.Column(db.Float, nullable=False, default=2.0)
    tolerance_abs = db.Column(db.Float, nullable=False, default=100.0)
    rule_version = db.Column(db.String(50), nullable=False)
    po_filename = db.Column(db.String(500))
    invoice_filename = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    purchase_orders = db.relationship("PurchaseOrder", backref="batch", cascade="all, delete-orphan")
    invoices = db.relationship("Invoice", backref="batch", cascade="all, delete-orphan")
    match_results = db.relationship("MatchResult", backref="batch", cascade="all, delete-orphan")
    exception_items = db.relationship("ExceptionItem", backref="batch", cascade="all, delete-orphan")
    tolerance_history = db.relationship("ToleranceHistory", backref="batch", cascade="all, delete-orphan")
    audit_logs = db.relationship("AuditLog", backref="batch", cascade="all, delete-orphan")

    def can_transition(self, new_status):
        allowed = VALID_TRANSITIONS.get(self.status, [])
        return new_status in allowed

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "po_filename": self.po_filename,
            "invoice_filename": self.invoice_filename,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @property
    def summary(self):
        matched = [r for r in self.match_results if r.match_type in (MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE)]
        unmatched_po = [r for r in self.match_results if r.match_type == MATCH_TYPE_UNMATCHED_PO]
        unmatched_inv = [r for r in self.match_results if r.match_type == MATCH_TYPE_UNMATCHED_INVOICE]
        exceptions = [e for e in self.exception_items if e.status == EXCEPTION_STATUS_PENDING]
        payable_total = sum(r.invoice_amount or 0 for r in matched if r.status != RESULT_STATUS_REJECTED)
        return {
            "matched_count": len(matched),
            "unmatched_po_count": len(unmatched_po),
            "unmatched_invoice_count": len(unmatched_inv),
            "exception_count": len(exceptions),
            "payable_total": round(payable_total, 2),
        }


class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    po_number = db.Column(db.String(100), nullable=False)
    vendor_code = db.Column(db.String(100), nullable=False)
    vendor_name = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default="CNY")
    po_date = db.Column(db.String(20), nullable=False)
    raw_data = db.Column(db.Text)

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "po_number": self.po_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "amount": self.amount,
            "currency": self.currency,
            "po_date": self.po_date,
        }


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    invoice_number = db.Column(db.String(100), nullable=False)
    vendor_code = db.Column(db.String(100), nullable=False)
    vendor_name = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default="CNY")
    invoice_date = db.Column(db.String(20), nullable=False)
    raw_data = db.Column(db.Text)

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "invoice_number": self.invoice_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "amount": self.amount,
            "currency": self.currency,
            "invoice_date": self.invoice_date,
        }


class MatchResult(db.Model):
    __tablename__ = "match_results"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    match_type = db.Column(db.String(30), nullable=False)
    po_amount = db.Column(db.Float)
    invoice_amount = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    is_exception = db.Column(db.Boolean, default=False)
    exception_type = db.Column(db.String(50))
    remarks = db.Column(db.Text)
    status = db.Column(db.String(20), default=RESULT_STATUS_PENDING)
    rule_version = db.Column(db.String(50))

    po = db.relationship("PurchaseOrder")
    invoice = db.relationship("Invoice")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "po_id": self.po_id,
            "invoice_id": self.invoice_id,
            "match_type": self.match_type,
            "po_number": self.po.po_number if self.po else None,
            "invoice_number": self.invoice.invoice_number if self.invoice else None,
            "vendor_code": self.po.vendor_code if self.po else (self.invoice.vendor_code if self.invoice else None),
            "vendor_name": self.po.vendor_name if self.po else (self.invoice.vendor_name if self.invoice else None),
            "po_amount": self.po_amount,
            "invoice_amount": self.invoice_amount,
            "amount_diff": self.amount_diff,
            "is_exception": self.is_exception,
            "exception_type": self.exception_type,
            "remarks": self.remarks,
            "status": self.status,
            "rule_version": self.rule_version,
        }


class ExceptionItem(db.Model):
    __tablename__ = "exception_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    match_result_id = db.Column(db.Integer, db.ForeignKey("match_results.id"))
    exception_type = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.Text)
    remarks = db.Column(db.Text)
    status = db.Column(db.String(20), default=EXCEPTION_STATUS_PENDING)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    match_result = db.relationship("MatchResult")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "match_result_id": self.match_result_id,
            "exception_type": self.exception_type,
            "detail": self.detail,
            "remarks": self.remarks,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ToleranceHistory(db.Model):
    __tablename__ = "tolerance_history"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    tolerance_pct = db.Column(db.Float, nullable=False)
    tolerance_abs = db.Column(db.Float, nullable=False)
    rule_version = db.Column(db.String(50), nullable=False)
    changed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    changed_by = db.Column(db.String(100), default="system")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
            "changed_by": self.changed_by,
        }


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    detail = db.Column(db.Text)
    operator = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "action": self.action,
            "detail": self.detail,
            "operator": self.operator,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PayableRecalcNote(db.Model):
    __tablename__ = "payable_recalc_notes"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    current_total = db.Column(db.Float, nullable=False, default=0.0)
    previous_total = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    change_source = db.Column(db.String(200))
    change_summary = db.Column(db.Text)
    po_numbers = db.Column(db.Text)
    invoice_numbers = db.Column(db.Text)
    rule_version = db.Column(db.String(50))
    content_hash = db.Column(db.String(64), nullable=False)
    result_snapshot = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="recalc_notes")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "version": self.version,
            "current_total": round(self.current_total, 2),
            "previous_total": round(self.previous_total, 2) if self.previous_total is not None else None,
            "amount_diff": round(self.amount_diff, 2) if self.amount_diff is not None else None,
            "change_source": self.change_source,
            "change_summary": self.change_summary,
            "po_numbers": json.loads(self.po_numbers) if self.po_numbers else [],
            "invoice_numbers": json.loads(self.invoice_numbers) if self.invoice_numbers else [],
            "rule_version": self.rule_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def compute_note_content_hash(batch):
    """基于批次状态、异常状态、备注、匹配结果、规则版本生成哈希，用于去重判断"""
    parts = []
    parts.append(f"rule:{batch.rule_version}")
    parts.append(f"status:{batch.status}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:{mr.status}:"
            f"{mr.rule_version}:{mr.exception_type}:{mr.remarks or ''}"
        )
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        parts.append(
            f"exc:{exc.match_result_id}:{exc.exception_type}:{exc.status}:{exc.remarks or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class NoteComparison(db.Model):
    __tablename__ = "note_comparisons"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    note_a_id = db.Column(db.Integer, db.ForeignKey("payable_recalc_notes.id"), nullable=False)
    note_b_id = db.Column(db.Integer, db.ForeignKey("payable_recalc_notes.id"), nullable=False)
    note_a_version = db.Column(db.Integer, nullable=False)
    note_b_version = db.Column(db.Integer, nullable=False)
    amount_diff = db.Column(db.Float, nullable=False, default=0.0)
    change_source = db.Column(db.Text)
    po_added = db.Column(db.Text)
    po_removed = db.Column(db.Text)
    po_changed = db.Column(db.Text)
    invoice_added = db.Column(db.Text)
    invoice_removed = db.Column(db.Text)
    invoice_changed = db.Column(db.Text)
    rule_version_a = db.Column(db.String(50))
    rule_version_b = db.Column(db.String(50))
    operator = db.Column(db.String(100), default="system")
    comparison_summary = db.Column(db.Text)
    detail = db.Column(db.Text)
    review_status = db.Column(db.String(20), default=REVIEW_STATUS_PENDING)
    review_remark = db.Column(db.Text)
    reviewed_by = db.Column(db.String(100))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="note_comparisons")
    note_a = db.relationship("PayableRecalcNote", foreign_keys=[note_a_id])
    note_b = db.relationship("PayableRecalcNote", foreign_keys=[note_b_id])

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "note_a_id": self.note_a_id,
            "note_b_id": self.note_b_id,
            "note_a_version": self.note_a_version,
            "note_b_version": self.note_b_version,
            "amount_diff": round(self.amount_diff, 2),
            "change_source": self.change_source,
            "po_added": json.loads(self.po_added) if self.po_added else [],
            "po_removed": json.loads(self.po_removed) if self.po_removed else [],
            "po_changed": json.loads(self.po_changed) if self.po_changed else [],
            "invoice_added": json.loads(self.invoice_added) if self.invoice_added else [],
            "invoice_removed": json.loads(self.invoice_removed) if self.invoice_removed else [],
            "invoice_changed": json.loads(self.invoice_changed) if self.invoice_changed else [],
            "rule_version_a": self.rule_version_a,
            "rule_version_b": self.rule_version_b,
            "operator": self.operator,
            "comparison_summary": self.comparison_summary,
            "review_status": self.review_status,
            "review_remark": self.review_remark,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ImportDraft(db.Model):
    __tablename__ = "import_drafts"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    file_type = db.Column(db.String(10), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=DRAFT_STATUS_PENDING)
    row_count = db.Column(db.Integer, default=0)
    valid_row_count = db.Column(db.Integer, default=0)
    error_count = db.Column(db.Integer, default=0)
    warning_count = db.Column(db.Integer, default=0)
    tolerance_pct = db.Column(db.Float, nullable=False)
    tolerance_abs = db.Column(db.Float, nullable=False)
    rule_version = db.Column(db.String(50), nullable=False)
    file_content = db.Column(db.Text, nullable=False)
    file_hash = db.Column(db.String(64), nullable=False)
    parsed_data = db.Column(db.Text)
    precheck_report = db.Column(db.Text)
    operator = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="import_drafts")
    issues = db.relationship("ImportDraftIssue", backref="draft", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "file_type": self.file_type,
            "filename": self.filename,
            "status": self.status,
            "row_count": self.row_count,
            "valid_row_count": self.valid_row_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "file_hash": self.file_hash,
            "operator": self.operator,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "precheck_report": json.loads(self.precheck_report) if self.precheck_report else None,
            "issues": [i.to_dict() for i in self.issues],
        }


class ImportDraftIssue(db.Model):
    __tablename__ = "import_draft_issues"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    draft_id = db.Column(db.Integer, db.ForeignKey("import_drafts.id"), nullable=False)
    issue_type = db.Column(db.String(10), nullable=False)
    issue_code = db.Column(db.String(50), nullable=False)
    row_number = db.Column(db.Integer)
    column_name = db.Column(db.String(100))
    message = db.Column(db.Text, nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "draft_id": self.draft_id,
            "issue_type": self.issue_type,
            "issue_code": self.issue_code,
            "row_number": self.row_number,
            "column_name": self.column_name,
            "message": self.message,
            "detail": json.loads(self.detail) if self.detail else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def init_db(app):
    if "sqlalchemy" not in app.extensions:
        db.init_app(app)
    with app.app_context():
        db.create_all()
