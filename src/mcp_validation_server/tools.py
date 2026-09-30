"""The four MCP tools.

Each function below is registered with ``@mcp.tool()``. The docstring is the
tool description the model reads when deciding whether to call it, so it states
what the tool does, what it does *not* do, what each argument means, and how to
read the result.

The shared contract, stated once: **a validation failure is a result, not an
exception.** All four tools return a JSON object with ``ok``, ``verdict``,
``findings`` and ``guidance``. ``ok`` is ``False`` when the data did not pass —
that is a successful call reporting a failing dataset, not an error. The only
thing that raises is a genuinely malformed argument, where there is nothing to
validate until the caller fixes the call.

On that last point, one SDK detail is load-bearing. A tool that raises an
ordinary exception has its message withheld: the model sees only
``Error executing tool <name>``. Only ``ToolError`` (subclass of
``mcp.server.mcpserver.exceptions.MCPServerError``) carries text through to the
client. Malformed arguments are therefore raised as :class:`ToolError` via
:func:`_argument_error`, so the model receives the detail it needs to fix the
call. See ``_argument_error`` for the full reasoning.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .app import mcp
from .contract import ColumnSpecError, RowContract, parse_columns
from .entities import count_distinct_rows, find_duplicate_groups
from .findings import (
    ERROR,
    INFO,
    VERDICT_CLEAN,
    Finding,
    envelope,
    rejection_envelope,
)
from .reconcile import reconcile_arithmetic

_CONTRACT_HELP = (
    "A column spec is an object: {name, required?, type?, min?, max?, range?, pattern?, "
    "allowed?, on_fail?}. type is one of string|integer|number|boolean|date|any; date means "
    "ISO-8601 (YYYY-MM-DD); pattern is a regular expression the whole value must match; "
    "on_fail is quarantine (default) or flag."
)


def _argument_error(message: str) -> ToolError:
    """Build the error a caller sees for a malformed argument.

    Why not ``ValueError``? Because in this SDK only ``ToolError`` reaches the
    model with its text intact. ``mcp.server.mcpserver.tools.base`` wraps any
    other exception as ``UnexpectedToolError``, whose message is the generic
    ``Error executing tool <name>`` and whose cause is logged server-side only.
    A contract error the model cannot read is a contract error the model cannot
    fix, so the deliberate failure path is raised as ``ToolError``.
    """
    return ToolError(message)


def _contract_findings(contract: RowContract) -> list[Finding]:
    findings: list[Finding] = []
    if not contract.fields:
        findings.append(
            Finding(
                code="empty_contract",
                message="the contract has no columns, so no row can be checked",
                severity=ERROR,
            )
        )
    return findings


@mcp.tool()
def validate_rows(
    rows: list[Any],
    columns: list[Any],
    row_key: str | None = None,
) -> dict[str, Any]:
    """Validate rows against a column contract and quarantine what fails.

    Use this when you have tabular data (spreadsheet rows, CSV records, query
    results) and you need to know which records are trustworthy before using
    them. It applies each column's rules — required, type, range, pattern,
    allowed values — and returns two lists: the rows that passed, and an explicit
    quarantine list giving a reason for every row that did not.

    Nothing is ever silently dropped. `accepted` plus `quarantine` always equals
    the number of rows you passed in, and each quarantined entry carries a
    `locator` (row index, row number, and the column that failed) plus a
    `reasons` list and machine-readable `findings`.

    Do NOT use this to repair data. It reports; it does not fix, coerce silently,
    or delete. Coercion only happens where it is unambiguous (`"100.50"` becomes
    the number 100.5), and the cleaned value is returned so you can see it.

    Args:
        rows: The data, as an array of objects. Each object is one row, mapping
            column name to value. Example:
            `[{"invoice": "INV-1", "amount": "100.50", "date": "2026-04-01"}]`.
        columns: The contract, as an array of column specs. Example:
            `[{"name": "invoice", "type": "string", "required": true,
            "pattern": "INV-\\\\d+"},
            {"name": "amount", "type": "number", "min": 0, "max": 1000000},
            {"name": "date", "type": "date", "required": true}]`.
            A required column applies to every row. Unknown spec keys are
            rejected rather than ignored, so a typo cannot become a rule that
            quietly never runs. Every column in `columns` must be listed once.
        row_key: Optional name of a column to quote in each locator, so that
            quarantined rows can be matched back to a record by a human. It does
            not affect validation.

    Returns:
        An object with:
        `ok` (true only if no row was quarantined),
        `verdict` ("clean" | "flagged" | "quarantined"),
        `summary` (rows_in, accepted, quarantined, flagged, pass_rate,
        accounted_for),
        `accepted` (the passing rows, with unambiguous coercions applied),
        `quarantine` (each entry: locator, row, reasons, findings),
        `flagged` (rows kept despite a problem on a flag-mode column),
        `errors_by_column`, `findings_by_code`, `columns_seen`,
        `columns_required_but_absent`, `contract` (the parsed contract, so you can
        confirm which rules ran), `findings`, and `guidance`.

        `columns_required_but_absent` is worth checking before anything else: a
        required column missing from every row usually means the wrong sheet or
        the wrong header row was read, which is a different problem from bad data.

    Raises:
        ToolError: if `columns` is malformed — an unknown type, a bad regular
            expression, a duplicate column name, an unparseable spec. There is
            nothing to validate until the contract parses, so the call is
            rejected with a message naming the offending column and listing the
            keys and types that are accepted. Row content never raises: a row
            that is not an object is quarantined with the reason.

            The contract is the call's definition, so a broken contract is an
            argument error. The rows are the data, so broken rows are a result.
            That is the line this server draws.
    """
    try:
        fields = parse_columns(columns)
    except ColumnSpecError as exc:
        raise _argument_error(f"{exc}  {_CONTRACT_HELP}") from exc

    contract = RowContract(fields)

    if not isinstance(rows, (list, tuple)):
        return rejection_envelope(
            "validate_rows",
            f"rows must be an array of row objects, got {type(rows).__name__}",
            "Pass the data as an array, for example [{\"invoice\": \"INV-1\"}].",
            summary={"rows_in": 0, "accepted": 0, "quarantined": 0, "flagged": 0},
            accepted=[],
            quarantine=[],
            flagged=[],
        )

    if row_key is not None and not isinstance(row_key, str):
        return rejection_envelope(
            "validate_rows",
            f"row_key must be a column name (a string) or omitted, got {type(row_key).__name__}",
            "Omit row_key, or pass the name of a column to quote in each locator.",
            summary={"rows_in": 0, "accepted": 0, "quarantined": 0, "flagged": 0},
            accepted=[],
            quarantine=[],
            flagged=[],
        )

    outcome = contract.validate_rows(rows, row_key=row_key)
    findings = list(outcome.findings)

    if outcome.rows_in == 0:
        findings.append(
            Finding(
                code="no_rows",
                message="no rows were supplied, so nothing was checked",
                severity=INFO,
                guidance="An empty input is not a pass. Confirm the source actually returned data.",
            )
        )

    for column in outcome.missing_columns:
        findings.append(
            Finding(
                code="required_column_absent",
                message=(
                    f"required column {column!r} is not present in any row"
                ),
                severity=ERROR,
                guidance=(
                    "Check the sheet name, the header row, and the column spelling before "
                    "reading anything into the validation result."
                ),
            )
        )

    if outcome.quarantine_count:
        codes = ", ".join(
            f"{code} ({count})"
            for code, count in _tally(outcome).items()
        )
        guidance = (
            f"{outcome.quarantine_count} of {outcome.rows_in} rows were quarantined and are listed "
            f"in `quarantine` with a locator and reasons. Failure codes: {codes}. "
            "Do not use the accepted rows as if they were the whole dataset: report both counts, "
            "and resolve or explicitly exclude the quarantined rows before aggregating."
        )
    elif outcome.flagged_count:
        guidance = (
            f"All {outcome.rows_in} rows were accepted, but {outcome.flagged_count} carry flagged "
            "problems listed in `flagged`. They are included in `accepted`; decide whether the "
            "flagged columns matter for your use before reporting on them."
        )
    elif outcome.rows_in:
        guidance = (
            f"All {outcome.rows_in} rows satisfied the contract. The data is safe to use for "
            "anything the contract covers — note that the contract only covers the columns and "
            "rules you supplied."
        )
    else:
        guidance = "No rows were supplied, so nothing was verified. This is not a pass."

    findings.extend(_contract_findings(contract))

    verdict = outcome.verdict
    if verdict == VERDICT_CLEAN and outcome.rows_in == 0:
        verdict = VERDICT_CLEAN

    payload = outcome.to_dict()
    return envelope(
        "validate_rows",
        ok=outcome.quarantine_count == 0 and outcome.rows_in > 0,
        verdict=verdict,
        findings=findings,
        guidance=guidance,
        contract={
            "row_key": row_key,
            "columns": [field.describe() for field in contract.fields],
        },
        **payload,
    )


def _tally(outcome: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in outcome.quarantine:
        for finding in item.findings:
            if finding.severity == ERROR:
                counts[finding.code] = counts.get(finding.code, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


@mcp.tool()
def reconcile(
    opening_balance: Any,
    movements: Any,
    closing_balance: Any,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    """Check that opening + movements = closing, and show both sides and the variance.

    Use this whenever a period of numbers is supposed to add up: a bank
    reconciliation, a roll-forward, a statement extract, a ledger movement
    summary. It is the check that does not depend on how plausible any single
    figure looked. If the arithmetic does not close, the numbers are wrong even
    if every field looked fine on its own — that is the failure that survives
    field-level validation and reaches a report.

    This tool is deterministic arithmetic. It has no opinion about whether the
    movements are the *right* movements; it only states whether the given
    opening, movements and closing are consistent with each other.

    Args:
        opening_balance: The balance at the start of the period, as a number or a
            numeric string. Example: `1000.00`.
        movements: The changes during the period, as an array. Each element is
            either a signed number (`250.0` increases, `-40.5` decreases) or an
            object `{"amount": 250.0, "direction": "in", "label": "INV-1001",
            "date": "2026-04-03"}`. Use `"direction": "out"` for a decrease, or
            pass the amount as a negative number — never both. Passing an outflow
            as a positive amount is the most common cause of a variance, so state
            the direction explicitly when you can.
        closing_balance: The balance at the end of the period, as a number or a
            numeric string. Example: `1210.00`.
        tolerance: The largest variance to accept as closing, in the same units
            as the balances. Default 0.01, which suits two-decimal currency.
            Set it to 0 for an exact check.

    Returns:
        An object with:
        `ok` (true only if the arithmetic closes),
        `verdict` ("clean" | "does_not_close" | "inputs_rejected"),
        `opening_balance`, `movements_net`, `expected_closing_balance`,
        `reported_closing_balance`, `variance`, `absolute_variance`, `tolerance`,
        `closes` (true/false, or null when the inputs were incomplete),
        `arithmetic_is_reliable`, `movement_counts`, `movement_totals`,
        `movements` (each parsed movement with its signed amount),
        `findings`, and `guidance`.

        `findings` always shows both sides of the equation in words, so the
        variance can be read without recomputing it. When the variance exactly
        equals the size of one movement, an extra finding says so — that is
        arithmetic, not a guess, and it is usually the answer.

    Raises:
        Nothing for bad data. A movement that cannot be read is reported in
        `findings` and excluded, and `closes` becomes null with
        `arithmetic_is_reliable: false`, because a total over an incomplete set of
        movements must never be reported as a pass. Genuinely malformed arguments
        (movements that are not an array at all, or a non-numeric opening or
        closing balance) return the same envelope with `verdict` set to
        "inputs_rejected".
    """
    outcome = reconcile_arithmetic(opening_balance, movements, closing_balance, tolerance)

    if outcome is None:
        return rejection_envelope(
            "reconcile",
            (
                "arguments are malformed: opening_balance and closing_balance must be numbers (or "
                "numeric strings) and movements must be an array"
            ),
            (
                'Re-issue the call as {"opening_balance": 1000.00, "movements": [{"amount": 250.0, '
                '"direction": "in"}], "closing_balance": 1250.00, "tolerance": 0.01}.'
            ),
            opening_balance=opening_balance if isinstance(opening_balance, (int, float)) else None,
            reported_closing_balance=(
                closing_balance if isinstance(closing_balance, (int, float)) else None
            ),
            closes=None,
            arithmetic_is_reliable=False,
        )

    if outcome.closes is True:
        guidance = (
            f"The arithmetic closes: {outcome.opening:,.2f} + {outcome.movements_net:,.2f} = "
            f"{outcome.expected_closing:,.2f}, matching the closing balance within the tolerance of "
            f"{outcome.tolerance:g}. This confirms internal consistency, not that the movements are "
            "complete or correct."
        )
    elif outcome.closes is False:
        guidance = (
            f"The arithmetic does NOT close. Expected {outcome.expected_closing:,.2f}, got "
            f"{outcome.reported_closing:,.2f} — a variance of {outcome.variance:,.2f} against a "
            f"tolerance of {outcome.tolerance:g}. Do not report either balance as verified. Find "
            "the variance's cause before using any figure from this period; the findings list any "
            "single movement whose size matches the variance."
        )
    else:
        unreliable = next(
            (f for f in outcome.findings if f.code == "arithmetic_unreliable"), None
        )
        guidance = (
            f"{unreliable.message if unreliable else 'The inputs were incomplete.'} `closes` is "
            "null, which is neither a pass nor a fail. Correct the inputs listed in `findings` and "
            "call again before drawing any conclusion about this period."
        )

    payload = outcome.to_dict()
    return envelope(
        "reconcile",
        ok=outcome.closes is True,
        verdict=outcome.verdict,
        findings=outcome.findings,
        guidance=guidance,
        **payload,
    )


@mcp.tool()
def count_distinct(
    rows: list[Any],
    key: str,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Count distinct entities on a stable key, and report the gap to a row count.

    Use this instead of counting rows whenever the unit you are reporting on is
    an entity — customers, patients, households, sites, invoices — rather than a
    line in a table. One customer can appear on fifty rows; "50" is a row count,
    not a customer count, and the difference is the error that survives review
    because the number looks plausible.

    This tool returns both numbers and the difference between them, broken into
    the two causes: keys repeated across rows, and rows that carry no usable key.
    Rows with a blank key are excluded from the count and listed individually —
    never counted as one entity, never dropped.

    Args:
        rows: The data, as an array of objects. Example:
            `[{"customer_id": "C-1", "site": "A"}, {"customer_id": "C-1",
            "site": "B"}]`.
        key: The column holding the stable identifier. Choose a real identifier
            (an account number, a registration key), not a name — names collide,
            and two different people sharing a name are not one entity.
        case_sensitive: If false (the default), keys are compared after trimming
            surrounding whitespace and folding case, so `"C-1"` and `" c-1 "`
            count as one entity. Set true to treat any difference as a different
            entity. Nothing else is normalised: no punctuation is stripped and no
            fuzzy matching is applied, because those change which records are
            considered the same entity, and that is a decision for a person.

    Returns:
        An object with:
        `ok` (true when the row count and the distinct count agree),
        `verdict` ("clean" | "review_required"),
        `row_count`, `distinct_count`, `difference`,
        `difference_breakdown` ({from_repeated_keys, from_rows_with_blank_key}),
        `rows_with_a_key`, `rows_with_blank_key`, `blank_key_rows`,
        `repeated_keys` (each with its key and the row indexes it appears on),
        `findings`, and `guidance`.

        Report `distinct_count` as the entity count. Report `row_count` as the
        row count. Never present one as the other.

    Raises:
        ToolError: if `key` is not a non-empty string. Rows that are not
            objects, or whose key is missing or blank, never raise — they are
            held back, counted in `rows_with_blank_key`, and listed in
            `blank_key_rows`.
    """
    if not isinstance(key, str) or not key.strip():
        raise _argument_error(
            "key must be the name of the column holding the stable identifier, "
            f"got {key!r}. Example: count_distinct(rows, key=\"customer_id\")"
        )

    if not isinstance(rows, (list, tuple)):
        return rejection_envelope(
            "count_distinct",
            f"rows must be an array of row objects, got {type(rows).__name__}",
            'Pass the data as an array, for example [{"customer_id": "C-1"}].',
            row_count=0,
            distinct_count=0,
            difference=0,
        )

    outcome = count_distinct_rows(
        rows, key, case_sensitive=bool(case_sensitive), strip_whitespace=True
    )
    findings = list(outcome.findings)

    if outcome.row_count == 0:
        guidance = "No rows were supplied, so there is nothing to count. This is not a count of zero yet."
        findings.append(
            Finding(
                code="no_rows",
                message="no rows were supplied, so nothing was counted",
                severity=INFO,
                guidance="Confirm the source actually returned data before reporting a count.",
            )
        )
    elif outcome.difference == 0:
        guidance = (
            f"{outcome.distinct_count} distinct entities across {outcome.row_count} rows — the two "
            "numbers agree, which is what an entity table should look like."
        )
    else:
        guidance = (
            f"{outcome.row_count} rows collapse to {outcome.distinct_count} distinct entities on "
            f"{key!r}: a difference of {outcome.difference} "
            f"({outcome.difference_from_repeated_keys} from repeated keys, "
            f"{outcome.difference_from_blank_keys} from rows with a blank key). Use "
            f"{outcome.distinct_count} as the entity count and say which number you are reporting. "
            "If the key is repeated on many rows, check whether this is a transaction table (where "
            "that is expected) or an entity table (where it is not)."
        )

    payload = outcome.to_dict()
    return envelope(
        "count_distinct",
        ok=outcome.difference == 0 and outcome.row_count > 0,
        verdict=outcome.verdict,
        findings=findings,
        guidance=guidance,
        **payload,
    )


