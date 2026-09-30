"""Tests for ``count_distinct`` and ``duplicate_report``.

Two rules under test here, both of which come from the same failure in practice:
a row count is not an entity count, and a suspected duplicate is a question for a
person, not an instruction to merge.
"""

import unittest

from mcp.server.mcpserver.exceptions import ToolError

from mcp_validation_server import count_distinct, duplicate_report
from mcp_validation_server.entities import (
    RECOMMENDED_ACTION,
    count_distinct_rows,
    find_duplicate_groups,
    normalise_key,
)

ROWS = [
    {"customer_id": "C-1", "name": "Acme Ltd", "postcode": "M5V 2T6"},
    {"customer_id": "C-1", "name": "Acme Ltd", "postcode": "M5V 2T6"},
    {"customer_id": "C-2", "name": "Beta Corp", "postcode": "K1A 0B1"},
]


class TestCountDistinct(unittest.TestCase):
    def test_distinct_is_lower_than_the_row_count(self):
        result = count_distinct(ROWS, "customer_id")
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["distinct_count"], 2)
        self.assertEqual(result["difference"], 1)

    def test_the_gap_is_broken_down_by_cause(self):
        rows = ROWS + [{"customer_id": "", "name": "No Key"}]
        result = count_distinct(rows, "customer_id")
        self.assertEqual(result["difference"], 2)
        self.assertEqual(result["difference_breakdown"]["from_repeated_keys"], 1)
        self.assertEqual(result["difference_breakdown"]["from_rows_with_blank_key"], 1)

    def test_all_unique_rows_agree_and_pass(self):
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        result = count_distinct(rows, "id")
        self.assertEqual(result["difference"], 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "clean")

    def test_repeated_keys_are_listed_with_their_row_indexes(self):
        result = count_distinct(ROWS, "customer_id")
        repeated = result["repeated_keys"][0]
        self.assertEqual(repeated["key"], "c-1")
        self.assertEqual(repeated["count"], 2)
        self.assertEqual(repeated["row_indexes"], [0, 1])

    def test_whitespace_and_case_are_normalised_by_default(self):
        rows = [{"id": "C-1"}, {"id": " c-1 "}, {"id": "c-1"}]
        self.assertEqual(count_distinct(rows, "id")["distinct_count"], 1)

    def test_case_sensitivity_can_be_requested(self):
        rows = [{"id": "C-1"}, {"id": "c-1"}]
        self.assertEqual(count_distinct(rows, "id", case_sensitive=True)["distinct_count"], 2)

    def test_an_integral_float_key_matches_its_integer_spelling(self):
        rows = [{"id": 1}, {"id": "1"}, {"id": 1.0}]
        self.assertEqual(count_distinct(rows, "id")["distinct_count"], 1)

    def test_nothing_beyond_whitespace_and_case_is_normalised(self):
        # Punctuation and accents are not touched: that would change what counts
        # as the same entity, which is a decision for a person.
        rows = [{"id": "ACME-1"}, {"id": "ACME 1"}, {"id": "ACMÉ-1"}]
        self.assertEqual(count_distinct(rows, "id")["distinct_count"], 3)


class TestBlankAndUnusableKeys(unittest.TestCase):
    def test_a_blank_key_is_never_counted_as_an_entity(self):
        rows = [{"id": ""}, {"id": "   "}, {"id": None}]
        result = count_distinct(rows, "id")
        self.assertEqual(result["distinct_count"], 0)
        self.assertEqual(result["rows_with_blank_key"], 3)

    def test_blank_key_rows_are_listed_not_dropped(self):
        rows = [{"id": "a"}, {"id": "", "note": "kept"}]
        result = count_distinct(rows, "id")
        self.assertEqual(len(result["blank_key_rows"]), 1)
        self.assertEqual(result["blank_key_rows"][0]["row"]["note"], "kept")

    def test_a_missing_key_column_is_a_blank_key_not_a_crash(self):
        result = count_distinct([{"other": 1}], "id")
        self.assertEqual(result["distinct_count"], 0)
        self.assertEqual(result["rows_with_blank_key"], 1)

    def test_a_row_that_is_not_an_object_is_held_back(self):
        result = count_distinct(["not an object", {"id": "a"}], "id")
        self.assertEqual(result["distinct_count"], 1)
        self.assertEqual(result["rows_with_blank_key"], 1)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("not_an_object", codes)

    def test_blank_keys_produce_a_finding(self):
        result = count_distinct([{"id": ""}], "id")
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("blank_key", codes)

    def test_empty_input_is_not_reported_as_a_pass(self):
        result = count_distinct([], "id")
        self.assertFalse(result["ok"])
        self.assertEqual(result["distinct_count"], 0)
        self.assertIn("no_rows", {finding["code"] for finding in result["findings"]})

    def test_guidance_names_the_number_to_report(self):
        result = count_distinct(ROWS, "customer_id")
        self.assertIn("use", result["guidance"].lower())
        self.assertIn("2", result["guidance"])

    def test_a_non_string_key_is_a_malformed_argument(self):
        with self.assertRaises(ToolError):
            count_distinct(ROWS, "")

    def test_a_non_list_rows_argument_is_rejected_as_input(self):
        result = count_distinct({"a": 1}, "id")
        self.assertEqual(result["verdict"], "inputs_rejected")


