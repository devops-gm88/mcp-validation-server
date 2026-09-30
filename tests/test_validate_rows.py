"""Tests for the row contract and the ``validate_rows`` tool.

These call the functions directly. No MCP client, no transport, no subprocess —
the validation logic is ordinary Python and is tested as such.
"""

import re
import unittest

from mcp_validation_server import validate_rows
from mcp_validation_server.contract import (
    ColumnSpecError,
    Field,
    Max,
    Min,
    NotBlank,
    OneOf,
    Pattern,
    Range,
    RowContract,
    parse_columns,
)

CLEAN_CONTRACT = [
    {"name": "invoice", "type": "string", "required": True, "pattern": r"INV-\d+"},
    {"name": "amount", "type": "number", "min": 0, "max": 1000000},
    {"name": "date", "type": "date", "required": True},
]


class TestAcceptedRows(unittest.TestCase):
    def test_clean_row_is_accepted(self):
        result = validate_rows(
            [{"invoice": "INV-1001", "amount": "100.50", "date": "2026-04-01"}],
            CLEAN_CONTRACT,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "clean")
        self.assertEqual(result["summary"]["accepted"], 1)
        self.assertEqual(result["quarantine"], [])

    def test_unambiguous_coercion_is_applied_and_visible(self):
        result = validate_rows(
            [{"invoice": "INV-1001", "amount": "100.50", "date": "2026-04-01"}],
            CLEAN_CONTRACT,
        )
        self.assertEqual(result["accepted"][0]["amount"], 100.5)

    def test_an_iso_datetime_is_reduced_to_its_date(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": 1, "date": "2026-04-01T09:30:00"}],
            CLEAN_CONTRACT,
        )
        self.assertEqual(result["accepted"][0]["date"], "2026-04-01")

    def test_omitted_optional_column_is_not_a_failure(self):
        result = validate_rows([{"invoice": "INV-1", "date": "2026-04-01"}], CLEAN_CONTRACT)
        self.assertTrue(result["ok"])


class TestQuarantine(unittest.TestCase):
    def test_blank_required_field_is_quarantined_with_a_reason(self):
        result = validate_rows(
            [{"invoice": "  ", "amount": 1, "date": "2026-04-01"}], CLEAN_CONTRACT
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"], "quarantined")
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "required_blank")
        self.assertIn("required but blank", result["quarantine"][0]["reasons"][0])

    def test_out_of_range_value_is_quarantined(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": -5, "date": "2026-04-01"}], CLEAN_CONTRACT
        )
        self.assertEqual(len(result["quarantine"]), 1)
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "below_minimum")

    def test_value_above_maximum_is_quarantined(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": 2_000_000, "date": "2026-04-01"}], CLEAN_CONTRACT
        )
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "above_maximum")

    def test_malformed_pattern_value_is_quarantined(self):
        result = validate_rows(
            [{"invoice": "1001", "amount": 1, "date": "2026-04-01"}], CLEAN_CONTRACT
        )
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "pattern_mismatch")

    def test_pattern_must_match_the_whole_value(self):
        # "\\d{4}" is not a full match for "12345", so the row is rejected.
        result = validate_rows([{"code": "12345"}], [{"name": "code", "pattern": r"\d{4}"}])
        self.assertEqual(len(result["quarantine"]), 1)

    def test_non_numeric_text_under_a_number_type_is_quarantined(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": "not a number", "date": "2026-04-01"}],
            CLEAN_CONTRACT,
        )
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "not_a_number")

    def test_unreadable_date_is_quarantined_rather_than_guessed(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": 1, "date": "04/05/2026"}], CLEAN_CONTRACT
        )
        self.assertEqual(result["quarantine"][0]["findings"][0]["code"], "not_a_date")

    def test_a_row_that_is_not_an_object_is_quarantined(self):
        result = validate_rows(["just a string", 42, None], CLEAN_CONTRACT)
        self.assertEqual(len(result["quarantine"]), 3)
        for item in result["quarantine"]:
            self.assertEqual(item["findings"][0]["code"], "not_an_object")

    def test_a_row_can_fail_more_than_one_rule(self):
        result = validate_rows([{"invoice": "nope", "amount": -1, "date": "x"}], CLEAN_CONTRACT)
        codes = {finding["code"] for finding in result["quarantine"][0]["findings"]}
        self.assertEqual(codes, {"pattern_mismatch", "below_minimum", "not_a_date"})


