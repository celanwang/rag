import unittest

from rag.answer_score import evaluate


class AnswerScoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_scores_only_actual_authorized_context_and_counts_other_outcomes(self):
        block = {"evidence_id": "allowed", "heading": "Section", "text": "The limit is 8.",
                 "excerpts": ["The limit is 8."]}
        rows = []
        for name in ("scored", "empty", "failed", "unauthorized", "no-reference"):
            rows.append({"case": {"id": name, "split": "development",
                                  "reference_answer": "The limit is 8." if name != "no-reference" else None},
                         "strategy": "fixed", "trial": 1,
                         "checks": {"authorization": name != "unauthorized", "citations": True, "policy": True},
                         "answer": {"context": [block], "claims": [] if name == "empty" else [{"text": block["text"]}]}})
        calls = []

        async def scorer(metric, case, answer, response):
            self.assertEqual(answer["context"], [block])
            self.assertEqual(response, block["text"])
            calls.append((case["id"], metric))
            if case["id"] == "failed":
                raise TimeoutError
            return 0.75

        results = await evaluate({"dataset_sha256": "test", "results": rows}, scorer)
        self.assertEqual(results["summary"], {"faithfulness": {"scored": 2, "skipped": 2, "failed": 1},
                                              "factual_correctness": {"scored": 1, "skipped": 3, "failed": 1}})
        self.assertNotIn("unauthorized", {name for name, _ in calls})
        self.assertEqual(results["results"][2]["scores"]["faithfulness"]["reason"], "TimeoutError")
        self.assertEqual(results["results"][4]["scores"]["factual_correctness"]["reason"], "no_reviewed_reference")
