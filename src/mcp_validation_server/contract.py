"""Column contracts: a row either satisfies its contract or it is quarantined.

The rule this module enforces (inherited from ``sheets-data-validation-kit``):
**nothing is ever silently dropped.** Every row handed in is either in the
accepted list or in the quarantine list with the reasons attached, so the
question "where did this record go?" always has an answer.

Contracts are supplied as JSON by the calling model, so :func:`parse_columns`
turns a list of column specs into :class:`Field` objects and raises
:class:`ColumnSpecError` on a spec that cannot be parsed. That is the *only*
failure mode in this module that raises: it is a malformed tool argument, not
data. A row is never allowed to raise.
"""

from __future__ import annotations

import datetime as _datetime
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from .findings import (
    ERROR,
    INFO,
    VERDICT_CLEAN,
    VERDICT_FLAGGED,
    VERDICT_QUARANTINED,
    WARNING,
    Finding,
    Locator,
    count_by_code,
    count_by_column,
    json_safe,
)

# Column `type` names accepted by a contract.
TYPE_STRING = "string"
TYPE_INTEGER = "integer"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_DATE = "date"
TYPE_ANY = "any"

TYPES = (TYPE_STRING, TYPE_INTEGER, TYPE_NUMBER, TYPE_BOOLEAN, TYPE_DATE, TYPE_ANY)

# Per-column failure handling.
ON_FAIL_QUARANTINE = "quarantine"
ON_FAIL_FLAG = "flag"
ON_FAIL_MODES = (ON_FAIL_QUARANTINE, ON_FAIL_FLAG)

_SPEC_KEYS = frozenset(
    {
        "name",
        "required",
        "type",
        "min",
        "max",
        "range",
        "pattern",
        "allowed",
        "not_blank",
        "on_fail",
        "description",
    }
)

_TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n", "0"})


class ColumnSpecError(ValueError):
    """Raised for a malformed column contract.

    This is a malformed *tool argument*, not a validation failure: there is no
    data to validate until the contract parses, so the caller has to fix the
    call. The message always names the offending column and lists what was
    accepted.
    """


@dataclass(frozen=True)
class Problem:
    """A failed rule check: a stable code plus a human-readable message."""

    code: str
    message: str


class Rule:
    """Base class for a single check applied to one column value.

    ``checks_blank`` decides whether the rule still runs when the value is
    missing or whitespace-only. It is ``False`` for every rule that needs a value
    to compare against — "is the number below zero?" has no answer for a blank,
    and reporting one would be inventing a failure. It is ``True`` only for rules
    whose subject *is* blankness, such as :class:`NotBlank`.
    """

    code = "rule"
    checks_blank = False

    def check(self, value: Any) -> Problem | None:
        """Return ``None`` if the value passes, else a :class:`Problem`."""
        raise NotImplementedError


@dataclass
class Min(Rule):
    """Numeric lower bound (inclusive)."""

    bound: float
    code = "below_minimum"

    def check(self, value: Any) -> Problem | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return Problem("not_numeric", f"not numeric, so cannot compare to minimum {self.bound}")
        if number < self.bound:
            return Problem(self.code, f"{number:g} is below the minimum of {self.bound:g}")
        return None


@dataclass
class Max(Rule):
    """Numeric upper bound (inclusive)."""

    bound: float
    code = "above_maximum"

    def check(self, value: Any) -> Problem | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return Problem("not_numeric", f"not numeric, so cannot compare to maximum {self.bound}")
        if number > self.bound:
            return Problem(self.code, f"{number:g} is above the maximum of {self.bound:g}")
        return None


@dataclass
class Range(Rule):
    """Inclusive numeric range ``low..high``."""

    low: float
    high: float
    code = "out_of_range"

    def check(self, value: Any) -> Problem | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return Problem("not_numeric", f"not numeric, so cannot range-check {self.low:g}..{self.high:g}")
        if number < self.low or number > self.high:
            return Problem(self.code, f"{number:g} is outside the range {self.low:g}..{self.high:g}")
        return None


