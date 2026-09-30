"""Arithmetic reconciliation: ``closing = opening + movements``.

Field-level validation asks "does this value look right?". Reconciliation asks
a stronger question: "are these values consistent with *each other*?"

It is the one check that needs no model, no confidence score, and no reference
data — only arithmetic that has to hold. If a statement extracts cleanly and the
arithmetic does not close, the extraction is wrong no matter how plausible each
individual number looked. That is the failure nobody sees, and it is why this
belongs in the same server as the row contract.

A movement is a signed change to the balance, or an explicit direction:

    {"amount": 250.00, "direction": "in",  "label": "INV-1001"}
    {"amount":  40.50, "direction": "out", "label": "bank fee"}
    -40.50                      # a bare number is taken as already signed

Nothing here raises on data. A movement that cannot be read is returned as a
finding and excluded, and the result says plainly that the arithmetic is
therefore not a reliable verdict.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from .findings import (
    ERROR,
    INFO,
    VERDICT_CLEAN,
    VERDICT_DOES_NOT_CLOSE,
    VERDICT_INPUTS_REJECTED,
    WARNING,
    Finding,
    Locator,
    json_safe,
)

DIRECTION_IN = "in"
DIRECTION_OUT = "out"

_IN_WORDS = frozenset({"in", "inflow", "inflows", "credit", "cr", "receipt", "deposit", "+"})
_OUT_WORDS = frozenset({"out", "outflow", "outflows", "debit", "dr", "payment", "withdrawal", "-"})

ARITHMETIC = "closing_balance = opening_balance + sum(signed movements)"


@dataclass(frozen=True)
class Movement:
    """One parsed, usable movement."""

    index: int
    amount: float
    direction: str
    label: str | None = None
    date: str | None = None

    @property
    def signed_amount(self) -> float:
        return self.amount if self.direction == DIRECTION_IN else -self.amount

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "movement_index": self.index,
            "movement_number": self.index + 1,
            "amount": self.amount,
            "direction": self.direction,
            "signed_amount": self.signed_amount,
        }
        if self.label is not None:
            out["label"] = self.label
        if self.date is not None:
            out["date"] = self.date
        return out


@dataclass
class ReconcileOutcome:
    """The result of checking one period of arithmetic."""

    opening: float = 0.0
    reported_closing: float = 0.0
    tolerance: float = 0.0
    movements: list[Movement] = dataclass_field(default_factory=list)
    findings: list[Finding] = dataclass_field(default_factory=list)
    movements_in: int = 0
    movements_out: int = 0
    movements_total_in: float = 0.0
    movements_total_out: float = 0.0
    rejected_movements: int = 0
    balances_readable: bool = True

    @property
    def movements_net(self) -> float:
        return self.movements_total_in - self.movements_total_out

    @property
    def expected_closing(self) -> float:
        return self.opening + self.movements_net

    @property
    def variance(self) -> float:
        return self.reported_closing - self.expected_closing

    @property
    def inputs_usable(self) -> bool:
        """True when every input was read, so the arithmetic means something.

        Both a balance that could not be read and a movement that could not be
        read make the check meaningless. Either one has to withhold the verdict,
        because "it closed" computed from numbers we partly guessed is exactly the
        confident wrong answer this server exists to prevent.
        """
        return self.balances_readable and self.rejected_movements == 0

    @property
    def closes(self) -> bool | None:
        """``None`` when the inputs were incomplete and the check cannot be trusted."""
        if not self.inputs_usable:
            return None
        return abs(self.variance) <= self.tolerance

    @property
    def verdict(self) -> str:
        if not self.inputs_usable:
            return VERDICT_INPUTS_REJECTED
        return VERDICT_CLEAN if self.closes else VERDICT_DOES_NOT_CLOSE

    def to_dict(self) -> dict[str, Any]:
        return {
            "arithmetic": ARITHMETIC,
            "opening_balance": self.opening,
            "movements_net": self.movements_net,
            "expected_closing_balance": self.expected_closing,
            "reported_closing_balance": self.reported_closing,
            "variance": self.variance,
            "absolute_variance": abs(self.variance),
            "tolerance": self.tolerance,
            "closes": self.closes,
            "arithmetic_is_reliable": self.inputs_usable,
            "movement_counts": {
                "total": len(self.movements),
                "in": self.movements_in,
                "out": self.movements_out,
                "unreadable": self.rejected_movements,
            },
            "movement_totals": {
                "in": self.movements_total_in,
                "out": self.movements_total_out,
            },
            "movements": [movement.to_dict() for movement in self.movements],
        }


def _as_finite_number(value: Any) -> float | None:
    """Return a finite float for a JSON number or numeric string, else ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _parse_direction(raw: Any) -> str | None:
    if isinstance(raw, str):
        token = raw.strip().casefold()
        if token in _IN_WORDS:
            return DIRECTION_IN
        if token in _OUT_WORDS:
            return DIRECTION_OUT
    return None