class TestNothingIsEverSilentlyDropped(unittest.TestCase):
    def test_accepted_plus_quarantined_equals_rows_in(self):
        rows = [
            {"invoice": "INV-1", "amount": 1, "date": "2026-04-01"},
            {"invoice": "", "amount": 1, "date": "2026-04-01"},
            {"invoice": "INV-3", "amount": "x", "date": "2026-04-01"},
            {"invoice": "INV-4", "amount": 1, "date": "nonsense"},
            "not an object at all",
        ]
        result = validate_rows(rows, CLEAN_CONTRACT)
        summary = result["summary"]
        self.assertEqual(summary["rows_in"], len(rows))
        self.assertEqual(summary["accounted_for"], len(rows))
        self.assertEqual(summary["accepted"] + summary["quarantined"], len(rows))

    def test_every_quarantined_row_carries_at_least_one_reason(self):
        result = validate_rows(
            [{"invoice": "", "amount": "x", "date": "y"}, {}, 7], CLEAN_CONTRACT
        )
        for item in result["quarantine"]:
            self.assertTrue(item["reasons"], f"no reason given for {item!r}")
            self.assertTrue(item["findings"])

    def test_every_quarantined_row_carries_a_locator(self):
        rows = [{"invoice": "INV-1", "amount": 1, "date": "2026-04-01"}, {"invoice": ""}]
        result = validate_rows(rows, CLEAN_CONTRACT, row_key="invoice")
        locator = result["quarantine"][0]["locator"]
        self.assertEqual(locator["row_index"], 1)
        self.assertEqual(locator["row_number"], 2)

    def test_every_finding_locates_the_row_and_the_column(self):
        result = validate_rows([{"invoice": ""}], CLEAN_CONTRACT)
        locator = result["quarantine"][0]["findings"][0]["locator"]
        self.assertEqual(locator["row_number"], 1)
        self.assertEqual(locator["column"], "invoice")

    def test_a_row_key_is_quoted_when_it_exists_and_omitted_when_blank(self):
        rows = [{"invoice": "INV-9", "amount": -1, "date": "2026-04-01"}, {"invoice": ""}]
        result = validate_rows(rows, CLEAN_CONTRACT, row_key="invoice")
        self.assertEqual(result["quarantine"][0]["locator"]["row_key"], "INV-9")
        self.assertNotIn("row_key", result["quarantine"][1]["locator"])

    def test_the_quarantined_row_is_returned_so_it_can_be_inspected(self):
        result = validate_rows([{"invoice": "", "amount": 20, "date": "2026-04-01"}], CLEAN_CONTRACT)
        self.assertEqual(result["quarantine"][0]["row"]["amount"], 20)


class TestFlagMode(unittest.TestCase):
    def test_a_flagged_column_keeps_the_row(self):
        result = validate_rows(
            [{"note": ""}, {"note": "fine"}],
            [{"name": "note", "not_blank": True, "on_fail": "flag"}],
        )
        self.assertEqual(result["summary"]["accepted"], 2)
        self.assertEqual(result["summary"]["quarantined"], 0)
        self.assertEqual(result["summary"]["flagged"], 1)
        self.assertEqual(result["verdict"], "flagged")
        self.assertTrue(result["ok"])

    def test_a_quarantine_column_still_wins_over_a_flag_column(self):
        result = validate_rows(
            [{"note": "", "id": ""}],
            [
                {"name": "note", "not_blank": True, "on_fail": "flag"},
                {"name": "id", "required": True},
            ],
        )
        self.assertEqual(result["verdict"], "quarantined")


