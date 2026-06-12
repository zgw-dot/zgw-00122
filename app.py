import csv
import io
import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file, send_from_directory
from models import (
    db, Batch, MatchResult, ExceptionItem, ToleranceHistory, AuditLog,
    BATCH_STATUS_CREATED, BATCH_STATUS_CONFIRMED, BATCH_STATUS_POSTED,
    BATCH_STATUS_ROLLED_BACK, BATCH_STATUS_FAILED,
    EXCEPTION_DUPLICATE_ROLLBACK, EXCEPTION_STATUS_PENDING,
    EXCEPTION_STATUS_RESOLVED, RESULT_STATUS_CONFIRMED, RESULT_STATUS_REJECTED,
    VALID_TRANSITIONS, compute_rule_version, init_db,
)
from matcher import (
    process_batch, validate_and_import_po, validate_and_import_invoice, ValidationError,
)

app = Flask(__name__)
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reconciliation.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

init_db(app)


@app.route("/")
def index():
    tmpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    return send_from_directory(tmpl_dir, "index.html")


@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    recent = Batch.query.order_by(Batch.updated_at.desc()).limit(10).all()
    batches_data = []
    for b in recent:
        d = b.to_dict()
        d["summary"] = b.summary
        batches_data.append(d)
    return jsonify({"batches": batches_data})


@app.route("/api/batches", methods=["GET"])
def list_batches():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    pagination = Batch.query.order_by(Batch.updated_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    batches_data = []
    for b in pagination.items:
        d = b.to_dict()
        d["summary"] = b.summary
        batches_data.append(d)
    return jsonify({
        "batches": batches_data,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
    })


@app.route("/api/batches", methods=["POST"])
def create_batch():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "批次名称不能为空"}), 400
    tolerance_pct = data.get("tolerance_pct", 2.0)
    tolerance_abs = data.get("tolerance_abs", 100.0)
    rule_version = compute_rule_version(tolerance_pct, tolerance_abs)
    batch = Batch(
        name=name,
        tolerance_pct=tolerance_pct,
        tolerance_abs=tolerance_abs,
        rule_version=rule_version,
    )
    db.session.add(batch)
    db.session.flush()
    th = ToleranceHistory(
        batch_id=batch.id,
        tolerance_pct=tolerance_pct,
        tolerance_abs=tolerance_abs,
        rule_version=rule_version,
    )
    db.session.add(th)
    log = AuditLog(batch_id=batch.id, action="CREATE", detail=f"创建批次: {name}")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict()), 201


@app.route("/api/batches/<int:batch_id>", methods=["GET"])
def get_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    d = batch.to_dict()
    d["summary"] = batch.summary
    d["tolerance_history"] = [th.to_dict() for th in batch.tolerance_history]
    d["audit_logs"] = [al.to_dict() for al in batch.audit_logs]
    return jsonify(d)


