"""Cross-cutting tests: the response envelope, the raise boundary, registration.

The four tools are different checks, but they share one interface. These tests
hold that interface still, because the whole point of the project is that a
calling model can rely on it without special-casing any one tool.
"""

import asyncio
import unittest

from mcp.server.mcpserver.exceptions import MCPServerError, ToolError

from mcp_validation_server import (
    SERVER_NAME,
    count_distinct,
    duplicate_report,
    mcp,
    reconcile,
    validate_rows,
)
from mcp_validation_server.tools import _argument_error

ENVELOPE_KEYS = {"tool", "ok", "verdict", "finding_count", "findings", "guidance"}

# One call per tool, each on deliberately failing data.
FAILING_CALLS = {
    "validate_rows": lambda: validate_rows(
        [{"amount": "-5"}], [{"name": "amount", "type": "number", "min": 0}]
    ),
    "reconcile": lambda: reconcile(100.0, [1.0], 200.0),
    "count_distinct": lambda: count_distinct([{"id": "a"}, {"id": "a"}], "id"),
    "duplicate_report": lambda: duplicate_report([{"id": "a"}, {"id": "a"}], "id"),
}

# One call per tool on data with nothing wrong with it.
PASSING_CALLS = {
    "validate_rows": lambda: validate_rows(
        [{"amount": "5"}], [{"name": "amount", "type": "number", "min": 0}]
    ),
    "reconcile": lambda: reconcile(100.0, [1.0], 101.0),
    "count_distinct": lambda: count_distinct([{"id": "a"}, {"id": "b"}], "id"),
    "duplicate_report": lambda: duplicate_report([{"id": "a"}, {"id": "b"}], "id"),
}


class TestSharedEnvelope(unittest.TestCase):
    def test_every_tool_returns_the_same_envelope_keys(self):
        for name, call in {**FAILING_CALLS, **PASSING_CALLS}.items():
            with self.subTest(tool=name):
                self.assertTrue(ENVELOPE_KEYS <= set(call()), f"{name} is missing envelope keys")

    def test_the_envelope_names_the_tool_that_produced_it(self):
        for name, call in FAILING_CALLS.items():
            with self.subTest(tool=name):
                self.assertEqual(call()["tool"], name)

    def test_a_failing_dataset_returns_a_result_and_does_not_raise(self):
        for name, call in FAILING_CALLS.items():
            with self.subTest(tool=name):
                result = call()
                self.assertFalse(result["ok"])
                self.assertNotEqual(result["verdict"], "clean")

    def test_ok_is_true_only_for_data_with_nothing_wrong(self):
        for name, call in PASSING_CALLS.items():
            with self.subTest(tool=name):
                self.assertTrue(call()["ok"], f"{name} should have passed")

    def test_a_failure_always_explains_itself(self):
        for name, call in FAILING_CALLS.items():
            with self.subTest(tool=name):
                result = call()
                self.assertGreater(result["finding_count"], 0)
                for finding in result["findings"]:
                    self.assertTrue(finding.get("code"), "a finding needs a code")
                    self.assertTrue(finding.get("message"), "a finding needs a message")
                    self.assertIn(finding.get("severity"), {"error", "warning", "info"})

    def test_every_response_carries_guidance(self):
        for name, call in {**FAILING_CALLS, **PASSING_CALLS}.items():
            with self.subTest(tool=name):
                guidance = call()["guidance"]
                self.assertIsInstance(guidance, str)
                self.assertTrue(guidance.strip(), f"{name} gave no guidance")

    def test_finding_count_matches_the_findings_array(self):
        for name, call in FAILING_CALLS.items():
            with self.subTest(tool=name):
                result = call()
                self.assertEqual(result["finding_count"], len(result["findings"]))

    def test_envelopes_survive_json_serialisation(self):
        import json

        for name, call in {**FAILING_CALLS, **PASSING_CALLS}.items():
            with self.subTest(tool=name):
                json.dumps(call())  # must not raise


class TestRaiseBoundary(unittest.TestCase):
    """Malformed arguments raise; bad data does not. Nothing else raises."""

    RAISING_CALLS = {
        "validate_rows": lambda: validate_rows([], [{"name": "a", "type": "intger"}]),
        "count_distinct": lambda: count_distinct([], ""),
        "duplicate_report_key": lambda: duplicate_report([], " "),
        "duplicate_report_threshold": lambda: duplicate_report(
            [], "id", match_fields=["name"], similarity_threshold=2
        ),
    }

    def test_malformed_arguments_raise_tool_error(self):
        for name, call in self.RAISING_CALLS.items():
            with self.subTest(case=name):
                with self.assertRaises(ToolError):
                    call()

    def test_tool_error_is_the_sdk_error_channel(self):
        # Only ToolError reaches the model with its message; anything else is
        # reported to the client as a generic crash.
        self.assertTrue(issubclass(ToolError, MCPServerError))

    def test_the_error_message_reaches_the_caller_intact(self):
        with self.assertRaises(ToolError) as caught:
            validate_rows([], [{"name": "amount", "type": "intger"}])
        message = str(caught.exception)
        self.assertIn("intger", message)
        self.assertIn("integer", message)
        self.assertIn("column spec is an object", message)

    def test_argument_error_helper_produces_tool_error(self):
        self.assertIsInstance(_argument_error("boom"), ToolError)

    def test_unusable_data_does_not_take_the_raising_path(self):
        for name, call in FAILING_CALLS.items():
            with self.subTest(tool=name):
                call()  # must not raise


class TestRegistration(unittest.TestCase):
    def test_the_server_registers_exactly_the_four_tools(self):
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(
            sorted(tool.name for tool in tools),
            ["count_distinct", "duplicate_report", "reconcile", "validate_rows"],
        )

    def test_every_tool_has_a_description_the_model_can_read(self):
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            with self.subTest(tool=tool.name):
                description = tool.description or ""
                self.assertGreater(
                    len(description),
                    200,
                    "the docstring is the description the model reads; keep it substantial",
                )
                self.assertIn("Returns:", description)
                self.assertIn("Args:", description)

    def test_every_tool_publishes_an_input_schema(self):
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.input_schema["type"], "object")
                self.assertTrue(tool.input_schema["properties"])

    def test_every_tool_publishes_an_output_schema(self):
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIsNotNone(tool.output_schema)

    def test_the_output_schema_is_not_wrapped_in_a_result_object(self):
        # A `typing.Dict[str, Any]` return annotation makes this SDK publish
        # {"result": {...}} and nest structuredContent one level down. The PEP 585
        # `dict[str, Any]` annotation keeps the payload at the top level.
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertNotIn("result", tool.output_schema.get("properties", {}))

    def test_the_server_carries_instructions_for_the_model(self):
        self.assertEqual(mcp.name, SERVER_NAME)
        self.assertIn("ok", mcp.instructions or "")
        self.assertIn("RESULT, not an error", mcp.instructions or "")


if __name__ == "__main__":
    unittest.main()
