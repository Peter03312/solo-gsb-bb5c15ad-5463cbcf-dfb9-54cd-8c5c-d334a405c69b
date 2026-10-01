"""银行回单批量导入与核验：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_receipts.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rcp_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 W + 权利人 A/B，播放 100 并出单 => A 60、B 40 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}
        c.post("/statements/issue", json=period)

        # ---- 导入前登记两笔付款：E-A1/A 60（匹配）、E-B1/B 40 ----
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E-A1", "amount": 60})
        check("pay A", r.status_code == 201, r.text)
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "E-B1", "amount": 40})
        check("pay B", r.status_code == 201, r.text)
        # 3 月再播放 100 并出单：A +60、B +40，给后续付款制造正余额
        c.post("/plays/import", json={
            "import_id": "P2", "work_code": "W",
            "occurred_at": "2026-03-10T20:00:00+08:00", "amount": 100})
        period2 = {"start": "2026-03-01T00:00:00+08:00",
                   "end": "2026-04-01T00:00:00+08:00"}
        c.post("/statements/issue", json=period2)
        # E-HLD 登记在 B 名下 10，回单写 A（权利人不符，金额一致）
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "E-HLD", "amount": 10})
        check("pay E-HLD on B", r.status_code == 201, r.text)
        # E-AMT 登记 30，回单写 31（金额不符）
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "E-AMT", "amount": 30})
        check("pay E-AMT on B 30", r.status_code == 201, r.text)
        # 两项均不符：E-BOTH 登记在 B 名下 7，回单写 A/8（B 余 0，先补正余额）
        c.post("/plays/import", json={
            "import_id": "P3", "work_code": "W",
            "occurred_at": "2026-03-20T20:00:00+08:00", "amount": 200})
        c.post("/statements/issue", json=period2)  # 追补 A +120、B +80
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "E-BOTH", "amount": 7})
        check("pay E-BOTH on B", r.status_code == 201, r.text)

        # ---- 无效字段整批拒绝（422，不落批次/回单）----
        bad_payloads = [
            {"batch_no": "BAD", "lines": []},  # 空行列表
            {"batch_no": "BAD", "lines": [  # 金额非正
                {"receipt_no": "X1", "external_ref": "EX1", "holder_code": "A", "amount": 0}]},
            {"batch_no": "BAD", "lines": [  # 金额负数
                {"receipt_no": "X1", "external_ref": "EX1", "holder_code": "A", "amount": -1}]},
            {"batch_no": "BAD", "lines": [  # 金额非整数
                {"receipt_no": "X1", "external_ref": "EX1", "holder_code": "A", "amount": 1.5}]},
            {"batch_no": "BAD", "lines": [  # 布尔
                {"receipt_no": "X1", "external_ref": "EX1", "holder_code": "A", "amount": True}]},
            {"batch_no": "BAD", "lines": [  # 缺字段
                {"receipt_no": "X1", "holder_code": "A", "amount": 1}]},
            {"batch_no": "", "lines": [  # 空批次号
                {"receipt_no": "X1", "external_ref": "EX1", "holder_code": "A", "amount": 1}]},
        ]
        for i, p in enumerate(bad_payloads):
            r = c.post("/bank-receipts/import", json=p)
            check(f"invalid payload {i} 422", r.status_code == 422, r.text)
        check("bad batch never stored",
              c.get("/bank-receipts/BAD").status_code == 404)

        # ---- 批内重复：流水号 ----
        r = c.post("/bank-receipts/import", json={
            "batch_no": "DUP-REF",
            "lines": [
                {"receipt_no": "DR1", "external_ref": "SAME", "holder_code": "A", "amount": 1},
                {"receipt_no": "DR2", "external_ref": "SAME", "holder_code": "A", "amount": 2},
            ]})
        check("dup ref in batch 422",
              r.status_code == 422 and r.json()["error"] == "duplicate_external_ref_in_batch",
              r.text)
        check("dup-ref batch not stored", c.get("/bank-receipts/DUP-REF").status_code == 404)

        # ---- 批内重复：回单编号 ----
        r = c.post("/bank-receipts/import", json={
            "batch_no": "DUP-NO",
            "lines": [
                {"receipt_no": "SAME-NO", "external_ref": "R1", "holder_code": "A", "amount": 1},
                {"receipt_no": "SAME-NO", "external_ref": "R2", "holder_code": "A", "amount": 2},
            ]})
        check("dup receipt_no in batch 422",
              r.status_code == 422 and r.json()["error"] == "duplicate_receipt_no_in_batch",
              r.text)
        check("dup-no batch not stored", c.get("/bank-receipts/DUP-NO").status_code == 404)

        # ---- 正式导入 B1：匹配/未登记/权利人不符/金额不符/两项均不符 ----
        b1_lines = [
            {"receipt_no": "RC1", "external_ref": "E-A1", "holder_code": "A", "amount": 60},
            {"receipt_no": "RC2", "external_ref": "E-MISSING", "holder_code": "A", "amount": 5},
            {"receipt_no": "RC3", "external_ref": "E-HLD", "holder_code": "A", "amount": 10},
            {"receipt_no": "RC4", "external_ref": "E-AMT", "holder_code": "B", "amount": 31},
            {"receipt_no": "RC5", "external_ref": "E-BOTH", "holder_code": "A", "amount": 8},
        ]
        r = c.post("/bank-receipts/import", json={"batch_no": "B1", "lines": b1_lines})
        body = r.json()
        check("B1 import 201", r.status_code == 201 and body["status"] == "imported", r.text)
        v1 = body["latest_version"]
        by_no = {l["receipt_no"]: l for l in v1["lines"]}
        check("B1 statuses",
              by_no["RC1"]["status"] == "matched"
              and by_no["RC2"]["status"] == "payment_unregistered"
              and by_no["RC3"]["status"] == "holder_mismatch"
              and by_no["RC4"]["status"] == "amount_mismatch"
              and by_no["RC5"]["status"] == "both_mismatch",
              str({k: v["status"] for k, v in by_no.items()}))
        check("B1 mismatches detail",
              by_no["RC1"]["mismatches"] == []
              and by_no["RC2"]["mismatches"] == ["payment_unregistered"]
              and by_no["RC3"]["mismatches"] == ["holder"]
              and by_no["RC4"]["mismatches"] == ["amount"]
              and set(by_no["RC5"]["mismatches"]) == {"holder", "amount"},
              str({k: v["mismatches"] for k, v in by_no.items()}))
        check("B1 payment snapshot on lines",
              by_no["RC3"]["payment"]["holder_code"] == "B"
              and by_no["RC3"]["payment"]["amount"] == 10
              and by_no["RC5"]["payment"]["holder_code"] == "B"
              and by_no["RC5"]["payment"]["amount"] == 7
              and by_no["RC2"]["payment"] is None, str(by_no["RC2"]))
        check("B1 counts",
              v1["counts"] == {"total": 5, "matched": 1, "payment_unregistered": 1,
                               "holder_mismatch": 1, "amount_mismatch": 1,
                               "both_mismatch": 1, "duplicate_receipt": 0,
                               "effective": 5, "duplicate_receipts": 0,
                               "differences": 4}, str(v1["counts"]))
        check("B1 difference lines",
              {l["receipt_no"] for l in v1["difference_lines"]}
              == {"RC2", "RC3", "RC4", "RC5"}
              and v1["duplicate_lines"] == []
              and v1["changed_lines"] == []
              and v1["created_by"] == "import" and v1["version_no"] == 1)
        check("B1 raw receipts all effective",
              all(x["duplicate"] is False for x in body["raw_receipts"]))

        # 核验不改写付款、余额、单据
        led = c.get("/rightsholders/B/ledger").json()
        check("verification does not touch payments",
              led["paid_total"] == 87 and len(led["payments"]) == 4, str(led["payments"]))

        # ---- 同批次同内容重试 => 200 duplicate_batch，返回原批次 ----
        r = c.post("/bank-receipts/import", json={"batch_no": "B1", "lines": b1_lines})
        body2 = r.json()
        check("B1 retry idempotent 200",
              r.status_code == 200 and body2["status"] == "duplicate_batch", r.text)
        check("B1 retry same version",
              body2["batch"]["version_count"] == 1
              and body2["latest_version"]["version_no"] == 1
              and body2["latest_version"]["created_at"] == v1["created_at"], r.text)

        # 同批次异内容 => 409 receipt_batch_conflict
        r = c.post("/bank-receipts/import", json={
            "batch_no": "B1",
            "lines": b1_lines[:-1]})  # 少一行 => 异内容
        check("B1 different content 409",
              r.status_code == 409 and r.json()["error"] == "receipt_batch_conflict", r.text)

        # ---- 回单编号跨批次复用（异内容复用）=> 整批拒绝 ----
        r = c.post("/bank-receipts/import", json={
            "batch_no": "B2",
            "lines": [
                {"receipt_no": "RC1", "external_ref": "E-OTHER", "holder_code": "A", "amount": 9},
                {"receipt_no": "RC9", "external_ref": "E-OTHER2", "holder_code": "A", "amount": 9},
            ]})
        check("receipt_no cross-batch conflict",
              r.status_code == 409 and r.json()["error"] == "receipt_no_conflict"
              and r.json()["detail"]["existing_batch_no"] == "B1", r.text)
        check("B2 not stored", c.get("/bank-receipts/B2").status_code == 404)

        # ---- 跨批次同一流水号：仅首次成功导入者参与核验，其余标重复 ----
        # B3 含：E-A1（B1 首次已导入 => 重复）、E-NEW1（本批首次 => 未登记）、
        #        E-BOTH（B1 已导入 => 重复）
        r = c.post("/bank-receipts/import", json={
            "batch_no": "B3",
            "lines": [
                {"receipt_no": "RC10", "external_ref": "E-A1",
                 "holder_code": "A", "amount": 60},
                {"receipt_no": "RC11", "external_ref": "E-NEW1",
                 "holder_code": "A", "amount": 50},
                {"receipt_no": "RC12", "external_ref": "E-BOTH",
                 "holder_code": "A", "amount": 8},
            ]})
        body3 = r.json()
        check("B3 import 201", r.status_code == 201, r.text)
        b3 = {l["receipt_no"]: l for l in body3["latest_version"]["lines"]}
        check("B3 duplicates marked",
              b3["RC10"]["status"] == "duplicate_receipt"
              and b3["RC10"]["duplicate"] is True
              and b3["RC10"]["duplicate_of_batch_no"] == "B1"
              and b3["RC12"]["status"] == "duplicate_receipt"
              and b3["RC12"]["duplicate_of_batch_no"] == "B1"
              and b3["RC11"]["status"] == "payment_unregistered"
              and b3["RC11"]["duplicate"] is False, str(body3))
        check("B3 counts",
              body3["latest_version"]["counts"] == {
                  "total": 3, "matched": 0, "payment_unregistered": 1,
                  "holder_mismatch": 0, "amount_mismatch": 0, "both_mismatch": 0,
                  "duplicate_receipt": 2, "effective": 1,
                  "duplicate_receipts": 2, "differences": 1},
              str(body3["latest_version"]["counts"]))
        # 重复行不出现在 difference_lines，在 duplicate_lines
        check("B3 dup/diff split",
                  {l["receipt_no"] for l in body3["latest_version"]["duplicate_lines"]}
                  == {"RC10", "RC12"}
                  and [l["receipt_no"] for l in body3["latest_version"]["difference_lines"]]
                  == ["RC11"])
        # 原始回单始终可查（含重复标记与归属）
        raw = {x["receipt_no"]: x for x in body3["raw_receipts"]}
        check("B3 raw duplicates kept",
              raw["RC10"]["duplicate"] is True
              and raw["RC10"]["duplicate_of_batch_no"] == "B1"
              and raw["RC11"]["duplicate"] is False)

        # ---- 付款迟到后复核：E-MISSING 在 A 名下补登 5 分后 B1 应出新版 ----
        # A 当前余额：60(2月)+60(3月)-60 = 60
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E-MISSING", "amount": 5})
        check("late pay E-MISSING", r.status_code == 201, r.text)
        r = c.post("/bank-receipts/B1/review")
        check("review needs POST not GET",
              c.get("/bank-receipts/B1/review").status_code == 405)
        rev = r.json()
        check("B1 review new version 201",
              r.status_code == 201 and rev["status"] == "new_version"
              and rev["new_version"] is True, r.text)
        v2 = rev["latest_version"]
        check("B1 v2 number/meta",
              v2["version_no"] == 2 and v2["created_by"] == "review"
              and v2["changed_lines_count"] == 1, str(v2))
        ch = {(x["line_no"], x["from_status"], x["status"]) for x in v2["changed_lines"]}
        check("B1 v2 changed line RC2",
              ch == {(2, "payment_unregistered", "matched")}, str(ch))
        v2by = {l["receipt_no"]: l for l in v2["lines"]}
        check("B1 v2 statuses",
              v2by["RC2"]["status"] == "matched" and v2by["RC2"]["mismatches"] == []
              and v2by["RC2"]["payment"]["amount"] == 5
              and v2by["RC2"]["payment"]["holder_code"] == "A"
              and v2by["RC1"]["status"] == "matched"
              and v2by["RC5"]["status"] == "both_mismatch", str(v2by["RC2"]))
        check("B1 v2 counts",
              v2["counts"]["matched"] == 2 and v2["counts"]["payment_unregistered"] == 0
              and v2["counts"]["holder_mismatch"] == 1
              and v2["counts"]["amount_mismatch"] == 1
              and v2["counts"]["both_mismatch"] == 1
              and v2["counts"]["differences"] == 3, str(v2["counts"]))
        check("B1 v2 difference lines",
              {l["receipt_no"] for l in v2["difference_lines"]}
              == {"RC3", "RC4", "RC5"})

        # 旧版始终可查：v1 仍是 payment_unregistered
        r = c.get("/bank-receipts/B1/versions/1")
        old = r.json()
        check("B1 v1 still available",
              r.status_code == 200 and old["version_no"] == 1
              and {l["receipt_no"]: l["status"] for l in old["lines"]}["RC2"]
              == "payment_unregistered"
              and old["counts"]["payment_unregistered"] == 1, r.text)
        check("B1 v1 changed_lines empty", old["changed_lines"] == [])
        # 批次详情显示两版
        r = c.get("/bank-receipts/B1")
        check("B1 summary 2 versions",
              r.json()["batch"]["version_count"] == 2
              and r.json()["batch"]["version_numbers"] == [1, 2]
              and r.json()["batch"]["latest_version_no"] == 2)

        # ---- 结论不变再复核 => 不新增版本 ----
        r = c.post("/bank-receipts/B1/review")
        rev2 = r.json()
        check("B1 review unchanged 200",
              r.status_code == 200 and rev2["status"] == "unchanged"
              and rev2["new_version"] is False and rev2["changed_lines"] == [], r.text)
        check("still 2 versions",
              c.get("/bank-receipts/B1").json()["batch"]["version_count"] == 2)

        # 连续多次无变化复核都不新增版本
        for _ in range(3):
            rr = c.post("/bank-receipts/B1/review")
            assert rr.status_code == 200 and rr.json()["status"] == "unchanged"
        check("repeated unchanged reviews",
              c.get("/bank-receipts/B1").json()["batch"]["version_count"] == 2)

        # ---- 再迟到两笔付款：E-NEW2 未登记->匹配；E-X1 付款在 B、回单写 A ----
        # B 当前余 80-7（E-HLD10、E-AMT30、E-BOTH7 已在 40+80=80 余额内支付 47）= 33
        r = c.post("/payments", json={"holder_code": "B", "external_ref": "E-X1", "amount": 12})
        check("pay E-X1 on B", r.status_code == 201, r.text)
        r = c.post("/bank-receipts/import", json={
            "batch_no": "B4",
            "lines": [
                {"receipt_no": "RC20", "external_ref": "E-NEW2",
                 "holder_code": "A", "amount": 40},
                {"receipt_no": "RC21", "external_ref": "E-X1",
                 "holder_code": "A", "amount": 12},
            ]})
        check("B4 import", r.status_code == 201, r.text)
        b4v1 = r.json()["latest_version"]
        check("B4 v1 statuses",
              {l["receipt_no"]: l["status"] for l in b4v1["lines"]}
              == {"RC20": "payment_unregistered", "RC21": "holder_mismatch"}, r.text)

        # 补登 E-NEW2 在 A 名下 40（A 余额：60+60+120-60-5=175）
        r = c.post("/payments", json={"holder_code": "A", "external_ref": "E-NEW2", "amount": 40})
        check("pay E-NEW2 on A", r.status_code == 201, r.text)
        r = c.post("/bank-receipts/B4/review")
        b4v2 = r.json()
        check("B4 review one change",
              r.status_code == 201
              and {(x["line_no"], x["from_status"], x["status"])
                   for x in b4v2["latest_version"]["changed_lines"]}
              == {(1, "payment_unregistered", "matched")}, r.text)
        # RC21 仍权利人不符（付款挂 B 不可变），差异行保留
        check("B4 v2 holder mismatch persists",
              {l["receipt_no"]: l["status"] for l in b4v2["latest_version"]["lines"]}
              == {"RC20": "matched", "RC21": "holder_mismatch"}, r.text)

        # ---- 不存在的资源 ----
        check("batch 404", c.get("/bank-receipts/NOPE").status_code == 404
              and c.get("/bank-receipts/NOPE").json()["error"] == "receipt_batch_not_found")
        check("review 404",
              c.post("/bank-receipts/NOPE/review").status_code == 404
              and c.post("/bank-receipts/NOPE/review").json()["error"]
              == "receipt_batch_not_found")
        check("version 404",
              c.get("/bank-receipts/B1/versions/99").status_code == 404
              and c.get("/bank-receipts/B1/versions/99").json()["error"]
              == "receipt_version_not_found")
        check("version batch missing 404 code",
              c.get("/bank-receipts/NOPE/versions/1").json()["error"]
              == "receipt_batch_not_found")

        # ---- 冻结性：回单/批次/版本/版本行不可修改删除（触发器强制）----
        with db.pool.connection() as conn:
            frozen = {}
            for label, sql, args in [
                ("batch_upd", "UPDATE receipt_batches SET line_count = 9 WHERE batch_no='B1'", None),
                ("batch_del", "DELETE FROM receipt_batches WHERE batch_no='B4'", None),
                ("rcpt_upd", "UPDATE bank_receipts SET amount = 1 WHERE receipt_no='RC1'", None),
                ("rcpt_del", "DELETE FROM bank_receipts WHERE receipt_no='RC10'", None),
                ("ver_upd",
                 "UPDATE receipt_verification_versions SET version_no = 99 WHERE batch_no='B1'",
                 None),
                ("verline_del",
                 "DELETE FROM receipt_verification_lines WHERE line_no = 1 AND version_id = 1",
                 None),
            ]:
                try:
                    conn.execute(sql)
                    frozen[label] = False
                except psycopg.Error:
                    frozen[label] = True
        check("receipt data frozen", all(frozen.values()), str(frozen))
        # 触发器拒绝后数据仍在
        check("B1 intact after tamper attempts",
              c.get("/bank-receipts/B1").json()["batch"]["line_count"] == 5
              and c.get("/bank-receipts/B1").json()["batch"]["version_count"] == 2)

        # ---- 并发：同批次同内容并发导入 => 一次 201，其余 200 幂等，只有一个 v1 ----
        r = c.post("/bank-receipts/import", json={
            "batch_no": "RACE1",
            "lines": [{"receipt_no": "RRC1", "external_ref": "RACE-E1",
                       "holder_code": "A", "amount": 1}]})
        assert r.status_code in (200, 201), r.text
        outcomes = []

        def fire_same_batch():
            rr = c.post("/bank-receipts/import", json={
                "batch_no": "RACE1",
                "lines": [{"receipt_no": "RRC1", "external_ref": "RACE-E1",
                           "holder_code": "A", "amount": 1}]})
            outcomes.append((rr.status_code, rr.json().get("status")))

        ts = [threading.Thread(target=fire_same_batch) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        check("concurrent same batch idempotent",
              all(s == 200 and st == "duplicate_batch" for s, st in outcomes)
              or (sum(1 for s, _ in outcomes if s == 201) <= 1), str(outcomes))
        summary = c.get("/bank-receipts/RACE1").json()
        check("race batch single version",
              summary["batch"]["version_count"] == 1
              and len(summary["raw_receipts"]) == 1, str(summary["batch"]))

        # ---- 并发：不同批次导入同一新流水号 => 恰好一批的回单有效，另一批标重复 ----
        race_outcomes = {}

        def fire_distinct_batch(suffix):
            rr = c.post("/bank-receipts/import", json={
                "batch_no": f"RACER-{suffix}",
                "lines": [{"receipt_no": f"RRC-{suffix}",
                           "external_ref": "RACE-SHARED",
                           "holder_code": "A", "amount": 3}]})
            race_outcomes[suffix] = rr

        ts = [threading.Thread(target=fire_distinct_batch, args=(i,)) for i in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        effective_owner = None
        duplicate_owners = []
        for i, rr in race_outcomes.items():
            assert rr.status_code == 201, (i, rr.text)
            line = rr.json()["latest_version"]["lines"][0]
            if line["duplicate"]:
                duplicate_owners.append((i, line["duplicate_of_batch_no"]))
            else:
                assert effective_owner is None
                effective_owner = i
        check("concurrent cross-batch first-wins",
              effective_owner is not None and len(duplicate_owners) == 5
              and all(owner == f"RACER-{effective_owner}" for _, owner in duplicate_owners),
              f"winner={effective_owner} dups={duplicate_owners}")
        # 全库中该流水号只有 1 条有效回单
        with db.pool.connection() as conn:
            n_eff = conn.execute(
                "SELECT count(*) AS n FROM bank_receipts "
                "WHERE external_ref='RACE-SHARED' AND duplicate=false").fetchone()["n"]
        check("exactly one effective receipt per ref", n_eff == 1, str(n_eff))

        # ---- 并发：付款登记与复核同时进行，结论来自同一已提交状态 ----
        # B3 中 RC11=E-NEW1 尚未登记；多个复核 + 一笔付款并发，
        # 每个生成版本的计数必须自洽（matched + 各不符 + 未登记 + 重复 = 总数）。
        review_results = []

        def fire_review():
            rr = c.post("/bank-receipts/B3/review")
            review_results.append(rr)

        def fire_late_pay():
            # A 余 175-40=135，足够
            c.post("/payments",
                   json={"holder_code": "A", "external_ref": "E-NEW1", "amount": 50})

        trev = [threading.Thread(target=fire_review) for _ in range(4)]
        tpay = threading.Thread(target=fire_late_pay)
        [t.start() for t in trev]; tpay.start()
        [t.join() for t in trev]; tpay.join()
        statuses = sorted(r.status_code for r in review_results)
        check("concurrent reviews serialized",
              all(s in (200, 201) for s in statuses), str(statuses))

        # 并发复核合法时序有两种（付款在首个复核快照前/后提交）：
        # 收敛一次：再触发复核，直到 RC11 一定按已提交付款结论；
        # 此后再复核必须结论不变、不新增版本。
        r = c.post("/bank-receipts/B3/review")
        check("settle review ok", r.status_code in (200, 201), r.text)
        final = c.get("/bank-receipts/B3").json()
        cnt = final["latest_version"]["counts"]
        check("concurrent review final consistent",
              cnt["matched"] + cnt["payment_unregistered"] + cnt["holder_mismatch"]
              + cnt["amount_mismatch"] + cnt["both_mismatch"]
              + cnt["duplicate_receipt"] == cnt["total"] == 3
              and cnt["matched"] == 1 and cnt["duplicate_receipt"] == 2
              and cnt["differences"] == 0, str(cnt))
        # 任何一版的计数都必须自洽（结论来自同一已提交状态，不允许半新半旧）
        for vn in final["batch"]["version_numbers"]:
            vc = c.get(f"/bank-receipts/B3/versions/{vn}").json()["counts"]
            assert (vc["matched"] + vc["payment_unregistered"] + vc["holder_mismatch"]
                    + vc["amount_mismatch"] + vc["both_mismatch"]
                    + vc["duplicate_receipt"]) == vc["total"] == 3, (vn, vc)
        check("every version internally consistent", True)
        # 付款迟到带来的结论变化只生成一次新版本；变化行只能是 RC11
        check("at most one change version",
              final["batch"]["version_count"] in (1, 2), str(final["batch"]))
        if final["batch"]["version_count"] == 2:
            v2json = c.get("/bank-receipts/B3/versions/2").json()
            changed = v2json["changed_lines"]
            check("concurrent change is RC11 only",
                  [(x["line_no"], x["from_status"], x["status"]) for x in changed]
                  == [(2, "payment_unregistered", "matched")]
                  and v2json["created_by"] == "review", str(changed))

        # ---- 再复核（付款早已提交）结论不变，不新增版本 ----
        r = c.post("/bank-receipts/B3/review")
        check("B3 settle unchanged",
              r.status_code == 200 and r.json()["status"] == "unchanged"
              and c.get("/bank-receipts/B3").json()["batch"]["version_count"]
              == final["batch"]["version_count"], r.text)

        # ---- 批次列表分页（在全部批次导入后按总数核对；被拒批次不在内）----
        all_batches = c.get("/bank-receipts", params={"page_size": 500}).json()
        stored = {b["batch_no"] for b in all_batches["batches"]}
        expected = {"B1", "B3", "B4", "RACE1"} | {f"RACER-{i}" for i in range(6)}
        check("stored batches exact",
              stored == expected and all_batches["total"] == len(expected), str(stored))
        check("rejected batches absent",
              not ({"B2", "BAD", "DUP-REF", "DUP-NO"} & stored))
        check("list page1 size 3",
              len(c.get("/bank-receipts", params={"page": 1, "page_size": 3})
                  .json()["batches"]) == 3)
        check("page < 1 422",
              c.get("/bank-receipts", params={"page": 0}).status_code == 422)
        check("page_size out of range 422",
              c.get("/bank-receipts", params={"page_size": 501}).status_code == 422)

        # ---- 核验绝不改写付款/余额/单据：终态再核 ----
        led_a = c.get("/rightsholders/A/ledger").json()
        payments_refs = {p["external_ref"] for p in led_a["payments"]}
        check("payments ledger untouched by verification",
              {"E-A1", "E-MISSING", "E-NEW2", "E-NEW1"} <= payments_refs
              and led_a["balance"] == led_a["billed_total"] - led_a["paid_total"],
              str(led_a))

        # ---- 同内容重试在多版本之后仍返回原批次内容（幂等键只与输入相关）----
        r = c.post("/bank-receipts/import",
                   json={"batch_no": "B1", "lines": b1_lines})
        check("retry after versions still duplicate",
              r.status_code == 200 and r.json()["status"] == "duplicate_batch"
              and r.json()["batch"]["version_count"] == 2, r.text)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL BANK RECEIPT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