@app.route("/api/batches/<int:batch_id>/tolerance", methods=["PUT"])
def update_tolerance(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if batch.status not in (BATCH_STATUS_CREATED, BATCH_STATUS_FAILED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许修改容差配置"}), 400
    data = request.get_json(force=True)
    tolerance_pct = data.get("tolerance_pct", batch.tolerance_pct)
    tolerance_abs = data.get("tolerance_abs", batch.tolerance_abs)
    batch.tolerance_pct = tolerance_pct
    batch.tolerance_abs = tolerance_abs
    batch.rule_version = compute_rule_version(tolerance_pct, tolerance_abs)
    th = ToleranceHistory(
        batch_id=batch.id,
        tolerance_pct=tolerance_pct,
        tolerance_abs=tolerance_abs,
        rule_version=batch.rule_version,
    )
    db.session.add(th)
    log = AuditLog(batch_id=batch.id, action="UPDATE_TOLERANCE", detail=f"容差更新: pct={tolerance_pct}, abs={tolerance_abs}, rule_version={batch.rule_version}")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@app.route("/api/batches/<int:batch_id>/upload-po", methods=["POST"])
def upload_po(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if batch.status not in (BATCH_STATUS_CREATED, BATCH_STATUS_FAILED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许上传采购单"}), 400
    if "file" not in request.files:
        return jsonify({"error": "未找到上传文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    try:
        validate_and_import_po(batch, f)
        return jsonify({"message": "采购单上传成功", "batch": batch.to_dict()})
    except ValidationError as e:
        return jsonify({"error": "采购单校验失败", "details": e.errors}), 400


@app.route("/api/batches/<int:batch_id>/upload-invoice", methods=["POST"])
def upload_invoice(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if batch.status not in (BATCH_STATUS_CREATED, BATCH_STATUS_FAILED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许上传发票"}), 400
    if "file" not in request.files:
        return jsonify({"error": "未找到上传文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    try:
        validate_and_import_invoice(batch, f)
        return jsonify({"message": "发票上传成功", "batch": batch.to_dict()})
    except ValidationError as e:
        return jsonify({"error": "发票校验失败", "details": e.errors}), 400


@app.route("/api/batches/<int:batch_id>/match", methods=["POST"])
def run_match(batch_id):
    try:
        result = process_batch(batch_id)
        return jsonify(result)
    except ValidationError as e:
        return jsonify({"error": "匹配失败", "details": e.errors}), 400


@app.route("/api/batches/<int:batch_id>/results", methods=["GET"])
def get_results(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    results = MatchResult.query.filter_by(batch_id=batch_id).all()
    return jsonify({
        "batch": batch.to_dict(),
        "summary": batch.summary,
        "results": [r.to_dict() for r in results],
    })


@app.route("/api/batches/<int:batch_id>/exceptions", methods=["GET"])
def get_exceptions(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    exceptions = ExceptionItem.query.filter_by(batch_id=batch_id).all()
    return jsonify({
        "batch": batch.to_dict(),
        "exceptions": [e.to_dict() for e in exceptions],
    })


@app.route("/api/batches/<int:batch_id>/exceptions/<int:exc_id>/remark", methods=["PUT"])
def update_exception_remark(batch_id, exc_id):
    exc = ExceptionItem.query.get_or_404(exc_id)
    if exc.batch_id != batch_id:
        return jsonify({"error": "异常项不属于该批次"}), 400
    data = request.get_json(force=True)
    exc.remarks = data.get("remarks", exc.remarks)
    db.session.commit()
    log = AuditLog(batch_id=batch_id, action="REMARK_EXCEPTION", detail=f"异常#{exc_id}备注: {exc.remarks}")
    db.session.add(log)
    db.session.commit()
    return jsonify(exc.to_dict())


@app.route("/api/batches/<int:batch_id>/exceptions/<int:exc_id>/resolve", methods=["PUT"])
def resolve_exception(batch_id, exc_id):
    exc = ExceptionItem.query.get_or_404(exc_id)
    if exc.batch_id != batch_id:
        return jsonify({"error": "异常项不属于该批次"}), 400
    data = request.get_json(force=True)
    action = data.get("action", "resolve")
    if action == "resolve":
        exc.status = EXCEPTION_STATUS_RESOLVED
        if exc.match_result_id:
            mr = MatchResult.query.get(exc.match_result_id)
            if mr:
                mr.is_exception = False
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


@app.route("/api/batches/<int:batch_id>/confirm", methods=["POST"])
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


@app.route("/api/batches/<int:batch_id>/post", methods=["POST"])
def post_batch(batch_id):
    batch = Batch.query.get_or_404(batch_id)
    if not batch.can_transition(BATCH_STATUS_POSTED):
        return jsonify({"error": f"批次状态'{batch.status}'不允许入账操作"}), 400
    batch.status = BATCH_STATUS_POSTED
    log = AuditLog(batch_id=batch_id, action="POST", detail="批次已入账")
    db.session.add(log)
    db.session.commit()
    return jsonify(batch.to_dict())


@app.route("/api/batches/<int:batch_id>/rollback", methods=["POST"])
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


@app.route("/api/batches/<int:batch_id>/reset", methods=["POST"])
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


@app.route("/api/batches/<int:batch_id>/export", methods=["GET"])
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


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)
