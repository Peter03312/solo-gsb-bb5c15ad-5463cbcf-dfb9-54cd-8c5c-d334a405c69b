"""银行回单批量导入与核验。

业务规则（本轮新增）：

- 财务按批次导入回单：输入批次编号 + 回单行
  （回单编号 receipt_no、外部付款流水号 external_ref、权利人编号 holder_code、正整数分额 amount）。
- 同批次编号同内容重试：幂等返回原批次（HTTP 200，status=duplicate_batch）；
  批次编号异内容复用、回单编号跨批次复用（回单编号全局唯一）、
  批内重复流水号/回单编号、无效字段：整批拒绝，
  拒绝发生在单事务内，不留下任何回单或批次。
- 跨批次同一外部付款流水号：仅首次成功导入（提交）的回单参与核验（duplicate=false），
  之后导入的回单一律标为“重复回单”（duplicate=true，状态 duplicate_receipt，不参与核验）。
- 有效回单按流水号对照“当时已提交”的已登记付款，逐行结论：
  matched（匹配）、payment_unregistered（付款未登记）、
  holder_mismatch（权利人不符）、amount_mismatch（金额不符）、
  both_mismatch（两项均不符，同时列出两个不符项）。
- 导入时逐行结论冻结为第 1 版；付款迟到后可复核，结论有变化才生成新版本，
  结论不变不新增版本；旧版本与原始回单始终可查。
- 每版输出状态计数、不符差异行及与上一版相比发生变化的行。
- 付款与复核并发：复核事务使用 REPEATABLE READ，单一批次咨询锁串行化同批并发，
  整版结论来自同一已提交付款快照。
- 核验只读 payments，绝不改写付款、余额或单据。
"""
import hashlib
import json

import psycopg

# 结论状态
MATCHED = "matched"
PAYMENT_UNREGISTERED = "payment_unregistered"
HOLDER_MISMATCH = "holder_mismatch"
AMOUNT_MISMATCH = "amount_mismatch"
BOTH_MISMATCH = "both_mismatch"
DUPLICATE_RECEIPT = "duplicate_receipt"

_STATUSES = (
    MATCHED,
    PAYMENT_UNREGISTERED,
    HOLDER_MISMATCH,
    AMOUNT_MISMATCH,
    BOTH_MISMATCH,
    DUPLICATE_RECEIPT,
)
# 差异（异常）状态：计数中的 differences 即这些行
_DIFFERENCE_STATUSES = (
    PAYMENT_UNREGISTERED,
    HOLDER_MISMATCH,
    AMOUNT_MISMATCH,
    BOTH_MISMATCH,
)

# 咨询锁命名空间
_BATCH_NS = 0x52435042  # "RCPB"：回单批次锁
_REF_NS = 0x52435031    # "RCP1"：回单流水号锁


# ---------------------------------------------------------------- 纯逻辑部分


def classify_receipt(holder_code: str, amount: int, payment: dict | None) -> tuple[str, list[str]]:
    """单条有效回单对照已登记付款（payments 快照行或 None）的结论。

    payment 为 None 表示该流水号在快照中没有任何已提交付款。
    权利人、金额两项均不符时状态为 both_mismatch，且 mismatches 同时列出两项。
    """
    if payment is None:
        return PAYMENT_UNREGISTERED, ["payment_unregistered"]
    holder_bad = payment["holder_code"] != holder_code
    amount_bad = int(payment["amount"]) != amount
    if holder_bad and amount_bad:
        return BOTH_MISMATCH, ["holder", "amount"]
    if holder_bad:
        return HOLDER_MISMATCH, ["holder"]
    if amount_bad:
        return AMOUNT_MISMATCH, ["amount"]
    return MATCHED, []


def counts_for_lines(lines: list[dict]) -> dict:
    """按行结论汇总计数：总数、各状态计数、有效回单数、重复回单数、差异行总数。"""
    counts = {s: 0 for s in _STATUSES}
    for line in lines:
        counts[line["status"]] += 1
    return {
        "total": len(lines),
        **counts,
        "effective": sum(counts[s] for s in _STATUSES if s != DUPLICATE_RECEIPT),
        "duplicate_receipts": counts[DUPLICATE_RECEIPT],
        "differences": sum(counts[s] for s in _DIFFERENCE_STATUSES),
    }