class TestNormaliseKey(unittest.TestCase):
    def test_blank_values_normalise_to_none(self):
        for blank in (None, "", "   ", "\t\n"):
            self.assertIsNone(normalise_key(blank), f"{blank!r} should be blank")

    def test_case_folding_is_optional(self):
        self.assertEqual(normalise_key("AbC"), "abc")
        self.assertEqual(normalise_key("AbC", case_sensitive=True), "AbC")

    def test_false_is_not_confused_with_the_string_false(self):
        self.assertEqual(normalise_key(False), "false")
        self.assertEqual(normalise_key("false"), "false")

    def test_zero_survives_as_a_key(self):
        self.assertEqual(normalise_key(0), "0")


class TestDuplicateReportNeverMerges(unittest.TestCase):
    def test_the_payload_states_that_nothing_was_merged(self):
        result = duplicate_report([{"id": "a"}, {"id": "a"}], "id")
        self.assertFalse(result["auto_merged"])
        self.assertTrue(result["nothing_was_merged_or_deleted"])
        for group in result["groups"]:
            self.assertFalse(group["auto_merged"])
            self.assertTrue(group["decision_required"])
            self.assertEqual(group["recommended_action"], RECOMMENDED_ACTION)

    def test_every_group_asks_a_question_a_person_can_answer(self):
        result = duplicate_report([{"id": "a"}, {"id": "a"}], "id")
        question = result["groups"][0]["review_question"]
        self.assertTrue(question.endswith("?"))
        self.assertIn("'a'", question)

    def test_all_members_are_returned_with_their_rows_and_locators(self):
        rows = [{"id": "a", "n": 1}, {"id": "b"}, {"id": "a", "n": 3}]
        result = duplicate_report(rows, "id")
        members = result["groups"][0]["members"]
        self.assertEqual([m["locator"]["row_number"] for m in members], [1, 3])
        self.assertEqual(members[0]["row"]["n"], 1)
        self.assertEqual(members[1]["row"]["n"], 3)

    def test_guidance_tells_the_model_not_to_resolve_it(self):
        result = duplicate_report([{"id": "a"}, {"id": "a"}], "id")
        self.assertIn("Nothing has been merged", result["guidance"])
        self.assertIn("do not", result["guidance"].lower())

    def test_clean_input_passes(self):
        result = duplicate_report([{"id": "a"}, {"id": "b"}], "id")
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "clean")
        self.assertEqual(result["groups"], [])

    def test_no_rows_is_not_reported_as_a_pass(self):
        result = duplicate_report([], "id")
        self.assertFalse(result["ok"])
        self.assertIn("no_rows", {finding["code"] for finding in result["findings"]})