def parse_movement(raw: Any, index: int) -> tuple[Movement | None, Finding | None]:
    """Parse one movement. Returns ``(movement, finding)``; exactly one is set."""
    locator = Locator(movement_index=index, side="movements")

    if isinstance(raw, Mapping):
        unknown = sorted(
            set(raw)
            - {"amount", "value", "direction", "sign", "label", "description", "date", "reference"}
        )
        if unknown:
            return None, Finding(
                code="unknown_movement_key",
                message=(
                    f"movement has unrecognised key(s) {', '.join(repr(k) for k in unknown)}; "
                    "expected amount, direction, label, date"
                ),
                severity=ERROR,
                locator=locator,
                value=json_safe(raw),
            )

        amount_raw = raw.get("amount", raw.get("value"))
        amount = _as_finite_number(amount_raw)
        if amount is None:
            return None, Finding(
                code="movement_not_numeric",
                message=(
                    f"movement amount {amount_raw!r} is not a finite number, so it cannot be "
                    "added to the balance"
                ),
                severity=ERROR,
                locator=locator,
                value=json_safe(amount_raw),
                guidance=(
                    "Fix or remove this movement, then re-run; until then the arithmetic "
                    "is not reliable."
                ),
            )

        direction_raw = raw.get("direction", raw.get("sign"))
        if direction_raw is None:
            # A signed amount with no direction: a bare negative is a decrease.
            direction = DIRECTION_OUT if amount < 0 else DIRECTION_IN
        else:
            direction = _parse_direction(direction_raw)
            if direction is None:
                return None, Finding(
                    code="unknown_direction",
                    message=(
                        f"movement direction {direction_raw!r} is not understood; "
                        "use 'in' or 'out'"
                    ),
                    severity=ERROR,
                    locator=locator,
                    value=json_safe(direction_raw),
                )

        label = raw.get("label", raw.get("description", raw.get("reference")))
        date = raw.get("date")
        return (
            Movement(
                index=index,
                amount=abs(amount),
                direction=direction,
                label=None if label is None else str(label),
                date=None if date is None else str(date),
            ),
            None,
        )

    number = _as_finite_number(raw)
    if number is None:
        return None, Finding(
            code="movement_not_numeric",
            message=(
                f"movement {raw!r} is not a finite number; supply a number or an object like "
                '{"amount": 250.00, "direction": "in"}'
            ),
            severity=ERROR,
            locator=locator,
            value=json_safe(raw),
            guidance="Fix or remove this movement, then re-run; until then the arithmetic is not reliable.",
        )

    return (
        Movement(
            index=index,
            amount=abs(number),
            direction=DIRECTION_OUT if number < 0 else DIRECTION_IN,
        ),
        None,
    )


def reconcile_arithmetic(
    opening_balance: Any,
    movements: Any,
    closing_balance: Any,
    tolerance: float = 0.01,
) -> ReconcileOutcome | None:
    """Check ``closing = opening + movements``.

    Returns ``None`` when the scalar arguments themselves are unusable — the
    caller turns that into a rejected-inputs envelope. Everything else, including
    unreadable individual movements, is reported as data.
    """
    opening = _as_finite_number(opening_balance)
    closing = _as_finite_number(closing_balance)

    outcome = ReconcileOutcome(opening=opening or 0.0, reported_closing=closing or 0.0)

    if opening is None:
        outcome.balances_readable = False
        outcome.findings.append(
            Finding(
                code="opening_balance_not_numeric",
                message=f"opening balance {opening_balance!r} is not a finite number",
                severity=ERROR,
                locator=Locator(side="opening_balance"),
                value=json_safe(opening_balance),
            )
        )
    if closing is None:
        outcome.balances_readable = False
        outcome.findings.append(
            Finding(
                code="closing_balance_not_numeric",
                message=f"closing balance {closing_balance!r} is not a finite number",
                severity=ERROR,
                locator=Locator(side="closing_balance"),
                value=json_safe(closing_balance),
            )
        )

    try:
        tolerance_value = float(tolerance)
    except (TypeError, ValueError):
        tolerance_value = 0.01
        outcome.findings.append(
            Finding(
                code="tolerance_not_numeric",
                message=f"tolerance {tolerance!r} is not a number; using 0.01",
                severity=WARNING,
                locator=Locator(side="tolerance"),
                value=json_safe(tolerance),
            )
        )
    if tolerance_value < 0:
        outcome.findings.append(
            Finding(
                code="tolerance_negative",
                message=f"tolerance {tolerance_value:g} is negative; using its absolute value",
                severity=WARNING,
                locator=Locator(side="tolerance"),
                value=tolerance_value,
            )
        )
        tolerance_value = abs(tolerance_value)
    outcome.tolerance = tolerance_value

    if isinstance(movements, Mapping):
        return None
    if movements is None:
        movement_list: Sequence[Any] = []
    elif isinstance(movements, (list, tuple)):
        movement_list = movements
    else:
        return None

    for index, raw in enumerate(movement_list):
        movement, problem = parse_movement(raw, index)
        if problem is not None:
            outcome.findings.append(problem)
            outcome.rejected_movements += 1
            continue
        assert movement is not None  # for type checkers; parse_movement sets one or the other
        outcome.movements.append(movement)
        if movement.direction == DIRECTION_IN:
            outcome.movements_in += 1
            outcome.movements_total_in += movement.amount
        else:
            outcome.movements_out += 1
            outcome.movements_total_out += movement.amount

    if outcome.inputs_usable:
        for finding in _diagnose_variance(outcome):
            outcome.findings.append(finding)
    else:
        outcome.findings.append(_unreliable_finding(outcome))

    return outcome