@dataclass
class Pattern(Rule):
    """Regular expression that the **whole** value must match.

    ``re.fullmatch`` semantics, so ``^\\d{4}$`` and ``\\d{4}`` behave the same
    way. A pattern that matches part of a value is not a passing value.
    """

    regex: re.Pattern[str]
    source: str
    code = "pattern_mismatch"

    def check(self, value: Any) -> Problem | None:
        text = "" if value is None else str(value)
        if self.regex.fullmatch(text) is None:
            return Problem(self.code, f"{text!r} does not match the required pattern {self.source}")
        return None


@dataclass
class OneOf(Rule):
    """Value must be one of an explicit set of allowed values."""

    allowed: Sequence[Any]
    code = "not_allowed"

    def check(self, value: Any) -> Problem | None:
        if value in self.allowed:
            return None
        rendered = ", ".join(repr(item) for item in self.allowed)
        return Problem(self.code, f"{value!r} is not one of the allowed values: {rendered}")


class NotBlank(Rule):
    """Value must not be missing, empty, or whitespace-only."""

    code = "blank"
    checks_blank = True

    def check(self, value: Any) -> Problem | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return Problem(self.code, "is blank")
        return None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce_type(value: Any, type_name: str) -> tuple[Any, Problem | None]:
    """Coerce ``value`` to ``type_name``.

    Returns ``(coerced_value, problem)``. Coercion is deliberately generous
    about the *representation* and strict about the *content*: ``"100.50"`` is a
    number, ``"one hundred"`` is not. Values are only coerced when the result is
    unambiguous, so this never invents data.
    """
    if type_name == TYPE_ANY:
        return value, None

    if type_name == TYPE_STRING:
        return (value if isinstance(value, str) else str(value)), None

    if type_name == TYPE_NUMBER:
        if isinstance(value, bool):
            return value, Problem("wrong_type", "is a boolean, not a number")
        if isinstance(value, (int, float)):
            return float(value), None
        if isinstance(value, str):
            text = value.strip()
            try:
                return float(text), None
            except ValueError:
                return value, Problem("not_a_number", f"{value!r} is not a number")
        return value, Problem("wrong_type", f"is a {type(value).__name__}, not a number")

    if type_name == TYPE_INTEGER:
        if isinstance(value, bool):
            return value, Problem("wrong_type", "is a boolean, not an integer")
        if isinstance(value, int):
            return value, None
        if isinstance(value, float):
            if value.is_integer():
                return int(value), None
            return value, Problem("not_an_integer", f"{value:g} is not a whole number")
        if isinstance(value, str):
            text = value.strip()
            try:
                return int(text), None
            except ValueError:
                pass
            try:
                number = float(text)
            except ValueError:
                return value, Problem("not_an_integer", f"{value!r} is not an integer")
            if number.is_integer():
                return int(number), None
            return value, Problem("not_an_integer", f"{value!r} is not a whole number")
        return value, Problem("wrong_type", f"is a {type(value).__name__}, not an integer")

    if type_name == TYPE_BOOLEAN:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            text = value.strip().casefold()
            if text in _TRUE_WORDS:
                return True, None
            if text in _FALSE_WORDS:
                return False, None
        return value, Problem("not_a_boolean", f"{value!r} is not a boolean (use true/false)")

    if type_name == TYPE_DATE:
        if isinstance(value, str):
            text = value.strip()
            try:
                return _datetime.date.fromisoformat(text).isoformat(), None
            except ValueError:
                pass
            try:
                return _datetime.datetime.fromisoformat(text).date().isoformat(), None
            except ValueError:
                pass
        if isinstance(value, _datetime.date):
            return value.isoformat(), None
        return value, Problem("not_a_date", f"{value!r} is not an ISO-8601 date (expected YYYY-MM-DD)")

    raise ColumnSpecError(f"unknown column type {type_name!r}; expected one of {', '.join(TYPES)}")


