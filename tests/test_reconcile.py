"""Tests for arithmetic reconciliation.

The reconciliation check is the one that needs no model and no reference data:
either the arithmetic holds or it does not. These tests pin down both directions,
including the cases where the inputs are too incomplete to give a verdict at all.
"""

import unittest

from mcp_validation_server import reconcile
from mcp_validation_server.reconcile import (
    ARITHMETIC,
    DIRECTION_IN,
    DIRECTION_OUT,
    parse_movement,
    reconcile_arithmetic,
)


class TestArithmeticCloses(unittest.TestCase):
    def test_matching_balances_close(self):
        result = reconcile(1000.00, [250.00, -40.50], 1209.50)
        self.assertTrue(result["ok"])
        self.assertTrue(result["closes"])
        self.assertEqual(result["verdict"], "clean")
        self.assertEqual(result["variance"], 0.0)

    def test_both_sides_are_shown(self):
        result = reconcile(1000.00, [250.00], 1250.00)
        self.assertEqual(result["opening_balance"], 1000.0)
        self.assertEqual(result["movements_net"], 250.0)
        self.assertEqual(result["expected_closing_balance"], 1250.0)
        self.assertEqual(result["reported_closing_balance"], 1250.0)
        self.assertEqual(result["arithmetic"], ARITHMETIC)

    def test_explicit_directions_are_honoured(self):
        result = reconcile(
            1000.00,
            [{"amount": 250.0, "direction": "in"}, {"amount": 40.5, "direction": "out"}],
            1209.50,
        )
        self.assertTrue(result["closes"])
        self.assertEqual(result["movement_counts"]["in"], 1)
        self.assertEqual(result["movement_counts"]["out"], 1)
        self.assertEqual(result["movement_totals"]["out"], 40.5)

    def test_a_negative_amount_is_a_decrease_without_a_direction(self):
        result = reconcile(100.0, [-30.0], 70.0)
        self.assertTrue(result["closes"])
        self.assertEqual(result["movement_counts"]["out"], 1)

    def test_numeric_strings_are_accepted(self):
        result = reconcile("1000.00", ["250.00"], "1250.00")
        self.assertTrue(result["closes"])

    def test_no_movements_closes_when_balances_agree(self):
        result = reconcile(500.0, [], 500.0)
        self.assertTrue(result["closes"])
        self.assertEqual(result["movement_counts"]["total"], 0)

    def test_a_variance_inside_the_tolerance_closes(self):
        result = reconcile(100.0, [0.0], 100.004, tolerance=0.01)
        self.assertTrue(result["closes"])
        self.assertAlmostEqual(result["variance"], 0.004)

    def test_a_zero_tolerance_demands_exactness(self):
        self.assertFalse(reconcile(100.0, [0.0], 100.004, tolerance=0.0)["closes"])

    def test_a_variance_outside_the_tolerance_does_not_close(self):
        result = reconcile(100.0, [0.0], 100.02, tolerance=0.01)
        self.assertFalse(result["closes"])
        self.assertFalse(result["ok"])


