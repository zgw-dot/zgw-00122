import csv
import io
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
DRAFT_STATUS_CONFLICT = "CONFLICT"
DRAFT_STATUS_CANCELLED = "CANCELLED"
DRAFT_FILE_TYPE_PO = "PO"
DRAFT_FILE_TYPE_INVOICE = "INVOICE"

PLAN_STATUS_PENDING = "PENDING"
PLAN_STATUS_CONFIRMED = "CONFIRMED"
PLAN_STATUS_CANCELLED = "CANCELLED"
PLAN_STATUS_UNDONE = "UNDONE"

ROW_ACTION_ADD = "ADD"
ROW_ACTION_OVERWRITE = "OVERWRITE"
ROW_ACTION_SKIP = "SKIP"
ROW_ACTION_CONFLICT = "CONFLICT"

DRAFT_EXPIRE_HOURS = 24

PRECHECK_ERROR = "ERROR"
PRECHECK_WARNING = "WARNING"
PRECHECK_INFO = "INFO"

HEALTH_SEVERITY_BLOCKER = "BLOCKER"
HEALTH_SEVERITY_WARNING = "WARNING"
HEALTH_SEVERITY_INFO = "INFO"

HANDOVER_STATUS_DRAFT = "DRAFT"
HANDOVER_STATUS_COMPLETED = "COMPLETED"
HANDOVER_STATUS_VOID = "VOID"

HANDOVER_ALLOWED_TRANSITIONS = {
    HANDOVER_STATUS_DRAFT: [HANDOVER_STATUS_COMPLETED, HANDOVER_STATUS_VOID],
    HANDOVER_STATUS_COMPLETED: [],
    HANDOVER_STATUS_VOID: [],
}

HANDOVER_ROLE_ADMIN = "admin"
HANDOVER_ROLE_FINANCE_LEAD = "finance_lead"
HANDOVER_ROLE_FINANCE = "finance"
HANDOVER_ROLE_VIEWER = "viewer"

HANDOVER_PERMISSION_COMPLETE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
HANDOVER_PERMISSION_VOID = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}

RELEASE_STATUS_DRAFT = "DRAFT"
RELEASE_STATUS_PENDING = "PENDING"
RELEASE_STATUS_APPROVED = "APPROVED"
RELEASE_STATUS_REJECTED = "REJECTED"
RELEASE_STATUS_REVOKED = "REVOKED"
RELEASE_STATUS_EXPIRED = "EXPIRED"

RELEASE_ALLOWED_TRANSITIONS = {
    RELEASE_STATUS_DRAFT: [RELEASE_STATUS_PENDING, RELEASE_STATUS_REVOKED],
    RELEASE_STATUS_PENDING: [RELEASE_STATUS_APPROVED, RELEASE_STATUS_REJECTED, RELEASE_STATUS_REVOKED, RELEASE_STATUS_EXPIRED],
    RELEASE_STATUS_APPROVED: [RELEASE_STATUS_REVOKED],
    RELEASE_STATUS_REJECTED: [],
    RELEASE_STATUS_REVOKED: [],
    RELEASE_STATUS_EXPIRED: [],
}

REHEARSAL_STATUS_ACTIVE = "ACTIVE"
REHEARSAL_STATUS_STALE = "STALE"
REHEARSAL_STATUS_VOID = "VOID"

REHEARSAL_PERMISSION_VOID = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
REHEARSAL_PERMISSION_CREATE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE}
REHEARSAL_PERMISSION_VIEW = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE, HANDOVER_ROLE_VIEWER}

ARCHIVE_STATUS_ACTIVE = "ACTIVE"
ARCHIVE_STATUS_STALE = "STALE"
ARCHIVE_STATUS_SEALED = "SEALED"
ARCHIVE_STATUS_VOID = "VOID"

ARCHIVE_ALLOWED_TRANSITIONS = {
    ARCHIVE_STATUS_ACTIVE: [ARCHIVE_STATUS_SEALED, ARCHIVE_STATUS_VOID, ARCHIVE_STATUS_STALE],
    ARCHIVE_STATUS_STALE: [ARCHIVE_STATUS_SEALED, ARCHIVE_STATUS_VOID],
    ARCHIVE_STATUS_SEALED: [],
    ARCHIVE_STATUS_VOID: [],
}

ARCHIVE_PERMISSION_SEAL = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
ARCHIVE_PERMISSION_VOID = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
ARCHIVE_PERMISSION_CREATE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE}
ARCHIVE_PERMISSION_VIEW = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE, HANDOVER_ROLE_VIEWER}

RELEASE_PERMISSION_APPROVE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
RELEASE_PERMISSION_REJECT = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
RELEASE_PERMISSION_REVOKE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD}
RELEASE_PERMISSION_CREATE = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE}
RELEASE_PERMISSION_VIEW = {HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE, HANDOVER_ROLE_VIEWER}

HEALTH_RULE_DUPLICATE_PO = "duplicate_po_number"
HEALTH_RULE_DUPLICATE_INVOICE = "duplicate_invoice_number"
HEALTH_RULE_MISSING_COLUMNS = "missing_required_columns"
HEALTH_RULE_NEGATIVE_AMOUNT = "negative_amount"
HEALTH_RULE_VENDOR_MISMATCH = "vendor_mismatch"
HEALTH_RULE_CONFIRMED_OVERRIDE_RISK = "confirmed_override_risk"

DEFAULT_HEALTH_RULES = {
    HEALTH_RULE_DUPLICATE_PO: {"enabled": True, "severity": HEALTH_SEVERITY_BLOCKER, "threshold": 1},
    HEALTH_RULE_DUPLICATE_INVOICE: {"enabled": True, "severity": HEALTH_SEVERITY_BLOCKER, "threshold": 1},
    HEALTH_RULE_MISSING_COLUMNS: {"enabled": True, "severity": HEALTH_SEVERITY_BLOCKER, "threshold": 1},
    HEALTH_RULE_NEGATIVE_AMOUNT: {"enabled": True, "severity": HEALTH_SEVERITY_WARNING, "threshold": 0},
    HEALTH_RULE_VENDOR_MISMATCH: {"enabled": True, "severity": HEALTH_SEVERITY_WARNING, "threshold": 1},
    HEALTH_RULE_CONFIRMED_OVERRIDE_RISK: {"enabled": True, "severity": HEALTH_SEVERITY_INFO, "threshold": 1},
}


def compute_health_rule_version(rules_dict):
    raw = json.dumps(rules_dict, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


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
    health_check_rules = db.relationship("HealthCheckRule", backref="batch", cascade="all, delete-orphan")

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
    plan_id = db.Column(db.Integer, db.ForeignKey("import_plans.id"), nullable=True)
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
    diff_analysis = db.Column(db.Text)
    conflict_reason = db.Column(db.Text)
    review_summary = db.Column(db.Text)
    confirmed_by = db.Column(db.String(100))
    confirmed_at = db.Column(db.DateTime)
    operator = db.Column(db.String(100), default="system")
    superseded_by_draft_id = db.Column(db.Integer)
    supersedes_draft_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="import_drafts")
    issues = db.relationship("ImportDraftIssue", backref="draft", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "plan_id": self.plan_id,
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
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "superseded_by_draft_id": self.superseded_by_draft_id,
            "supersedes_draft_id": self.supersedes_draft_id,
            "conflict_reason": self.conflict_reason,
            "review_summary": self.review_summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "precheck_report": json.loads(self.precheck_report) if self.precheck_report else None,
            "diff_analysis": json.loads(self.diff_analysis) if self.diff_analysis else None,
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


class ImportPlan(db.Model):
    __tablename__ = "import_plans"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=PLAN_STATUS_PENDING)
    plan_summary = db.Column(db.Text)
    confirmed_by = db.Column(db.String(100))
    confirmed_at = db.Column(db.DateTime)
    cancelled_by = db.Column(db.String(100))
    cancelled_at = db.Column(db.DateTime)
    undone_by = db.Column(db.String(100))
    undone_at = db.Column(db.DateTime)
    undo_of_plan_id = db.Column(db.Integer)
    operator = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="import_plans")
    drafts = db.relationship("ImportDraft", backref="plan", foreign_keys="ImportDraft.plan_id")
    snapshots = db.relationship("PlanSnapshot", backref="plan", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "status": self.status,
            "plan_summary": json.loads(self.plan_summary) if self.plan_summary else None,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "cancelled_by": self.cancelled_by,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "undone_by": self.undone_by,
            "undone_at": self.undone_at.isoformat() if self.undone_at else None,
            "undo_of_plan_id": self.undo_of_plan_id,
            "operator": self.operator,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "drafts": [d.to_dict() for d in self.drafts],
        }


