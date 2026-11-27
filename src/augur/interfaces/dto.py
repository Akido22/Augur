"""Общие DTO для GUI, REST и CLI (ARCHITECTURE.md §9.3).

Единственный источник моделей ответа интерфейсов. Деньги и объёмы — десятичные строки
(как в БД), время — Unix ms UTC. DTO не содержат секретов.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class StatusDTO(BaseModel):
    """`GET /api/v1/status`: версии, режим, CB, сводка по портфелю."""

    app_version: str
    db_schema_version: int
    api_version: str
    configured_mode: str | None  # из конфига; None — настройки не загружены
    max_allowed_mode: str | None
    mode: str | None  # mode_state; None — строки нет (эффективно не выше PAPER)
    mode_changed_ts: int | None
    mode_changed_by: str | None
    cb_state: str
    cb_reason: str | None
    cb_tripped_ts: int | None
    fusion_enabled: bool | None
    open_orders: int
    awaiting_approval: int
    open_positions: int
    active_news_reactions: int
    last_audit_id: int | None
    server_ts: int


class SignalDTO(BaseModel):
    id: str
    kind: str
    strategy: str | None
    symbol: str
    timeframe: str | None
    action: str
    strength: float
    source: str
    status: str
    fused_signal_id: str | None
    parent_ids: list[str]
    risk_flags: list[Any]
    explanation_ref: str | None
    stop_loss: str | None
    take_profit: str | None
    data_hash: str | None
    config_hash: str
    code_version: str
    bar_ts: int | None
    created_ts: int
    expires_ts: int | None


class FusedSignalDTO(BaseModel):
    id: str
    signal_id: str
    api_version: str
    asset: str
    timestamp: int
    expires_at: int
    action: str
    strength: float
    reason: str
    risk_flags: list[Any]
    source_breakdown: dict[str, Any]
    fusion_config_version: str
    valid: bool
    validation_errors: list[Any]


class DecisionAuditDTO(BaseModel):
    id: str
    signal_id: str
    fused_signal_id: str | None
    order_id: str | None
    app_version: str
    strategy_version: str
    ml_model_version: str
    llm_prompt_version: str
    ml_prediction_id: str | None
    llm_status: str | None
    risk_verdict: str
    risk_reason: str | None
    config_hash: str
    mode: str
    execution_result: dict[str, Any] | None
    explanation: str | None
    created_at: int


class FillDTO(BaseModel):
    id: str
    order_id: str
    venue_fill_id: str
    qty: str
    price: str
    fee: str
    ts: int


class OrderDTO(BaseModel):
    id: str
    client_order_id: str
    venue_order_id: str | None
    signal_id: str
    risk_verdict_id: str
    venue: str
    symbol: str
    side: str
    type: str
    qty: str
    limit_price: str | None
    stop_price: str | None
    filled_qty: str
    avg_fill_price: str | None
    fee: str
    fee_ccy: str | None
    state: str
    mode: str
    reject_reason: str | None
    created_ts: int
    updated_ts: int
    fills: list[FillDTO] = Field(default_factory=list)


class PositionDTO(BaseModel):
    venue: str
    symbol: str
    mode: str
    qty: str
    avg_price: str
    realized_pnl: str
    unrealized_pnl: str
    stop_loss: str | None
    take_profit: str | None
    opened_ts: int | None
    updated_ts: int


class PortfolioSnapshotDTO(BaseModel):
    ts: int
    mode: str
    equity: str
    cash: str
    day_pnl: str
    peak_equity: str
    drawdown_pct: str


class AuditEventDTO(BaseModel):
    id: int
    ts: int
    actor: str
    event: str
    severity: str
    ref_type: str | None
    ref_id: str | None
    payload: dict[str, Any]


class DecisionChainDTO(BaseModel):
    """Цепочка решения (ARCH §7.5): сигналы → fused → decision_audit → ордера → fills.

    `signal` — итоговый (`kind='final'`) сигнал, если он есть; иначе запрошенный.
    """

    requested_id: str
    signal: SignalDTO
    strategy_signals: list[SignalDTO]
    fused_signal: FusedSignalDTO | None
    decisions: list[DecisionAuditDTO]
    orders: list[OrderDTO]
    positions: list[PositionDTO]
    events: list[AuditEventDTO]


class NewsDTO(BaseModel):
    id: str
    source: str
    url: str | None
    title: str
    summary: str | None
    published_ts: int
    fetched_ts: int
    symbols: list[str]
    sentiment: float | None
    sentiment_method: str | None
    confidence: float | None
    importance: str | None


class NewsReactionDTO(BaseModel):
    id: str
    rule: str
    action: str
    symbol: str | None
    strategy: str | None
    flag_code: str | None
    flag_severity: str | None
    news_id: str
    created_ts: int
    expires_ts: int


class AuditChainReportDTO(BaseModel):
    """Результат проверки hash-chain `audit_log` (`augur audit verify-chain`)."""

    ok: bool
    rows_checked: int
    first_bad_id: int | None = None
    problem: str | None = None


class KillSwitchRequestDTO(BaseModel):
    flatten: bool = True


class KillSwitchResultDTO(BaseModel):
    was_state: str
    state: Literal["KILLED"] = "KILLED"
    mode: Literal["ANALYSIS_ONLY"] = "ANALYSIS_ONLY"
    cancelled: int
    flatten_orders: int
    warnings: list[str] = Field(default_factory=list)


class EventDTO(BaseModel):
    """Сообщение `/ws/events` (ARCH §9.1)."""

    type: Literal[
        "signal_ready",
        "risk_verdict",
        "order_update",
        "position_update",
        "news_event",
        "cb_state",
        "mode_changed",
        "audit",
    ]
    data: dict[str, Any]
