"""Configurazione: config.yaml (non segreto) + variabili d'ambiente / .env (segreti)."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .types import Mode, canonical_json

STRATEGY_SCHEMA_VERSION = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskConfig(_Strict):
    starting_equity_usd: Decimal = Decimal("1000")
    max_leaders: int = Field(3, ge=0, le=3)
    max_positions: int = Field(3, ge=0, le=3)
    max_position_pct: Decimal = Field(Decimal("0.05"), gt=0, le=Decimal("0.05"))
    max_gross_exposure_pct: Decimal = Field(Decimal("0.15"), gt=0, le=Decimal("0.15"))
    max_daily_loss_pct: Decimal = Field(Decimal("0.02"), gt=0, le=Decimal("0.02"))
    flat_ticket_usd: Decimal = Decimal("20")
    min_order_usd: Decimal = Decimal("5")
    # ipotesi iniziali prudenti: NON garantiscono la sicurezza del token
    min_liquidity_usd: Decimal = Decimal("50000")
    max_price_impact_pct: Decimal = Decimal("1.5")
    max_order_liquidity_fraction: Decimal = Decimal("0.02")
    max_price_age_s: int = 90
    max_quote_age_s: int = 30
    max_slippage_bps: int = 150
    min_token_age_minutes: int = 120
    max_top10_holder_pct: Decimal = Decimal("60")
    max_rugcheck_score_normalised: int = 30
    min_lp_locked_pct: Decimal = Decimal("0")
    lp_lock_unknown_policy: Literal["reject"] = "reject"
    max_event_age_s: int = 120
    consecutive_loss_limit: int = Field(2, ge=1)
    cooldown_s: int = 600
    stop_loss_pct: Decimal = Decimal("0.25")
    take_profit_pct: Decimal = Decimal("1.0")
    max_hold_hours: int = 72
    token_check_ttl_s: int = 300
    # addendum sicurezza: ipotesi di test, non garanzie
    max_chase_pct: Decimal = Field(Decimal("15"), gt=0, le=Decimal("15"))
    # quota dell'esposizione massima assegnata a ciascun leader; None = 1/max_leaders
    leader_book_fraction: Decimal | None = Field(None, gt=0, le=1)
    feed_lag_max_s: int = 60
    quote_lag_max_s: int = 20
    breaker_trip_count: int = 3
    breaker_recover_count: int = 3
    allowed_token2022_extensions: list[str] = [
        "metadataPointer",
        "tokenMetadata",
        "groupPointer",
        "groupMemberPointer",
        "tokenGroup",
        "tokenGroupMember",
    ]


class SimulationConfig(_Strict):
    fill_delay_s: int = 4
    base_fee_lamports: int = 5000
    priority_fee_lamports: int = 50000
    dex_fee_bps: int = 30
    account_rent_lamports: int = 2039280
    extra_slippage_bps: int = 50
    quote_deviation_max_pct: Decimal = Decimal("3")
    max_exit_price_impact_pct: Decimal = Decimal("15")


class TraderConfig(_Strict):
    min_round_trips: int = 8
    min_score: Decimal = Decimal("0.55")
    min_median_hold_s: int = 120
    max_history_tx: int = 100
    lookback_days: int = 30


class StrategyConfig(_Strict):
    primary: str = "switch2"
    enabled: list[str] = ["switch2", "switch3", "naive", "ai_rank", "flat20"]


class DiscoveryConfig(_Strict):
    max_candidates: int = 25
    max_token_scans_per_cycle: int = 3
    swaps_per_token_scan: int = 20
    interval_s: int = 900


class BudgetConfig(_Strict):
    daily_requests: int
    monthly_requests: int
    per_minute: int


class ProvidersConfig(_Strict):
    rpc_http_url: str = "https://api.mainnet-beta.solana.com"
    rpc_ws_url: str = "wss://api.mainnet-beta.solana.com"
    use_helius: bool = False
    helius_http_url: str = "https://mainnet.helius-rpc.com/"
    helius_ws_url: str = "wss://mainnet.helius-rpc.com/"
    dexscreener_url: str = "https://api.dexscreener.com"
    jupiter_url: str = "https://api.jup.ag"
    rugcheck_url: str = "https://api.rugcheck.xyz"
    rugcheck_enabled: bool = True
    http_timeout_s: float = 10.0
    poll_interval_s: int = 20
    price_refresh_s: int = 30
    budget: dict[str, BudgetConfig] = {
        "rpc": BudgetConfig(daily_requests=20000, monthly_requests=300000, per_minute=60),
        "dexscreener": BudgetConfig(daily_requests=10000, monthly_requests=250000, per_minute=50),
        "jupiter": BudgetConfig(daily_requests=3000, monthly_requests=60000, per_minute=30),
        "rugcheck": BudgetConfig(daily_requests=1000, monthly_requests=20000, per_minute=10),
    }


class LLMConfig(_Strict):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    timeout_s: float = 60.0
    max_concurrency: int = 1
    max_queue: int = 8

    @field_validator("base_url")
    @classmethod
    def _local_only(cls, v: str) -> str:
        if not (v.startswith("http://127.0.0.1") or v.startswith("http://localhost")):
            raise ValueError("L'LLM deve essere locale (http://127.0.0.1:11434)")
        return v


class DashboardConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765
    allow_non_local: bool = False

    @model_validator(mode="after")
    def _local(self) -> DashboardConfig:
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.allow_non_local:
            raise ValueError("La dashboard può ascoltare solo su 127.0.0.1 (allow_non_local=false)")
        return self


class AppConfig(_Strict):
    mode: Mode = Mode.PAPER
    live_enabled: bool = False
    network: Literal["solana-mainnet-readonly"] = "solana-mainnet-readonly"
    data_dir: str = "data"
    seed_wallets: list[str] = []
    risk: RiskConfig = RiskConfig()
    simulation: SimulationConfig = SimulationConfig()
    trader: TraderConfig = TraderConfig()
    strategy: StrategyConfig = StrategyConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    providers: ProvidersConfig = ProvidersConfig()
    llm: LLMConfig = LLMConfig()
    dashboard: DashboardConfig = DashboardConfig()

    @model_validator(mode="after")
    def _no_live(self) -> AppConfig:
        if self.live_enabled or self.mode == Mode.LIVE:
            raise ValueError(
                "Modalità live NON implementata in MVP 0.1: live_enabled deve essere false "
                "(vedi docs/LIVE_CHECKLIST.md)"
            )
        return self

    def strategy_version(self) -> str:
        """Hash delle soglie: ogni modifica crea una nuova versione (niente ritocchi retroattivi)."""
        blob = canonical_json(
            {
                "schema": STRATEGY_SCHEMA_VERSION,
                "risk": self.risk.model_dump(),
                "sim": self.simulation.model_dump(),
                "trader": self.trader.model_dump(),
                "strategy": self.strategy.model_dump(),
            }
        )
        return "v" + STRATEGY_SCHEMA_VERSION + "-" + hashlib.sha256(blob.encode()).hexdigest()[:10]


class Secrets(BaseSettings):
    """Segreti letti solo da ambiente/.env, mai loggati."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    helius_api_key: SecretStr | None = Field(default=None, alias="HELIUS_API_KEY")
    jupiter_api_key: SecretStr | None = Field(default=None, alias="JUPITER_API_KEY")
    dashboard_token: SecretStr | None = Field(default=None, alias="M4DESK_DASHBOARD_TOKEN")


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else Path("config.yaml")
    if not p.exists():
        return AppConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def load_secrets() -> Secrets:
    return Secrets()
