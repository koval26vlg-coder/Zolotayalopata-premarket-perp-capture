"""Static proof that the runtime cannot do what the risk contract forbids.

The spot monitor's audit found that its strongest rules - no private API, no second
writer, no hidden run - existed only as prose in AGENTS.md while the code enforced
none of them. This project starts from the opposite end: the capabilities it must
never acquire are a bound data file, and the runtime is scanned against it.

Two things are checked.

Forbidden markers: literal strings that only appear in source which places orders,
signs requests, carries credentials, or changes leverage. Observing a leveraged
market is the point of this project; taking leverage is not, and the difference has
to be mechanical.

Endpoint reach: every URL in the runtime must be covered by the allow-list in
project_config. Widening what the project can contact then means editing that list,
reissuing the PlanOnly and passing review, rather than pasting a string into a
collector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCANNED_GLOBS = ("src/*.py", "tools/*.ps1", "tools/*.py")
# Tests are excluded deliberately: proving a forbidden capability is rejected requires
# naming it. test_capability_scan.py asserts this exclusion is exactly this narrow.
EXCLUDED_DIRS = ("tests", "docs", ".git", "__pycache__")

_URL = re.compile(r"https?://[^\s\"'<>)\\]+")
# A declaration has to be able to name what it forbids: a flag spelled api_key(s) set  # risk-scan: allow api_key
# to False contains the very marker it rules out. Exemptions are therefore per line and
# per pattern, spelled out in the source and counted in the result, so they cannot
# accumulate unnoticed the way a blanket file exclusion would.
_EXEMPTION = re.compile(r"#\s*risk-scan:\s*allow\s+([^\s#]+)")


@dataclass(frozen=True)
class Marker:
    pattern: str
    reason: str


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    detail: str
    reason: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.detail} ({self.reason})"


def load_markers(markers_path: Path) -> list[Marker]:
    markers: list[Marker] = []
    for raw in markers_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, _, reason = line.partition("|")
        pattern = pattern.strip()
        if not pattern:
            continue
        markers.append(Marker(pattern=pattern.lower(), reason=reason.strip() or "forbidden"))
    if not markers:
        raise ValueError(f"no markers loaded from {markers_path}")
    return markers


def scanned_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(root.glob(pattern)):
            if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
                continue
            found.append(path)
    return found


def _endpoint_allowed(url: str, allowed: Sequence[tuple[str, str]]) -> bool:
    without_scheme = url.split("://", 1)[-1]
    host, _, remainder = without_scheme.partition("/")
    path = "/" + remainder.split("?", 1)[0]
    return any(host == a_host and path.startswith(a_path) for a_host, a_path in allowed)


def scan_runtime(
    root: Path,
    *,
    markers: Iterable[Marker],
    allowed_endpoints: Sequence[tuple[str, str]],
) -> tuple[list[Finding], list[Finding]]:
    markers = list(markers)
    findings: list[Finding] = []
    exemptions: list[Finding] = []
    for path in scanned_files(root):
        relative = path.relative_to(root).as_posix()
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = raw.lower()
            exempted = {item.lower() for item in _EXEMPTION.findall(raw)}
            for marker in markers:
                if marker.pattern not in lowered:
                    continue
                if marker.pattern in exempted:
                    exemptions.append(
                        Finding(relative, number, "exemption", marker.pattern,
                                "declared inline")
                    )
                    continue
                findings.append(
                    Finding(relative, number, "forbidden capability",
                            marker.pattern, marker.reason)
                )
            for url in _URL.findall(raw):
                if url.lower() in exempted:
                    exemptions.append(
                        Finding(relative, number, "exemption", url, "declared inline")
                    )
                    continue
                if not _endpoint_allowed(url, allowed_endpoints):
                    findings.append(
                        Finding(relative, number, "endpoint outside the allow-list",
                                url, "declare it in project_config and reissue the plan")
                    )
    return findings, exemptions


def assert_runtime_is_clean(
    root: Path,
    *,
    markers_path: Path,
    allowed_endpoints: Sequence[tuple[str, str]],
) -> dict[str, object]:
    findings, exemptions = scan_runtime(
        root, markers=load_markers(markers_path), allowed_endpoints=allowed_endpoints
    )
    if findings:
        raise CapabilityViolation(
            "runtime declares capabilities the risk contract forbids:\n  "
            + "\n  ".join(finding.describe() for finding in findings)
        )
    return {
        "status": "CAPABILITY_SCAN_CLEAN",
        "files_scanned": len(scanned_files(root)),
        "markers": len(load_markers(markers_path)),
        "allowed_endpoints": len(allowed_endpoints),
        # Reported rather than hidden: an exemption is a decision, and a growing count
        # is something a reviewer should see in the CI output without digging.
        "exemptions": [f"{f.path}:{f.line} {f.detail}" for f in exemptions],
    }


class CapabilityViolation(RuntimeError):
    """The runtime can reach or do something the risk contract rules out."""
