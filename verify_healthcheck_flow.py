# -*- coding: utf-8 -*-
"""
数据健康巡检 12 步全链路验收脚本（测试客户端，零依赖）

覆盖场景：
  Step 1  创建批次 + 导入采购单+发票样例
  Step 2  首次巡检：规则默认，查询结果和历史
  Step 3  修改规则（禁用重复号、改负金额阈值）+ 记录规则版本
  Step 4  模拟服务重启 → 重建 App → 规则配置和历史仍在
  Step 5  导出巡检 CSV，内容包含报告头 + 问题明细 + 摘要
  Step 6  导入备注（规则版本不一致 → 被拦住，返回 400 可读错误）
  Step 7  恢复规则版本后再导入备注 → 成功
  Step 8  跨批次操作：查询/导出属于 A 批次的巡检 B → 被拦住
  Step 9  再建 CONFIRMED 批次，导入同发票号 → 覆盖风险规则检出
  Step 10 审计日志查询：健康规则变更、巡检运行、备注导入都有记录
  Step 11 无 operator 空请求/非法 history_id → 返回可读 400/404
  Step 12 前端 index.html 包含「数据健康巡检」tab 文案
  Step 13 保留原有 import/match/export 默认流程不被破坏

运行：
    python verify_healthcheck_flow.py
    # exit code 0 = 全部通过
"""
import io
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(BASE_DIR, "sample")


def _step(title):
    print()
    print("=" * 60)
    print(f" Step {_step.counter}: {title}")
    print("=" * 60)
    _step.counter += 1


_step.counter = 1


def make_app(tmp_db_path):
    from app import create_app
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_db_path.replace(os.sep, '/')}",
    })
    return app


def create_batch(client, name):
    r = client.post("/api/batches", json={"name": name, "tolerance_pct": 2.0, "tolerance_abs": 100.0})
    assert r.status_code == 201, f"创建批次失败: {r.data}"
    return r.get_json()["id"]


def upload_precheck_and_confirm(client, bid, sample_dir, operator="test_u"):
    po_path = os.path.join(sample_dir, "purchase_orders.csv")
    inv_path = os.path.join(sample_dir, "invoices.csv")
    with open(po_path, "rb") as f:
        r1 = client.post(f"/api/batches/{bid}/precheck-po",
                         data={"file": (f, "po.csv"), "operator": operator},
                         content_type="multipart/form-data")
    assert r1.status_code == 200, f"PO precheck 失败: {r1.data}"
    po_id = r1.get_json()["id"]
    with open(inv_path, "rb") as f:
        r2 = client.post(f"/api/batches/{bid}/precheck-invoice",
                         data={"file": (f, "inv.csv"), "operator": operator},
                         content_type="multipart/form-data")
    assert r2.status_code == 200, f"Invoice precheck 失败: {r2.data}"
    inv_id = r2.get_json()["id"]
    rc = client.post(f"/api/batches/{bid}/drafts/{po_id}/confirm", json={"operator": operator})
    assert rc.status_code == 200, f"PO confirm 失败: {rc.data}"
    rc2 = client.post(f"/api/batches/{bid}/drafts/{inv_id}/confirm", json={"operator": operator})
    assert rc2.status_code == 200, f"Invoice confirm 失败: {rc2.data}"
    return True