@dataclass
class Field:
    """One column in a row contract.

    ``on_fail`` decides what a failure on this column means:

    ``quarantine`` (default)
        Hold the whole row back and report it.
    ``flag``
        Keep the row, record the problem. For columns the pipeline does not
        depend on, where refusing the row would cost more than the defect.
    """

    name: str
    required: bool = False
    type: str | None = None
    rules: list[Rule] = dataclass_field(default_factory=list)
    on_fail: str = ON_FAIL_QUARANTINE
    description: str = ""

    @property
    def quarantines(self) -> bool:
        return self.on_fail == ON_FAIL_QUARANTINE

    def describe(self) -> dict[str, Any]:
        """The contract as the caller supplied it, after parsing.

        Echoed back in tool results so a model can confirm which rules actually
        ran. A rule that silently did not apply is worse than a rule that failed.
        """
        described: dict[str, Any] = {
            "name": self.name,
            "required": self.required,
            "type": self.type or TYPE_ANY,
            "on_fail": self.on_fail,
        }
        if self.description:
            described["description"] = self.description
        checks: list[dict[str, Any]] = []
        for rule in self.rules:
            if isinstance(rule, Min):
                checks.append({"rule": "min", "bound": rule.bound})
            elif isinstance(rule, Max):
                checks.append({"rule": "max", "bound": rule.bound})
            elif isinstance(rule, Range):
                checks.append({"rule": "range", "low": rule.low, "high": rule.high})
            elif isinstance(rule, Pattern):
                checks.append({"rule": "pattern", "regex": rule.source, "matching": "fullmatch"})
            elif isinstance(rule, OneOf):
                checks.append({"rule": "one_of", "allowed": list(rule.allowed)})
            elif isinstance(rule, NotBlank):
                checks.append({"rule": "not_blank"})
        described["checks"] = checks
        return described


def parse_column_spec(spec: Mapping[str, Any], index: int) -> Field:
    """Build one :class:`Field` from a JSON column spec.

    Unknown keys are rejected rather than ignored. A silently-ignored key is how
    a typo like ``"regexp"`` becomes a rule that never ran while the report
    still says "clean".
    """
    if not isinstance(spec, Mapping):
        raise ColumnSpecError(
            f"column #{index + 1} must be an object like "
            '{"name": "amount", "type": "number", "min": 0}, got '
            f"{type(spec).__name__}"
        )

    unknown = sorted(set(spec) - _SPEC_KEYS)
    if unknown:
        raise ColumnSpecError(
            f"column #{index + 1}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"Supported keys: {', '.join(sorted(_SPEC_KEYS))}"
        )

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ColumnSpecError(f'column #{index + 1} needs a non-empty "name" string')
    name = name.strip()

    type_name = spec.get("type", TYPE_ANY)
    if type_name not in TYPES:
        raise ColumnSpecError(
            f'column "{name}": unknown type {type_name!r}. Supported types: {", ".join(TYPES)}'
        )

    on_fail = spec.get("on_fail", ON_FAIL_QUARANTINE)
    if on_fail not in ON_FAIL_MODES:
        raise ColumnSpecError(
            f'column "{name}": on_fail must be one of {", ".join(ON_FAIL_MODES)}, got {on_fail!r}'
        )

    required = spec.get("required", False)
    if not isinstance(required, bool):
        raise ColumnSpecError(f'column "{name}": "required" must be true or false')

    not_blank = spec.get("not_blank", False)
    if not isinstance(not_blank, bool):
        raise ColumnSpecError(f'column "{name}": "not_blank" must be true or false')

    rules: list[Rule] = []

    for key, rule_type in (("min", Min), ("max", Max)):
        if key in spec:
            bound = spec[key]
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                raise ColumnSpecError(f'column "{name}": "{key}" must be a number, got {bound!r}')
            rules.append(rule_type(float(bound)))

    if "range" in spec:
        bounds = spec["range"]
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 2
            or any(isinstance(b, bool) or not isinstance(b, (int, float)) for b in bounds)
        ):
            raise ColumnSpecError(
                f'column "{name}": "range" must be a two-number array like [0, 100], got {bounds!r}'
            )
        low, high = float(bounds[0]), float(bounds[1])
        if low > high:
            raise ColumnSpecError(
                f'column "{name}": "range" low bound {low:g} is above the high bound {high:g}'
            )
        rules.append(Range(low, high))

    if "pattern" in spec:
        source = spec["pattern"]
        if not isinstance(source, str):
            raise ColumnSpecError(f'column "{name}": "pattern" must be a string, got {source!r}')
        try:
            compiled = re.compile(source)
        except re.error as exc:
            raise ColumnSpecError(
                f'column "{name}": "pattern" is not a valid regular expression ({exc}): {source!r}'
            ) from exc
        rules.append(Pattern(regex=compiled, source=source))

    if "allowed" in spec:
        allowed = spec["allowed"]
        if not isinstance(allowed, (list, tuple)) or not allowed:
            raise ColumnSpecError(
                f'column "{name}": "allowed" must be a non-empty array, got {allowed!r}'
            )
        rules.append(OneOf(list(allowed)))

    if not_blank:
        rules.append(NotBlank())

    return Field(
        name=name,
        required=required,
        type=None if type_name == TYPE_ANY else type_name,
        rules=rules,
        on_fail=on_fail,
        description=str(spec.get("description", "")),
    )


