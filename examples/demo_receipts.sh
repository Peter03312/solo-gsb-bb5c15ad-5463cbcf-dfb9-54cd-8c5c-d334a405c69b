#!/usr/bin/env bash
# 银行回单批量导入与核验：端到端调用示例。
# 前置：docker compose up -d 已启动（或 BASE 指向运行中的服务）。
# 用法: BASE=http://localhost:8000 bash examples/demo_receipts.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

# ---- 0. 前置数据：作品/权利人/协议/播放/出单/付款 ----
say "0) 准备：作品 + 权利人 A/B + 100 分播放（A 60% / B 40%）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-RCP","title":"回单核验演示作品"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' \
  -d '{"code":"RS-A","name":"甲"}'; echo
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' \
  -d '{"code":"RS-B","name":"乙"}'; echo
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-RCP",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"60"},
            {"holder_code":"RS-B","percent":"40"}]}'; echo
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"RCP-PLAY-1","work_code":"W-RCP",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'; echo
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' >/dev/null; echo

say "0a) 已登记付款（导入前就存在 => 期望 matched）：TX-A1 / RS-A / 60 分"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"TX-A1","amount":60}'; echo
say "0b) 付款挂在 RS-B 名下 10 分（回单写 RS-A => 权利人不符）：TX-HLD"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"TX-HLD","amount":10}'; echo
say "0c) 付款 30 分（回单写 31 => 金额不符）：TX-AMT"
# 给 B 造正余额后再登记
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"RCP-PLAY-2","work_code":"W-RCP",
       "occurred_at":"2026-03-10T20:00:00+08:00","amount":200}' >/dev/null
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-03-01T00:00:00+08:00","end":"2026-04-01T00:00:00+08:00"}' >/dev/null
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"TX-AMT","amount":30}'; echo
say "0d) 付款挂 RS-B/7（回单写 RS-A/8 => 两项均不符）：TX-BOTH"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"TX-BOTH","amount":7}'; echo

# ---- 1. 按批次导入回单 ----
say "1) 导入批次 BR-20260301-01：匹配/未登记/权利人不符/金额不符/两项均不符"
$C -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-20260301-01",
  "lines":[
    {"receipt_no":"R-001","external_ref":"TX-A1",      "holder_code":"RS-A","amount":60},
    {"receipt_no":"R-002","external_ref":"TX-LATE",    "holder_code":"RS-A","amount":5},
    {"receipt_no":"R-003","external_ref":"TX-HLD",     "holder_code":"RS-A","amount":10},
    {"receipt_no":"R-004","external_ref":"TX-AMT",     "holder_code":"RS-B","amount":31},
    {"receipt_no":"R-005","external_ref":"TX-BOTH",    "holder_code":"RS-A","amount":8}
  ]}'; echo
# 逐行结论（导入时冻结为第 1 版）：
# R-001 matched；R-002 payment_unregistered（付款迟到）；
# R-003 holder_mismatch；R-004 amount_mismatch；R-005 both_mismatch（mismatches=[holder,amount]）

say "2) 同批次同内容重试 => 200 status=duplicate_batch，返回原批次（不新增版本/回单）"
$C -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-20260301-01",
  "lines":[
    {"receipt_no":"R-001","external_ref":"TX-A1",      "holder_code":"RS-A","amount":60},
    {"receipt_no":"R-002","external_ref":"TX-LATE",    "holder_code":"RS-A","amount":5},
    {"receipt_no":"R-003","external_ref":"TX-HLD",     "holder_code":"RS-A","amount":10},
    {"receipt_no":"R-004","external_ref":"TX-AMT",     "holder_code":"RS-B","amount":31},
    {"receipt_no":"R-005","external_ref":"TX-BOTH",    "holder_code":"RS-A","amount":8}
  ]}' | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["batch"]["version_count"])'

say "3) 批次号异内容复用 => 409 receipt_batch_conflict（整批拒绝）"
$C -i -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-20260301-01",
  "lines":[{"receipt_no":"R-001","external_ref":"TX-A1","holder_code":"RS-A","amount":60}]}' \
  | head -1

say "4) 批内重复流水号 / 无效金额 => 422，整批拒绝（不落批次）"
$C -i -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-BAD",
  "lines":[
    {"receipt_no":"R-B1","external_ref":"TX-SAME","holder_code":"RS-A","amount":1},
    {"receipt_no":"R-B2","external_ref":"TX-SAME","holder_code":"RS-A","amount":2}
  ]}' | head -1
$C -i -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-BAD2",
  "lines":[{"receipt_no":"R-B3","external_ref":"TX-Z","holder_code":"RS-A","amount":0}]}' \
  | head -1

say "5) 跨批次复用回单编号 R-001 => 409 receipt_no_conflict（整批拒绝）"
$C -i -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-20260302-01",
  "lines":[{"receipt_no":"R-001","external_ref":"TX-OTHER","holder_code":"RS-A","amount":9}]}' \
  | head -1

# ---- 6. 跨批次同一流水号：首次有效、其余标重复 ----
say "6) 第二个批次复用 TX-A1/TX-BOTH => 重复回单；TX-NEW => 付款未登记"
$C -X POST "$BASE/bank-receipts/import" -H 'Content-Type: application/json' -d '{
  "batch_no":"BR-20260302-02",
  "lines":[
    {"receipt_no":"R-101","external_ref":"TX-A1",  "holder_code":"RS-A","amount":60},
    {"receipt_no":"R-102","external_ref":"TX-BOTH","holder_code":"RS-A","amount":8},
    {"receipt_no":"R-103","external_ref":"TX-NEW", "holder_code":"RS-A","amount":20}
  ]}' | python3 -m json.tool

# ---- 7. 付款迟到后复核：结论变化才出新版 ----
say "7) 迟到付款 TX-LATE / RS-A / 5 补登记"
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"TX-LATE","amount":5}'; echo

say "7a) 复核 BR-20260301-01 => 201 new_version，仅 R-002 由 payment_unregistered -> matched"
$C -X POST "$BASE/bank-receipts/BR-20260301-01/review" | python3 -m json.tool

say "7b) 再次复核（结论不变）=> 200 unchanged，不新增版本"
$C -X POST "$BASE/bank-receipts/BR-20260301-01/review" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["new_version"])'

# ---- 8. 旧版与原始回单始终可查 ----
say "8a) 查询第 1 版（旧结论仍在：R-002 当时为 payment_unregistered）"
$C "$BASE/bank-receipts/BR-20260301-01/versions/1" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["version_no"],d["created_by"],
              {l["receipt_no"]:l["status"] for l in d["lines"]})'
say "8b) 查询批次详情（原始回单 + 最新版 + 版本列表）"
$C "$BASE/bank-receipts/BR-20260301-01" | python3 -m json.tool
say "8c) 批次列表"
$C "$BASE/bank-receipts?page=1&page_size=20" | python3 -m json.tool

say "完成。核验全程只读付款，可用以下命令确认付款/余额未被改写："
echo "  curl -sS $BASE/rightsholders/RS-A/ledger | python3 -m json.tool"
