from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


CLAIM_SCHEMA = "trading_mvp_global_market_writer_claim_v1"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class GlobalMarketWriterClaimError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _read_claim(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise GlobalMarketWriterClaimError("global writer claim must be an object")
    if value.get("schema") != CLAIM_SCHEMA:
        raise GlobalMarketWriterClaimError("global writer claim schema mismatch")
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(_json_bytes(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_owner(
    claim: Mapping[str, Any],
    *,
    run_id: str,
    owner_pid: int,
    ownership_token: str,
) -> None:
    if str(claim.get("run_id") or "") != run_id:
        raise GlobalMarketWriterClaimError("global writer claim run_id mismatch")
    if int(claim.get("owner_pid") or 0) != int(owner_pid):
        raise GlobalMarketWriterClaimError("global writer claim owner_pid mismatch")
    if str(claim.get("ownership_token") or "") != ownership_token:
        raise GlobalMarketWriterClaimError("global writer claim token mismatch")


def claim_global_market_writer(
    claim_path: str | Path,
    *,
    run_id: str,
    owner_pid: int,
    owner_kind: str,
    plan_hash: str | None,
    output_namespace: str | Path,
    terminal_pid: int | None = None,
    ownership_token: str | None = None,
) -> dict[str, Any]:
    if SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id contains unsafe characters")
    if int(owner_pid) <= 0:
        raise ValueError("owner_pid must be positive")
    token = ownership_token or secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("ownership_token must be 32 lowercase hex characters")
    target = Path(claim_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": CLAIM_SCHEMA,
        "project": "trading_mvp",
        "status": "CLAIMED",
        "run_id": run_id,
        "owner_pid": int(owner_pid),
        # Additive: lets a reader tell "owner process is gone" from "owner lives on
        # another machine, so its pid means nothing here". Other consumers of this
        # shared file ignore unknown keys.
        "owner_host": socket.gethostname(),
        "writer_pid": None,
        "terminal_pid": int(terminal_pid) if terminal_pid else None,
        "owner_kind": owner_kind,
        "ownership_token": token,
        "plan_hash": plan_hash,
        "output_namespace": str(Path(output_namespace).expanduser().resolve()),
        "claimed_at_utc": utc_now(),
        "research_only": True,
        "live_orders": False,
        "private_api_keys": False,  # risk-scan: allow api_key - this line is the declaration itself
        "real_capital": False,
        "leverage_or_margin": False,
    }
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        try:
            existing = _read_claim(target)
            detail = (
                f"run_id={existing.get('run_id')} "
                f"owner_pid={existing.get('owner_pid')} "
                f"writer_pid={existing.get('writer_pid')}"
            )
        except (OSError, ValueError, json.JSONDecodeError) as read_exc:
            detail = f"unreadable existing claim: {type(read_exc).__name__}"
        raise GlobalMarketWriterClaimError(
            f"GLOBAL_MARKET_WRITER_CLAIM_EXISTS: {detail}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return payload


def attach_writer_pid(
    claim_path: str | Path,
    *,
    run_id: str,
    owner_pid: int,
    ownership_token: str,
    writer_pid: int,
) -> dict[str, Any]:
    if int(writer_pid) <= 0:
        raise ValueError("writer_pid must be positive")
    target = Path(claim_path).expanduser().resolve()
    claim = _read_claim(target)
    _assert_owner(
        claim,
        run_id=run_id,
        owner_pid=owner_pid,
        ownership_token=ownership_token,
    )
    claim["writer_pid"] = int(writer_pid)
    claim["writer_attached_at_utc"] = utc_now()
    _write_json_atomic(target, claim)
    return claim


def release_global_market_writer(
    claim_path: str | Path,
    *,
    run_id: str,
    owner_pid: int,
    ownership_token: str,
    final_status: str,
    archive_dir: str | Path | None = None,
) -> Path:
    target = Path(claim_path).expanduser().resolve()
    claim = _read_claim(target)
    _assert_owner(
        claim,
        run_id=run_id,
        owner_pid=owner_pid,
        ownership_token=ownership_token,
    )
    claim["status"] = "RELEASED"
    claim["final_status"] = final_status
    claim["released_at_utc"] = utc_now()
    claim["owner_pid"] = None
    claim["writer_pid"] = None
    _write_json_atomic(target, claim)
    archive_root = (
        Path(archive_dir).expanduser().resolve()
        if archive_dir is not None
        else target.parent / "global-writer-claim-archive"
    )
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / (
        f"{run_id}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    os.replace(target, archive_path)
    return archive_path


def inspect_global_market_writer_claim(
    claim_path: str | Path,
) -> dict[str, Any] | None:
    target = Path(claim_path).expanduser().resolve()
    return _read_claim(target) if target.exists() else None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the single global trading_mvp market-writer claim."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim = subparsers.add_parser("claim")
    claim.add_argument("--path", required=True)
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--owner-pid", type=int, required=True)
    claim.add_argument("--owner-kind", required=True)
    claim.add_argument("--plan-hash")
    claim.add_argument("--output-namespace", required=True)
    claim.add_argument("--terminal-pid", type=int)

    attach = subparsers.add_parser("attach")
    attach.add_argument("--path", required=True)
    attach.add_argument("--run-id", required=True)
    attach.add_argument("--owner-pid", type=int, required=True)
    attach.add_argument("--ownership-token", required=True)
    attach.add_argument("--writer-pid", type=int, required=True)

    release = subparsers.add_parser("release")
    release.add_argument("--path", required=True)
    release.add_argument("--run-id", required=True)
    release.add_argument("--owner-pid", type=int, required=True)
    release.add_argument("--ownership-token", required=True)
    release.add_argument("--final-status", required=True)
    release.add_argument("--archive-dir")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--path", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "claim":
        result: Any = claim_global_market_writer(
            args.path,
            run_id=args.run_id,
            owner_pid=args.owner_pid,
            owner_kind=args.owner_kind,
            plan_hash=args.plan_hash,
            output_namespace=args.output_namespace,
            terminal_pid=args.terminal_pid,
        )
    elif args.command == "attach":
        result = attach_writer_pid(
            args.path,
            run_id=args.run_id,
            owner_pid=args.owner_pid,
            ownership_token=args.ownership_token,
            writer_pid=args.writer_pid,
        )
    elif args.command == "release":
        result = {
            "archive_path": str(
                release_global_market_writer(
                    args.path,
                    run_id=args.run_id,
                    owner_pid=args.owner_pid,
                    ownership_token=args.ownership_token,
                    final_status=args.final_status,
                    archive_dir=args.archive_dir,
                )
            )
        }
    else:
        result = inspect_global_market_writer_claim(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
