import csv
import io
import os
from datetime import datetime, timezone
from flask import Flask, Blueprint, request, jsonify, send_file, send_from_directory
from models import (
    db, Batch, MatchResult, ExceptionItem, ToleranceHistory, AuditLog,
    BATCH_STATUS_CREATED, BATCH_STATUS_CONFIRMED, BATCH_STATUS_POSTED,
    BATCH_STATUS_ROLLED_BACK, BATCH_STATUS_FAILED,
    EXCEPTION_STATUS_PENDING, EXCEPTION_STATUS_RESOLVED,
    RESULT_STATUS_PENDING, RESULT_STATUS_CONFIRMED, RESULT_STATUS_REJECTED,
    VALID_TRANSITIONS, compute_rule_version, init_db,
)
from matcher import (
    process_batch, validate_and_import_po, validate_and_import_invoice, ValidationError,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

bp = Blueprint("recon", __name__)


def create_app(config=None):
    """App Factory：每个测试用例都能拿到全新 Flask 实例 + 独立 DB 连接"""
    app = Flask(__name__)
    db_path = os.path.join(BASE_DIR, "reconciliation.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
    if config:
        app.config.update(config)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    @app.errorhandler(Exception)
    def handle_unexpected_error(e):
        import traceback
        traceback.print_exc()
        status = getattr(e, "code", 500)
        if isinstance(e, NameError):
            return jsonify({"error": "服务内部错误，请联系管理员"}), 500
        if status == 500:
            return jsonify({"error": str(e)}), 500
        return jsonify({"error": str(e)}), status

    init_db(app)
    app.register_blueprint(bp)
    return app


@bp.route("/")
def index():
    tmpl_dir = os.path.join(BASE_DIR, "templates")
    return send_from_directory(tmpl_dir, "index.html")


@bp.route("/api/dashboard", methods=["GET"])
def dashboard():
    recent = Batch.query.order_by(Batch.updated_at.desc()).limit(10).all()
    batches_data = []
    for b in recent:
        d = b.to_dict()
        d["summary"] = b.summary
        batches_data.append(d)
    return jsonify({"batches": batches_data})


@bp.route("/api/batches", methods=["GET"])
def list_batches():
    batches = Batch.query.order_by(Batch.updated_at.desc()).all()
    data = []
    for b in batches:
        d = b.to_dict()
        d["summary"] = b.summary
        data.append(d)
    return jsonify({"batches": data})


@bp.route("/api/batches", methods=["POST"])
def create_batch():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not name:
        return jsonify({"error": "批次名称必填"}), 400
    pct = float(payload.get("tolerance_pct", 2.0))
    ab = float(payload.get("tolerance_abs", 100.0))
    rule_version = compute_rule_version(pct, ab)
    batch = Batch(
        name=name,
        tolerance_pct=pct,
        tolerance_abs=ab,
        rule_version=rule_version,
        status=BATCH_STATUS_CREATED,
    )
    db.session.add(batch)
    db.session.flush()
    th = ToleranceHistory(
        batch_id=batch.id,
        tolerance_pct=pct,
        tolerance_abs=ab,
        rule_version=rule_version,
        changed_by="system",
    )
    db.session.add(th)
    log = AuditLog(batch_id=batch.id, action="CREATE", detail=f"创建批次: {name}")
    db.session.add(log)
    db.session.commit()
    resp = batch.to_dict()
    resp["summary"] = batch.summary
    return jsonify(resp), 201


@bp.route("/api/batches/<int:batch_id>", methods=["GET"])
def get_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    resp = batch.to_dict()
    resp["summary"] = batch.summary
    return jsonify(resp)


@bp.route("/api/batches/<int:batch_id>/tolerance", methods=["PUT"])
def update_tolerance(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    payload = request.get_json(silent=True) or {}
    pct = float(payload.get("tolerance_pct", batch.tolerance_pct))
    ab = float(payload.get("tolerance_abs", batch.tolerance_abs))
    rule_version = compute_rule_version(pct, ab)
    batch.tolerance_pct = pct
    batch.tolerance_abs = ab
    batch.rule_version = rule_version
    th = ToleranceHistory(
        batch_id=batch.id,
        tolerance_pct=pct,
        tolerance_abs=ab,
        rule_version=rule_version,
        changed_by="user",
    )
    db.session.add(th)
    log = AuditLog(
        batch_id=batch.id,
        action="UPDATE_TOLERANCE",
        detail=f"容差更新为 {pct}% / {ab}",
    )
    db.session.add(log)
    db.session.commit()
    resp = batch.to_dict()
    resp["summary"] = batch.summary
    return jsonify(resp)


@bp.route("/api/batches/<int:batch_id>/upload-po", methods=["POST"])
def upload_po(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if "file" not in request.files:
        return jsonify({"error": "缺少文件字段 file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    batch.po_filename = f.filename
    try:
        content = f.read().decode("utf-8-sig")
        count = validate_and_import_po(batch.id, content)
        log = AuditLog(batch_id=batch.id, action="UPLOAD_PO", detail=f"导入 {f.filename} 共 {count} 行")
        db.session.add(log)
        db.session.commit()
        return jsonify({"imported": count, "filename": f.filename})
    except ValidationError as e:
        db.session.rollback()
        return jsonify({"error": str(e), "details": e.details}), 400


@bp.route("/api/batches/<int:batch_id>/upload-invoice", methods=["POST"])
def upload_invoice(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if "file" not in request.files:
        return jsonify({"error": "缺少文件字段 file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    batch.invoice_filename = f.filename
    try:
        content = f.read().decode("utf-8-sig")
        count = validate_and_import_invoice(batch.id, content)
        log = AuditLog(batch_id=batch.id, action="UPLOAD_INVOICE", detail=f"导入 {f.filename} 共 {count} 行")
        db.session.add(log)
        db.session.commit()
        return jsonify({"imported": count, "filename": f.filename})
    except ValidationError as e:
        db.session.rollback()
        return jsonify({"error": str(e), "details": e.details}), 400


@bp.route("/api/batches/<int:batch_id>/match", methods=["POST"])
def match_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    try:
        success = process_batch(batch.id)
        return jsonify({"success": True, "has_exceptions": not success})
    except ValidationError as e:
        return jsonify({"success": False, "error": str(e), "details": e.details}), 400


@bp.route("/api/batches/<int:batch_id>/results", methods=["GET"])
def list_results(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    return jsonify({"results": [r.to_dict() for r in batch.match_results]})


@bp.route("/api/batches/<int:batch_id>/exceptions", methods=["GET"])
def list_exceptions(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    data = [e.to_dict() for e in batch.exception_items]
    for d in data:
        if d["match_result_id"]:
            mr = MatchResult.query.get(d["match_result_id"])
            if mr:
                d["po_number"] = mr.po.po_number if mr.po else None
                d["invoice_number"] = mr.invoice.invoice_number if mr.invoice else None
                d["amount_diff"] = mr.amount_diff
                d["po_amount"] = mr.po_amount
                d["invoice_amount"] = mr.invoice_amount
                d["rule_version"] = mr.rule_version
    return jsonify({"exceptions": data})


@bp.route("/api/batches/<int:batch_id>/exceptions/<int:exc_id>/remark", methods=["PUT"])
def remark_exception(batch_id, exc_id):
    batch = Batch.query.get_or_404(batch_id)
    exc = ExceptionItem.query.get_or_404(exc_id)
    if exc.batch_id != batch_id:
        return jsonify({"error": "异常不属于该批次"}), 400
    payload = request.get_json(silent=True) or {}
    remarks = payload.get("remarks", "")
    exc.remarks = remarks
    log = AuditLog(batch_id=batch_id, action="REMARK", detail=f"异常#{exc_id} 备注: {remarks}")
    db.session.add(log)
    db.session.commit()
    return jsonify(exc.to_dict())


@bp.route("/api/batches/<int:batch_id>/exceptions/<int:exc_id>/resolve", methods=["PUT"])
def resolve_exception(batch_id, exc_id):
    batch = Batch.query.get_or_404(batch_id)
    exc = ExceptionItem.query.get_or_404(exc_id)
    if exc.batch_id != batch_id:
        return jsonify({"error": "异常不属于该批次"}), 400
    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "resolve")
    if action == "resolve":
        exc.status = EXCEPTION_STATUS_RESOLVED
        if exc.match_result_id:
            mr = MatchResult.query.get(exc.match_result_id)
            if mr:
                mr.exception_type = None
                mr.status = RESULT_STATUS_CONFIRMED
    elif action == "reject":
        exc.status = EXCEPTION_STATUS_RESOLVED
        if exc.match_result_id:
            mr = MatchResult.query.get(exc.match_result_id)
            if mr:
                mr.status = RESULT_STATUS_REJECTED
    log = AuditLog(batch_id=batch_id, action=f"EXCEPTION_{action.upper()}", detail=f"异常#{exc_id}: {exc.status}")
    db.session.add(log)
    db.session.commit()

    batch = Batch.query.get(batch_id)
    pending = ExceptionItem.query.filter_by(batch_id=batch_id, status=EXCEPTION_STATUS_PENDING).count()
    if pending == 0 and batch.status == "EXCEPTION_PENDING":
        batch.status = BATCH_STATUS_CONFIRMED
        log2 = AuditLog(batch_id=batch_id, action="AUTO_CONFIRM", detail="所有异常已处理，自动确认")
        db.session.add(log2)
        db.session.commit()

    return jsonify(exc.to_dict())


@bp.route("/api/batches/<int:batch_id>/confirm", methods=["POST"])
def confirm_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if not batch.can_transition(BATCH_STATUS_CONFIRMED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许确认操作"}), 400
    pending = ExceptionItem.query.filter_by(batch_id=batch_id, status=EXCEPTION_STATUS_PENDING).count()
    if pending > 0:
        return jsonify({"error": f"尚有{pending}条未处理异常，请先处理"}), 400
    batch.status = BATCH_STATUS_CONFIRMED
    for mr in batch.match_results:
        if mr.status == RESULT_STATUS_PENDING:
            mr.status = RESULT_STATUS_CONFIRMED
    log = AuditLog(batch_id=batch_id, action="CONFIRM", detail="批次确认入账")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@bp.route("/api/batches/<int:batch_id>/post", methods=["POST"])
def post_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if not batch.can_transition(BATCH_STATUS_POSTED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许入账操作"}), 400
    batch.status = BATCH_STATUS_POSTED
    log = AuditLog(batch_id=batch_id, action="POST", detail="批次已入账")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@bp.route("/api/batches/<int:batch_id>/rollback", methods=["POST"])
def rollback_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if not batch.can_transition(BATCH_STATUS_ROLLED_BACK):
        if batch.status == BATCH_STATUS_ROLLED_BACK:
            return jsonify({"error": "该批次已回滚，不允许重复回滚"}), 400
        return jsonify({"error": f"批次状态'{batch.status}'不允许回滚操作"}), 400
    batch.status = BATCH_STATUS_ROLLED_BACK
    log = AuditLog(batch_id=batch_id, action="ROLLBACK", detail="批次回滚")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@bp.route("/api/batches/<int:batch_id>/reset", methods=["POST"])
def reset_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if batch.status not in (BATCH_STATUS_ROLLED_BACK, BATCH_STATUS_FAILED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许重置操作"}), 400
    MatchResult.query.filter_by(batch_id=batch_id).delete()
    ExceptionItem.query.filter_by(batch_id=batch_id).delete()
    batch.status = BATCH_STATUS_CREATED
    log = AuditLog(batch_id=batch_id, action="RESET", detail="批次重置为创建状态")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@bp.route("/api/batches/<int:batch_id>/export", methods=["GET"])
def export_report(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    results = MatchResult.query.filter_by(batch_id=batch_id).all()
    exceptions = ExceptionItem.query.filter_by(batch_id=batch_id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "匹配ID", "采购单号", "发票号", "供应商编码", "供应商名称",
        "采购金额", "发票金额", "金额差异", "匹配类型", "是否异常",
        "异常类型", "匹配状态", "规则版本", "异常备注",
    ])
    for r in results:
        exc_remark = ""
        for e in exceptions:
            if e.match_result_id == r.id and e.remarks:
                exc_remark = e.remarks
                break
        writer.writerow([
            r.id,
            r.po.po_number if r.po else "",
            r.invoice.invoice_number if r.invoice else "",
            r.po.vendor_code if r.po else (r.invoice.vendor_code if r.invoice else ""),
            r.po.vendor_name if r.po else (r.invoice.vendor_name if r.invoice else ""),
            r.po_amount or "",
            r.invoice_amount or "",
            f"{r.amount_diff:.2f}" if r.amount_diff is not None else "",
            r.match_type,
            "是" if r.is_exception else "否",
            r.exception_type or "",
            r.status,
            r.rule_version or "",
            exc_remark,
        ])

    summary = batch.summary
    writer.writerow([])
    writer.writerow(["汇总信息"])
    writer.writerow(["匹配成功数", summary["matched_count"]])
    writer.writerow(["未匹配采购单数", summary["unmatched_po_count"]])
    writer.writerow(["未匹配发票数", summary["unmatched_invoice_count"]])
    writer.writerow(["待处理异常数", summary["exception_count"]])
    writer.writerow(["应付合计", summary["payable_total"]])
    writer.writerow(["容差百分比", batch.tolerance_pct])
    writer.writerow(["容差绝对值", batch.tolerance_abs])
    writer.writerow(["规则版本", batch.rule_version])

    output.seek(0)
    filename = f"reconciliation_batch_{batch_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


app = create_app()


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)