@mcp.tool()
def duplicate_report(
    rows: list[Any],
    key: str,
    match_fields: list[str] | None = None,
    similarity_threshold: float | None = None,
    case_sensitive: bool = False,
    max_groups: int = 50,
) -> dict[str, Any]:
    """Find suspected duplicate groups and return them for human review.

    Use this when the same real entity may be recorded more than once — a
    re-registration, a rename, a typo in a reference field — and you need to know
    how much of the data that affects before trusting any count.

    This tool NEVER merges, deletes, or picks a winner. Every group it returns is
    a suspicion with `auto_merged: false` and `decision_required: true`, plus a
    `review_question` phrased for a person. A silent merge corrupts counts
    invisibly and is very hard to unpick later, so the output is a review queue,
    not a result.

    Three kinds of suspicion are reported, each labelled in `kind`:

    - `same_key`: the same normalised key appears on more than one row. Either
      genuine duplicates or legitimate repeated transactions — the tool cannot
      tell which, so it reports rather than decides.
    - `same_signature`: different keys whose values across `match_fields` are
      identical. This is the re-registration case.
    - `near_match`: different keys whose values across `match_fields` are similar
      rather than identical. Only produced when you supply
      `similarity_threshold`, because "close enough" is a policy choice.

    Args:
        rows: The data, as an array of objects.
        key: The column holding the identifier. Needed to tell "one entity,
            several rows" from "several entities".
        match_fields: Columns whose combination suggests two different keys are
            the same entity, for example `["name", "postcode"]` or
            `["given_name", "family_name", "date_of_birth"]`. Comparing on
            several fields is far safer than one: a single name column collides
            constantly. Omit it to check only exact key repeats.
        similarity_threshold: A number above 0 and at most 1. When supplied,
            `match_fields` values are also compared with string similarity
            (`difflib.SequenceMatcher`) and groups scoring at or above this value
            are reported as `near_match`. 0.9 is a reasonable starting point;
            lower values find more and mean less. Omit it and only exact matches
            are reported.
        case_sensitive: If false (the default), values are compared after trimming
            whitespace and folding case.
        max_groups: Cap on the number of groups returned, default 50. When the
            cap bites, `groups_omitted` says how many were left out — the count is
            never silently reduced.

    Returns:
        An object with:
        `ok` (true only when no suspicion was found),
        `verdict` ("clean" | "review_required"),
        `row_count`, `distinct_keys`, `duplicate_group_count`,
        `rows_in_duplicate_groups`, `auto_merged` (always false),
        `nothing_was_merged_or_deleted` (always true),
        `blank_key_rows`, `groups`, `groups_omitted`, `findings`, and `guidance`.

        Each group has `group_id`, `kind`, `reason`, `size`, `keys`,
        `recommended_action` ("human_review"), `decision_required`, a
        `review_question`, and `members` (each with a locator and the full row).

    Raises:
        ToolError: if `key` is not a non-empty string, if `match_fields` is not
            an array of column names, or if `similarity_threshold` is not above
            0 and at most 1. All three are malformed arguments rather than data
            problems. Row content never raises.
    """
    if not isinstance(key, str) or not key.strip():
        raise _argument_error(
            "key must be the name of the column holding the identifier, "
            f"got {key!r}. Example: duplicate_report(rows, key=\"customer_id\")"
        )

    if match_fields is not None:
        if not isinstance(match_fields, (list, tuple)) or not all(
            isinstance(name, str) and name.strip() for name in match_fields
        ):
            raise _argument_error(
                "match_fields must be an array of column names, for example "
                f'["name", "postcode"]. Got {match_fields!r}'
            )
        match_fields = [name.strip() for name in match_fields]

    if not isinstance(rows, (list, tuple)):
        return rejection_envelope(
            "duplicate_report",
            f"rows must be an array of row objects, got {type(rows).__name__}",
            'Pass the data as an array, for example [{"customer_id": "C-1"}].',
            row_count=0,
            distinct_keys=0,
            duplicate_group_count=0,
            auto_merged=False,
            groups=[],
        )

    try:
        threshold = None if similarity_threshold is None else float(similarity_threshold)
    except (TypeError, ValueError):
        raise _argument_error(
            f"similarity_threshold must be a number above 0 and at most 1, got {similarity_threshold!r}"
        ) from None
    if threshold is not None and not 0.0 < threshold <= 1.0:
        raise _argument_error(
            f"similarity_threshold must be above 0 and at most 1, got {threshold:g}. "
            "0.9 is a reasonable starting point."
        )

    try:
        group_cap = int(max_groups)
    except (TypeError, ValueError):
        group_cap = 50
    if group_cap < 0:
        group_cap = 0

    outcome = find_duplicate_groups(
        rows,
        key,
        match_fields=match_fields,
        similarity_threshold=threshold,
        case_sensitive=bool(case_sensitive),
        strip_whitespace=True,
        max_groups=group_cap,
    )
    findings = list(outcome.findings)

    if outcome.row_count == 0:
        findings.append(
            Finding(
                code="no_rows",
                message="no rows were supplied, so no duplicate check was possible",
                severity=INFO,
            )
        )
        guidance = "No rows were supplied, so nothing was checked. This is not evidence of clean data."
    elif outcome.group_count == 0:
        guidance = (
            f"No duplicate suspicions found across {outcome.row_count} rows on {key!r}"
            + (f" and the signature {match_fields}" if match_fields else "")
            + ". Note what that does and does not mean: it does not mean no duplicate exists, only "
            "that none is visible from the fields and key you supplied."
        )
    else:
        kinds: dict[str, int] = {}
        for group in outcome.groups:
            kinds[group.kind] = kinds.get(group.kind, 0) + 1
        rendered = ", ".join(f"{kind} ({count})" for kind, count in sorted(kinds.items()))
        guidance = (
            f"{outcome.group_count} suspected duplicate group(s) across {outcome.row_count} rows: "
            f"{rendered}. Nothing has been merged or deleted. Present these groups to a human with "
            "the `review_question` for each one; do not resolve them yourself and do not report a "
            "deduplicated count as if the merge had happened."
        )

    if outcome.groups_omitted:
        findings.append(
            Finding(
                code="groups_truncated",
                message=(
                    f"{outcome.groups_omitted} further duplicate group(s) were not returned because "
                    f"max_groups is {group_cap}"
                ),
                severity=INFO,
                guidance=(
                    "The returned groups are a subset. Raise max_groups, or narrow the input, before "
                    "concluding anything about how many duplicates exist."
                ),
            )
        )

    payload = outcome.to_dict()
    return envelope(
        "duplicate_report",
        ok=outcome.group_count == 0 and outcome.row_count > 0,
        verdict=outcome.verdict,
        findings=findings,
        guidance=guidance,
        **payload,
    )


__all__ = ["count_distinct", "duplicate_report", "reconcile", "validate_rows"]