def _unreliable_finding(outcome: ReconcileOutcome) -> Finding:
    """Say plainly that no verdict is available, and which input caused that."""
    reasons: list[str] = []
    if not outcome.balances_readable:
        reasons.append("an opening or closing balance could not be read as a number")
    if outcome.rejected_movements:
        reasons.append(f"{outcome.rejected_movements} movement(s) could not be read")

    return Finding(
        code="arithmetic_unreliable",
        message=(
            f"{'; '.join(reasons).capitalize()}, so every total below is computed from an "
            "incomplete set of inputs and no verdict is given"
        ),
        severity=ERROR,
        locator=Locator(side="movements"),
        expected="every balance and movement readable",
        guidance=(
            "`closes` is null and `arithmetic_is_reliable` is false. Correct the inputs listed in "
            "`findings` and call again — do not treat this as either a pass or a fail."
        ),
    )


def _diagnose_variance(outcome: ReconcileOutcome) -> list[Finding]:
    """Turn a non-closing result into a specific, checkable statement.

    The variance itself is the finding. The extra hints below only fire when the
    arithmetic is *exactly* consistent with a single common cause, so they are
    observations about the numbers rather than guesses about the business.
    """
    absolute = abs(outcome.variance)
    if absolute <= outcome.tolerance:
        return []

    findings = [
        Finding(
            code="does_not_close",
            message=(
                f"opening {outcome.opening:,.2f} + movements {outcome.movements_net:,.2f} "
                f"= {outcome.expected_closing:,.2f}, but the closing balance given is "
                f"{outcome.reported_closing:,.2f} — a variance of {outcome.variance:,.2f} "
                f"(tolerance {outcome.tolerance:g})"
            ),
            severity=ERROR,
            locator=Locator(side="closing_balance"),
            value=outcome.reported_closing,
            expected=outcome.expected_closing,
            guidance=(
                "Do not report the closing balance as verified. The variance is the size of the "
                "problem: locate it before trusting any figure in this period."
            ),
        )
    ]

    for movement in outcome.movements:
        if abs(absolute - movement.amount) <= outcome.tolerance and movement.amount > 0:
            findings.append(
                Finding(
                    code="variance_matches_single_movement",
                    message=(
                        f"the variance {outcome.variance:,.2f} is exactly the size of movement "
                        f"#{movement.index + 1} ({movement.amount:,.2f}, direction "
                        f"'{movement.direction}')"
                    ),
                    severity=WARNING,
                    locator=Locator(movement_index=movement.index, side="movements"),
                    guidance=(
                        "Consistent with that movement being recorded with the wrong direction, "
                        "or counted on both sides — check it first."
                    ),
                )
            )
            break

    half = absolute / 2
    if half > outcome.tolerance:
        for movement in outcome.movements:
            if abs(half - movement.amount) <= outcome.tolerance and movement.amount > 0:
                findings.append(
                    Finding(
                        code="variance_is_double_a_movement",
                        message=(
                            f"the variance {outcome.variance:,.2f} is exactly twice movement "
                            f"#{movement.index + 1} ({movement.amount:,.2f})"
                        ),
                        severity=WARNING,
                        locator=Locator(movement_index=movement.index, side="movements"),
                        guidance=(
                            "Consistent with that movement being counted on both sides of the "
                            "equation rather than once — check it first."
                        ),
                    )
                )
                break

    if abs(outcome.movements_net) <= outcome.tolerance and abs(outcome.variance) > outcome.tolerance:
        findings.append(
            Finding(
                code="movements_net_to_zero",
                message=(
                    "the movements net to zero, so the variance equals the difference between "
                    "the opening and closing balances"
                ),
                severity=INFO,
                locator=Locator(side="movements"),
            )
        )

    return findings


__all__ = [
    "ARITHMETIC",
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "Movement",
    "ReconcileOutcome",
    "parse_movement",
    "reconcile_arithmetic",
]
