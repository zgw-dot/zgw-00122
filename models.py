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

    def to_dict(self):
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "tolerance_pct": self.tolerance_pct,
            "tolerance_abs": self.tolerance_abs,
            "rule_version": self.rule_version,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
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


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