class TestReporting(unittest.TestCase):
    def test_findings_are_tallied_by_column(self):
        result = validate_rows(
            [{"invoice": "", "amount": 1, "date": "2026-04-01"}] * 2, CLEAN_CONTRACT
        )
        self.assertEqual(result["errors_by_column"]["invoice"], 2)

    def test_findings_are_tallied_by_code(self):
        result = validate_rows(
            [{"invoice": "", "amount": 1, "date": "2026-04-01"}] * 3, CLEAN_CONTRACT
        )
        self.assertEqual(result["findings_by_code"]["required_blank"], 3)

    def test_pass_rate_is_reported(self):
        rows = [
            {"invoice": "INV-1", "amount": 1, "date": "2026-04-01"},
            {"invoice": "", "amount": 1, "date": "2026-04-01"},
        ]
        self.assertEqual(validate_rows(rows, CLEAN_CONTRACT)["summary"]["pass_rate"], 0.5)

    def test_the_contract_is_echoed_back_so_rules_can_be_confirmed(self):
        result = validate_rows([{"invoice": "INV-1", "amount": 1, "date": "2026-04-01"}],
                               CLEAN_CONTRACT)
        described = {column["name"]: column for column in result["contract"]["columns"]}
        self.assertEqual(described["invoice"]["checks"][0]["regex"], r"INV-\d+")
        self.assertEqual(described["amount"]["checks"][1]["bound"], 1000000.0)

    def test_a_declared_column_absent_from_every_row_is_reported(self):
        result = validate_rows([{"amount": 5}], CLEAN_CONTRACT)
        self.assertEqual(
            result["columns_required_but_absent"], ["date", "invoice"]
        )
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("required_column_absent", codes)

    def test_an_undeclared_column_is_reported_but_not_an_error(self):
        result = validate_rows(
            [{"invoice": "INV-1", "amount": 1, "date": "2026-04-01", "notes": "hi"}],
            CLEAN_CONTRACT,
        )
        self.assertEqual(result["columns_seen"], ["notes"])
        self.assertTrue(result["ok"])

    def test_empty_input_is_not_reported_as_a_pass(self):
        result = validate_rows([], CLEAN_CONTRACT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["rows_in"], 0)
        self.assertIn("no_rows", {finding["code"] for finding in result["findings"]})


class TestMalformedContractRaises(unittest.TestCase):
    """The contract is the call's definition, so a broken contract is an argument error."""

    def test_unknown_type_is_rejected_with_the_supported_list(self):
        with self.assertRaises(ColumnSpecError) as caught:
            parse_columns([{"name": "amount", "type": "intger"}])
        self.assertIn("intger", str(caught.exception))
        self.assertIn("integer", str(caught.exception))

    def test_unknown_spec_key_is_rejected_rather_than_ignored(self):
        # "regexp" is a typo for "pattern"; ignoring it would silently drop a rule.
        with self.assertRaises(ColumnSpecError) as caught:
            parse_columns([{"name": "code", "regexp": r"\d+"}])
        self.assertIn("regexp", str(caught.exception))

    def test_uncompilable_pattern_is_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"name": "code", "pattern": "([unclosed"}])

    def test_duplicate_column_names_are_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"name": "a"}, {"name": "a"}])

    def test_empty_contract_is_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([])

    def test_inverted_range_is_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"name": "x", "range": [100, 1]}])

    def test_range_must_be_two_numbers(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"name": "x", "range": [1]}])

    def test_column_without_a_name_is_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"type": "number"}])

    def test_bad_on_fail_mode_is_rejected(self):
        with self.assertRaises(ColumnSpecError):
            parse_columns([{"name": "x", "on_fail": "explode"}])


class TestRowContractUnit(unittest.TestCase):
    """The contract object on its own, mirroring the primitives in the kit."""

    def setUp(self):
        self.contract = RowContract(
            [
                Field("account_id", required=True),
                Field("amount", type="number", rules=[Min(0)]),
                Field("rate", type="number", rules=[Range(0.5, 1.5)]),
                Field("period", rules=[Pattern(re.compile(r"\d{4}-Q[1-4]"), r"\d{4}-Q[1-4]")]),
                Field("stage", rules=[OneOf(["open", "closed"])]),
                Field("note", rules=[NotBlank()], on_fail="flag"),
            ]
        )

    def test_field_helpers_are_constructible_directly(self):
        contract = RowContract([Field("x", rules=[Max(10)])])
        outcome = contract.validate_rows([{"x": 11}])
        self.assertEqual(len(outcome.quarantine), 1)

    def test_rule_objects_report_a_code(self):
        self.assertEqual(Min(0).check(-1).code, "below_minimum")
        self.assertEqual(Max(0).check(1).code, "above_maximum")
        self.assertEqual(Range(0, 1).check(2).code, "out_of_range")
        self.assertEqual(NotBlank().check("  ").code, "blank")
        self.assertIsNone(Min(0).check(5))

    def test_one_of_accepts_allowed_and_rejects_others(self):
        self.assertIsNone(OneOf(["a", "b"]).check("a"))
        self.assertEqual(OneOf(["a", "b"]).check("c").code, "not_allowed")


if __name__ == "__main__":
    unittest.main()
