# mcp-validation-server

[![CI](https://github.com/devops-gm88/mcp-validation-server/actions/workflows/ci.yml/badge.svg)](https://github.com/devops-gm88/mcp-validation-server/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-validation-server.svg)](https://pypi.org/project/mcp-validation-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp-validation-server.svg)](https://pypi.org/project/mcp-validation-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives a language model a set of
data-validation tools it cannot argue with.

```bash
uvx mcp-validation-server      # or: pip install mcp-validation-server
```

**MCP in one sentence:** the Model Context Protocol is an open standard that lets an AI application
call tools running in a separate process, so the tool does the work and returns a result the model has
to accept.

## Why this exists

A model asked "is this data clean?" will answer. It will answer confidently whether or not it checked
anything. The fix is not a better prompt; it is to take the question away from the model and give it to
code that computes an answer and hands it back.

This server exposes four deterministic checks — contract validation, arithmetic reconciliation, distinct
entity counting, and duplicate detection. Each one returns a structured result with reasons and
row/column locators. None of them need a model, a confidence score, or reference data. All of them can
be wrong in ways you can see, which is the only kind of check worth trusting.

## The design rule

> **A validation failure is a result, not an exception.**

Every tool returns a JSON object with the same envelope:

| Field | Meaning |
| --- | --- |
| `ok` | `true` only when the data has nothing wrong with it. The one field to branch on. |
| `verdict` | `clean`, `quarantined`, `flagged`, `does_not_close`, `review_required`, `inputs_rejected` |
| `findings` | One entry per problem: a stable `code`, a human `message`, and a `locator` |
| `guidance` | Plain English: what to do next |

`ok: false` is a **successful call reporting a failing dataset**. The model reads `findings`, sees
exactly which row and column failed, and can say so instead of guessing.

Only a genuinely malformed *tool argument* raises — a contract that does not parse, a regex that does
not compile, a key that is not a column name. There is nothing to validate until the call itself is
valid, so the call is rejected with a message naming the problem.

The line this server draws:

- **The contract is the call's definition** → a broken contract is an argument error.
- **The rows are the data** → broken rows are a result, with a reason each.

## The four tools

### `validate_rows`

Applies a column contract — `required`, `type`, `min`, `max`, `range`, `pattern`, `allowed`,
`not_blank`, and per-column `on_fail` — and returns **the passing rows and an explicit quarantine
list**. Every quarantined row carries a locator (index, one-based row number, and the column that
failed), a list of human-readable `reasons`, and machine-readable `findings`.

`accepted + quarantined` always equals the number of rows you passed in. Nothing is ever silently
dropped. A row that is not even an object is quarantined with that as its reason.

### `reconcile`

Given an opening balance, a list of movements, and a closing balance, reports whether
`closing = opening + movements` holds within a tolerance, showing both sides and the variance. This is
the check that does not depend on how plausible any single figure looked: clean extraction with quietly
wrong numbers is the failure that survives field-level validation, and arithmetic is what catches it.

When the variance exactly equals the size of one movement, the result says so — that is arithmetic, not
a guess, and it is usually the answer. When a movement cannot be read, `closes` becomes `null` rather
than `false`: a total computed over an incomplete set of movements must never be reported as a pass.

### `count_distinct`

Counts distinct entities on a caller-supplied stable key and reports **the difference between that and
the row count**, broken into its two causes: keys repeated across rows, and rows carrying no usable
key. One customer can appear on fifty rows; "50" is a row count, not a customer count, and the
difference is the error that survives review because the number looks plausible.

Rows with a blank key are excluded from the count and listed individually — never counted as one
entity, never dropped. Nothing beyond whitespace and letter case is normalised: stripping punctuation
or applying fuzzy matching changes which records count as the same entity, and that is a decision for
a person.

### `duplicate_report`

Finds suspected duplicate groups and **returns them for human review**. It never merges, never deletes,
and never picks a winner. Every group carries `auto_merged: false`, `decision_required: true`, the full
rows with locators, and a `review_question` phrased for a person to answer.

Three kinds of suspicion, each labelled:

- `same_key` — the same normalised key on more than one row. Genuine duplicates, or legitimate repeated
  transactions? The tool cannot tell, so it reports rather than decides.
- `same_signature` — different keys whose values across `match_fields` are identical. The
  re-registration case.
- `near_match` — different keys with similar values. Only produced when you supply
  `similarity_threshold`, because "close enough" is a policy choice, not a fact.

## Install

Fastest, with nothing to clone:

```bash
uvx mcp-validation-server
```

Or install it into an environment:

```bash
pip install mcp-validation-server
```

Or from source, for development:

```bash
git clone https://github.com/devops-gm88/mcp-validation-server.git
cd mcp-validation-server
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Python 3.10 or newer. `mcp` is the only runtime dependency; everything else is the standard library.

## Register it with an MCP client

The server speaks MCP over **stdio**, so an MCP host launches it as a subprocess and talks to it on
stdin/stdout. This is the standard configuration block used by MCP hosts that launch local servers
(Claude Desktop, Claude Code, and others):

```json
{
  "mcpServers": {
    "data-validation": {
      "command": "/absolute/path/to/mcp-validation-server/.venv/bin/python",
      "args": ["-m", "mcp_validation_server"]
    }
  }
}
```

Use the **absolute** path to the interpreter — an MCP host does not inherit your shell's `PATH` or
virtualenv. If you installed with `pipx` or globally, the console script is equivalent:

```json
{
  "mcpServers": {
    "data-validation": {
      "command": "mcp-validation-server",
      "args": []
    }
  }
}
```

Verify the server starts and lists its tools without involving a client at all:

```bash
.venv/bin/python examples/stdio_handshake.py
```

Or with the official MCP Inspector CLI, which speaks to it as a real MCP host does. Point `command` at
the same absolute interpreter path as the block above:

```bash
cat > /tmp/mcp.json <<JSON
{
  "mcpServers": {
    "data-validation": {
      "command": "$PWD/.venv/bin/python",
      "args": ["-m", "mcp_validation_server"]
    }
  }
}
JSON
npx @modelcontextprotocol/inspector --cli --config /tmp/mcp.json --server data-validation --method tools/list
```

To call a tool through the Inspector rather than just list it:

```bash
npx @modelcontextprotocol/inspector --cli --config /tmp/mcp.json --server data-validation \
  --method tools/call --tool-name count_distinct \
  --tool-arg 'rows=[{"id":"C-1"},{"id":"C-1"},{"id":""}]' 'key=id'
```

### On the tool schemas

The Inspector reports **0 errors and 7 warnings** for these tools, and the warnings are deliberate. Four
are on the `items` of a `rows` or `columns` array; three are on `reconcile`'s `opening_balance`,
`movements` and `closing_balance`, which accept a number or a numeric string and report anything else as
a finding rather than refusing the call:

> Schema carries no validation keyword at all, so it accepts any value.

That looseness is the design rule, applied at the protocol boundary. If `rows` were declared as
`array of objects`, a client sending a row that is a bare string would be stopped by the input schema and
the model would get an unrecoverable protocol error — when the useful answer is "row 3 of your data is a
string, not an object; here it is, held back with that reason". So row content is intentionally
unconstrained and every unusable row comes back as a quarantine finding with a locator. What *is*
constrained is the shape of the call: a missing `key`, a `similarity_threshold` outside 0–1, and a
column spec that will not parse are all rejected before anything runs.

The trade is stated plainly rather than papered over: it costs a schema warning, and it buys a model
that can recover from bad data instead of just failing on it.

## Worked example

Real output. The script that produced it is `examples/worked_example.py`, and the untrimmed result is
saved beside it as `examples/worked_example_output.json`.

A model calls `validate_rows` on four rows of deliberately broken data:

```json
{
  "rows": [
    {"invoice": "INV-1001", "amount": "100.50", "date": "2026-04-01"},
    {"invoice": "",         "amount": "20",     "date": "2026-04-02"},
    {"invoice": "INV-1003", "amount": "-5",     "date": "2026-04-03"},
    {"invoice": "INV-1004", "amount": "abc",    "date": "04/05/2026"}
  ],
  "columns": [
    {"name": "invoice", "type": "string", "required": true, "pattern": "INV-\\d+"},
    {"name": "amount",  "type": "number", "min": 0},
    {"name": "date",    "type": "date",   "required": true}
  ],
  "row_key": "invoice"
}
```

and gets back (`is_error: false` — the call succeeded; the data failed):

```json
{
  "tool": "validate_rows",
  "ok": false,
  "verdict": "quarantined",
  "finding_count": 4,
  "findings": "<<4 findings, one per quarantine reason; see worked_example_output.json>>",
  "guidance": "3 of 4 rows were quarantined and are listed in `quarantine` with a locator and reasons. Failure codes: below_minimum (1), not_a_date (1), not_a_number (1), required_blank (1). Do not use the accepted rows as if they were the whole dataset: report both counts, and resolve or explicitly exclude the quarantined rows before aggregating.",
  "contract": "<<the parsed contract echoed back; see worked_example_output.json>>",
  "summary": {
    "rows_in": 4,
    "accepted": 1,
    "quarantined": 3,
    "flagged": 0,
    "pass_rate": 0.25,
    "accounted_for": 4
  },
  "accepted": [
    {
      "invoice": "INV-1001",
      "amount": 100.5,
      "date": "2026-04-01"
    }
  ],
  "quarantine": [
    {
      "locator": {
        "row_index": 1,
        "row_number": 2
      },
      "row": {
        "invoice": "",
        "amount": 20.0,
        "date": "2026-04-02"
      },
      "reasons": [
        "invoice: is required but blank"
      ],
      "findings": [
        {
          "code": "required_blank",
          "severity": "error",
          "message": "is required but blank",
          "locator": {
            "row_index": 1,
            "row_number": 2,
            "column": "invoice"
          },
          "expected": "a non-blank value"
        }
      ]
    },
    {
      "locator": {
        "row_index": 2,
        "row_number": 3,
        "row_key": "INV-1003"
      },
      "row": {
        "invoice": "INV-1003",
        "amount": -5.0,
        "date": "2026-04-03"
      },
      "reasons": [
        "amount: -5 is below the minimum of 0"
      ],
      "findings": [
        {
          "code": "below_minimum",
          "severity": "error",
          "message": "-5 is below the minimum of 0",
          "locator": {
            "row_index": 2,
            "row_number": 3,
            "row_key": "INV-1003",
            "column": "amount"
          },
          "value": -5.0
        }
      ]
    },
    {
      "locator": {
        "row_index": 3,
        "row_number": 4,
        "row_key": "INV-1004"
      },
      "row": {
        "invoice": "INV-1004",
        "amount": "abc",
        "date": "04/05/2026"
      },
      "reasons": [
        "amount: 'abc' is not a number",
        "date: '04/05/2026' is not an ISO-8601 date (expected YYYY-MM-DD)"
      ],
      "findings": [
        {
          "code": "not_a_number",
          "severity": "error",
          "message": "'abc' is not a number",
          "locator": {
            "row_index": 3,
            "row_number": 4,
            "row_key": "INV-1004",
            "column": "amount"
          },
          "value": "abc",
          "expected": "a value of type number"
        },
        {
          "code": "not_a_date",
          "severity": "error",
          "message": "'04/05/2026' is not an ISO-8601 date (expected YYYY-MM-DD)",
          "locator": {
            "row_index": 3,
            "row_number": 4,
            "row_key": "INV-1004",
            "column": "date"
          },
          "value": "04/05/2026",
          "expected": "a value of type date"
        }
      ]
    }
  ],
  "flagged": [],
  "errors_by_column": {
    "amount": 2,
    "date": 1,
    "invoice": 1
  },
  "findings_by_code": {
    "below_minimum": 1,
    "not_a_date": 1,
    "not_a_number": 1,
    "required_blank": 1
  },
  "columns_seen": [],
  "columns_required_but_absent": []
}
```

What a model can now do that it could not before: say **"3 of your 4 rows are unusable, here is which
one and why"**, and be right. The one clean row came back with its string amount coerced to the number
`100.5`; the three broken rows came back with their original values intact and a reason each. Row 4
failed twice, on two different columns, and both reasons are listed.

Note the locator gives both `row_index` (0-based, for the machine) and `row_number` (1-based, for the
person reading the report), and quotes `row_key` when the row has one.

## What this does not do

- **It does not repair data.** It reports which records are untrustworthy and why. It never fills,
  imputes, or corrects a value.
- **It does not merge duplicates, ever.** `duplicate_report` returns suspicions with a question
  attached. Merging is a decision with consequences, and it belongs to a person.
- **It does not score data quality.** There is no 0–100 number and no dashboard. There are findings
  with codes and locators.
- **It does not prove the data is correct.** Passing a contract means the rows satisfy *the rules you
  supplied and nothing else*. A contract that does not check a column says nothing about that column.
  `reconcile` confirms internal consistency, not completeness: movements that are missing entirely will
  still balance if the closing balance was derived from the same wrong set.
- **It does not know your domain.** Whether a repeated key is a defect depends on whether the table is a
  transaction log or an entity register. The tools report the number; you decide what it means.
- **It does not normalise beyond whitespace and case.** No punctuation stripping, no accent folding, no
  nickname tables, no fuzzy matching except where you explicitly ask for a similarity threshold.
- **It has no persistence, network access, or file access.** Every tool takes its data as an argument
  and returns a result. Nothing is written anywhere.
- **It does not cap the size of the data you send it.** A large `rows` array comes back in full, in the
  result. `duplicate_report` has a `max_groups` cap that reports what it omitted; `validate_rows` and
  `count_distinct` do not truncate, so pass a summary-sized batch rather than a whole table.
- **It does not check the caller.** There is no authentication. It is a local stdio server for one
  user's machine.

## Relationship to `sheets-data-validation-kit`

This project is a **sibling**, not a wrapper. It is self-contained: `pip install -e .` here installs
everything it needs, and nothing here imports the other package.

The ideas and the naming come from
[`sheets-data-validation-kit`](https://github.com/devops-gm88/sheets-data-validation-kit), a
standard-library-only Python library of the same primitives — `Field`, `RowContract`, the rule objects,
`Min`/`Max`/`Range`/`Pattern`, accept-or-quarantine, exact-match entity resolution, and never
auto-merging a duplicate. The kit is the library you import; this is the MCP server that puts those
checks behind a protocol so a model can call them.

Two differences are worth knowing:

| | `sheets-data-validation-kit` | `mcp-validation-server` |
| --- | --- | --- |
| Interface | Python objects you construct in code | JSON arguments over MCP |
| Contract shape | `Field(...)` with `Rule` instances | Column spec objects; parsed, and unknown keys rejected |
| Locators | Reasons as strings | Findings with codes and row/column locators |
| Failure model | Returns a result object | Returns a result, plus a `guidance` string for the model |

The server's `contract.py` is a re-implementation for JSON-supplied contracts, not a copy: it adds type
coercion, locators, per-finding codes, and the parsing layer that turns a model's JSON into rules and
rejects a malformed contract before any data is touched.

## Tests

```bash
pip install -e .
python3 -m unittest discover -s tests -v
```

139 tests, all passing on Python 3.14.7 — the count and result are from an actual run, not an estimate.
Every tool has non-MCP unit tests that call the underlying functions directly; no test needs a client,
a transport, or a subprocess.

Without installing first:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests exercise the design rule directly: that `accepted + quarantined` equals the input count, that
a failing dataset never raises, that an unreadable movement withholds the reconciliation verdict instead
of producing a false pass, that nothing is ever merged, and that the four tools publish one shared
envelope.

## Layout

```
mcp-validation-server/
├── examples/
│   ├── stdio_handshake.py          # start the server and list its tools, no client needed
│   ├── worked_example.py           # produces the README's worked example
│   └── worked_example_output.json  # the untrimmed result, as produced
├── src/mcp_validation_server/
│   ├── __init__.py                 # public API
│   ├── __main__.py                 # python3 -m mcp_validation_server
│   ├── app.py                      # the MCPServer instance and its instructions
│   ├── tools.py                    # the four @mcp.tool() functions and their docstrings
│   ├── findings.py                 # Finding, Locator, the shared response envelope
│   ├── contract.py                 # column specs, rules, RowContract, quarantine
│   ├── reconcile.py                # opening + movements = closing
│   ├── entities.py                 # distinct counting, duplicate grouping
│   └── server.py                   # entry point and transport selection
└── tests/                          # stdlib unittest, no MCP client required
```

## Notes on the SDK

Built against the official MCP Python SDK, `mcp` 2.2.0. Two things differ from most published examples,
which were written for `mcp` 1.x:

- **`FastMCP` was renamed to `MCPServer`.** In `mcp` 2.x the import is
  `from mcp.server.mcpserver import MCPServer`; `mcp.server.fastmcp` no longer exists and raises a
  `ModuleNotFoundError` pointing at the migration guide. `pyproject.toml` therefore requires
  `mcp>=2.0,<3`.
- **Only `ToolError` carries a message to the caller.** Any other exception raised inside a tool is
  wrapped by the SDK as `UnexpectedToolError`, whose text is the generic
  `Error executing tool <name>`, with the original logged server-side only. Since a contract error the
  model cannot read is a contract error the model cannot fix, malformed arguments are raised as
  `mcp.server.mcpserver.exceptions.ToolError`.
- **Return annotations matter for structured output.** Annotating a tool with `typing.Dict[str, Any]`
  makes the SDK publish an output schema of `{"result": {...}}` and nest `structuredContent` one level
  down; the PEP 585 `dict[str, Any]` annotation keeps the payload at the top level. These tools use
  `dict[str, Any]`.

## Licence

MIT. See [LICENSE](LICENSE).
