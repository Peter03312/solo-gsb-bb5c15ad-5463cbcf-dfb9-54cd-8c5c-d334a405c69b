"""银行回单批量导入、核验、复核与查询的 HTTP 路由。"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from .. import receipts_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    ReceiptBatchIn,
    ReceiptBatchList,
    ReceiptBatchOut,
    ReceiptReviewOut,
    ReceiptVersionOut,
)

router = APIRouter(tags=["bank-receipts"])


@router.post("/bank-receipts/import")
def import_receipts(body: ReceiptBatchIn, conn=Depends(get_conn)):
    """按批次导入银行回单并冻结第 1 版逐行核验结论。

    - 同批次编号同内容重试：HTTP 200 status=duplicate_batch，返回原批次（原版本不变）；
    - 批次编号异内容复用：409 receipt_batch_conflict，整批拒绝；
    - 回单编号跨批次复用（全局唯一冲突）：409 receipt_no_conflict，整批拒绝；
    - 批内回单编号/外部流水号重复：422，整批拒绝；
    - 字段缺失、金额非正整数：422，整批拒绝；整批在单事务内，拒绝不留痕；
    - 跨批次同一流水号仅首次成功导入的回单参与核验，其余标为重复回单
      （status=duplicate_receipt）；
    - 导入时按当时已提交付款逐行对照并冻结（matched / payment_unregistered /
      holder_mismatch / amount_mismatch / both_mismatch，两项均不符同时列出）。
    """
    lines = [l.model_dump() for l in body.lines]
    http_status, payload = svc.import_receipt_batch(conn, body.batch_no, lines)
    return JSONResponse(
        status_code=http_status,
        content=ReceiptBatchOut(**payload).model_dump(mode="json"),
    )


@router.post("/bank-receipts/{batch_no}/review", response_model=ReceiptReviewOut)
def review_receipts(batch_no: str, conn=Depends(get_conn)):
    """付款迟到后按当前已提交付款复核整批。

    - 结论全部不变：HTTP 200 status=unchanged，new_version=false，不新增版本；
    - 有行结论变化：HTTP 201 status=new_version，生成新版本并列出 changed_lines；
    - 批次不存在：404 receipt_batch_not_found；
    - 并发复核由批次咨询锁串行化，REPEATABLE READ 快照保证同一版本结论来自同一
      已提交付款状态；核验只读付款，不改写付款、余额或单据。
    """
    http_status, payload = svc.review_receipt_batch(conn, batch_no)
    return JSONResponse(
        status_code=http_status,
        content=ReceiptReviewOut(**payload).model_dump(mode="json"),
    )


@router.get("/bank-receipts", response_model=ReceiptBatchList)
def list_receipt_batches(
    conn=Depends(get_conn),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    """列出回单批次（按导入时间排序）。"""
    limit = page_size
    offset = (page - 1) * page_size
    return svc.list_batches(conn, limit, offset)


@router.get("/bank-receipts/{batch_no}", response_model=ReceiptBatchOut)
def get_receipt_batch(batch_no: str, conn=Depends(get_conn)):
    """查询批次：原始回单（含重复标记）与最新冻结版本。不存在 404。"""
    result = svc.get_batch(conn, batch_no)
    if result is None:
        raise APIError(
            404,
            "receipt_batch_not_found",
            f"receipt batch {batch_no} not found",
        )
    return {"status": "imported", **result}


@router.get(
    "/bank-receipts/{batch_no}/versions/{version_no}",
    response_model=ReceiptVersionOut,
)
def get_receipt_version(batch_no: str, version_no: int, conn=Depends(get_conn)):
    """查询指定历史版本（旧版始终可查）；批次 404、版本不存在 404。"""
    result = svc.get_version(conn, batch_no, version_no)
    if result is None:
        # 区分批次不存在与版本不存在
        exists = svc.get_batch(conn, batch_no) is not None
        if not exists:
            raise APIError(
                404,
                "receipt_batch_not_found",
                f"receipt batch {batch_no} not found",
            )
        raise APIError(
            404,
            "receipt_version_not_found",
            f"version {version_no} of receipt batch {batch_no} not found",
        )
    return result
