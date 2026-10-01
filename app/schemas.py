from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import settings


def _tz_aware(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{field} must include a timezone, e.g. 2026-01-15T20:00:00+08:00")
    return dt


class WorkIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)


class WorkOut(BaseModel):
    code: str
    title: str
    created: bool


class HolderIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=500)


class HolderOut(BaseModel):
    code: str
    name: str | None
    created: bool


class ShareIn(BaseModel):
    # 用字符串接收，避免浮点误差，如 "62.5"；数字也兼容
    holder_code: str = Field(min_length=1, max_length=200)
    percent: Decimal = Field(gt=0, le=100)

    @field_validator("percent")
    @classmethod
    def scale_limit(cls, v: Decimal) -> Decimal:
        exp = v.as_tuple().exponent
        digits = -exp if isinstance(exp, int) and exp < 0 else 0
        if digits > settings.percent_scale:
            raise ValueError(
                f"percent supports at most {settings.percent_scale} decimal places"
            )
        return v


class AgreementIn(BaseModel):
    work_code: str = Field(min_length=1)
    valid_from: datetime
    valid_to: datetime | None = None
    note: str | None = None
    shares: list[ShareIn] = Field(min_length=1)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def tz(cls, v: datetime | None, info):
        if v is not None:
            _tz_aware(v, info.field_name)
        return v

    @model_validator(mode="after")
    def validate_model(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        holder_codes = [s.holder_code for s in self.shares]
        if len(set(holder_codes)) != len(holder_codes):
            raise ValueError("duplicate holder_code in shares")
        total = sum((s.percent for s in self.shares), Decimal(0))
        if total != Decimal(100):
            raise ValueError(f"shares percent must total exactly 100, got {total}")
        return self


class AgreementShareOut(BaseModel):
    holder_code: str
    percent: str


class AgreementOut(BaseModel):
    id: int
    work_code: str
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    shares: list[AgreementShareOut]


class PlayIn(BaseModel):
    import_id: str = Field(min_length=1, max_length=200)
    work_code: str = Field(min_length=1)
    occurred_at: datetime
    # 非负整数，最小货币单位
    amount: int = Field(ge=0)
    # 更正时引用原记录编号，本记录必须使用新编号
    corrects_id: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v

    @field_validator("occurred_at")
    @classmethod
    def tz(cls, v: datetime):
        return _tz_aware(v, "occurred_at")

    @model_validator(mode="after")
    def validate_model(self):
        if self.corrects_id is not None and self.corrects_id == self.import_id:
            raise ValueError("corrects_id must differ from import_id")
        return self


class PlayBulkIn(BaseModel):
    plays: list[PlayIn] = Field(min_length=1, max_length=1000)


class PlayResult(BaseModel):
    import_id: str
    status: str  # created | duplicate
    is_correction: bool
    corrects_id: str | None
    chain_tail: bool


class PlayChainItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    corrects_id: str | None
    chain_tail: bool


class PlayChainOut(BaseModel):
    import_id: str
    chain: list[PlayChainItem]


class PeriodParams(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def tz(cls, v: datetime, info):
        return _tz_aware(v, info.field_name)

    @model_validator(mode="after")
    def validate_model(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        return self


class PendingItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    reason: str


class StatementLineOut(BaseModel):
    holder_code: str
    amount: int
    delta: int | None = None


class StatementOut(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    period_start: datetime
    period_end: datetime
    issued_at: datetime
    note: str | None = None
    # 是否具备逐笔归因证据；升级前的存量单据为 false（不可追溯）
    traceable: bool = True
    payable: list[StatementLineOut]
    pending: list[PendingItem]


class StatementSummary(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    created_at: datetime
    traceable: bool = True
    lines: list[StatementLineOut]
    pending: list[PendingItem]


# ---- 逐笔归因证据 ----

class AttributionRecord(BaseModel):
    """单条逐笔归因：某权利人在某更正链（按原始编号归集）上的分摊。"""
    holder_code: str
    change_type: str  # added | removed | legacy
    # 更正链原始编号；legacy（旧账差额）行为 None
    root_import_id: str | None = None
    # 出具时冻结的链尾与核算上下文；legacy 行为 None
    tail_import_id: str | None = None
    work_code: str | None = None
    occurred_at: datetime | None = None
    agreement_id: int | None = None
    tail_amount: int | None = None
    share_percent: str | None = None
    # 该笔分摊对本单据的贡献：added 非负、removed 为负（撤销）、legacy 为旧账差额
    amount: int


class AttributionHolderTotal(BaseModel):
    holder_code: str
    # 该权利人在本单据全部归因记录的金额合计（含撤销负额、旧账差额）
    attributed_total: int
    # 该权利人在原单据明细行上的金额（初次单为应付，追补单为差额）
    statement_line_amount: int
    # 两者是否一致；不可追溯的存量单据恒为 false（无逐笔证据可核对）
    reconciles: bool


class AttributionPage(BaseModel):
    batch_id: int
    traceable: bool
    holder_code: str | None = None
    page: int
    page_size: int
    total_records: int
    # 各权利人合计（受 holder_code 过滤）；不可追溯单据为空列表
    holder_totals: list[AttributionHolderTotal]
    # 当前页归因记录
    records: list[AttributionRecord]


# ---- 付款登记与对账 ----

class PaymentIn(BaseModel):
    # 权利人编号
    holder_code: str = Field(min_length=1, max_length=200)
    # 外部付款流水号（调用方提供，全局唯一）
    external_ref: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class PaymentOut(BaseModel):
    payment_id: int
    holder_code: str
    external_ref: str
    amount: int
    registered_at: datetime
    # created=新登记；duplicate=同流水号同内容重试，返回原记录
    status: str
    # 登记后余额（同流水号重试时为当前余额）
    balance_after: int


# 单据增减条目（全部期间已出具单据的明细行，追补差额保留正负号）
class LedgerStatementItem(BaseModel):
    batch_id: int
    kind: str  # initial | supplement
    period_start: datetime
    period_end: datetime
    amount: int
    issued_at: datetime


# 付款流水条目（已登记付款，从余额中扣减）
class LedgerPaymentItem(BaseModel):
    payment_id: int
    external_ref: str
    amount: int
    registered_at: datetime


class HolderLedger(BaseModel):
    holder_code: str
    name: str | None
    # 全部期间已出具单据明细的正负金额累计（不含试算、不含待处理播放）
    billed_total: int
    # 已登记付款累计
    paid_total: int
    # 当前余额 = billed_total - paid_total（可为负：跨期负额追补后留待后续抵扣）
    balance: int
    statements: list[LedgerStatementItem]
    payments: list[LedgerPaymentItem]


# ---- 银行回单批量导入与核验 ----

class ReceiptLineIn(BaseModel):
    # 回单编号（批次内不重复，全局唯一）
    receipt_no: str = Field(min_length=1, max_length=200)
    # 外部付款流水号（与已登记付款的 external_ref 对照；批内不重复）
    external_ref: str = Field(min_length=1, max_length=200)
    # 权利人编号
    holder_code: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class ReceiptBatchIn(BaseModel):
    # 批次编号（调用方提供，全局唯一）
    batch_no: str = Field(min_length=1, max_length=200)
    # 回单行：至少 1 行；批内回单编号与流水号重复由服务层整批拒绝
    lines: list[ReceiptLineIn] = Field(min_length=1, max_length=10000)


class ReceiptPaymentRef(BaseModel):
    # 冻结该版本结论时对照到的已登记付款快照（无则 None）
    payment_id: int
    holder_code: str
    amount: int


class ReceiptVerificationLineOut(BaseModel):
    line_no: int
    receipt_no: str
    external_ref: str
    holder_code: str
    amount: int
    # matched | payment_unregistered | holder_mismatch | amount_mismatch
    # | both_mismatch | duplicate_receipt
    status: str
    # 不符项：holder / amount；两项均不符同时列出；matched/重复为空
    mismatches: list[str]
    # 是否为跨批次重复回单（不参与核验）
    duplicate: bool
    duplicate_of_batch_no: str | None = None
    payment: ReceiptPaymentRef | None = None
    # 复核版本中相对上一版的原状态（仅变化行出现于 changed_lines 时有值）
    from_status: str | None = None


class ReceiptVerificationCounts(BaseModel):
    total: int
    matched: int
    payment_unregistered: int
    holder_mismatch: int
    amount_mismatch: int
    both_mismatch: int
    duplicate_receipt: int
    # 参与核验的有效回单数（不含重复回单）
    effective: int
    duplicate_receipts: int
    # 差异行总数（付款未登记 + 三种不符）
    differences: int


class ReceiptVersionOut(BaseModel):
    version_no: int
    # import=导入时冻结；review=付款迟到后复核生成
    created_by: str
    created_at: datetime
    changed_lines_count: int
    counts: ReceiptVerificationCounts
    # 跨批次重复回单行（不参与核验）
    duplicate_lines: list[ReceiptVerificationLineOut]
    # 差异行（付款未登记/权利人不符/金额不符/两项均不符）
    difference_lines: list[ReceiptVerificationLineOut]
    # 与上一版相比发生变化的行（导入第 1 版为空）
    changed_lines: list[ReceiptVerificationLineOut] = []
    lines: list[ReceiptVerificationLineOut]


class ReceiptBatchSummary(BaseModel):
    batch_no: str
    line_count: int
    imported_at: datetime
    version_count: int
    version_numbers: list[int]
    latest_version_no: int | None


class ReceiptRawLineOut(BaseModel):
    # 原始回单行（始终可查，不随后续核验/复核改写）
    line_no: int
    receipt_no: str
    external_ref: str
    holder_code: str
    amount: int
    duplicate: bool
    duplicate_of_batch_no: str | None = None
    imported_at: datetime


class ReceiptBatchOut(BaseModel):
    status: str = "imported"  # imported | duplicate_batch
    batch: ReceiptBatchSummary
    raw_receipts: list[ReceiptRawLineOut]
    latest_version: ReceiptVersionOut


class ReceiptReviewOut(BaseModel):
    # unchanged=结论无变化未新增版本；new_version=生成新版本
    status: str
    new_version: bool
    batch: ReceiptBatchSummary
    raw_receipts: list[ReceiptRawLineOut]
    latest_version: ReceiptVersionOut
    # status=unchanged 时为空；new_version 时为结论变化行
    changed_lines: list[ReceiptVerificationLineOut] = []


class ReceiptBatchList(BaseModel):
    total: int
    limit: int
    offset: int
    batches: list[ReceiptBatchSummary]