def parse_columns(specs: Sequence[Any]) -> list[Field]:
    """Parse a list of column specs into fields.

    Raises :class:`ColumnSpecError` on anything malformed. This is the
    malformed-tool-argument path: a contract that does not parse cannot validate
    anything, so guessing at it would be worse than refusing it.
    """
    if not isinstance(specs, (list, tuple)):
        raise ColumnSpecError(
            'the "columns" argument must be an array of column objects, got '
            f"{type(specs).__name__}"
        )
    if not specs:
        raise ColumnSpecError(
            'the "columns" argument is empty — a contract with no columns would accept every row '
            "unchecked. Supply at least one column, or do not call this tool."
        )

    fields = [parse_column_spec(spec, index) for index, spec in enumerate(specs)]

    seen: dict[str, int] = {}
    for field in fields:
        seen[field.name] = seen.get(field.name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise ColumnSpecError(
            f"duplicate column name(s) in the contract: {', '.join(duplicates)}. "
            "Each column may appear once; combine its rules into a single entry."
        )
    return fields


@dataclass
class QuarantinedRow:
    """A row that was held back, with the reasons it was held back."""

    locator: Locator
    row: dict[str, Any]
    reasons: list[str]
    findings: list[Finding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_dict(),
            "row": json_safe(self.row),
            "reasons": list(self.reasons),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass
class ValidationOutcome:
    """The full result of validating a batch of rows.

    ``accepted + quarantined`` is always every row that came in. That invariant
    is the point of the module.
    """

    accepted: list[dict[str, Any]] = dataclass_field(default_factory=list)
    quarantine: list[QuarantinedRow] = dataclass_field(default_factory=list)
    flagged: list[QuarantinedRow] = dataclass_field(default_factory=list)
    findings: list[Finding] = dataclass_field(default_factory=list)
    rows_in: int = 0
    undeclared_columns: list[str] = dataclass_field(default_factory=list)
    missing_columns: list[str] = dataclass_field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return len(self.accepted)

    @property
    def quarantine_count(self) -> int:
        return len(self.quarantine)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged)

    @property
    def pass_rate(self) -> float:
        return 0.0 if self.rows_in == 0 else self.pass_count / self.rows_in

    @property
    def verdict(self) -> str:
        if self.quarantine_count:
            return VERDICT_QUARANTINED
        if self.flagged_count:
            return VERDICT_FLAGGED
        return VERDICT_CLEAN

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "rows_in": self.rows_in,
                "accepted": self.pass_count,
                "quarantined": self.quarantine_count,
                "flagged": self.flagged_count,
                "pass_rate": round(self.pass_rate, 4),
                "accounted_for": self.pass_count + self.quarantine_count,
            },
            "accepted": [json_safe(row) for row in self.accepted],
            "quarantine": [item.to_dict() for item in self.quarantine],
            "flagged": [item.to_dict() for item in self.flagged],
            "errors_by_column": count_by_column(self.findings),
            "findings_by_code": count_by_code(self.findings),
            "columns_seen": self.undeclared_columns,
            "columns_required_but_absent": self.missing_columns,
        }