class PlanSnapshot(db.Model):
    __tablename__ = "plan_snapshots"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("import_plans.id"), nullable=False)
    table_name = db.Column(db.String(50), nullable=False)
    row_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)
    original_data = db.Column(db.Text)
    restored = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "table_name": self.table_name,
            "row_id": self.row_id,
            "action": self.action,
            "original_data": json.loads(self.original_data) if self.original_data else None,
            "restored": self.restored,
        }


class HealthCheckRule(db.Model):
    __tablename__ = "health_check_rules"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    rule_key = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    severity = db.Column(db.String(20), nullable=False, default=HEALTH_SEVERITY_WARNING)
    threshold = db.Column(db.Float, default=0.0)
    rule_version = db.Column(db.String(50), nullable=False)
    updated_by = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (db.UniqueConstraint("batch_id", "rule_key", name="uq_batch_rule"),)

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "rule_key": self.rule_key,
            "enabled": self.enabled,
            "severity": self.severity,
            "threshold": self.threshold,
            "rule_version": self.rule_version,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class HealthCheckHistory(db.Model):
    __tablename__ = "health_check_history"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    rule_version = db.Column(db.String(50), nullable=False)
    operator = db.Column(db.String(100), default="system")
    source_files = db.Column(db.Text)
    summary = db.Column(db.Text)
    blocker_count = db.Column(db.Integer, default=0)
    warning_count = db.Column(db.Integer, default=0)
    info_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="health_check_history")
    results = db.relationship("HealthCheckResult", backref="history", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "rule_version": self.rule_version,
            "operator": self.operator,
            "source_files": json.loads(self.source_files) if self.source_files else [],
            "summary": json.loads(self.summary) if self.summary else None,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class HealthCheckResult(db.Model):
    __tablename__ = "health_check_results"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    history_id = db.Column(db.Integer, db.ForeignKey("health_check_history.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    rule_key = db.Column(db.String(100), nullable=False)
    severity = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(50))
    message = db.Column(db.Text, nullable=False)
    related_numbers = db.Column(db.Text)
    table_name = db.Column(db.String(50))
    row_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "history_id": self.history_id,
            "batch_id": self.batch_id,
            "rule_key": self.rule_key,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "related_numbers": json.loads(self.related_numbers) if self.related_numbers else [],
            "table_name": self.table_name,
            "row_id": self.row_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class HandoverList(db.Model):
    __tablename__ = "handover_lists"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    list_number = db.Column(db.String(100), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default=HANDOVER_STATUS_DRAFT)
    batch_status = db.Column(db.String(30))
    payable_total = db.Column(db.Float, default=0.0)
    matched_count = db.Column(db.Integer, default=0)
    exception_count = db.Column(db.Integer, default=0)
    unmatched_po_count = db.Column(db.Integer, default=0)
    unmatched_invoice_count = db.Column(db.Integer, default=0)
    latest_import_plan = db.Column(db.Text)
    latest_health_summary = db.Column(db.Text)
    latest_health_history_id = db.Column(db.Integer)
    export_filename = db.Column(db.String(500))
    pending_remarks = db.Column(db.Text)
    batch_updated_at = db.Column(db.DateTime)
    content_hash = db.Column(db.String(64))
    created_by = db.Column(db.String(100), default="system")
    completed_by = db.Column(db.String(100))
    completed_at = db.Column(db.DateTime)
    voided_by = db.Column(db.String(100))
    voided_at = db.Column(db.DateTime)
    void_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="handover_lists")
    items = db.relationship("HandoverListItem", backref="handover_list", cascade="all, delete-orphan")

    def can_transition(self, new_status):
        allowed = HANDOVER_ALLOWED_TRANSITIONS.get(self.status, [])
        return new_status in allowed

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "list_number": self.list_number,
            "status": self.status,
            "batch_status": self.batch_status,
            "payable_total": round(self.payable_total, 2) if self.payable_total is not None else None,
            "matched_count": self.matched_count,
            "exception_count": self.exception_count,
            "unmatched_po_count": self.unmatched_po_count,
            "unmatched_invoice_count": self.unmatched_invoice_count,
            "latest_import_plan": json.loads(self.latest_import_plan) if self.latest_import_plan else None,
            "latest_health_summary": json.loads(self.latest_health_summary) if self.latest_health_summary else None,
            "latest_health_history_id": self.latest_health_history_id,
            "export_filename": self.export_filename,
            "pending_remarks": self.pending_remarks,
            "batch_updated_at": self.batch_updated_at.isoformat() if self.batch_updated_at else None,
            "content_hash": self.content_hash,
            "created_by": self.created_by,
            "completed_by": self.completed_by,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "void_reason": self.void_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class HandoverListItem(db.Model):
    __tablename__ = "handover_list_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    handover_list_id = db.Column(db.Integer, db.ForeignKey("handover_lists.id"), nullable=False)
    match_result_id = db.Column(db.Integer, db.ForeignKey("match_results.id"))
    po_number = db.Column(db.String(100))
    invoice_number = db.Column(db.String(100))
    vendor_code = db.Column(db.String(100))
    vendor_name = db.Column(db.String(200))
    po_amount = db.Column(db.Float)
    invoice_amount = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    match_type = db.Column(db.String(30))
    is_exception = db.Column(db.Boolean, default=False)
    exception_type = db.Column(db.String(50))
    status = db.Column(db.String(20))
    remarks = db.Column(db.Text)
    rule_version = db.Column(db.String(50))
    item_order = db.Column(db.Integer, default=0)

    match_result = db.relationship("MatchResult")

    def to_dict(self):
        return {
            "id": self.id,
            "handover_list_id": self.handover_list_id,
            "match_result_id": self.match_result_id,
            "po_number": self.po_number,
            "invoice_number": self.invoice_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "po_amount": self.po_amount,
            "invoice_amount": self.invoice_amount,
            "amount_diff": self.amount_diff,
            "match_type": self.match_type,
            "is_exception": self.is_exception,
            "exception_type": self.exception_type,
            "status": self.status,
            "remarks": self.remarks,
            "rule_version": self.rule_version,
            "item_order": self.item_order,
        }


def compute_handover_content_hash(batch):
    parts = []
    parts.append(f"status:{batch.status}")
    parts.append(f"updated:{batch.updated_at.isoformat() if batch.updated_at else ''}")
    summary = batch.summary
    parts.append(f"matched:{summary['matched_count']}")
    parts.append(f"exception:{summary['exception_count']}")
    parts.append(f"payable:{summary['payable_total']}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.id}:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:"
            f"{mr.status}:{mr.po_amount}:{mr.invoice_amount}:{mr.amount_diff}:"
            f"{mr.is_exception}:{mr.exception_type or ''}:{mr.remarks or ''}:{mr.rule_version or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_release_content_hash(batch, note=None, health_history=None):
    parts = []
    parts.append(f"batch_status:{batch.status}")
    parts.append(f"batch_updated:{batch.updated_at.isoformat() if batch.updated_at else ''}")
    parts.append(f"rule_version:{batch.rule_version}")
    parts.append(f"tolerance_pct:{batch.tolerance_pct}")
    parts.append(f"tolerance_abs:{batch.tolerance_abs}")
    summary = batch.summary
    parts.append(f"matched:{summary['matched_count']}")
    parts.append(f"exception:{summary['exception_count']}")
    parts.append(f"payable:{summary['payable_total']}")
    if note:
        parts.append(f"note_id:{note.id}")
        parts.append(f"note_version:{note.version}")
        parts.append(f"note_hash:{note.content_hash}")
    if health_history:
        parts.append(f"health_id:{health_history.id}")
        parts.append(f"health_version:{health_history.rule_version}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.id}:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:"
            f"{mr.status}:{mr.po_amount}:{mr.invoice_amount}:{mr.amount_diff}:"
            f"{mr.is_exception}:{mr.exception_type or ''}:{mr.remarks or ''}:{mr.rule_version or ''}"
        )
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        parts.append(
            f"exc:{exc.id}:{exc.match_result_id}:{exc.exception_type}:{exc.status}:{exc.remarks or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ReleasePackage(db.Model):
    __tablename__ = "release_packages"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    package_number = db.Column(db.String(100), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default=RELEASE_STATUS_DRAFT)
    batch_status = db.Column(db.String(30))
    payable_total = db.Column(db.Float, default=0.0)
    matched_count = db.Column(db.Integer, default=0)
    exception_count = db.Column(db.Integer, default=0)
    unmatched_po_count = db.Column(db.Integer, default=0)
    unmatched_invoice_count = db.Column(db.Integer, default=0)
    tolerance_pct = db.Column(db.Float)
    tolerance_abs = db.Column(db.Float)
    rule_version = db.Column(db.String(50))
    recalc_note_id = db.Column(db.Integer, db.ForeignKey("payable_recalc_notes.id"))
    recalc_note_version = db.Column(db.Integer)
    recalc_note_summary = db.Column(db.Text)
    health_history_id = db.Column(db.Integer, db.ForeignKey("health_check_history.id"))
    health_rule_version = db.Column(db.String(50))
    health_summary = db.Column(db.Text)
    health_blocker_count = db.Column(db.Integer, default=0)
    health_warning_count = db.Column(db.Integer, default=0)
    health_info_count = db.Column(db.Integer, default=0)
    import_plan_id = db.Column(db.Integer, db.ForeignKey("import_plans.id"))
    import_plan_summary = db.Column(db.Text)
    export_filename = db.Column(db.String(500))
    content_hash = db.Column(db.String(64))
    is_expired = db.Column(db.Boolean, default=False)
    expire_reason = db.Column(db.Text)
    remarks = db.Column(db.Text)
    created_by = db.Column(db.String(100), default="system")
    submitted_by = db.Column(db.String(100))
    submitted_at = db.Column(db.DateTime)
    approved_by = db.Column(db.String(100))
    approved_at = db.Column(db.DateTime)
    rejected_by = db.Column(db.String(100))
    rejected_at = db.Column(db.DateTime)
    reject_reason = db.Column(db.Text)
    revoked_by = db.Column(db.String(100))
    revoked_at = db.Column(db.DateTime)
    revoke_reason = db.Column(db.Text)
    expired_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="release_packages")
    recalc_note = db.relationship("PayableRecalcNote", foreign_keys=[recalc_note_id])
    health_history = db.relationship("HealthCheckHistory", foreign_keys=[health_history_id])
    import_plan = db.relationship("ImportPlan", foreign_keys=[import_plan_id])
    items = db.relationship("ReleasePackageItem", backref="release_package", cascade="all, delete-orphan")

    def can_transition(self, new_status):
        allowed = RELEASE_ALLOWED_TRANSITIONS.get(self.status, [])
        return new_status in allowed

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "package_number": self.package_number,
            "status": self.status,
            "batch_status": self.batch_status,
            "payable_total": round(self.payable_total, 2) if self.payable_total is not None else None,
            "matched_count": self.matched_count,
            "exception_count": self.exception_count,
            "unmatched_po_count": self.unmatched_po_count,
            "unmatched_invoice_count": self.unmatched_invoice_count,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "recalc_note_id": self.recalc_note_id,
            "recalc_note_version": self.recalc_note_version,
            "recalc_note_summary": self.recalc_note_summary,
            "health_history_id": self.health_history_id,
            "health_rule_version": self.health_rule_version,
            "health_summary": json.loads(self.health_summary) if self.health_summary else None,
            "health_blocker_count": self.health_blocker_count,
            "health_warning_count": self.health_warning_count,
            "health_info_count": self.health_info_count,
            "import_plan_id": self.import_plan_id,
            "import_plan_summary": json.loads(self.import_plan_summary) if self.import_plan_summary else None,
            "export_filename": self.export_filename,
            "content_hash": self.content_hash,
            "is_expired": self.is_expired,
            "expire_reason": self.expire_reason,
            "remarks": self.remarks,
            "created_by": self.created_by,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "rejected_by": self.rejected_by,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "reject_reason": self.reject_reason,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoke_reason": self.revoke_reason,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ReleasePackageItem(db.Model):
    __tablename__ = "release_package_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    release_package_id = db.Column(db.Integer, db.ForeignKey("release_packages.id"), nullable=False)
    match_result_id = db.Column(db.Integer, db.ForeignKey("match_results.id"))
    po_number = db.Column(db.String(100))
    invoice_number = db.Column(db.String(100))
    vendor_code = db.Column(db.String(100))
    vendor_name = db.Column(db.String(200))
    po_amount = db.Column(db.Float)
    invoice_amount = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    match_type = db.Column(db.String(30))
    is_exception = db.Column(db.Boolean, default=False)
    exception_type = db.Column(db.String(50))
    status = db.Column(db.String(20))
    remarks = db.Column(db.Text)
    exception_remarks = db.Column(db.Text)
    rule_version = db.Column(db.String(50))
    item_order = db.Column(db.Integer, default=0)

    match_result = db.relationship("MatchResult")

    def to_dict(self):
        return {
            "id": self.id,
            "release_package_id": self.release_package_id,
            "match_result_id": self.match_result_id,
            "po_number": self.po_number,
            "invoice_number": self.invoice_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "po_amount": self.po_amount,
            "invoice_amount": self.invoice_amount,
            "amount_diff": self.amount_diff,
            "match_type": self.match_type,
            "is_exception": self.is_exception,
            "exception_type": self.exception_type,
            "status": self.status,
            "remarks": self.remarks,
            "exception_remarks": self.exception_remarks,
            "rule_version": self.rule_version,
            "item_order": self.item_order,
        }


class RehearsalSlip(db.Model):
    __tablename__ = "rehearsal_slips"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    slip_number = db.Column(db.String(100), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default=REHEARSAL_STATUS_ACTIVE)
    batch_status = db.Column(db.String(30))
    payable_total = db.Column(db.Float, default=0.0)
    matched_count = db.Column(db.Integer, default=0)
    exception_count = db.Column(db.Integer, default=0)
    unmatched_po_count = db.Column(db.Integer, default=0)
    unmatched_invoice_count = db.Column(db.Integer, default=0)
    tolerance_pct = db.Column(db.Float)
    tolerance_abs = db.Column(db.Float)
    rule_version = db.Column(db.String(50))
    recalc_note_id = db.Column(db.Integer, db.ForeignKey("payable_recalc_notes.id"))
    recalc_note_version = db.Column(db.Integer)
    recalc_note_summary = db.Column(db.Text)
    health_history_id = db.Column(db.Integer, db.ForeignKey("health_check_history.id"))
    health_rule_version = db.Column(db.String(50))
    health_summary = db.Column(db.Text)
    health_blocker_count = db.Column(db.Integer, default=0)
    health_warning_count = db.Column(db.Integer, default=0)
    health_info_count = db.Column(db.Integer, default=0)
    release_package_id = db.Column(db.Integer, db.ForeignKey("release_packages.id"))
    release_package_status = db.Column(db.String(20))
    release_package_snapshot = db.Column(db.Text)
    vendor_payable_summary = db.Column(db.Text)
    exception_result_summary = db.Column(db.Text)
    recalc_note_version_snapshot = db.Column(db.Text)
    health_inspection_summary = db.Column(db.Text)
    release_package_status_snapshot = db.Column(db.Text)
    content_hash = db.Column(db.String(64))
    is_stale = db.Column(db.Boolean, default=False)
    stale_reason = db.Column(db.Text)
    stale_at = db.Column(db.DateTime)
    export_filename = db.Column(db.String(500))
    voided_by = db.Column(db.String(100))
    voided_at = db.Column(db.DateTime)
    void_reason = db.Column(db.Text)
    created_by = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="rehearsal_slips")
    recalc_note = db.relationship("PayableRecalcNote", foreign_keys=[recalc_note_id])
    health_history = db.relationship("HealthCheckHistory", foreign_keys=[health_history_id])
    release_package = db.relationship("ReleasePackage", foreign_keys=[release_package_id])
    items = db.relationship("RehearsalSlipItem", backref="rehearsal_slip", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "slip_number": self.slip_number,
            "status": self.status,
            "batch_status": self.batch_status,
            "payable_total": round(self.payable_total, 2) if self.payable_total is not None else None,
            "matched_count": self.matched_count,
            "exception_count": self.exception_count,
            "unmatched_po_count": self.unmatched_po_count,
            "unmatched_invoice_count": self.unmatched_invoice_count,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "recalc_note_id": self.recalc_note_id,
            "recalc_note_version": self.recalc_note_version,
            "recalc_note_summary": self.recalc_note_summary,
            "health_history_id": self.health_history_id,
            "health_rule_version": self.health_rule_version,
            "health_summary": json.loads(self.health_summary) if self.health_summary else None,
            "health_blocker_count": self.health_blocker_count,
            "health_warning_count": self.health_warning_count,
            "health_info_count": self.health_info_count,
            "release_package_id": self.release_package_id,
            "release_package_status": self.release_package_status,
            "release_package_snapshot": json.loads(self.release_package_snapshot) if self.release_package_snapshot else None,
            "vendor_payable_summary": json.loads(self.vendor_payable_summary) if self.vendor_payable_summary else None,
            "exception_result_summary": json.loads(self.exception_result_summary) if self.exception_result_summary else None,
            "recalc_note_version_snapshot": json.loads(self.recalc_note_version_snapshot) if self.recalc_note_version_snapshot else None,
            "health_inspection_summary": json.loads(self.health_inspection_summary) if self.health_inspection_summary else None,
            "release_package_status_snapshot": json.loads(self.release_package_status_snapshot) if self.release_package_status_snapshot else None,
            "content_hash": self.content_hash,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "stale_at": self.stale_at.isoformat() if self.stale_at else None,
            "export_filename": self.export_filename,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "void_reason": self.void_reason,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class RehearsalSlipItem(db.Model):
    __tablename__ = "rehearsal_slip_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    rehearsal_slip_id = db.Column(db.Integer, db.ForeignKey("rehearsal_slips.id"), nullable=False)
    match_result_id = db.Column(db.Integer, db.ForeignKey("match_results.id"))
    po_number = db.Column(db.String(100))
    invoice_number = db.Column(db.String(100))
    vendor_code = db.Column(db.String(100))
    vendor_name = db.Column(db.String(200))
    po_amount = db.Column(db.Float)
    invoice_amount = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    match_type = db.Column(db.String(30))
    is_exception = db.Column(db.Boolean, default=False)
    exception_type = db.Column(db.String(50))
    status = db.Column(db.String(20))
    remarks = db.Column(db.Text)
    exception_remarks = db.Column(db.Text)
    rule_version = db.Column(db.String(50))
    item_order = db.Column(db.Integer, default=0)

    match_result = db.relationship("MatchResult")

    def to_dict(self):
        return {
            "id": self.id,
            "rehearsal_slip_id": self.rehearsal_slip_id,
            "match_result_id": self.match_result_id,
            "po_number": self.po_number,
            "invoice_number": self.invoice_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "po_amount": self.po_amount,
            "invoice_amount": self.invoice_amount,
            "amount_diff": self.amount_diff,
            "match_type": self.match_type,
            "is_exception": self.is_exception,
            "exception_type": self.exception_type,
            "status": self.status,
            "remarks": self.remarks,
            "exception_remarks": self.exception_remarks,
            "rule_version": self.rule_version,
            "item_order": self.item_order,
        }


def compute_rehearsal_content_hash(batch, note=None, health_history=None, release_pkg=None):
    parts = []
    parts.append(f"batch_status:{batch.status}")
    parts.append(f"batch_updated:{batch.updated_at.isoformat() if batch.updated_at else ''}")
    parts.append(f"rule_version:{batch.rule_version}")
    parts.append(f"tolerance_pct:{batch.tolerance_pct}")
    parts.append(f"tolerance_abs:{batch.tolerance_abs}")
    summary = batch.summary
    parts.append(f"matched:{summary['matched_count']}")
    parts.append(f"exception:{summary['exception_count']}")
    parts.append(f"payable:{summary['payable_total']}")
    if note:
        parts.append(f"note_id:{note.id}")
        parts.append(f"note_version:{note.version}")
        parts.append(f"note_hash:{note.content_hash}")
    if health_history:
        parts.append(f"health_id:{health_history.id}")
        parts.append(f"health_version:{health_history.rule_version}")
    if release_pkg:
        parts.append(f"release_id:{release_pkg.id}")
        parts.append(f"release_status:{release_pkg.status}")
        parts.append(f"release_hash:{release_pkg.content_hash}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.id}:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:"
            f"{mr.status}:{mr.po_amount}:{mr.invoice_amount}:{mr.amount_diff}:"
            f"{mr.is_exception}:{mr.exception_type or ''}:{mr.remarks or ''}:{mr.rule_version or ''}"
        )
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        parts.append(
            f"exc:{exc.id}:{exc.match_result_id}:{exc.exception_type}:{exc.status}:{exc.remarks or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_closing_archive_content_hash(batch, note=None, health_history=None, release_pkg=None, rehearsal_slip=None):
    parts = []
    parts.append(f"batch_status:{batch.status}")
    parts.append(f"batch_updated:{batch.updated_at.isoformat() if batch.updated_at else ''}")
    parts.append(f"rule_version:{batch.rule_version}")
    parts.append(f"tolerance_pct:{batch.tolerance_pct}")
    parts.append(f"tolerance_abs:{batch.tolerance_abs}")
    summary = batch.summary
    parts.append(f"matched:{summary['matched_count']}")
    parts.append(f"exception:{summary['exception_count']}")
    parts.append(f"payable:{summary['payable_total']}")
    if note:
        parts.append(f"note_id:{note.id}")
        parts.append(f"note_version:{note.version}")
        parts.append(f"note_hash:{note.content_hash}")
    if health_history:
        parts.append(f"health_id:{health_history.id}")
        parts.append(f"health_version:{health_history.rule_version}")
    if release_pkg:
        parts.append(f"release_id:{release_pkg.id}")
        parts.append(f"release_status:{release_pkg.status}")
        parts.append(f"release_hash:{release_pkg.content_hash}")
    if rehearsal_slip:
        parts.append(f"rehearsal_id:{rehearsal_slip.id}")
        parts.append(f"rehearsal_status:{rehearsal_slip.status}")
        parts.append(f"rehearsal_hash:{rehearsal_slip.content_hash}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.id}:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:"
            f"{mr.status}:{mr.po_amount}:{mr.invoice_amount}:{mr.amount_diff}:"
            f"{mr.is_exception}:{mr.exception_type or ''}:{mr.remarks or ''}:{mr.rule_version or ''}"
        )
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        parts.append(
            f"exc:{exc.id}:{exc.match_result_id}:{exc.exception_type}:{exc.status}:{exc.remarks or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ClosingArchive(db.Model):
    __tablename__ = "closing_archives"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), nullable=False)
    archive_number = db.Column(db.String(100), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default=ARCHIVE_STATUS_ACTIVE)
    batch_status = db.Column(db.String(30))
    payable_total = db.Column(db.Float, default=0.0)
    matched_count = db.Column(db.Integer, default=0)
    exception_count = db.Column(db.Integer, default=0)
    unmatched_po_count = db.Column(db.Integer, default=0)
    unmatched_invoice_count = db.Column(db.Integer, default=0)
    tolerance_pct = db.Column(db.Float)
    tolerance_abs = db.Column(db.Float)
    rule_version = db.Column(db.String(50))
    recalc_note_id = db.Column(db.Integer, db.ForeignKey("payable_recalc_notes.id"))
    recalc_note_version = db.Column(db.Integer)
    recalc_note_summary = db.Column(db.Text)
    health_history_id = db.Column(db.Integer, db.ForeignKey("health_check_history.id"))
    health_rule_version = db.Column(db.String(50))
    health_summary = db.Column(db.Text)
    health_blocker_count = db.Column(db.Integer, default=0)
    health_warning_count = db.Column(db.Integer, default=0)
    health_info_count = db.Column(db.Integer, default=0)
    release_package_id = db.Column(db.Integer, db.ForeignKey("release_packages.id"))
    release_package_status = db.Column(db.String(20))
    release_package_snapshot = db.Column(db.Text)
    rehearsal_slip_id = db.Column(db.Integer, db.ForeignKey("rehearsal_slips.id"))
    rehearsal_slip_status = db.Column(db.String(20))
    rehearsal_slip_snapshot = db.Column(db.Text)
    batch_summary_snapshot = db.Column(db.Text)
    match_results_snapshot = db.Column(db.Text)
    exceptions_snapshot = db.Column(db.Text)
    recalc_notes_snapshot = db.Column(db.Text)
    content_hash = db.Column(db.String(64))
    is_stale = db.Column(db.Boolean, default=False)
    stale_reason = db.Column(db.Text)
    stale_at = db.Column(db.DateTime)
    export_filename = db.Column(db.String(500))
    sealed_by = db.Column(db.String(100))
    sealed_at = db.Column(db.DateTime)
    voided_by = db.Column(db.String(100))
    voided_at = db.Column(db.DateTime)
    void_reason = db.Column(db.Text)
    created_by = db.Column(db.String(100), default="system")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    batch = db.relationship("Batch", backref="closing_archives")
    recalc_note = db.relationship("PayableRecalcNote", foreign_keys=[recalc_note_id])
    health_history = db.relationship("HealthCheckHistory", foreign_keys=[health_history_id])
    release_package = db.relationship("ReleasePackage", foreign_keys=[release_package_id])
    rehearsal_slip = db.relationship("RehearsalSlip", foreign_keys=[rehearsal_slip_id])
    items = db.relationship("ClosingArchiveItem", backref="closing_archive", cascade="all, delete-orphan")

    def can_transition(self, new_status):
        allowed = ARCHIVE_ALLOWED_TRANSITIONS.get(self.status, [])
        return new_status in allowed

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "archive_number": self.archive_number,
            "status": self.status,
            "batch_status": self.batch_status,
            "payable_total": round(self.payable_total, 2) if self.payable_total is not None else None,
            "matched_count": self.matched_count,
            "exception_count": self.exception_count,
            "unmatched_po_count": self.unmatched_po_count,
            "unmatched_invoice_count": self.unmatched_invoice_count,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "recalc_note_id": self.recalc_note_id,
            "recalc_note_version": self.recalc_note_version,
            "recalc_note_summary": json.loads(self.recalc_note_summary) if self.recalc_note_summary else None,
            "health_history_id": self.health_history_id,
            "health_rule_version": self.health_rule_version,
            "health_summary": json.loads(self.health_summary) if self.health_summary else None,
            "health_blocker_count": self.health_blocker_count,
            "health_warning_count": self.health_warning_count,
            "health_info_count": self.health_info_count,
            "release_package_id": self.release_package_id,
            "release_package_status": self.release_package_status,
            "release_package_snapshot": json.loads(self.release_package_snapshot) if self.release_package_snapshot else None,
            "rehearsal_slip_id": self.rehearsal_slip_id,
            "rehearsal_slip_status": self.rehearsal_slip_status,
            "rehearsal_slip_snapshot": json.loads(self.rehearsal_slip_snapshot) if self.rehearsal_slip_snapshot else None,
            "batch_summary_snapshot": json.loads(self.batch_summary_snapshot) if self.batch_summary_snapshot else None,
            "content_hash": self.content_hash,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "stale_at": self.stale_at.isoformat() if self.stale_at else None,
            "export_filename": self.export_filename,
            "sealed_by": self.sealed_by,
            "sealed_at": self.sealed_at.isoformat() if self.sealed_at else None,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "void_reason": self.void_reason,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ClosingArchiveItem(db.Model):
    __tablename__ = "closing_archive_items"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    closing_archive_id = db.Column(db.Integer, db.ForeignKey("closing_archives.id"), nullable=False)
    match_result_id = db.Column(db.Integer, db.ForeignKey("match_results.id"))
    po_number = db.Column(db.String(100))
    invoice_number = db.Column(db.String(100))
    vendor_code = db.Column(db.String(100))
    vendor_name = db.Column(db.String(200))
    po_amount = db.Column(db.Float)
    invoice_amount = db.Column(db.Float)
    amount_diff = db.Column(db.Float)
    match_type = db.Column(db.String(30))
    is_exception = db.Column(db.Boolean, default=False)
    exception_type = db.Column(db.String(50))
    status = db.Column(db.String(20))
    remarks = db.Column(db.Text)
    exception_remarks = db.Column(db.Text)
    rule_version = db.Column(db.String(50))
    item_order = db.Column(db.Integer, default=0)

    match_result = db.relationship("MatchResult")

    def to_dict(self):
        return {
            "id": self.id,
            "closing_archive_id": self.closing_archive_id,
            "match_result_id": self.match_result_id,
            "po_number": self.po_number,
            "invoice_number": self.invoice_number,
            "vendor_code": self.vendor_code,
            "vendor_name": self.vendor_name,
            "po_amount": self.po_amount,
            "invoice_amount": self.invoice_amount,
            "amount_diff": self.amount_diff,
            "match_type": self.match_type,
            "is_exception": self.is_exception,
            "exception_type": self.exception_type,
            "status": self.status,
            "remarks": self.remarks,
            "exception_remarks": self.exception_remarks,
            "rule_version": self.rule_version,
            "item_order": self.item_order,
        }


def init_db(app):
    if "sqlalchemy" not in app.extensions:
        db.init_app(app)
    with app.app_context():
        db.create_all()


ARCHIVE_CSV_HEADER_FIELDS = {
    "归档编号": "archive_number",
    "批次ID": "batch_id",
    "状态": "status",
    "批次状态": "batch_status",
    "应付总额": "payable_total",
    "匹配数": "matched_count",
    "异常数": "exception_count",
    "未匹配PO数": "unmatched_po_count",
    "未匹配发票数": "unmatched_invoice_count",
    "容差比例%": "tolerance_pct",
    "容差绝对值": "tolerance_abs",
    "规则版本": "rule_version",
    "重算说明ID": "recalc_note_id",
    "重算说明版本": "recalc_note_version",
    "巡检记录ID": "health_history_id",
    "巡检规则版本": "health_rule_version",
    "阻塞问题数": "health_blocker_count",
    "警告问题数": "health_warning_count",
    "提示问题数": "health_info_count",
    "放行包ID": "release_package_id",
    "放行包状态": "release_package_status",
    "预演单ID": "rehearsal_slip_id",
    "预演单状态": "rehearsal_slip_status",
    "内容哈希": "content_hash",
    "创建人": "created_by",
    "创建时间": "created_at",
    "封存人": "sealed_by",
    "封存时间": "sealed_at",
    "作废人": "voided_by",
    "作废时间": "voided_at",
    "作废原因": "void_reason",
    "是否过期": "is_stale",
    "过期原因": "stale_reason",
}

ARCHIVE_CSV_DETAIL_FIELDS = [
    "序号", "匹配结果ID", "采购单号", "发票号", "供应商编码", "供应商名称",
    "采购金额", "发票金额", "金额差异", "匹配类型", "是否异常", "异常类型",
    "匹配状态", "规则版本", "匹配备注", "异常备注",
]

ARCHIVE_CSV_SECTIONS = {
    "header": "===== 结账归档头段 =====",
    "detail": "===== 结账归档明细 =====",
    "batch_summary": "===== 批次摘要快照 =====",
    "recalc": "===== 重算说明快照 =====",
    "health": "===== 巡检摘要快照 =====",
    "release": "===== 放行包快照 =====",
    "rehearsal": "===== 预演单快照 =====",
}


def _archive_safe_float(val):
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _archive_safe_int(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def can_generate_archive(batch):
    """判断批次能否生成归档：CONFIRMED 或 POSTED 状态都可以"""
    return batch.status in (BATCH_STATUS_CONFIRMED, BATCH_STATUS_POSTED, BATCH_STATUS_MATCHED)


def collect_archive_related(batch):
    """收集归档关联对象：最新重算说明、巡检、放行包、预演单"""
    latest_note = PayableRecalcNote.query.filter_by(batch_id=batch.id).order_by(
        PayableRecalcNote.created_at.desc()
    ).first()
    latest_health = HealthCheckHistory.query.filter_by(batch_id=batch.id).order_by(
        HealthCheckHistory.created_at.desc()
    ).first()
    latest_release = ReleasePackage.query.filter_by(batch_id=batch.id).order_by(
        ReleasePackage.created_at.desc()
    ).first()
    latest_rehearsal = RehearsalSlip.query.filter_by(batch_id=batch.id).order_by(
        RehearsalSlip.created_at.desc()
    ).first()
    return latest_note, latest_health, latest_release, latest_rehearsal


def build_archive_snapshots(batch, note, health_history, release_pkg, rehearsal_slip):
    """统一组装所有归档快照：批次摘要、匹配结果、异常、重算说明、巡检、放行包、预演单"""
    matched_types = (MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE)
    matched_results = [r for r in batch.match_results if r.match_type in matched_types]

    batch_summary = batch.summary

    match_snapshot = []
    for mr in sorted(matched_results, key=lambda x: x.id or 0):
        match_snapshot.append({
            "id": mr.id,
            "po_id": mr.po_id,
            "invoice_id": mr.invoice_id,
            "po_number": mr.po.po_number if mr.po else None,
            "invoice_number": mr.invoice.invoice_number if mr.invoice else None,
            "vendor_code": mr.po.vendor_code if mr.po else (mr.invoice.vendor_code if mr.invoice else None),
            "vendor_name": mr.po.vendor_name if mr.po else (mr.invoice.vendor_name if mr.invoice else None),
            "po_amount": mr.po_amount,
            "invoice_amount": mr.invoice_amount,
            "amount_diff": mr.amount_diff,
            "match_type": mr.match_type,
            "status": mr.status,
            "is_exception": mr.is_exception,
            "exception_type": mr.exception_type,
            "remarks": mr.remarks,
            "rule_version": mr.rule_version,
        })

    exception_snapshot = []
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        exception_snapshot.append({
            "id": exc.id,
            "match_result_id": exc.match_result_id,
            "exception_type": exc.exception_type,
            "status": exc.status,
            "detail": exc.detail,
            "remarks": exc.remarks,
        })

    recalc_snapshot = None
    if note:
        recalc_snapshot = {
            "id": note.id,
            "version": note.version,
            "current_total": note.current_total,
            "previous_total": note.previous_total,
            "amount_diff": note.amount_diff,
            "change_source": note.change_source,
            "change_summary": note.change_summary,
            "po_numbers": json.loads(note.po_numbers) if note.po_numbers else [],
            "invoice_numbers": json.loads(note.invoice_numbers) if note.invoice_numbers else [],
            "rule_version": note.rule_version,
            "content_hash": note.content_hash,
        }

    health_snapshot = None
    if health_history:
        health_snapshot = {
            "id": health_history.id,
            "rule_version": health_history.rule_version,
            "blocker_count": health_history.blocker_count,
            "warning_count": health_history.warning_count,
            "info_count": health_history.info_count,
            "summary": json.loads(health_history.summary) if health_history.summary else None,
        }

    release_snapshot = None
    if release_pkg:
        release_snapshot = {
            "id": release_pkg.id,
            "package_number": release_pkg.package_number,
            "status": release_pkg.status,
            "batch_status": release_pkg.batch_status,
            "payable_total": release_pkg.payable_total,
            "matched_count": release_pkg.matched_count,
            "exception_count": release_pkg.exception_count,
            "tolerance_pct": release_pkg.tolerance_pct,
            "tolerance_abs": release_pkg.tolerance_abs,
            "rule_version": release_pkg.rule_version,
            "recalc_note_id": release_pkg.recalc_note_id,
            "recalc_note_version": release_pkg.recalc_note_version,
            "health_history_id": release_pkg.health_history_id,
            "health_rule_version": release_pkg.health_rule_version,
            "health_blocker_count": release_pkg.health_blocker_count,
            "health_warning_count": release_pkg.health_warning_count,
            "health_info_count": release_pkg.health_info_count,
            "is_expired": release_pkg.is_expired,
            "content_hash": release_pkg.content_hash,
            "created_at": release_pkg.created_at.isoformat() if release_pkg.created_at else None,
        }

    rehearsal_snapshot = None
    if rehearsal_slip:
        rehearsal_snapshot = {
            "id": rehearsal_slip.id,
            "slip_number": rehearsal_slip.slip_number,
            "status": rehearsal_slip.status,
            "batch_status": rehearsal_slip.batch_status,
            "payable_total": rehearsal_slip.payable_total,
            "matched_count": rehearsal_slip.matched_count,
            "exception_count": rehearsal_slip.exception_count,
            "tolerance_pct": rehearsal_slip.tolerance_pct,
            "tolerance_abs": rehearsal_slip.tolerance_abs,
            "rule_version": rehearsal_slip.rule_version,
            "recalc_note_id": rehearsal_slip.recalc_note_id,
            "recalc_note_version": rehearsal_slip.recalc_note_version,
            "health_history_id": rehearsal_slip.health_history_id,
            "health_rule_version": rehearsal_slip.health_rule_version,
            "health_blocker_count": rehearsal_slip.health_blocker_count,
            "health_warning_count": rehearsal_slip.health_warning_count,
            "health_info_count": rehearsal_slip.health_info_count,
            "release_package_id": rehearsal_slip.release_package_id,
            "release_package_status": rehearsal_slip.release_package_status,
            "is_stale": rehearsal_slip.is_stale,
            "content_hash": rehearsal_slip.content_hash,
            "created_at": rehearsal_slip.created_at.isoformat() if rehearsal_slip.created_at else None,
        }

    return {
        "batch_summary": batch_summary,
        "match_snapshot": match_snapshot,
        "exception_snapshot": exception_snapshot,
        "recalc_snapshot": recalc_snapshot,
        "health_snapshot": health_snapshot,
        "release_snapshot": release_snapshot,
        "rehearsal_snapshot": rehearsal_snapshot,
    }


def compute_archive_content_hash(batch, note, health_history, release_pkg, rehearsal_slip):
    """统一计算归档内容哈希"""
    parts = []
    parts.append(f"batch_status:{batch.status}")
    parts.append(f"batch_updated:{batch.updated_at.isoformat() if batch.updated_at else ''}")
    parts.append(f"rule_version:{batch.rule_version}")
    parts.append(f"tolerance_pct:{batch.tolerance_pct}")
    parts.append(f"tolerance_abs:{batch.tolerance_abs}")
    summary = batch.summary
    parts.append(f"matched:{summary['matched_count']}")
    parts.append(f"exception:{summary['exception_count']}")
    parts.append(f"payable:{summary['payable_total']}")
    if note:
        parts.append(f"note_id:{note.id}")
        parts.append(f"note_version:{note.version}")
        parts.append(f"note_hash:{note.content_hash}")
    if health_history:
        parts.append(f"health_id:{health_history.id}")
        parts.append(f"health_version:{health_history.rule_version}")
    if release_pkg:
        parts.append(f"release_id:{release_pkg.id}")
        parts.append(f"release_status:{release_pkg.status}")
        parts.append(f"release_hash:{release_pkg.content_hash}")
    if rehearsal_slip:
        parts.append(f"rehearsal_id:{rehearsal_slip.id}")
        parts.append(f"rehearsal_status:{rehearsal_slip.status}")
        parts.append(f"rehearsal_hash:{rehearsal_slip.content_hash}")
    for mr in sorted(batch.match_results, key=lambda x: x.id or 0):
        parts.append(
            f"mr:{mr.id}:{mr.po_id}:{mr.invoice_id}:{mr.match_type}:"
            f"{mr.status}:{mr.po_amount}:{mr.invoice_amount}:{mr.amount_diff}:"
            f"{mr.is_exception}:{mr.exception_type or ''}:{mr.remarks or ''}:{mr.rule_version or ''}"
        )
    for exc in sorted(batch.exception_items, key=lambda x: x.id or 0):
        parts.append(
            f"exc:{exc.id}:{exc.match_result_id}:{exc.exception_type}:{exc.status}:{exc.remarks or ''}"
        )
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_archive_items(batch, archive_id):
    """统一构建归档明细项"""
    exc_map = {}
    for exc in batch.exception_items:
        if exc.match_result_id:
            exc_map[exc.match_result_id] = exc

    matched_types = (MATCH_TYPE_EXACT, MATCH_TYPE_TOLERANCE, MATCH_TYPE_OVER_TOLERANCE)
    matched_results = [r for r in batch.match_results if r.match_type in matched_types]

    items = []
    for idx, mr in enumerate(sorted(matched_results, key=lambda x: x.id or 0)):
        exc = exc_map.get(mr.id)
        items.append(ClosingArchiveItem(
            closing_archive_id=archive_id,
            match_result_id=mr.id,
            po_number=mr.po.po_number if mr.po else None,
            invoice_number=mr.invoice.invoice_number if mr.invoice else None,
            vendor_code=mr.po.vendor_code if mr.po else (mr.invoice.vendor_code if mr.invoice else None),
            vendor_name=mr.po.vendor_name if mr.po else (mr.invoice.vendor_name if mr.invoice else None),
            po_amount=mr.po_amount,
            invoice_amount=mr.invoice_amount,
            amount_diff=mr.amount_diff,
            match_type=mr.match_type,
            is_exception=mr.is_exception,
            exception_type=mr.exception_type,
            status=mr.status,
            remarks=mr.remarks,
            exception_remarks=exc.remarks if exc else None,
            rule_version=mr.rule_version,
            item_order=idx,
        ))
    return items


def _generate_archive_number(batch_id):
    count = ClosingArchive.query.filter_by(batch_id=batch_id).count()
    return f"CA-{batch_id}-{count + 1:04d}"


def parse_archive_csv(csv_content):
    """统一解析归档 CSV，返回结构化数据（头段、明细、各快照）"""
    if isinstance(csv_content, bytes):
        csv_content = csv_content.decode("utf-8-sig")

    reader = csv.reader(io.StringIO(csv_content))
    rows = list(reader)

    sections = {v: k for k, v in ARCHIVE_CSV_SECTIONS.items()}
    current_section = None

    header_data = {}
    detail_items = []
    detail_headers = None
    snapshots = {
        "batch_summary": None,
        "recalc": None,
        "health": None,
        "release": None,
        "rehearsal": None,
    }

    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue

        first = row[0].strip()
        if first in sections:
            current_section = sections[first]
            if current_section in snapshots and snapshots[current_section] is None:
                snapshots[current_section] = {}
            continue

        if current_section == "header" and len(row) >= 2:
            key = row[0].strip()
            value = row[1].strip() if len(row) > 1 else ""
            if key in ARCHIVE_CSV_HEADER_FIELDS:
                header_data[ARCHIVE_CSV_HEADER_FIELDS[key]] = value

        elif current_section == "detail":
            if detail_headers is None:
                detail_headers = [h.strip() for h in row]
                continue
            item = {}
            for i, h in enumerate(detail_headers):
                item[h] = row[i].strip() if i < len(row) else ""
            detail_items.append(item)

        elif current_section in snapshots and len(row) >= 2:
            k = row[0].strip()
            v = row[1].strip() if len(row) > 1 else ""
            if v.replace(".", "").isdigit() and "." in v:
                snapshots[current_section][k] = float(v)
            elif v.lstrip("-").isdigit():
                snapshots[current_section][k] = int(v)
            else:
                snapshots[current_section][k] = v

    return {
        "header": header_data,
        "detail_items": detail_items,
        "detail_headers": detail_headers,
        "snapshots": snapshots,
    }


def validate_archive_import(batch_id, parsed):
    """统一校验归档回导：必填字段、跨批次、编号重复、哈希重复、已封存冲突、明细列"""
    errors = []
    header = parsed["header"]
    detail_headers = parsed["detail_headers"]

    required_headers = ["archive_number", "batch_id", "content_hash"]
    field_map = {v: k for k, v in ARCHIVE_CSV_HEADER_FIELDS.items()}
    missing_headers = [k for k in required_headers if not header.get(k)]
    if missing_headers:
        missing_names = [field_map[k] for k in missing_headers]
        errors.append(f"CSV 头段缺少必填字段: {', '.join(missing_names)}")

    if detail_headers is not None:
        missing_cols = [f for f in ARCHIVE_CSV_DETAIL_FIELDS if f not in detail_headers]
        if missing_cols:
            errors.append(f"CSV 明细缺少必填列: {', '.join(missing_cols)}")
    else:
        errors.append("CSV 缺少明细分段")

    csv_batch_id = None
    if header.get("batch_id"):
        try:
            csv_batch_id = int(header["batch_id"])
        except (ValueError, TypeError):
            errors.append(f"批次ID格式错误: {header['batch_id']}")

    if csv_batch_id is not None and csv_batch_id != batch_id:
        errors.append(f"跨批次回导被拒绝：CSV 属于批次 #{csv_batch_id}，当前批次为 #{batch_id}")

    archive_number = header.get("archive_number")
    if archive_number:
        existing_by_number = ClosingArchive.query.filter_by(archive_number=archive_number).first()
        if existing_by_number:
            errors.append(f"归档编号重复：{archive_number} 已存在 (#{existing_by_number.id})")

    content_hash = header.get("content_hash")
    if content_hash:
        existing_by_hash = ClosingArchive.query.filter_by(
            batch_id=batch_id, content_hash=content_hash,
        ).first()
        if existing_by_hash:
            errors.append(
                f"内容哈希重复：哈希 {content_hash[:12]} 已存在于归档 {existing_by_hash.archive_number}"
            )

    existing_sealed = ClosingArchive.query.filter_by(
        batch_id=batch_id, status=ARCHIVE_STATUS_SEALED,
    ).first()
    if existing_sealed:
        errors.append(
            f"已封存冲突：批次 #{batch_id} 存在已封存归档 {existing_sealed.archive_number}，不允许回导"
        )

    if not parsed["detail_items"]:
        errors.append("CSV 没有明细数据")

    return errors


def build_archive_from_parsed(batch_id, parsed, operator="system"):
    """从 CSV 解析结果构建归档对象和明细，写入 DB 并记录审计日志"""
    header = parsed["header"]
    detail_items = parsed["detail_items"]
    snapshots = parsed["snapshots"]

    archive_number = header["archive_number"]
    content_hash = header["content_hash"]

    recalc_snapshot = snapshots.get("recalc")
    health_snapshot = snapshots.get("health")
    release_snapshot = snapshots.get("release")
    rehearsal_snapshot = snapshots.get("rehearsal")
    batch_summary_snapshot = snapshots.get("batch_summary")

    archive = ClosingArchive(
        batch_id=batch_id,
        archive_number=archive_number,
        status=header.get("status", ARCHIVE_STATUS_ACTIVE),
        batch_status=header.get("batch_status"),
        payable_total=_archive_safe_float(header.get("payable_total")),
        matched_count=_archive_safe_int(header.get("matched_count")) or 0,
        exception_count=_archive_safe_int(header.get("exception_count")) or 0,
        unmatched_po_count=_archive_safe_int(header.get("unmatched_po_count")) or 0,
        unmatched_invoice_count=_archive_safe_int(header.get("unmatched_invoice_count")) or 0,
        tolerance_pct=_archive_safe_float(header.get("tolerance_pct")),
        tolerance_abs=_archive_safe_float(header.get("tolerance_abs")),
        rule_version=header.get("rule_version"),
        recalc_note_id=_archive_safe_int(header.get("recalc_note_id")),
        recalc_note_version=_archive_safe_int(header.get("recalc_note_version")),
        recalc_note_summary=json.dumps(recalc_snapshot, ensure_ascii=False) if recalc_snapshot else None,
        health_history_id=_archive_safe_int(header.get("health_history_id")),
        health_rule_version=header.get("health_rule_version"),
        health_summary=json.dumps(health_snapshot, ensure_ascii=False) if health_snapshot else None,
        health_blocker_count=_archive_safe_int(header.get("health_blocker_count")) or 0,
        health_warning_count=_archive_safe_int(header.get("health_warning_count")) or 0,
        health_info_count=_archive_safe_int(header.get("health_info_count")) or 0,
        release_package_id=_archive_safe_int(header.get("release_package_id")),
        release_package_status=header.get("release_package_status"),
        release_package_snapshot=json.dumps(release_snapshot, ensure_ascii=False) if release_snapshot else None,
        rehearsal_slip_id=_archive_safe_int(header.get("rehearsal_slip_id")),
        rehearsal_slip_status=header.get("rehearsal_slip_status"),
        rehearsal_slip_snapshot=json.dumps(rehearsal_snapshot, ensure_ascii=False) if rehearsal_snapshot else None,
        batch_summary_snapshot=json.dumps(batch_summary_snapshot, ensure_ascii=False) if batch_summary_snapshot else None,
        match_results_snapshot=json.dumps([], ensure_ascii=False),
        exceptions_snapshot=json.dumps([], ensure_ascii=False),
        recalc_notes_snapshot=json.dumps(recalc_snapshot, ensure_ascii=False) if recalc_snapshot else None,
        content_hash=content_hash,
        is_stale=(header.get("is_stale") == "是"),
        stale_reason=header.get("stale_reason"),
        created_by=header.get("created_by", operator),
        sealed_by=header.get("sealed_by"),
        voided_by=header.get("voided_by"),
        void_reason=header.get("void_reason"),
    )
    db.session.add(archive)
    db.session.flush()

    for idx, item in enumerate(detail_items):
        archive_item = ClosingArchiveItem(
            closing_archive_id=archive.id,
            match_result_id=_archive_safe_int(item.get("匹配结果ID")),
            po_number=item.get("采购单号") or None,
            invoice_number=item.get("发票号") or None,
            vendor_code=item.get("供应商编码") or None,
            vendor_name=item.get("供应商名称") or None,
            po_amount=_archive_safe_float(item.get("采购金额")),
            invoice_amount=_archive_safe_float(item.get("发票金额")),
            amount_diff=_archive_safe_float(item.get("金额差异")),
            match_type=item.get("匹配类型") or None,
            is_exception=(item.get("是否异常") == "是"),
            exception_type=item.get("异常类型") or None,
            status=item.get("匹配状态") or None,
            remarks=item.get("匹配备注") or None,
            exception_remarks=item.get("异常备注") or None,
            rule_version=item.get("规则版本") or None,
            item_order=idx,
        )
        db.session.add(archive_item)

    log = AuditLog(
        batch_id=batch_id,
        action="ARCHIVE_IMPORT",
        detail=f"回导结账归档 {archive_number} (#{archive.id})，共 {len(detail_items)} 条明细",
        operator=operator,
    )
    db.session.add(log)
    db.session.commit()
    db.session.refresh(archive)
    return archive


def export_archive_to_csv(archive):
    """统一导出归档为 CSV 字符串"""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([ARCHIVE_CSV_SECTIONS["header"]])
    header_data = archive.to_dict()
    header_data["created_at"] = header_data["created_at"] if header_data["created_at"] else ""
    header_data["sealed_at"] = header_data["sealed_at"] if header_data["sealed_at"] else ""
    header_data["voided_at"] = header_data["voided_at"] if header_data["voided_at"] else ""
    header_data["is_stale"] = "是" if header_data["is_stale"] else "否"

    for label, key in ARCHIVE_CSV_HEADER_FIELDS.items():
        value = header_data.get(key, "")
        writer.writerow([label, value])

    writer.writerow([])
    writer.writerow([ARCHIVE_CSV_SECTIONS["detail"]])
    writer.writerow(ARCHIVE_CSV_DETAIL_FIELDS)

    for idx, item in enumerate(sorted(archive.items, key=lambda x: x.item_order)):
        writer.writerow([
            idx + 1,
            item.match_result_id or "",
            item.po_number or "",
            item.invoice_number or "",
            item.vendor_code or "",
            item.vendor_name or "",
            item.po_amount if item.po_amount is not None else "",
            item.invoice_amount if item.invoice_amount is not None else "",
            item.amount_diff if item.amount_diff is not None else "",
            item.match_type or "",
            "是" if item.is_exception else "否",
            item.exception_type or "",
            item.status or "",
            item.rule_version or "",
            item.remarks or "",
            item.exception_remarks or "",
        ])

    snapshot_sections = [
        ("batch_summary", "批次摘要快照"),
        ("recalc", "重算说明快照"),
        ("health", "巡检摘要快照"),
        ("release", "放行包快照"),
        ("rehearsal", "预演单快照"),
    ]
    snapshot_attrs = {
        "batch_summary": "batch_summary_snapshot",
        "recalc": "recalc_note_summary",
        "health": "health_summary",
        "release": "release_package_snapshot",
        "rehearsal": "rehearsal_slip_snapshot",
    }
    for key, label in snapshot_sections:
        writer.writerow([])
        writer.writerow([f"===== {label} ====="])
        attr = snapshot_attrs[key]
        raw = getattr(archive, attr, None)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (dict, list)):
                        writer.writerow([k, json.dumps(v, ensure_ascii=False)])
                    else:
                        writer.writerow([k, v])

    content = output.getvalue()
    output.close()
    return content, f"{archive.archive_number}.csv"