def changed_between(previous: list[dict], current: list[dict]) -> list[dict]:
    """与上一版逐行比较：返回结论（状态）发生变化的行，含 from/to。"""
    prev = {l["line_no"]: l for l in previous}
    changes = []
    for line in current:
        old = prev.get(line["line_no"])
        if old is None or old["status"] != line["status"]:
            changes.append(
                {**line, "from_status": old["status"] if old is not None else None}
            )
    return changes


def content_hash_for(lines: list[dict]) -> str:
    """整批内容指纹：仅由各行（编号/流水号/权利人/金额）决定，与顺序敏感。"""
    payload = [
        {
            "receipt_no": l["receipt_no"],
            "external_ref": l["external_ref"],
            "holder_code": l["holder_code"],
            "amount": int(l["amount"]),
        }
        for l in lines
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _lock_key(text: str) -> int:
    digest = hashlib.blake2b(text.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


# ---------------------------------------------------------------- 序列化


def _line_output(row: dict) -> dict:
    """DB 行 -> 对外逐行结论（冻结快照）。"""
    return {
        "line_no": row["line_no"],
        "receipt_no": row["receipt_no"],
        "external_ref": row["external_ref"],
        "holder_code": row["holder_code"],
        "amount": int(row["receipt_amount"]),
        "status": row["status"],
        "mismatches": list(row.get("mismatches") or []),
        "duplicate": bool(row["duplicate"]),
        "duplicate_of_batch_no": row.get("duplicate_of_batch_no"),
        "payment": None
        if row.get("payment_id") is None
        else {
            "payment_id": int(row["payment_id"]),
            "holder_code": row["payment_holder_code"],
            "amount": int(row["payment_amount"]),
        },
    }


def _internal_line_output(l: dict) -> dict:
    """内部构造的版本行（receipt_amount 键）-> 对外逐行结论形状。"""
    out = _line_output(l)
    out["from_status"] = l.get("from_status")
    return out


def _load_version_lines(conn, version_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT line_no, receipt_no, external_ref, holder_code, receipt_amount,
               status, mismatches, duplicate, duplicate_of_batch_no,
               payment_id, payment_holder_code, payment_amount
        FROM receipt_verification_lines
        WHERE version_id = %s
        ORDER BY line_no
        """,
        (version_id,),
    ).fetchall()
    return [_line_output(r) for r in rows]


def _raw_receipt_output(r: dict) -> dict:
    return {
        "line_no": r["line_no"],
        "receipt_no": r["receipt_no"],
        "external_ref": r["external_ref"],
        "holder_code": r["holder_code"],
        "amount": int(r["amount"]),
        "duplicate": bool(r["duplicate"]),
        "duplicate_of_batch_no": r["duplicate_of_batch_no"],
        "imported_at": r["created_at"],
    }


def _version_output(conn, vrow: dict, lines: list[dict] | None = None) -> dict:
    if lines is None:
        lines = _load_version_lines(conn, vrow["id"])
    return {
        "version_no": vrow["version_no"],
        "created_by": vrow["created_by"],
        "created_at": vrow["created_at"],
        "changed_lines_count": int(vrow["changed_lines_count"]),
        "counts": counts_for_lines(lines),
        "duplicate_lines": [l for l in lines if l["status"] == DUPLICATE_RECEIPT],
        "difference_lines": [l for l in lines if l["status"] in _DIFFERENCE_STATUSES],
        "lines": lines,
    }


# ---------------------------------------------------------------- 错误


def _error(status_code: int, code: str, message: str, detail=None):
    from .errors import APIError

    return APIError(status_code, code, message, detail=detail)


# ---------------------------------------------------------------- 导入


def import_receipt_batch(conn, batch_no: str, lines: list[dict]) -> dict:
    """整批导入回单并冻结第 1 版核验结论。

    调用方（Pydantic）已保证字段类型/正整数等有效；此处再检查批内重复，
    随后在单事务内处理幂等/冲突、跨批次重复标记、对照付款、冻结版本。
    返回 (http_status, payload)。
    """
    # 批内自洽性：回单编号、外部流水号均不得在本批重复
    seen_receipt, seen_ref = set(), set()
    for l in lines:
        if l["receipt_no"] in seen_receipt:
            raise _error(
                422, "duplicate_receipt_no_in_batch",
                f"duplicate receipt_no {l['receipt_no']} within batch {batch_no}",
                detail={"batch_no": batch_no, "receipt_no": l["receipt_no"]},
            )
        if l["external_ref"] in seen_ref:
            raise _error(
                422, "duplicate_external_ref_in_batch",
                f"duplicate external_ref {l['external_ref']} within batch {batch_no}",
                detail={"batch_no": batch_no, "external_ref": l["external_ref"]},
            )
        seen_receipt.add(l["receipt_no"])
        seen_ref.add(l["external_ref"])

    digest = content_hash_for(lines)
    ref_locks = sorted({_lock_key(l["external_ref"]) for l in lines})

    with conn.transaction():
        # 加锁顺序全局一致：先批次锁（升序）、再全部流水号锁（哈希升序）。
        # 这样不同批次并发导入相同流水号时双方按同一顺序取锁，不会死锁。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_BATCH_NS, _lock_key(batch_no)),),
        )
        for key in ref_locks:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (_combined_key(_REF_NS, key),),
            )

        # 锁取齐后再查批次：并发的同批次请求此时已串行，第二个必能看到首个已提交/本事务的行。
        existing_batch = conn.execute(
            "SELECT batch_no, content_hash, created_at FROM receipt_batches WHERE batch_no = %s",
            (batch_no,),
        ).fetchone()

        if existing_batch is not None:
            if existing_batch["content_hash"] != digest:
                raise _error(
                    409, "receipt_batch_conflict",
                    f"batch_no {batch_no} already imported with different content; "
                    "reusing a batch_no with different content is rejected",
                    detail={"batch_no": batch_no},
                )
            # 同内容重试：原批次原封不动返回（不新增版本、不新增回单）
            return 200, {"status": "duplicate_batch", **_get_batch_payload(conn, batch_no)}

        # 回单编号全局唯一：跨批次复用一律拒绝（同内容重试请使用原批次编号，
        # 那会在上面按批次编号短路为幂等返回）。
        conflict_no = conn.execute(
            "SELECT receipt_no, batch_no FROM bank_receipts WHERE receipt_no = ANY(%s) "
            "ORDER BY receipt_no LIMIT 1",
            ([l["receipt_no"] for l in lines],),
        ).fetchone()
        if conflict_no is not None:
            raise _error(
                409, "receipt_no_conflict",
                f"receipt_no {conflict_no['receipt_no']} is globally unique and already "
                f"imported in batch {conflict_no['batch_no']}; importing it in another "
                "batch is rejected (retry the original batch_no for idempotency)",
                detail={
                    "receipt_no": conflict_no["receipt_no"],
                    "existing_batch_no": conflict_no["batch_no"],
                },
            )

        # 跨批次重复判定：同一流水号在“有效（参与核验）”回单中已有先提交者即重复。
        # 批内重复已被前置校验，故同一批次内不会出现自我重复。
        first_refs = {
            r["external_ref"]
            for r in conn.execute(
                "SELECT DISTINCT external_ref FROM bank_receipts WHERE duplicate = false"
            ).fetchall()
        }

        # 本次付款对照使用同一已提交快照（READ COMMITTED 下单条语句即一致快照）。
        payments_by_ref = {
            r["external_ref"]: r
            for r in conn.execute(
                """
                SELECT id, holder_code, amount, external_ref
                FROM payments
                WHERE external_ref = ANY(%s)
                """,
                (sorted({l["external_ref"] for l in lines}),),
            ).fetchall()
        }

        # 先落批次头（bank_receipts 有指向它的外键）。
        # 同批次已由批次咨询锁串行，此处不会再撞号；唯一约束为最终防线。
        conn.execute(
            "INSERT INTO receipt_batches(batch_no, line_count, content_hash) "
            "VALUES (%s, %s, %s)",
            (batch_no, len(lines), digest),
        )

        # 同批次内首个出现的流水号行可参与核验（批内同号已拒绝，这里实际每行都唯一）；
        # 与先前批次共享流水号的行标为重复回单。
        inserted = []
        effective_refs_in_batch = set()
        for idx, l in enumerate(lines, start=1):
            is_duplicate = l["external_ref"] in first_refs or (
                l["external_ref"] in effective_refs_in_batch
            )
            dup_of = None
            if is_duplicate:
                owner = conn.execute(
                    "SELECT batch_no FROM bank_receipts "
                    "WHERE external_ref = %s AND duplicate = false "
                    "ORDER BY created_at, id LIMIT 1",
                    (l["external_ref"],),
                ).fetchone()
                dup_of = owner["batch_no"] if owner else None
            else:
                effective_refs_in_batch.add(l["external_ref"])

            # 保存点：防御并发提交的回单编号撞号（预检与插入之间的竞态），
            # 映射为 receipt_no_conflict，整批回滚，不留任何回单。
            try:
                with conn.transaction():
                    conn.execute(
                        """
                        INSERT INTO bank_receipts(
                            batch_no, line_no, receipt_no, external_ref, holder_code,
                            amount, duplicate, duplicate_of_batch_no)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (batch_no, idx, l["receipt_no"], l["external_ref"], l["holder_code"],
                         l["amount"], is_duplicate, dup_of),
                    )
            except psycopg.errors.UniqueViolation:
                owner = conn.execute(
                    "SELECT batch_no FROM bank_receipts WHERE receipt_no = %s",
                    (l["receipt_no"],),
                ).fetchone()
                raise _error(
                    409, "receipt_no_conflict",
                    f"receipt_no {l['receipt_no']} already imported in another batch "
                    f"{owner['batch_no'] if owner else '?'}",
                    detail={
                        "receipt_no": l["receipt_no"],
                        "existing_batch_no": owner["batch_no"] if owner else None,
                    },
                )
            inserted.append((idx, l, is_duplicate, dup_of))

        version_lines = []
        for idx, l, is_duplicate, dup_of in inserted:
            if is_duplicate:
                status, mismatches, payment = DUPLICATE_RECEIPT, [], None
            else:
                p = payments_by_ref.get(l["external_ref"])
                status, mismatches = classify_receipt(l["holder_code"], l["amount"], p)
                payment = p
            version_lines.append({
                "line_no": idx,
                "receipt_no": l["receipt_no"],
                "external_ref": l["external_ref"],
                "holder_code": l["holder_code"],
                "receipt_amount": l["amount"],
                "status": status,
                "mismatches": mismatches,
                "duplicate": is_duplicate,
                "duplicate_of_batch_no": dup_of,
                "payment_id": None if payment is None else payment["id"],
                "payment_holder_code": None if payment is None else payment["holder_code"],
                "payment_amount": None if payment is None else int(payment["amount"]),
            })

        version_id = _insert_version(conn, batch_no, 1, "import", [], version_lines)
        vrow = conn.execute(
            "SELECT id, version_no, created_by, changed_lines_count, created_at "
            "FROM receipt_verification_versions WHERE id = %s",
            (version_id,),
        ).fetchone()
        out_lines = _load_version_lines(conn, version_id)
        payload = _get_batch_payload(conn, batch_no, latest_version=_version_output(conn, vrow, out_lines))
        return 201, {"status": "imported", **payload}


# ---------------------------------------------------------------- 复核


def review_receipt_batch(conn, batch_no: str) -> tuple[int, dict]:
    """对已导入批次按当前已提交付款重新核验。

    - 结论与最新版相比无变化：不新增版本，HTTP 200，new_version=false，返回最新版；
    - 有变化：新增版本（created_by=review），HTTP 201，new_version=true，
      并列出与上一版相比的变化行；
    - 复核事务 REPEATABLE READ：同一版本内所有行的付款对照来自同一已提交快照；
      批次咨询锁串行化同批并发复核，结论不变不重复生成版本。
    """
    # 在 autocommit 连接上为本次事务块指定 REPEATABLE READ：
    # conn.isolation_level 会在事务块 BEGIN 时带上 ISOLATION LEVEL，
    # 使事务内所有付款/回单读取共享同一已提交快照。
    prev_level = conn.isolation_level
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        with conn.transaction():
            # 批次咨询锁串行化同批并发复核，结论不变不重复生成版本
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (_combined_key(_BATCH_NS, _lock_key(batch_no)),),
            )
            batch = conn.execute(
                "SELECT batch_no FROM receipt_batches WHERE batch_no = %s",
                (batch_no,),
            ).fetchone()
            if batch is None:
                raise _error(
                    404, "receipt_batch_not_found",
                    f"receipt batch {batch_no} not found",
                )

            latest = conn.execute(
                """
                SELECT id, version_no, created_by, changed_lines_count, created_at
                FROM receipt_verification_versions
                WHERE batch_no = %s
                ORDER BY version_no DESC
                LIMIT 1
                """,
                (batch_no,),
            ).fetchone()
            old_lines = _load_version_lines(conn, latest["id"])

            # 一次性取齐本批次有效回单流水号的全部已登记付款：
            # REPEATABLE READ 下整个事务共享同一快照，
            # 付款与复核并发时同一版本结论来自同一已提交状态。
            receipts = conn.execute(
                """
                SELECT line_no, receipt_no, external_ref, holder_code, amount,
                       duplicate, duplicate_of_batch_no
                FROM bank_receipts
                WHERE batch_no = %s
                ORDER BY line_no
                """,
                (batch_no,),
            ).fetchall()
            refs = sorted({r["external_ref"] for r in receipts if not r["duplicate"]})
            payments_by_ref = {
                r["external_ref"]: r
                for r in conn.execute(
                    "SELECT id, holder_code, amount, external_ref "
                    "FROM payments WHERE external_ref = ANY(%s)",
                    (refs,),
                ).fetchall()
            }

            new_lines = []
            for r in receipts:
                if r["duplicate"]:
                    status, mismatches, payment = DUPLICATE_RECEIPT, [], None
                else:
                    payment = payments_by_ref.get(r["external_ref"])
                    status, mismatches = classify_receipt(
                        r["holder_code"], int(r["amount"]), payment
                    )
                new_lines.append({
                    "line_no": r["line_no"],
                    "receipt_no": r["receipt_no"],
                    "external_ref": r["external_ref"],
                    "holder_code": r["holder_code"],
                    "receipt_amount": int(r["amount"]),
                    "status": status,
                    "mismatches": mismatches,
                    "duplicate": bool(r["duplicate"]),
                    "duplicate_of_batch_no": r["duplicate_of_batch_no"],
                    "payment_id": None if payment is None else payment["id"],
                    "payment_holder_code": None if payment is None else payment["holder_code"],
                    "payment_amount": None if payment is None else int(payment["amount"]),
                })

            # 内部行用于写库；输出行（amount 键）用于差异比较与响应
            new_out_lines = [_internal_line_output(l) for l in new_lines]

            changes = changed_between(old_lines, new_out_lines)
            if not changes:
                # 结论不变：不新增版本，旧版保持为最新且始终可查
                payload = _get_batch_payload(
                    conn, batch_no,
                    latest_version=_version_output(conn, latest, old_lines),
                )
                return 200, {
                    "status": "unchanged",
                    "new_version": False,
                    "changed_lines": [],
                    **payload,
                }

            new_version_no = int(latest["version_no"]) + 1
            version_id = _insert_version(
                conn, batch_no, new_version_no, "review", changes, new_lines
            )
            vrow = conn.execute(
                "SELECT id, version_no, created_by, changed_lines_count, created_at "
                "FROM receipt_verification_versions WHERE id = %s",
                (version_id,),
            ).fetchone()
            # 以写库后回读的冻结行构造版本（保证与持久化内容一致）
            out_lines = _load_version_lines(conn, version_id)
            version = _version_output(conn, vrow, out_lines)
            version["changed_lines"] = changes
            payload = _get_batch_payload(conn, batch_no, latest_version=version)
            return 201, {
                "status": "new_version",
                "new_version": True,
                "changed_lines": changes,
                **payload,
            }
    finally:
        # 连接归还连接池前恢复默认隔离级别（autocommit 下下一个请求干净开始）
        conn.isolation_level = prev_level


# ---------------------------------------------------------------- 写入辅助


def _insert_version(conn, batch_no, version_no, created_by, changes, lines) -> int:
    row = conn.execute(
        """
        INSERT INTO receipt_verification_versions(
            batch_no, version_no, created_by, changed_lines_count)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (batch_no, version_no, created_by, len(changes)),
    ).fetchone()
    version_id = row["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO receipt_verification_lines(
                version_id, line_no, receipt_no, external_ref, holder_code,
                receipt_amount, status, mismatches, duplicate,
                duplicate_of_batch_no, payment_id, payment_holder_code, payment_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    version_id,
                    l["line_no"],
                    l["receipt_no"],
                    l["external_ref"],
                    l["holder_code"],
                    l["receipt_amount"],
                    l["status"],
                    l["mismatches"],
                    l["duplicate"],
                    l["duplicate_of_batch_no"],
                    l["payment_id"],
                    l["payment_holder_code"],
                    l["payment_amount"],
                )
                for l in lines
            ],
        )
    return version_id


# ---------------------------------------------------------------- 查询


def _batch_summary(conn, batch_no: str, b_row=None) -> dict:
    if b_row is None:
        b_row = conn.execute(
            "SELECT batch_no, line_count, content_hash, created_at "
            "FROM receipt_batches WHERE batch_no = %s",
            (batch_no,),
        ).fetchone()
    versions = conn.execute(
        "SELECT version_no, created_at FROM receipt_verification_versions "
        "WHERE batch_no = %s ORDER BY version_no",
        (batch_no,),
    ).fetchall()
    return {
        "batch_no": b_row["batch_no"],
        "line_count": int(b_row["line_count"]),
        "imported_at": b_row["created_at"],
        "version_count": len(versions),
        "version_numbers": [v["version_no"] for v in versions],
        "latest_version_no": versions[-1]["version_no"] if versions else None,
    }


def _get_batch_payload(conn, batch_no: str, latest_version: dict | None = None) -> dict:
    summary = _batch_summary(conn, batch_no)
    raw_rows = conn.execute(
        """
        SELECT line_no, receipt_no, external_ref, holder_code, amount,
               duplicate, duplicate_of_batch_no, created_at
        FROM bank_receipts WHERE batch_no = %s ORDER BY line_no
        """,
        (batch_no,),
    ).fetchall()
    if latest_version is None:
        lv = conn.execute(
            """
            SELECT id, version_no, created_by, changed_lines_count, created_at
            FROM receipt_verification_versions
            WHERE batch_no = %s ORDER BY version_no DESC LIMIT 1
            """,
            (batch_no,),
        ).fetchone()
        latest_version = _version_output(conn, lv)
    return {
        "batch": summary,
        "raw_receipts": [_raw_receipt_output(r) for r in raw_rows],
        "latest_version": latest_version,
    }


def get_batch(conn, batch_no: str) -> dict | None:
    row = conn.execute(
        "SELECT batch_no, line_count, content_hash, created_at "
        "FROM receipt_batches WHERE batch_no = %s",
        (batch_no,),
    ).fetchone()
    if row is None:
        return None
    return _get_batch_payload(conn, batch_no)


def get_version(conn, batch_no: str, version_no: int) -> dict | None:
    if conn.execute(
        "SELECT 1 FROM receipt_batches WHERE batch_no = %s", (batch_no,)
    ).fetchone() is None:
        return None
    vrow = conn.execute(
        """
        SELECT id, version_no, created_by, changed_lines_count, created_at
        FROM receipt_verification_versions
        WHERE batch_no = %s AND version_no = %s
        """,
        (batch_no, version_no),
    ).fetchone()
    if vrow is None:
        return None
    version = _version_output(conn, vrow)
    # 复核产生的版本附带与上一版相比的变化行（导入第 1 版为空）
    if vrow["created_by"] == "review":
        prev = conn.execute(
            """
            SELECT id FROM receipt_verification_versions
            WHERE batch_no = %s AND version_no < %s
            ORDER BY version_no DESC LIMIT 1
            """,
            (batch_no, version_no),
        ).fetchone()
        if prev is not None:
            prev_lines = _load_version_lines(conn, prev["id"])
            version["changed_lines"] = changed_between(prev_lines, version["lines"])
        else:
            version["changed_lines"] = []
    else:
        version["changed_lines"] = []
    return version


def list_batches(conn, limit: int, offset: int) -> dict:
    total = int(conn.execute("SELECT count(*) AS n FROM receipt_batches").fetchone()["n"])
    rows = conn.execute(
        """
        SELECT b.batch_no, b.line_count, b.created_at,
               count(v.id) AS version_count,
               COALESCE(array_agg(v.version_no ORDER BY v.version_no), '{}') AS version_numbers,
               max(v.version_no) AS latest_version_no
        FROM receipt_batches b
        LEFT JOIN receipt_verification_versions v ON v.batch_no = b.batch_no
        GROUP BY b.batch_no, b.line_count, b.created_at
        ORDER BY b.created_at, b.batch_no
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "batches": [
            {
                "batch_no": r["batch_no"],
                "line_count": int(r["line_count"]),
                "imported_at": r["created_at"],
                "version_count": int(r["version_count"]),
                "version_numbers": [int(x) for x in (r["version_numbers"] or [])],
                "latest_version_no": int(r["latest_version_no"]),
            }
            for r in rows
        ],
    }
