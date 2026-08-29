"""Paths, shared control, and the single source of truth for what may be contacted.

This project observes crypto perpetual futures. That is a leveraged instrument class,
which is exactly why the endpoint list lives here and is enforced mechanically rather
than described in prose: observing a leveraged market must never become using leverage.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The same shared control surface the spot monitor uses. This project is a second
# market-data writer in the same workspace, so it takes the same single-writer claim
# rather than inventing a parallel one - two writers is the failure that contract exists
# to prevent, and a new repository does not make it a different workspace.
CONTROL_ROOT = Path(
    os.environ.get("PREMARKET_CAPTURE_CONTROL_ROOT", "C:/Users/koval/Documents/ZolotyayLopata")
).expanduser()
SHARED_WRITER_CLAIM_PATH = Path(
    os.environ.get(
        "PREMARKET_CAPTURE_WRITER_CLAIM_PATH",
        str(CONTROL_ROOT / "docs/agent-log/active-market-data-writer-claim.json"),
    )
).expanduser()
SHARED_GATE_PATH = Path(
    os.environ.get(
        "PREMARKET_CAPTURE_GATE_PATH",
        str(CONTROL_ROOT / "tools/check_active_run_gate.ps1"),
    )
).expanduser()

CAPTURE_ROOT = Path(
    os.environ.get(
        "PREMARKET_CAPTURE_ROOT",
        "E:/trading_mvp/premarket-perp-capture/captures",
    )
).expanduser()

V1_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822.json"
V2_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v2.json"
V3_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v3.json"
V4_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v4.json"
V5_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v5.json"
V6_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v6.json"
V7_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v7.json"
V8_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
V9_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v9.json"
V10_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v10.json"
V11_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v11.json"
V12_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v12.json"
V13_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v13.json"
V14_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v14.json"
V15_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v15.json"
V16_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v16.json"
V17_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v17.json"
V18_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v18.json"
V19_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v19.json"
V20_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v20.json"
V21_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v21.json"
V22_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v22.json"
V23_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v23.json"
V24_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v24.json"
V25_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v25.json"
V26_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v26.json"
V27_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v27.json"
V28_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v28.json"
V29_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v29.json"
V30_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v30.json"
V31_PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v31.json"
PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v32.json"
RUN_RECORD_PATH = PROJECT_ROOT / "docs/run/capture-run.json"
STOP_REQUEST_PATH = PROJECT_ROOT / "docs/run/stop-request.json"
CAPTURE_TOKEN_PATH = PROJECT_ROOT / "docs/run/capture-token.json"
EVIDENCE_DIR = PROJECT_ROOT / "docs/evidence"
CLAIM_ARCHIVE_DIR = PROJECT_ROOT / "docs/run/global-writer-claim-archive"
REGISTRY_QUARANTINE_ROOT = PROJECT_ROOT / "docs/registry/quarantine"
ANNOUNCEMENT_CANDIDATE_PATH = (
    PROJECT_ROOT / "docs/announcements/official-listing-candidates-v1.jsonl"
)
ANNOUNCEMENT_ATTEMPTS_PATH = (
    PROJECT_ROOT / "docs/announcements/official-listing-discovery-attempts-v1.jsonl"
)
ANNOUNCEMENT_STATE_PATH = (
    PROJECT_ROOT / "docs/announcements/official-listing-discovery-state-v1.json"
)

# Exact authority surface copied from a selected registry episode into the capture
# job, immutable manifest and receipt. Keep it here so the collector, replay loader
# and PlanOnly builder cannot evolve subtly different lineage vocabularies.
CAPTURE_LINEAGE_FIELDS: tuple[str, ...] = (
    "episode_id",
    "venue",
    "listing_venue",
    "premarket_contract_id",
    "spot_symbol",
    "official_spot_t0",
    "t0_source_class",
    "t0_precision_sec",
    "official_record_hash",
    "official_source_url",
    "official_source_identity",
    "registry_sha256",
    "registry_tail_record_hash",
    "mutation_receipt_seq",
    "mutation_receipt_hash",
    "summary_content_sha256",
    "registry_authority_state_hash",
    "plan_id",
    "plan_hash",
    "asset_class",
    "issuer_namespace",
    "issuer_id",
    "asset_identity_hash",
)

# Runtime files bound by the PlanOnly, as repo-relative paths so the plan can be
# generated and verified from any checkout location.
BOUND_RUNTIME_FILES: tuple[tuple[str, str], ...] = (
    ("project_config", "src/project_config.py"),
    ("canonical_hash", "src/canonical_hash.py"),
    ("capability_scan", "src/capability_scan.py"),
    ("risk_gate", "src/risk_gate.py"),
    ("plan_builder", "src/plan_builder.py"),
    ("public_http", "src/public_http.py"),
    ("event_registry", "src/event_registry.py"),
    ("registry_quarantine", "src/registry_quarantine.py"),
    ("global_market_writer_claim", "src/global_market_writer_claim.py"),
    ("capture", "src/capture.py"),
    ("replay", "src/replay.py"),
    ("paper_replay", "src/paper_replay.py"),
    ("paper_only_launcher", "tools/start_premarket_perp_paper_only_visible.ps1"),
    ("official_attestation", "src/official_attestation.py"),
    ("announcement_candidate_store", "src/announcement_candidate_store.py"),
    ("announcement_discovery", "src/announcement_discovery.py"),
    (
        "announcement_discovery_launcher",
        "tools/start_premarket_announcement_discovery_visible.ps1",
    ),
    # The forbidden-capability vocabulary is part of the contract, not a note:
    # widening it must require reissuing the plan like any runtime change.
    ("forbidden_capabilities", "docs/risk/forbidden-capabilities.txt"),
)

# Every host the runtime is allowed to contact, and every exact path under it. A URL
# that is not covered here is a finding, not a feature: the capability scan reads this
# tuple, so widening the reach of the project means editing this list, reissuing the
# PlanOnly and passing review - not adding a string somewhere in a collector.
MARKET_DATA_ALLOWED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    # Bybit v5 public market data (linear perpetuals, incl. pre-market instruments)
    ("api.bybit.com", "/v5/market/instruments-info"),
    ("api.bybit.com", "/v5/market/tickers"),
    ("api.bybit.com", "/v5/market/kline"),
    ("api.bybit.com", "/v5/market/orderbook"),
    ("api.bybit.com", "/v5/market/recent-trade"),
    # OKX v5 public market data
    ("www.okx.com", "/api/v5/public/instruments"),
    ("www.okx.com", "/api/v5/market/tickers"),
    ("www.okx.com", "/api/v5/market/ticker"),
    ("www.okx.com", "/api/v5/market/candles"),
    ("www.okx.com", "/api/v5/market/books"),
    ("www.okx.com", "/api/v5/market/trades"),
    # Gate v4 public USDT-margined futures market data
    ("api.gateio.ws", "/api/v4/futures/usdt/contracts"),
    ("api.gateio.ws", "/api/v4/futures/usdt/tickers"),
    ("api.gateio.ws", "/api/v4/futures/usdt/candlesticks"),
    ("api.gateio.ws", "/api/v4/futures/usdt/order_book"),
    ("api.gateio.ws", "/api/v4/futures/usdt/trades"),
)

# Discovery reads only bounded JSON indexes.  Article URLs are stored for explicit
# human review and are never fetched by this runtime.  Keeping this tuple separate
# prevents an announcement source from silently becoming a market-data venue.
ANNOUNCEMENT_ALLOWED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("api.bybit.com", "/v5/announcements/index"),
    ("api.bitget.com", "/api/v2/public/annoucements"),
    ("api.kucoin.com", "/api/v3/announcements"),
)
ALLOWED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    MARKET_DATA_ALLOWED_ENDPOINTS + ANNOUNCEMENT_ALLOWED_ENDPOINTS
)

ANNOUNCEMENT_MAX_PAGES = 3
ANNOUNCEMENT_MAX_ARTICLES_PER_VENUE = 150
ANNOUNCEMENT_MAX_TARGETS_PER_TICK = 20
ANNOUNCEMENT_VENUES: tuple[str, ...] = ("bybit", "bitget", "kucoin")

# Provenance policy for human-attested official listing moments.  Registry validation
# imports this policy rather than the attestation writer, so an arbitrary JSON row
# cannot become official merely by spelling OFFICIAL_ANNOUNCEMENT correctly.
OFFICIAL_ATTESTATION_SCHEMA = "premarket_perp_official_attestation_v3"
LEGACY_OFFICIAL_ATTESTATION_SCHEMAS: tuple[str, ...] = (
    "premarket_perp_official_attestation_v2",
)
SAME_UNDERLYING_ATTESTATION_SCHEMA = (
    "premarket_perp_same_underlying_attestation_v1"
)
# Whose announcement counts as official, keyed by the venue that LISTS the underlying
# on spot. That is not necessarily the venue trading the pre-market perpetual: a token
# whose perp sits on Bybit may be spot-listed on Binance or Upbit, and that listing is
# the catalyst the hypothesis is about. Tying the two together - as this list did until
# 2026-08-24 - would refuse the very announcement that matters.
#
# Every host here was measured against the venue's own announcement index rather than
# guessed: bybit 30/30 articles on announcements.bybit.com, okx 20/20 on www.okx.com,
# bitget 10/10 on www.bitget.com, binance articles under
# www.binance.com/en/support/announcement/<code>, kucoin relative paths under
# www.kucoin.com.
#
# What this list is NOT: an aggregator, a social account, or a news site. An "official"
# t0 taken from those is a different datum wearing the same word, and the point of
# naming hosts is to keep that distinction mechanical.
OFFICIAL_ANNOUNCEMENT_HOSTS: dict[str, tuple[str, ...]] = {
    "bybit": ("announcements.bybit.com", "www.bybit.com"),
    "okx": ("www.okx.com",),
    "gate": ("www.gate.com", "www.gate.io", "gate.io"),
    "binance": ("www.binance.com",),
    "bitget": ("www.bitget.com",),
    "kucoin": ("www.kucoin.com",),
    "upbit": ("upbit.com", "www.upbit.com"),
}

# The venues whose pre-market perpetuals this project captures. Deliberately a subset
# of the announcement list above and deliberately a separate name: widening who may
# announce a listing must never widen where this project reaches for market data.
PERP_VENUES: tuple[str, ...] = ("bybit", "okx", "gate")

# What this project does, as opposed to what it looks at. Every one of these is
# asserted by tests and recorded in the PlanOnly; the capability scan enforces the
# ones that can be enforced statically.
RISK_CONTRACT: dict[str, object] = {
    "observed_instrument_class": "crypto_perpetual_futures",
    "research_only": True,
    "public_data_only": True,
    "private_api": False,
    "api_keys": False,  # risk-scan: allow api_key - this line is the prohibition itself
    "request_signing": False,
    "orders": False,
    "offline_paper_simulation": True,
    "paper_execution": False,
    "live_execution": False,
    "uses_leverage": False,
    "uses_margin": False,
    "real_capital": False,
    "withdrawals_or_transfers": False,
    "execution_replay": "offline_simulation_over_captured_public_data_only",
    "acceptance_decision": "NONE_CAPTURE_ONLY",
}

# Fixed before any qualifying seconds-grade event or capture exists.  This model is
# descriptive feasibility research only; it is neither a venue testnet executor nor
# an instruction to place an order.
OFFLINE_PAPER_MODEL: dict[str, object] = {
    "direction": "LONG",
    "virtual_notional_usdt": 25,
    "leverage_equivalent": 1,
    "entry_lead_sec": 60,
    "exit_offsets_sec": [0, 5, 15, 60],
    "execution_style": "TAKER_LIKE_CAUSAL_DEPTH",
}

# Not everything that writes a file is the same kind of write, and treating them alike
# would be wrong in both directions: requiring the exclusive claim for a three-request
# metadata refresh would block a capture for no reason, while letting a sustained
# capture run without it is the two-writer accident the claim exists to prevent.
WRITE_CLASSES: dict[str, dict[str, object]] = {
    "metadata_registry": {
        "what": "listing-event registry refresh from public instrument endpoints",
        "requests": "a handful, one pass",
        "exclusive_writer_claim": False,
        "capture_token": False,
        "plan_and_capability_scan": True,
        "endpoint_allow_list": True,
    },
    "official_attestation": {
        "what": "append one human-verified official spot t0 to the event registry",
        "requests": "none; records already-read announcement evidence",
        "exclusive_writer_claim": False,
        "capture_token": False,
        "plan_and_capability_scan": True,
        "endpoint_allow_list": True,
    },
    "announcement_discovery": {
        "what": (
            "fetch bounded official announcement indexes and append unverified "
            "announcement candidates"
        ),
        "requests": "bounded index pages only; article bodies are never fetched",
        "exclusive_writer_claim": False,
        "capture_token": False,
        "plan_and_capability_scan": True,
        "endpoint_allow_list": True,
        "max_retries_per_request": 0,
    },
    "registry_quarantine": {
        "what": "archive one failed registry generation before deactivation",
        "requests": "none; local recovery transaction only",
        "exclusive_writer_claim": True,
        "capture_token": False,
        "plan_and_capability_scan": True,
        "endpoint_allow_list": True,
    },
    "market_data_capture": {
        "what": "continuous capture around a listing t0",
        "requests": "sustained, while a market is moving",
        "exclusive_writer_claim": True,
        "capture_token": True,
        "plan_and_capability_scan": True,
        "endpoint_allow_list": True,
    },
}

# Continuous capture around an event is a different risk class from a bounded tick:
# it runs while a market is moving, so it needs its own ceilings.
CAPTURE_WINDOW_BEFORE_SEC = 30 * 60
CAPTURE_WINDOW_AFTER_SEC = 15 * 60
MAX_CAPTURE_RUNTIME_SEC = CAPTURE_WINDOW_BEFORE_SEC + CAPTURE_WINDOW_AFTER_SEC + 600
MAX_REQUESTS_PER_CAPTURE = 20000
MAX_EVENTS_PER_CAPTURE = 1
CAPTURE_LAUNCH_EARLY_GRACE_SEC = 30
CAPTURE_LAUNCH_LATE_GRACE_SEC = 5

# A contract is current only when a complete, writer-timestamped venue-universe
# refresh is recent.  A sharp universe collapse is acquisition failure rather than
# evidence that every missing pre-market contract terminated.
MAX_COMPLETE_METADATA_REFRESH_AGE_SEC = 300
MIN_FULL_UNIVERSE_RETENTION_RATIO = 0.50
FULL_UNIVERSE_SURFACE_IDS = (
    "bybit_linear_trading",
    "okx_swap",
    "okx_futures",
    "gate_usdt_contracts",
)
LEGITIMATELY_EMPTY_SURFACE_IDS = ("bybit_linear_prelaunch",)


# REST polling yields a sample at the instants we ask, not a tape of what happened.
# For a hypothesis about +5/+15/+60 seconds that gap is the whole question, so the
# cadence is declared, budgeted, and the achieved rate is measured and published rather
# than assumed. Near t0 the sampling tightens, which is where the budget is worth spending.
PROBE_CADENCE_SEC: dict[str, float] = {"trades": 3.0, "orderbook": 3.0, "ticker": 6.0}
BURST_CADENCE_SEC: dict[str, float] = {"trades": 0.5, "orderbook": 0.5, "ticker": 2.0}
BURST_HALF_WIDTH_SEC = 120
ORDERBOOK_DEPTH = 50

# Replay-readiness is stricter than process completion.  Every exit is fixed before
# data exists, and the largest successful-sample gap in the burst may be only a small
# multiple of the declared cadence for that probe.
PRIMARY_EXIT_OFFSETS_SEC: tuple[int, ...] = (0, 5, 15, 60)
PRIMARY_ENTRY_LEAD_SEC = 60
REPLAY_REQUIRED_PROBES_BY_VENUE: dict[str, tuple[str, ...]] = {
    "bybit": ("trades", "orderbook", "ticker"),
    "okx": ("trades", "orderbook", "ticker"),
    # Gate's public futures ticker has no exchange timestamp (and may omit BBO).
    # It is still retained as an optional descriptive payload, but cannot be used
    # as causal clock evidence. Timestamped trades and depth are the replay surface.
    "gate": ("trades", "orderbook"),
}
MAX_BURST_GAP_CADENCE_MULTIPLIER = 3.0
MAX_SAMPLE_STALENESS_SEC: dict[str, float] = {
    "trades": 10.0,
    "orderbook": 5.0,
    "ticker": 10.0,
}
MAX_EXCHANGE_FUTURE_SKEW_SEC = 2.0


def path_is_absolute(value: object) -> bool:
    """Absolute in the semantics of the path itself, not of the OS reading it.

    The PlanOnly records a Windows capture root. Under PurePosixPath "E:/captures" is
    a relative path, so a check that used the running platform's flavour passed on
    Windows and failed on Linux for the same, correct plan - which is how a gate ends
    up refusing a valid plan on CI while approving it at home.

    Judging by the path's own flavour keeps the protection intact: "captures/x" is
    relative under both flavours and is still refused.
    """
    text = str(value or "")
    if not text:
        return False
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()
