import json
import unittest

from rag.benchmark import BAD_CASES, QUESTIONS, evidence_stages, load_cases


class BenchmarkTest(unittest.TestCase):
    def test_variant_must_reference_a_base_in_its_suite(self):
        original = load_cases(QUESTIONS.read_bytes())
        bad_cases = load_cases((BAD_CASES / "questions.json").read_bytes(), require_behavior=True)
        self.assertEqual(len(original), 30)

        variant = next(case for case in bad_cases if case.get("variant_of") == "Q04")
        with self.assertRaisesRegex(ValueError, "must reference a base case in the same suite"):
            load_cases(json.dumps([variant]).encode(), require_behavior=True)

    def test_recall_and_prefinal_cutoffs_report_distinct_missing_gold(self):
        stages = evidence_stages(
            {"found", "missed_in_recall", "dropped_at_cutoff"},
            {"vector": ["found", "dropped_at_cutoff"], "keyword": ["distractor", "found"]},
            ["found", "distractor", "dropped_at_cutoff"],
            top_k=2,
        )

        self.assertEqual(stages["candidate_recall"], 2 / 3)
        self.assertEqual(stages["prefinal_recall_at_k"], 1 / 3)
        self.assertEqual(stages["missing_from_candidates"], ["missed_in_recall"])
        self.assertEqual(stages["outside_prefinal_top_k"], ["dropped_at_cutoff"])
