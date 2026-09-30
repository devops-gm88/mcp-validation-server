"""mcp-validation-server — deterministic data-validation tools over MCP.

A Model Context Protocol server that exposes four checks a model cannot argue
with: contract validation with quarantine, arithmetic reconciliation, distinct
entity counting, and duplicate reporting for human review.

The design rule: **a validation failure is a result, not an exception.** Every
tool returns structured findings with stable codes and row/column locators, so a
calling model can report what failed and recover. Only malformed tool arguments
raise.

The tool functions are importable and callable directly, which is how the unit
tests exercise them without an MCP client:

    >>> from mcp_validation_server import validate_rows
    >>> result = validate_rows([{"amount": "-5"}], [{"name": "amount", "type": "number", "min": 0}])
    >>> result["ok"], result["summary"]["quarantined"]
    (False, 1)
"""

from __future__ import annotations

from .app import SERVER_NAME, SERVER_VERSION, mcp
from .contract import (
    ColumnSpecError,
    Field,
    Max,
    Min,
    NotBlank,
    OneOf,
    Pattern,
    Range,
    RowContract,
    Rule,
    ValidationOutcome,
    parse_columns,
)
from .entities import (
    DistinctOutcome,
    DuplicateGroup,
    DuplicateOutcome,
    count_distinct_rows,
    find_duplicate_groups,
    normalise_key,
)
from .findings import Finding, Locator, envelope
from .reconcile import Movement, ReconcileOutcome, reconcile_arithmetic
from .tools import count_distinct, duplicate_report, reconcile, validate_rows

__version__ = SERVER_VERSION

__all__ = [
    "ColumnSpecError",
    "DistinctOutcome",
    "DuplicateGroup",
    "DuplicateOutcome",
    "Field",
    "Finding",
    "Locator",
    "Max",
    "Min",
    "Movement",
    "NotBlank",
    "OneOf",
    "Pattern",
    "Range",
    "ReconcileOutcome",
    "RowContract",
    "Rule",
    "SERVER_NAME",
    "SERVER_VERSION",
    "ValidationOutcome",
    "__version__",
    "count_distinct",
    "count_distinct_rows",
    "duplicate_report",
    "envelope",
    "find_duplicate_groups",
    "mcp",
    "normalise_key",
    "parse_columns",
    "reconcile",
    "reconcile_arithmetic",
    "validate_rows",
]