class TestDuplicateKinds(unittest.TestCase):
    def test_the_same_key_on_several_rows_is_reported(self):
        rows = [{"id": "a"}, {"id": "b"}, {"id": "a"}]
        result = duplicate_report(rows, "id")
        self.assertEqual(result["groups"][0]["kind"], "same_key")
        self.assertEqual(result["groups"][0]["size"], 2)
        self.assertEqual(result["distinct_keys"], 2)

    def test_different_keys_with_an_identical_signature_are_reported(self):
        rows = [
            {"id": "C-1", "name": "Acme Ltd", "postcode": "M5V 2T6"},
            {"id": "C-2", "name": "Acme Ltd", "postcode": "M5V 2T6"},
        ]
        result = duplicate_report(rows, "id", match_fields=["name", "postcode"])
        kinds = {group["kind"] for group in result["groups"]}
        self.assertIn("same_signature", kinds)
        group = next(g for g in result["groups"] if g["kind"] == "same_signature")
        self.assertEqual(group["keys"], ["c-1", "c-2"])

    def test_near_matches_are_only_reported_when_a_threshold_is_given(self):
        rows = [
            {"id": "C-1", "name": "Acme Ltd", "postcode": "M5V 2T6"},
            {"id": "C-2", "name": "Acme Limited", "postcode": "M5V 2T6"},
        ]
        without = duplicate_report(rows, "id", match_fields=["name", "postcode"])
        self.assertNotIn("near_match", {g["kind"] for g in without["groups"]})

        with_threshold = duplicate_report(
            rows, "id", match_fields=["name", "postcode"], similarity_threshold=0.8
        )
        kinds = {g["kind"] for g in with_threshold["groups"]}
        self.assertIn("near_match", kinds)

    def test_a_near_match_records_its_score(self):
        rows = [
            {"id": "C-1", "name": "Acme Ltd"},
            {"id": "C-2", "name": "Acme Limited"},
        ]
        result = duplicate_report(rows, "id", match_fields=["name"], similarity_threshold=0.8)
        self.assertAlmostEqual(result["groups"][0]["similarity"], 0.8, places=4)

    def test_a_dissimilar_pair_is_not_reported_even_with_a_threshold(self):
        rows = [{"id": "C-1", "name": "Acme Ltd"}, {"id": "C-2", "name": "Beta Corp"}]
        result = duplicate_report(rows, "id", match_fields=["name"], similarity_threshold=0.8)
        self.assertEqual(result["groups"], [])

    def test_a_missing_signature_field_does_not_create_a_match(self):
        rows = [{"id": "C-1"}, {"id": "C-2"}]
        result = duplicate_report(rows, "id", match_fields=["name"])
        self.assertEqual(result["groups"], [])

    def test_match_fields_are_not_used_when_omitted(self):
        rows = [
            {"id": "C-1", "name": "Acme Ltd"},
            {"id": "C-2", "name": "Acme Ltd"},
        ]
        result = duplicate_report(rows, "id")
        self.assertEqual(result["groups"], [])

    def test_rows_with_a_blank_key_are_held_back_and_listed(self):
        rows = [{"id": ""}, {"id": "a"}, {"id": "a"}]
        result = duplicate_report(rows, "id")
        self.assertEqual(len(result["blank_key_rows"]), 1)
        self.assertEqual(result["duplicate_group_count"], 1)


class TestGroupCaps(unittest.TestCase):
    def test_a_group_cap_is_reported_rather_than_applied_silently(self):
        rows = [{"id": "a"}, {"id": "a"}, {"id": "b"}, {"id": "b"}, {"id": "c"}, {"id": "c"}]
        result = duplicate_report(rows, "id", max_groups=1)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["duplicate_group_count"], 3)
        self.assertEqual(result["groups_omitted"], 2)
        codes = {finding["code"] for finding in result["findings"]}
        self.assertIn("groups_truncated", codes)

    def test_members_beyond_the_per_group_cap_are_counted_not_hidden(self):
        rows = [{"id": "a"}] * 30
        outcome = find_duplicate_groups(rows, "id", max_members_per_group=5)
        group = outcome.groups[0]
        self.assertEqual(len(group.members), 5)
        self.assertEqual(group.size, 30)
        self.assertEqual(group.to_dict()["members_omitted"], 25)


class TestMalformedArguments(unittest.TestCase):
    def test_a_blank_key_is_rejected(self):
        with self.assertRaises(ToolError):
            duplicate_report([{"id": "a"}], "  ")

    def test_a_non_string_match_fields_entry_is_rejected(self):
        with self.assertRaises(ToolError):
            duplicate_report([{"id": "a"}], "id", match_fields=["name", 7])

    def test_an_out_of_range_threshold_is_rejected(self):
        with self.assertRaises(ToolError):
            duplicate_report([{"id": "a"}], "id", match_fields=["name"], similarity_threshold=1.5)

        with self.assertRaises(ToolError):
            duplicate_report([{"id": "a"}], "id", match_fields=["name"], similarity_threshold=0)

    def test_a_non_list_rows_argument_is_rejected_as_input(self):
        result = duplicate_report({"a": 1}, "id")
        self.assertEqual(result["verdict"], "inputs_rejected")

    def test_find_duplicate_groups_rejects_a_non_list_rows_argument(self):
        with self.assertRaises(TypeError):
            find_duplicate_groups({"a": 1}, "id")


class TestUnderlyingOutcomeObjects(unittest.TestCase):
    def test_distinct_outcome_exposes_its_counts(self):
        outcome = count_distinct_rows(ROWS, "customer_id")
        self.assertEqual(outcome.row_count, 3)
        self.assertEqual(outcome.distinct_count, 2)
        self.assertEqual(outcome.difference, 1)
        self.assertEqual(outcome.difference_from_repeated_keys, 1)
        self.assertEqual(outcome.difference_from_blank_keys, 0)
        self.assertEqual(outcome.verdict, "review_required")

    def test_duplicate_outcome_exposes_its_groups(self):
        outcome = find_duplicate_groups([{"id": "a"}, {"id": "a"}], "id")
        self.assertEqual(outcome.group_count, 1)
        self.assertEqual(outcome.rows_in_groups, 2)
        self.assertEqual(outcome.verdict, "review_required")


if __name__ == "__main__":
    unittest.main()