class RowContract:
    """A set of column rules applied to every row."""

    def __init__(self, fields: Sequence[Field]):
        self.fields = list(fields)

    @property
    def column_names(self) -> list[str]:
        return [field.name for field in self.fields]

    def validate_row(
        self, row: Mapping[str, Any], locator: Locator
    ) -> tuple[dict[str, Any], list[Finding], str]:
        """Return ``(cleaned_row, findings, verdict)``.

        ``verdict`` is one of ``pass`` / ``flag`` / ``quarantine``. Findings are
        returned even for accepted rows so that flagged problems stay visible.
        """
        cleaned: dict[str, Any] = dict(row)
        findings: list[Finding] = []
        verdict = "pass"

        for spec in self.fields:
            column_locator = Locator(
                row_index=locator.row_index,
                row_key=locator.row_key,
                column=spec.name,
            )
            value = row.get(spec.name)

            if _is_blank(value):
                if spec.required:
                    # A required blank is reported once, as required_blank, rather
                    # than once per rule that cannot evaluate a missing value.
                    findings.append(
                        Finding(
                            code="required_blank",
                            message="is required but blank",
                            severity=ERROR if spec.quarantines else WARNING,
                            locator=column_locator,
                            expected="a non-blank value",
                        )
                    )
                    verdict = self._worse(verdict, spec)
                    continue

                # Not required, but a rule may still have something to say about a
                # blank — `not_blank` exists precisely to say it.
                for rule in spec.rules:
                    if not rule.checks_blank:
                        continue
                    problem = rule.check(value)
                    if problem is not None:
                        findings.append(
                            Finding(
                                code=problem.code,
                                message=problem.message,
                                severity=ERROR if spec.quarantines else WARNING,
                                locator=column_locator,
                                value=json_safe(value),
                            )
                        )
                        verdict = self._worse(verdict, spec)
                continue

            if spec.type is not None:
                coerced, problem = _coerce_type(value, spec.type)
                if problem is not None:
                    findings.append(
                        Finding(
                            code=problem.code,
                            message=problem.message,
                            severity=ERROR if spec.quarantines else WARNING,
                            locator=column_locator,
                            value=json_safe(value),
                            expected=f"a value of type {spec.type}",
                        )
                    )
                    verdict = self._worse(verdict, spec)
                    continue
                value = coerced
                cleaned[spec.name] = coerced

            for rule in spec.rules:
                problem = rule.check(value)
                if problem is not None:
                    findings.append(
                        Finding(
                            code=problem.code,
                            message=problem.message,
                            severity=ERROR if spec.quarantines else WARNING,
                            locator=column_locator,
                            value=json_safe(value),
                        )
                    )
                    verdict = self._worse(verdict, spec)

        return cleaned, findings, verdict

    @staticmethod
    def _worse(current: str, spec: Field) -> str:
        want = ON_FAIL_QUARANTINE if spec.quarantines else ON_FAIL_FLAG
        if want == ON_FAIL_QUARANTINE:
            return "quarantine"
        return current if current != "pass" else "flag"

    def validate_rows(self, rows: Iterable[Any], row_key: str | None = None) -> ValidationOutcome:
        """Validate every row. No input can raise; bad rows are quarantined."""
        outcome = ValidationOutcome()
        known_columns = set(self.column_names)
        seen_columns: list[str] = []
        present_columns: set[str] = set()

        for index, row in enumerate(rows):
            outcome.rows_in += 1

            if not isinstance(row, Mapping):
                locator = Locator(row_index=index)
                outcome.quarantine.append(
                    QuarantinedRow(
                        locator=locator,
                        row={"_raw": json_safe(row)},
                        reasons=[
                            f"row is a {type(row).__name__}, not an object; "
                            "every row must be a JSON object of column: value pairs"
                        ],
                        findings=[
                            Finding(
                                code="not_an_object",
                                message=(
                                    f"row is a {type(row).__name__}, not an object with named columns"
                                ),
                                severity=ERROR,
                                locator=locator,
                                value=json_safe(row),
                            )
                        ],
                    )
                )
                continue

            for column in row:
                present_columns.add(column)
                if column not in known_columns and column not in seen_columns:
                    seen_columns.append(column)

            key_value = row.get(row_key) if row_key else None
            if isinstance(key_value, str) and not key_value.strip():
                key_value = None
            locator = Locator(
                row_index=index,
                row_key=None if key_value is None else str(key_value),
            )
            cleaned, findings, verdict = self.validate_row(row, locator)
            outcome.findings.extend(findings)

            if verdict == "quarantine":
                outcome.quarantine.append(
                    QuarantinedRow(
                        locator=locator,
                        row=cleaned,
                        reasons=[finding.as_reason() for finding in findings],
                        findings=findings,
                    )
                )
            elif verdict == "flag":
                outcome.flagged.append(
                    QuarantinedRow(
                        locator=locator,
                        row=cleaned,
                        reasons=[finding.as_reason() for finding in findings],
                        findings=findings,
                    )
                )
                outcome.accepted.append(cleaned)
            else:
                outcome.accepted.append(cleaned)

        outcome.undeclared_columns = seen_columns
        outcome.missing_columns = self._absent_required_columns(present_columns)
        return outcome

    def _absent_required_columns(self, present_columns: Iterable[str]) -> list[str]:
        """Required columns that never appeared in any row at all.

        Separate from "required but blank". A column that is simply absent from
        the payload usually means the wrong sheet or the wrong header row was
        read, which is worth saying once rather than once per row.
        """
        present = set(present_columns)
        return sorted(
            field.name
            for field in self.fields
            if field.required and field.name not in present
        )


def validate_rows_with_contract(
    rows: Any, columns: Any, row_key: str | None = None
) -> ValidationOutcome:
    """Parse a JSON contract, then validate rows.

    Raises :class:`ColumnSpecError` if ``columns`` is malformed. Returns an
    outcome for any ``rows`` payload at all, including one that is not a list.
    """
    fields = parse_columns(columns)
    contract = RowContract(fields)
    if not isinstance(rows, (list, tuple)):
        raise TypeError(
            f"rows must be an array of row objects, got {type(rows).__name__}"
        )
    return contract.validate_rows(rows, row_key=row_key)


__all__ = [
    "ColumnSpecError",
    "Field",
    "Max",
    "Min",
    "NotBlank",
    "OneOf",
    "Pattern",
    "Problem",
    "QuarantinedRow",
    "Range",
    "RowContract",
    "Rule",
    "TYPES",
    "TYPE_ANY",
    "TYPE_BOOLEAN",
    "TYPE_DATE",
    "TYPE_INTEGER",
    "TYPE_NUMBER",
    "TYPE_STRING",
    "ValidationOutcome",
    "parse_column_spec",
    "parse_columns",
    "validate_rows_with_contract",
    "INFO",
]
