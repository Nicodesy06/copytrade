"""Tipi condivisi: enum, reason code e envelope evento immutabile."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000


def utcnow() -> datetime:
    return datetime.now(UTC)


class Mode(StrEnum):
    DEMO = "demo"
    REPLAY = "replay"
    PAPER = "paper"
    LIVE = "live"


class DataOrigin(StrEnum):
    SIMULATED = "SIMULATO / DATI DI ESEMPIO"
    REPLAY = "REPLAY"
    MAINNET_READONLY = "MAINNET READ-ONLY"


class SystemState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"  # nuovi acquisti sospesi
    SAFE = "SAFE"  # modalità sicura: nessun acquisto, serve RIPRENDI umano
    STOPPED = "STOPPED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Decision(StrEnum):
    COPY = "COPY"
    SKIP = "SKIP"


class OrderState(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNFILLABLE = "UNFILLABLE"
    FAILED = "FAILED"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class LeaderStatus(StrEnum):
    SELECTED = "SELECTED"
    MANAGE_ONLY = "MANAGE_ONLY"  # non più seguito, posizioni aperte in gestione
    BLOCKED = "BLOCKED"


class EventType(StrEnum):
    WALLET_CANDIDATE = "WALLET_CANDIDATE"
    TRADER_SCORED = "TRADER_SCORED"
    LEADERS_SELECTED = "LEADERS_SELECTED"
    LEADER_SWAP = "LEADER_SWAP"
    PUBLIC_SWAP = "PUBLIC_SWAP"
    TX_RETRACTED = "TX_RETRACTED"
    TOKEN_ASSESSED = "TOKEN_ASSESSED"
    DECISION = "DECISION"
    ORDER_UPDATE = "ORDER_UPDATE"
    POSITION_CLOSED = "POSITION_CLOSED"
    PRICE_TICK = "PRICE_TICK"
    DAY_END = "DAY_END"
    PROVIDER_ALERT = "PROVIDER_ALERT"
    STATE_CHANGE = "STATE_CHANGE"


class ReasonCode(StrEnum):
    OK = "OK"
    # stato di sistema
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    SYSTEM_SAFE_MODE = "SYSTEM_SAFE_MODE"
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    LIVE_DISABLED = "LIVE_DISABLED"
    # token
    RUGCHECK_MISSING = "RUGCHECK_MISSING"
    RUGCHECK_STALE = "RUGCHECK_STALE"
    RUGCHECK_SCHEMA_INVALID = "RUGCHECK_SCHEMA_INVALID"
    RUGCHECK_DANGER = "RUGCHECK_DANGER"
    RUGCHECK_SCORE_HIGH = "RUGCHECK_SCORE_HIGH"
    RUGGED = "RUGGED"
    LP_LOCK_UNKNOWN = "LP_LOCK_UNKNOWN"
    LP_LOCK_TOO_LOW = "LP_LOCK_TOO_LOW"
    MINT_AUTHORITY_OPEN = "MINT_AUTHORITY_OPEN"
    FREEZE_AUTHORITY_OPEN = "FREEZE_AUTHORITY_OPEN"
    AUTHORITY_UNKNOWN = "AUTHORITY_UNKNOWN"
    AUTHORITY_MISMATCH = "AUTHORITY_MISMATCH"
    TOKEN2022_EXTENSION_UNSUPPORTED = "TOKEN2022_EXTENSION_UNSUPPORTED"
    TOKEN_PROGRAM_UNKNOWN = "TOKEN_PROGRAM_UNKNOWN"
    TOKEN_TOO_NEW = "TOKEN_TOO_NEW"
    TOKEN_AGE_UNKNOWN = "TOKEN_AGE_UNKNOWN"
    TOP_HOLDERS_CONCENTRATED = "TOP_HOLDERS_CONCENTRATED"
    HOLDERS_UNKNOWN = "HOLDERS_UNKNOWN"
    METADATA_SUSPICIOUS = "METADATA_SUSPICIOUS"
    # mercato
    LIQUIDITY_UNAVAILABLE = "LIQUIDITY_UNAVAILABLE"
    LIQUIDITY_TOO_LOW = "LIQUIDITY_TOO_LOW"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    PRICE_STALE = "PRICE_STALE"
    PRICE_IMPACT_TOO_HIGH = "PRICE_IMPACT_TOO_HIGH"
    QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
    QUOTE_LOW_CONFIDENCE = "QUOTE_LOW_CONFIDENCE"
    QUOTE_STALE = "QUOTE_STALE"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    UNFILLABLE_LIQUIDITY = "UNFILLABLE_LIQUIDITY"
    # provider
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    # llm
    LLM_DISABLED = "LLM_DISABLED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    # rischio / portafoglio
    LEADER_NOT_SELECTED = "LEADER_NOT_SELECTED"
    LEADER_BLOCKED_LOSSES = "LEADER_BLOCKED_LOSSES"
    MAX_POSITIONS = "MAX_POSITIONS"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    DOUBLE_EXPOSURE = "DOUBLE_EXPOSURE"
    COOLDOWN = "COOLDOWN"
    ORDER_TOO_SMALL = "ORDER_TOO_SMALL"
    EVENT_TOO_OLD = "EVENT_TOO_OLD"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    DATA_DESYNC = "DATA_DESYNC"
    # gestione posizioni
    NO_POSITION = "NO_POSITION"
    LEADER_SELL_MIRRORED = "LEADER_SELL_MIRRORED"
    LEADER_HOLDINGS_UNKNOWN = "LEADER_HOLDINGS_UNKNOWN"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MAX_HOLD = "MAX_HOLD"
    EXIT_NOT_RELIABLE = "EXIT_NOT_RELIABLE"
    RECOVERED_INCOMPLETE_ORDER = "RECOVERED_INCOMPLETE_ORDER"
    REORG_RETRACTED = "REORG_RETRACTED"
    REORG_AFTER_COPY = "REORG_AFTER_COPY"
    # trader / strategia
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    SCORE_TOO_LOW = "SCORE_TOO_LOW"
    NO_REPLACEMENT_CANDIDATE = "NO_REPLACEMENT_CANDIDATE"
    UNKNOWN_TX_CLASSIFICATION = "UNKNOWN_TX_CLASSIFICATION"
    # addendum sicurezza
    MAX_CHASE_EXCEEDED = "MAX_CHASE_EXCEEDED"
    LEADER_FILL_UNKNOWN = "LEADER_FILL_UNKNOWN"
    LEADER_BOOK_FULL = "LEADER_BOOK_FULL"
    RESERVATION_CONFLICT = "RESERVATION_CONFLICT"
    FEED_LAG_BREAKER = "FEED_LAG_BREAKER"
    QUOTE_LAG_BREAKER = "QUOTE_LAG_BREAKER"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"


# Spiegazioni in italiano mostrate in dashboard e report.
REASON_IT: dict[str, str] = {
    "OK": "Tutti i controlli superati",
    "SYSTEM_PAUSED": "Nuovi acquisti in pausa (comando manuale)",
    "SYSTEM_SAFE_MODE": "Modalità sicura attiva: nuovi acquisti bloccati",
    "SYSTEM_STOPPED": "Sistema arrestato",
    "LIVE_DISABLED": "Esecuzione reale disabilitata",
    "RUGCHECK_MISSING": "Report Rugcheck mancante",
    "RUGCHECK_STALE": "Report Rugcheck troppo vecchio",
    "RUGCHECK_SCHEMA_INVALID": "Risposta Rugcheck con schema inatteso",
    "RUGCHECK_DANGER": "Rugcheck segnala rischi di livello 'danger'",
    "RUGCHECK_SCORE_HIGH": "Punteggio di rischio Rugcheck troppo alto",
    "RUGGED": "Token segnalato come 'rugged'",
    "LP_LOCK_UNKNOWN": "Percentuale LP bloccata sconosciuta",
    "LP_LOCK_TOO_LOW": "Percentuale LP bloccata sotto soglia",
    "MINT_AUTHORITY_OPEN": "Mint authority ancora attiva",
    "FREEZE_AUTHORITY_OPEN": "Freeze authority ancora attiva",
    "AUTHORITY_UNKNOWN": "Stato delle authority sconosciuto",
    "AUTHORITY_MISMATCH": "Fonti discordanti sulle authority",
    "TOKEN2022_EXTENSION_UNSUPPORTED": "Estensione Token-2022 non supportata",
    "TOKEN_PROGRAM_UNKNOWN": "Programma token sconosciuto",
    "TOKEN_TOO_NEW": "Token/mercato troppo recente",
    "TOKEN_AGE_UNKNOWN": "Età del token sconosciuta",
    "TOP_HOLDERS_CONCENTRATED": "Detentori principali troppo concentrati",
    "HOLDERS_UNKNOWN": "Distribuzione dei detentori sconosciuta",
    "METADATA_SUSPICIOUS": "Metadati con testo sospetto (ignorato, solo segnalazione)",
    "LIQUIDITY_UNAVAILABLE": "Liquidità non disponibile",
    "LIQUIDITY_TOO_LOW": "Liquidità sotto la soglia minima",
    "PRICE_UNAVAILABLE": "Prezzo non disponibile",
    "PRICE_STALE": "Prezzo non aggiornato",
    "PRICE_IMPACT_TOO_HIGH": "Impatto sul prezzo stimato troppo alto",
    "QUOTE_UNAVAILABLE": "Quotazione non disponibile",
    "QUOTE_LOW_CONFIDENCE": "Quotazione poco affidabile",
    "QUOTE_STALE": "Quotazione troppo vecchia",
    "SLIPPAGE_TOO_HIGH": "Slippage stimato troppo alto",
    "UNFILLABLE_LIQUIDITY": "Ordine non eseguibile per liquidità insufficiente",
    "PROVIDER_RATE_LIMITED": "Provider in rate limit (429)",
    "PROVIDER_ERROR": "Errore del provider",
    "PROVIDER_DISABLED": "Provider disabilitato",
    "BUDGET_EXHAUSTED": "Budget gratuito del provider esaurito",
    "CIRCUIT_OPEN": "Circuit breaker aperto sul provider",
    "LLM_DISABLED": "LLM locale disabilitato",
    "LLM_TIMEOUT": "Timeout LLM locale",
    "LLM_INVALID_OUTPUT": "Risposta LLM non valida rispetto allo schema",
    "LEADER_NOT_SELECTED": "Wallet non tra i leader seguiti",
    "LEADER_BLOCKED_LOSSES": "Leader bloccato dopo perdite consecutive",
    "MAX_POSITIONS": "Numero massimo di posizioni raggiunto",
    "GROSS_EXPOSURE": "Esposizione lorda massima raggiunta",
    "DAILY_LOSS_LIMIT": "Perdita giornaliera massima raggiunta",
    "INSUFFICIENT_CASH": "Liquidità virtuale insufficiente",
    "DOUBLE_EXPOSURE": "Posizione già aperta su questo token",
    "COOLDOWN": "Periodo di attesa (cooldown) attivo",
    "ORDER_TOO_SMALL": "Ordine troppo piccolo",
    "EVENT_TOO_OLD": "Evento del leader troppo vecchio per essere copiato",
    "DUPLICATE_EVENT": "Evento duplicato ignorato",
    "POSITION_MISMATCH": "Posizioni non riconciliate",
    "DATA_DESYNC": "Dati desincronizzati",
    "NO_POSITION": "Nessuna posizione da vendere",
    "LEADER_SELL_MIRRORED": "Vendita del leader replicata in proporzione",
    "LEADER_HOLDINGS_UNKNOWN": "Saldo del leader sconosciuto: uscita totale prudenziale",
    "STOP_LOSS": "Stop-loss simulato",
    "TAKE_PROFIT": "Take-profit simulato",
    "MAX_HOLD": "Tempo massimo di detenzione raggiunto",
    "EXIT_NOT_RELIABLE": "Uscita non eseguibile in modo affidabile",
    "RECOVERED_INCOMPLETE_ORDER": "Ordine incompleto trovato al riavvio: annullato",
    "REORG_RETRACTED": "Transazione ritirata (reorg/fork)",
    "REORG_AFTER_COPY": "Transazione ritirata dopo la copia: posizione mantenuta in gestione",
    "INSUFFICIENT_HISTORY": "Storico insufficiente per valutare il trader",
    "SCORE_TOO_LOW": "Punteggio sotto la soglia minima",
    "NO_REPLACEMENT_CANDIDATE": "Nessun sostituto difendibile: nessun leader seguito",
    "UNKNOWN_TX_CLASSIFICATION": "Transazione non classificabile come swap",
    "MAX_CHASE_EXCEEDED": "Prezzo eseguibile troppo sopra il fill osservato del leader (max chase)",
    "LEADER_FILL_UNKNOWN": "Prezzo di esecuzione del leader sconosciuto",
    "LEADER_BOOK_FULL": "Quota di portafoglio assegnata al leader esaurita",
    "RESERVATION_CONFLICT": "Prenotazione di posizione in conflitto (limiti di portafoglio)",
    "FEED_LAG_BREAKER": "Breaker: ritardo del feed eventi oltre soglia",
    "QUOTE_LAG_BREAKER": "Breaker: ritardo delle quotazioni oltre soglia",
    "CONTRACT_VIOLATION": "Violazione di un invariante contabile/di rischio",
}


def reason_it(code: str) -> str:
    return REASON_IT.get(code, code)


def _canon(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, StrEnum):
        return obj.value
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_canon(obj), sort_keys=True, separators=(",", ":"))


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    origin: DataOrigin
    ref: str | None = None  # firma tx, URL o chiave fixture
    fetched_at: datetime | None = None


class EventEnvelope(BaseModel):
    """Envelope immutabile scambiato fra agenti."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: EventType
    source: str
    slot: int | None = None
    observed_at: datetime
    event_at: datetime
    wallet: str | None = None
    token: str | None = None
    idempotency_key: str
    provenance: Provenance
    payload: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def make_key(*parts: Any) -> str:
        return hashlib.sha256(canonical_json(list(parts)).encode()).hexdigest()[:32]


class SwapObservation(BaseModel):
    """Swap pubblico osservato (dal leader o da altri wallet) ricavato da variazioni di saldo nette."""

    model_config = ConfigDict(frozen=True)

    signature: str
    slot: int
    wallet: str
    mint: str
    side: Side
    token_amount_raw: int
    decimals: int
    quote_mint: str
    quote_amount_raw: int
    quote_decimals: int
    wallet_pre_amount_raw: int | None
    wallet_post_amount_raw: int | None
    fee_lamports: int = 0
    event_at: datetime
    commitment: str = "confirmed"
    program_labels: tuple[str, ...] = ()

    @property
    def token_amount(self) -> Decimal:
        return Decimal(self.token_amount_raw) / (Decimal(10) ** self.decimals)

    @property
    def quote_amount(self) -> Decimal:
        return Decimal(self.quote_amount_raw) / (Decimal(10) ** self.quote_decimals)

    @property
    def price_in_quote(self) -> Decimal | None:
        if self.token_amount_raw <= 0:
            return None
        return self.quote_amount / self.token_amount