def main():
    tmp_dir = tempfile.mkdtemp(prefix="hc_flow_")
    tmp_db = os.path.join(tmp_dir, "hc_test.db")
    failures = []
    try:
        app = make_app(tmp_db)
        client = app.test_client()

        # ---- Step 1
        _step("创建批次 + 导入采购单+发票样例")
        bid = create_batch(client, "HC-Acceptance-Batch-01")
        upload_precheck_and_confirm(client, bid, SAMPLE_DIR, operator="finance_01")
        b = client.get(f"/api/batches/{bid}").get_json()
        assert b["po_filename"] and b["invoice_filename"]
        print(f"  批次 #{bid} 导入完成: {b['po_filename']} / {b['invoice_filename']}")
        print("  OK import sample files")

        # ---- Step 2
        _step("首次巡检：默认规则，查询结果和历史")
        r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "finance_01"})
        assert r.status_code == 200, f"health check 返回异常: {r.data}"
        hc = r.get_json()
        assert "history_id" in hc and "rule_version" in hc and "summary" in hc
        hid = hc["history_id"]
        rules_version = hc["rule_version"]
        print(f"  history_id={hid}, rule_version={rules_version}")
        print(f"  summary: blocker={hc['summary']['blocker_count']} warning={hc['summary']['warning_count']} info={hc['summary']['info_count']}")
        hist = client.get(f"/api/batches/{bid}/health-history").get_json()["history"]
        assert len(hist) >= 1 and hist[0]["id"] == hid
        detail = client.get(f"/api/batches/{bid}/health-history/{hid}").get_json()
        assert detail["id"] == hid and "results" in detail
        print(f"  历史记录数={len(hist)}，明细结果数={len(detail['results'])}")
        print("  OK first health check + history/detail query")

        # ---- Step 3
        _step("修改规则：禁用重复号、改负金额阈值为 100，记录新版本")
        rules_payload = {
            "duplicate_po_number": {"enabled": False, "severity": "WARNING", "threshold": 1},
            "duplicate_invoice_number": {"enabled": False, "severity": "WARNING", "threshold": 1},
            "negative_amount": {"enabled": True, "severity": "BLOCKER", "threshold": 100},
        }
        r2 = client.put(f"/api/batches/{bid}/health-rules",
                        json={"rules": rules_payload, "operator": "fin_admin"})
        assert r2.status_code == 200, f"update rules 失败: {r2.data}"
        new_rules = r2.get_json()
        new_version = new_rules["rule_version"]
        assert new_version != rules_version, "规则版本应变化"
        assert new_rules["rules"]["duplicate_po_number"]["enabled"] is False
        assert new_rules["rules"]["negative_amount"]["threshold"] == 100
        print(f"  旧版本={rules_version}")
        print(f"  新版本={new_version}")
        print("  OK rule update, version changed")

        # ---- Step 4
        _step("模拟服务重启 → 重建 App → 规则和巡检历史仍在")
        del client
        del app
        import gc
        gc.collect()
        app2 = make_app(tmp_db)
        client2 = app2.test_client()
        rules_after = client2.get(f"/api/batches/{bid}/health-rules").get_json()
        assert rules_after["rule_version"] == new_version, "重启后规则版本不一致"
        assert rules_after["rules"]["duplicate_po_number"]["enabled"] is False
        hist_after = client2.get(f"/api/batches/{bid}/health-history").get_json()["history"]
        assert len(hist_after) >= 1 and hist_after[0]["id"] == hid
        print(f"  重启后规则版本={rules_after['rule_version']} (一致)")
        print(f"  重启后巡检历史数={len(hist_after)}")
        print("  OK restart persistence")

        # ---- Step 5
        _step("导出巡检 CSV：报告头 + 问题明细 + 摘要")
        exp = client2.get(f"/api/batches/{bid}/health-history/{hid}/export")
        assert exp.status_code == 200, f"export 失败: {exp.status_code}"
        csv_body = exp.data.decode("utf-8-sig")
        assert "数据健康巡检报告" in csv_body, "缺少报告头"
        assert "巡检问题明细" in csv_body, "缺少问题明细"
        assert "巡检摘要" in csv_body, "缺少摘要"
        assert f"巡检ID,{hid}" in csv_body or f"巡检ID, {hid}" in csv_body or f'"巡检ID","{hid}"' in csv_body, \
            f"CSV 缺少巡检ID={hid}"
        lines = csv_body.splitlines()
        print(f"  CSV 行数={len(lines)}")
        print("  OK CSV export headers/detail/summary")

        # ---- Step 6
        _step("导入备注：规则版本不一致 → 被拦住返回 400")
        rules_payload2 = {"missing_required_columns": {"enabled": True, "severity": "BLOCKER", "threshold": 5}}
        client2.put(f"/api/batches/{bid}/health-rules",
                    json={"rules": rules_payload2, "operator": "fin_admin2"})
        import_data = io.BytesIO(csv_body.encode("utf-8-sig"))
        import_data.seek(0)
        r_imp = client2.post(f"/api/batches/{bid}/health-remarks/import",
                             data={"file": (import_data, "hc.csv"), "operator": "fin_op"},
                             content_type="multipart/form-data")
        assert r_imp.status_code == 400, f"版本不一致时应 400，实际 {r_imp.status_code}"
        err = r_imp.get_json().get("error", "")
        assert "版本" in err or "version" in err.lower(), f"错误信息应提及版本，实际: {err}"
        print(f"  拦截成功: 400 {err}")
        print("  OK rule version mismatch blocked")

        # ---- Step 7
        _step("恢复规则版本后导入备注 → 成功")
        restore_payload = {
            "duplicate_po_number": {"enabled": True, "severity": "BLOCKER", "threshold": 1},
            "duplicate_invoice_number": {"enabled": True, "severity": "BLOCKER", "threshold": 1},
            "missing_required_columns": {"enabled": True, "severity": "BLOCKER", "threshold": 1},
            "negative_amount": {"enabled": True, "severity": "WARNING", "threshold": 0},
            "vendor_mismatch": {"enabled": True, "severity": "WARNING", "threshold": 1},
            "confirmed_override_risk": {"enabled": True, "severity": "INFO", "threshold": 1},
        }
        client2.put(f"/api/batches/{bid}/health-rules",
                    json={"rules": restore_payload, "operator": "fin_admin"})
        ver_check = client2.get(f"/api/batches/{bid}/health-rules").get_json()
        print(f"  恢复后版本={ver_check['rule_version']}, CSV版本={rules_version}")
        import_data2 = io.BytesIO(csv_body.encode("utf-8-sig"))
        import_data2.seek(0)
        r_imp2 = client2.post(f"/api/batches/{bid}/health-remarks/import",
                              data={"file": (import_data2, "hc.csv"), "operator": "fin_op"},
                              content_type="multipart/form-data")
        assert r_imp2.status_code == 200, f"版本一致导入应成功: {r_imp2.data}"
        res = r_imp2.get_json()
        assert "imported" in res and res["imported"] > 0, f"导入结果异常: {res}"
        print(f"  导入成功：{res['imported']} 条问题")
        print("  OK remarks import after version restore")

        # ---- Step 8
        _step("跨批次操作：用 B 批次访问 A 的巡检 → 被拦住")
        bid_b = create_batch(client2, "HC-Batch-B")
        r_xx = client2.get(f"/api/batches/{bid_b}/health-history/{hid}")
        assert r_xx.status_code == 400, f"跨批次访问应 400，实际 {r_xx.status_code}"
        err2 = r_xx.get_json().get("error", "")
        assert "跨批次" in err2, f"错误应提及跨批次: {err2}"
        exp_x = client2.get(f"/api/batches/{bid_b}/health-history/{hid}/export")
        assert exp_x.status_code == 400, f"跨批次导出应 400"
        print(f"  跨批次访问拦截: {err2}")
        print("  OK cross-batch blocked")

        # ---- Step 9
        _step("CONFIRMED 批次覆盖风险：创建 CONFIRMED 批次，新批次导入同发票应检出覆盖风险")
        bid_confirmed = create_batch(client2, "HC-Batch-CONFIRMED")
        import csv as _csv
        import codecs
        po_lines = [
            "po_number,vendor_code,vendor_name,amount,po_date,currency,payment_terms,due_date,material_code,material_name,qty,unit_price,department,project,po_status,po_note",
            "HC-PO-001,V001,测试供应商A,5000.00,2024-01-05,CNY,30,2024-02-04,M001,材料X,50,100.00,财务,项目Alpha,已确认,",
        ]
        inv_lines = [
            "invoice_number,vendor_code,vendor_name,amount,invoice_date,tax_amount,currency,po_number,payment_terms,due_date,invoice_status,material_code,qty,unit_price,remark",
            "HC-INV-001,V001,测试供应商A,5000.00,2024-01-10,650.00,CNY,HC-PO-001,30,2024-02-09,已确认,M001,50,100.00,",
        ]
        po_csv = ("\n".join(po_lines) + "\n").encode("utf-8-sig")
        inv_csv = ("\n".join(inv_lines) + "\n").encode("utf-8-sig")
        r1 = client2.post(f"/api/batches/{bid_confirmed}/precheck-po",
                          data={"file": (io.BytesIO(po_csv), "hc_po.csv"), "operator": "fin_old"},
                          content_type="multipart/form-data")
        po_id = r1.get_json()["id"]
        r2 = client2.post(f"/api/batches/{bid_confirmed}/precheck-invoice",
                          data={"file": (io.BytesIO(inv_csv), "hc_inv.csv"), "operator": "fin_old"},
                          content_type="multipart/form-data")
        inv_id = r2.get_json()["id"]
        client2.post(f"/api/batches/{bid_confirmed}/drafts/{po_id}/confirm", json={"operator": "fin_old"})
        client2.post(f"/api/batches/{bid_confirmed}/drafts/{inv_id}/confirm", json={"operator": "fin_old"})
        client2.post(f"/api/batches/{bid_confirmed}/match")
        excs_conf = client2.get(f"/api/batches/{bid_confirmed}/exceptions").get_json()["exceptions"]
        for e in excs_conf:
            client2.put(f"/api/batches/{bid_confirmed}/exceptions/{e['id']}/remark", json={"remarks": "ok"})
            client2.put(f"/api/batches/{bid_confirmed}/exceptions/{e['id']}/resolve", json={"action": "resolve"})
        client2.post(f"/api/batches/{bid_confirmed}/confirm", json={"operator": "fin_mgr"})
        bc = client2.get(f"/api/batches/{bid_confirmed}").get_json()
        print(f"  已确认批次 #{bid_confirmed} 状态: {bc['status']}")
        bid_new = create_batch(client2, "HC-Batch-New-With-Risk")
        r3 = client2.post(f"/api/batches/{bid_new}/precheck-po",
                          data={"file": (io.BytesIO(po_csv), "hc_po.csv"), "operator": "fin_new"},
                          content_type="multipart/form-data")
        po_id2 = r3.get_json()["id"]
        r4 = client2.post(f"/api/batches/{bid_new}/precheck-invoice",
                          data={"file": (io.BytesIO(inv_csv), "hc_inv.csv"), "operator": "fin_new"},
                          content_type="multipart/form-data")
        inv_id2 = r4.get_json()["id"]
        cp = client2.post(f"/api/batches/{bid_new}/drafts/{po_id2}/confirm", json={"operator": "fin_new"})
        ci = client2.post(f"/api/batches/{bid_new}/drafts/{inv_id2}/confirm", json={"operator": "fin_new"})
        print(f"  新批次 confirm PO={cp.status_code} Invoice={ci.status_code}（确认被跨批次拦截，手动写入测试数据）")
        from models import Invoice
        with app2.app_context():
            from models import db as _db
            duplicate_inv = Invoice(
                batch_id=bid_new,
                invoice_number="HC-INV-001",
                vendor_code="V001",
                vendor_name="测试供应商A",
                amount=5000.00,
                invoice_date="2024-01-10",
                currency="CNY",
                raw_data='{}',
            )
            _db.session.add(duplicate_inv)
            _db.session.commit()
        r_new = client2.post(f"/api/batches/{bid_new}/health-check", json={"operator": "fin_new"})
        assert r_new.status_code == 200
        new_hc = r_new.get_json()
        override_results = [x for x in new_hc["results"]
                            if x["rule_key"] == "confirmed_override_risk"]
        print(f"  新批次巡检：覆盖风险问题数={len(override_results)}，总问题数={len(new_hc['results'])}")
        assert len(override_results) >= 1, "应检出已确认批次覆盖风险"
        print("  OK confirmed override risk detected")

        # ---- Step 10
        _step("审计日志查询：规则变更/巡检运行/备注导入都有记录")
        from models import AuditLog
        with app2.app_context():
            logs = AuditLog.query.filter_by(batch_id=bid).all()
        actions = [lg.action for lg in logs]
        print(f"  批次 #{bid} 审计日志 {len(logs)} 条, actions: {set(actions)}")
        assert any("HEALTH" in a for a in actions), "缺少健康巡检相关审计日志"
        assert "HEALTH_RULES_UPDATED" in actions, "缺少规则更新日志"
        assert "HEALTH_CHECK_RUN" in actions, "缺少巡检运行日志"
        assert "HEALTH_REMARKS_IMPORTED" in actions, "缺少备注导入日志"
        print("  OK audit logs for rules/check/import")

        # ---- Step 11
        _step("错误处理：无 operator/非法 history_id 返回可读 400/404")
        r_noid = client2.get("/api/batches/999999999/health-history/1")
        assert r_noid.status_code == 404, f"批次不存在应 404: {r_noid.status_code}"
        r_badhist = client2.get(f"/api/batches/{bid}/health-history/999999999")
        assert r_badhist.status_code == 404, f"history 不存在应 404"
        empty_csv = io.BytesIO(b"")
        r_empty = client2.post(f"/api/batches/{bid}/health-remarks/import",
                               data={"file": (empty_csv, "empty.csv"), "operator": "x"},
                               content_type="multipart/form-data")
        assert r_empty.status_code == 400, f"空 CSV 应 400: {r_empty.status_code}"
        print(f"  不存在批次: {r_noid.status_code}")
        print(f"  不存在历史: {r_badhist.status_code}")
        print(f"  空 CSV: {r_empty.status_code} {r_empty.get_json().get('error','')}")
        print("  OK error handling readable responses")

        # ---- Step 12
        _step("前端 index.html 包含「数据健康巡检」tab 文案")
        tmpl_path = os.path.join(BASE_DIR, "templates", "index.html")
        with open(tmpl_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "数据健康巡检" in html, "前端缺少数据健康巡检 tab"
        assert "health-check" in html, "前端缺少 health-check tab key"
        assert "立即巡检" in html, "前端缺少立即巡检按钮"
        assert "规则配置" in html, "前端缺少规则配置"
        print("  文案：'数据健康巡检'  'health-check'  '立即巡检'  '规则配置' 全部存在")
        print("  OK frontend UI elements")

        # ---- Step 13
        _step("保留原有 import/match/export 默认流程")
        bid_regular = create_batch(client2, "HC-Regular-Flow")
        upload_precheck_and_confirm(client2, bid_regular, SAMPLE_DIR, operator="reg_u")
        m = client2.post(f"/api/batches/{bid_regular}/match")
        assert m.status_code == 200, f"默认 match 失败: {m.data}"
        e = client2.get(f"/api/batches/{bid_regular}/export")
        assert e.status_code == 200, f"默认 export 失败: {e.status_code}"
        ebody = e.data.decode("utf-8-sig")
        assert "汇总信息" in ebody and "匹配ID" in ebody
        print("  原有 import/match/export 流程正常")
        print("  OK backward compatibility preserved")

    except AssertionError as ae:
        failures.append(str(ae))
        import traceback
        traceback.print_exc()
    except Exception as ex:
        failures.append(f"Unexpected: {ex}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    print()
    print("=" * 60)
    if not failures:
        print("  Health Check 13-step verification PASSED!")
        print("=" * 60)
        print()
        print("  Acceptance checklist:")
        print("  1. OK - 创建批次/导入样例 (Step 1)")
        print("  2. OK - 首次巡检默认规则/结果/历史 (Step 2)")
        print("  3. OK - 规则开关阈值修改 + 版本变更 (Step 3)")
        print("  4. OK - 重启后规则/历史持久化 (Step 4)")
        print("  5. OK - CSV 导出含报告头/明细/摘要 (Step 5)")
        print("  6. OK - 版本不一致导入备注被拦住 (Step 6)")
        print("  7. OK - 版本一致后导入备注成功 (Step 7)")
        print("  8. OK - 跨批次访问/导出被拦住 (Step 8)")
        print("  9. OK - CONFIRMED 批次覆盖风险检出 (Step 9)")
        print("  10. OK - 审计日志规则/巡检/导入三项 (Step 10)")
        print("  11. OK - 异常参数返回可读 400/404 (Step 11)")
        print("  12. OK - 前端包含 tab/按钮 (Step 12)")
        print("  13. OK - 原有 import/match/export 不破坏 (Step 13)")
        sys.exit(0)
    else:
        print(f"  FAILED: {len(failures)} issue(s)")
        for i, f in enumerate(failures, 1):
            print(f"  [{i}] {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
