"""Structured findings — the return vocabulary of every tool in this server.

The single design rule this project is built around:

    **A validation failure is a result, not an exception.**

A model that calls one of these tools on broken data must get something back it
can reason about and act on: what went wrong, where it went wrong, and what to
do next. So every tool returns a JSON object with the same envelope, and every
problem is a :class:`Finding` — a stable machine-readable ``code``, a
human-readable ``message``, and a :class:`Locator` that points at the exact row
and column.

Exceptions are reserved for genuinely malformed *tool arguments* — a column
contract that does not parse, a regex that does not compile — where there is no
data to validate and the caller must fix the call before anything can happen.

This module has no MCP dependency so it can be unit-tested and reused directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Severity levels. Kept to three so a caller can make one decision:
# "error" means the result cannot be trusted as-is, "warning" means it can but
# the caller should look, "info" means it is context rather than a problem.
ERROR = "error"
WARNING = "warning"
INFO = "info"

# Verdicts returned in the `verdict` field of every envelope.
VERDICT_CLEAN = "clean"
VERDICT_FLAGGED = "flagged"
VERDICT_QUARANTINED = "quarantined"
VERDICT_DOES_NOT_CLOSE = "does_not_close"
VERDICT_REVIEW_REQUIRED = "review_required"
VERDICT_INPUTS_REJECTED = "inputs_rejected"


@dataclass(frozen=True)
class Locator:
    """Where a finding applies.

    Every field is optional because not every finding has an address: a
    reconciliation failure points at a *side* of an equation, not at a row.

    Row indexes are zero-based (``row_index``) and one-based (``row_number``),
    because machines count from zero and spreadsheet users count from one, and
    a validation report is read by both.
    """

    row_index: int | None = None
    row_key: str | None = None
    column: str | None = None
    movement_index: int | None = None
    side: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.row_index is not None:
            out["row_index"] = self.row_index
            out["row_number"] = self.row_index + 1
        if self.row_key is not None:
            out["row_key"] = self.row_key
        if self.column is not None:
            out["column"] = self.column
        if self.movement_index is not None:
            out["movement_index"] = self.movement_index
            out["movement_number"] = self.movement_index + 1
        if self.side is not None:
            out["side"] = self.side
        return out


@dataclass(frozen=True)
class Finding:
    """One problem, with a stable code, a message, and an address."""

    code: str
    message: str
    severity: str = ERROR
    locator: Locator | None = None
    value: Any = None
    expected: Any = None
    guidance: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.locator is not None:
            locator = self.locator.to_dict()
            if locator:
                out["locator"] = locator
        if self.value is not None:
            out["value"] = self.value
        if self.expected is not None:
            out["expected"] = self.expected
        if self.guidance is not None:
            out["guidance"] = self.guidance
        return out

    def as_reason(self) -> str:
        """A one-line human summary, 'column: message' when a column is known."""
        if self.locator is not None and self.locator.column:
            return f"{self.locator.column}: {self.message}"
        return self.message


def findings_to_dicts(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [finding.to_dict() for finding in findings]


def count_by_code(findings: Sequence[Finding]) -> dict[str, int]:
    """Tally findings by code, most frequent first."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def count_by_column(findings: Sequence[Finding]) -> dict[str, int]:
    """Tally findings by the column they point at, most frequent first."""
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.locator is None or not finding.locator.column:
            continue
        column = finding.locator.column
        counts[column] = counts.get(column, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def envelope(
    tool: str,
    *,
    ok: bool,
    verdict: str,
    findings: Sequence[Finding] = (),
    guidance: str = "",
    **payload: Any,
) -> dict[str, Any]:
    """Build the standard tool response.

    ``ok`` is the one field a caller can branch on without reading anything
    else. It is ``True`` only when the data itself is fully trustworthy: for
    reconciliation that means the arithmetic closed, for duplicate detection it
    means no suspects were found.

    ``ok=False`` is never an error condition of the *call*. It means the data
    did not pass, and the findings say why.
    """
    body: dict[str, Any] = {
        "tool": tool,
        "ok": ok,
        "verdict": verdict,
        "finding_count": len(findings),
        "findings": findings_to_dicts(findings),
        "guidance": guidance,
    }
    body.update(payload)
    return body


def rejection_envelope(tool: str, message: str, guidance: str, **payload: Any) -> dict[str, Any]:
    """Envelope for input that is well-formed JSON but unusable as data.

    Used when the argument shape is wrong in a way we choose not to raise on —
    for example a ``rows`` argument that is not a list. Callers get the same
    envelope shape as everywhere else, so they never have to special-case it.
    """
    finding = Finding(code="invalid_arguments", message=message, severity=ERROR)
    return envelope(
        tool,
        ok=False,
        verdict=VERDICT_INPUTS_REJECTED,
        findings=[finding],
        guidance=guidance,
        **payload,
    )


def json_safe(value: Any) -> Any:
    """Best-effort conversion of a JSON-decoded value into a serialisable one.

    Inputs arrive from JSON, so this normally returns the value unchanged. It
    exists so that a non-finite float (``NaN``/``Infinity``, which some CSV
    readers emit and ``json.loads`` accepts) is reported as text instead of
    breaking response serialisation.
    """
    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
