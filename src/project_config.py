"""Paths, shared control, and the single source of truth for what may be contacted.

This project observes crypto perpetual futures. That is a leveraged instrument class,
which is exactly why the endpoint list lives here and is enforced mechanically rather
than described in prose: observing a leveraged market must never become using leverage.
"""

from __future__ import annotations

import os
from pathlib import Path


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
PLAN_PATH = PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v3.json"
RUN_RECORD_PATH = PROJECT_ROOT / "docs/run/capture-run.json"
STOP_REQUEST_PATH = PROJECT_ROOT / "docs/run/stop-request.json"
CAPTURE_TOKEN_PATH = PROJECT_ROOT / "docs/run/capture-token.json"
EVIDENCE_DIR = PROJECT_ROOT / "docs/evidence"
CLAIM_ARCHIVE_DIR = PROJECT_ROOT / "docs/run/global-writer-claim-archive"

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
    # The forbidden-capability vocabulary is part of the contract, not a note:
    # widening it must require reissuing the plan like any runtime change.
    ("forbidden_capabilities", "docs/risk/forbidden-capabilities.txt"),
)

# Every host the runtime is allowed to contact, and every exact path under it. A URL
# that is not covered here is a finding, not a feature: the capability scan reads this
# tuple, so widening the reach of the project means editing this list, reissuing the
# PlanOnly and passing review - not adding a string somewhere in a collector.
ALLOWED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    # Bybit v5 public market data (linear perpetuals, incl. pre-market instruments)
    ("api.bybit.com", "/v5/market/instruments-info"),
    ("api.bybit.com", "/v5/market/tickers"),
    ("api.bybit.com", "/v5/market/kline"),
    ("api.bybit.com", "/v5/market/orderbook"),
    ("api.bybit.com", "/v5/market/recent-trade"),
    # OKX v5 public market data
    ("www.okx.com", "/api/v5/public/instruments"),
    ("www.okx.com", "/api/v5/market/tickers"),
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
    "paper_execution": False,
    "live_execution": False,
    "uses_leverage": False,
    "uses_margin": False,
    "real_capital": False,
    "withdrawals_or_transfers": False,
    "execution_replay": "offline_simulation_over_captured_public_data_only",
    "acceptance_decision": "NONE_CAPTURE_ONLY",
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