class TestArithmeticDoesNotClose(unittest.TestCase):
    def test_the_variance_is_reported_with_both_sides(self):
        result = reconcile(1000.00, [{"amount": 250.0, "direction": "in"}], 1300.00)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"], "does_not_close")
        self.assertEqual(result["expected_closing_balance"], 1250.0)
        self.assertEqual(result["reported_closing_balance"], 1300.0)
        self.assertEqual(result["variance"], 50.0)

    def test_a_variance_against_a_stated_closing_is_flagged(self):
        result = reconcile(1000.00, [{"amount": 250.0, "direction": "in"}], 1300.00)
        self.assertEqual(result["variance"], 50.0)
        self.assertEqual(result["absolute_variance"], 50.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("does_not_close", codes)

    def test_the_finding_names_the_side_and_keeps_the_expected_value(self):
        result = reconcile(10.0, [1.0], 99.0)
        finding = next(f for f in result["findings"] if f["code"] == "does_not_close")
        self.assertEqual(finding["locator"]["side"], "closing_balance")
        self.assertEqual(finding["value"], 99.0)
        self.assertEqual(finding["expected"], 11.0)

    def test_a_variance_matching_one_movement_is_called_out(self):
        # 40.0 was taken out but the closing balance was entered as if it was not.
        result = reconcile(
            1000.0,
            [{"amount": 250.0, "direction": "in"}, {"amount": 40.0, "direction": "out"}],
            1250.0,
        )
        self.assertEqual(result["variance"], 40.0)
        hints = [f for f in result["findings"] if f["code"] == "variance_matches_single_movement"]
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["locator"]["movement_number"], 2)

    def test_a_variance_of_twice_a_movement_is_called_out(self):
        result = reconcile(1000.0, [{"amount": 25.0, "direction": "in"}], 1075.0)
        self.assertEqual(result["variance"], 50.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("variance_is_double_a_movement", codes)

    def test_no_hint_is_given_when_no_movement_matches_the_variance(self):
        result = reconcile(1000.0, [{"amount": 25.0, "direction": "in"}], 1300.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertNotIn("variance_matches_single_movement", codes)

    def test_guidance_tells_the_model_not_to_report_the_figure(self):
        result = reconcile(1000.0, [1.0], 2000.0)
        self.assertIn("does NOT close", result["guidance"])
        self.assertIn("Do not report", result["guidance"])


class TestIncompleteInputs(unittest.TestCase):
    """A total computed over unreadable movements must never be reported as a pass."""

    def test_an_unreadable_movement_is_a_finding_not_an_exception(self):
        result = reconcile(100.0, [50.0, "not a number"], 150.0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["closes"])
        self.assertEqual(result["verdict"], "inputs_rejected")
        self.assertFalse(result["arithmetic_is_reliable"])
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("movement_not_numeric", codes)

    def test_the_unreadable_movement_is_excluded_from_the_total(self):
        result = reconcile(100.0, [50.0, "oops"], 150.0)
        self.assertEqual(result["movement_counts"]["unreadable"], 1)
        self.assertEqual(result["movement_counts"]["total"], 1)
        self.assertEqual(result["movements_net"], 50.0)

    def test_an_incomplete_total_is_never_reported_as_closing(self):
        # The parsed movements happen to make it close; the verdict must still be withheld.
        result = reconcile(100.0, [50.0, "oops"], 150.0)
        self.assertIsNone(result["closes"])
        self.assertFalse(result["ok"])
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("arithmetic_unreliable", codes)

    def test_an_unknown_direction_is_rejected_rather_than_assumed(self):
        result = reconcile(100.0, [{"amount": 50.0, "direction": "sideways"}], 150.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("unknown_direction", codes)
        self.assertIsNone(result["closes"])

    def test_an_unrecognised_movement_key_is_rejected(self):
        result = reconcile(100.0, [{"ammount": 50.0}], 150.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("unknown_movement_key", codes)

    def test_a_boolean_is_not_a_movement_amount(self):
        result = reconcile(100.0, [True], 150.0)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("movement_not_numeric", codes)

    def test_a_non_finite_balance_is_an_argument_problem_not_a_pass(self):
        result = reconcile(float("nan"), [], 0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"], "inputs_rejected")
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("opening_balance_not_numeric", codes)

    def test_a_negative_tolerance_is_corrected_and_reported(self):
        result = reconcile(100.0, [0.0], 100.0, tolerance=-1)
        self.assertTrue(result["closes"])
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("tolerance_negative", codes)
        self.assertEqual(result["tolerance"], 1.0)


class TestMalformedArguments(unittest.TestCase):
    """Movements that are not a list at all cannot be interpreted as data."""

    def test_a_mapping_for_movements_is_rejected_as_input(self):
        result = reconcile(100.0, {"in": 50}, 150.0)
        self.assertEqual(result["verdict"], "inputs_rejected")
        self.assertEqual(result["findings"][0]["code"], "invalid_arguments")
        self.assertIsNone(result["closes"])

    def test_a_scalar_for_movements_is_rejected_as_input(self):
        result = reconcile(100.0, "50", 150.0)
        self.assertEqual(result["verdict"], "inputs_rejected")

    def test_a_non_numeric_closing_balance_is_rejected_as_input(self):
        result = reconcile(100.0, [50.0], "closing")
        self.assertEqual(result["verdict"], "inputs_rejected")

    def test_the_guidance_shows_a_working_call_shape(self):
        result = reconcile(100.0, {"in": 50}, 150.0)
        self.assertIn("opening_balance", result["guidance"])


class TestMovementParsing(unittest.TestCase):
    def test_a_bare_number_is_signed_as_given(self):
        movement, problem = parse_movement(250.0, 0)
        self.assertIsNone(problem)
        self.assertEqual(movement.direction, DIRECTION_IN)
        self.assertEqual(movement.signed_amount, 250.0)

    def test_a_bare_negative_number_is_a_decrease(self):
        movement, _ = parse_movement(-250.0, 0)
        self.assertEqual(movement.direction, DIRECTION_OUT)
        self.assertEqual(movement.signed_amount, -250.0)

    def test_an_out_direction_produces_a_negative_signed_amount(self):
        movement, _ = parse_movement({"amount": 10.0, "direction": "out"}, 0)
        self.assertEqual(movement.signed_amount, -10.0)

    def test_the_sign_of_an_amount_is_not_double_applied(self):
        # {"amount": -10, "direction": "out"} means ten out, not minus ten out.
        movement, _ = parse_movement({"amount": -10.0, "direction": "out"}, 0)
        self.assertEqual(movement.amount, 10.0)
        self.assertEqual(movement.signed_amount, -10.0)

    def test_credit_and_debit_are_understood(self):
        self.assertEqual(parse_movement({"amount": 1, "direction": "credit"}, 0)[0].direction, DIRECTION_IN)
        self.assertEqual(parse_movement({"amount": 1, "direction": "debit"}, 0)[0].direction, DIRECTION_OUT)

    def test_a_label_and_date_are_carried_through(self):
        movement, _ = parse_movement(
            {"amount": 1, "direction": "in", "label": "INV-1", "date": "2026-04-01"}, 3
        )
        self.assertEqual(movement.label, "INV-1")
        self.assertEqual(movement.date, "2026-04-01")
        self.assertEqual(movement.index, 3)

    def test_reconcile_arithmetic_reports_an_unreadable_balance(self):
        outcome = reconcile_arithmetic(None, [], 0.0)
        self.assertFalse(outcome.inputs_usable)
        self.assertIsNone(outcome.closes)
        self.assertEqual(
            [f.code for f in outcome.findings if f.code == "opening_balance_not_numeric"],
            ["opening_balance_not_numeric"],
        )

    def test_reconcile_arithmetic_returns_none_when_movements_are_not_a_list(self):
        self.assertIsNone(reconcile_arithmetic(0.0, {}, 0.0))


if __name__ == "__main__":
    unittest.main()
