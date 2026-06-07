import unittest

from server import _ai_evidence_payload, _normalize_ai_answer


class AIResponseTest(unittest.TestCase):
    def test_structured_answer_and_evidence_share_the_same_citations(self):
        parsed = {
            "overview": "总体结论 [#100]，非法引用 [#999]。",
            "findings": [
                {
                    "title": "要点",
                    "detail": "来自正文和评论 [#101]",
                    "cited": ["101", "999"],
                }
            ],
            "caveat": "",
            "cited": ["100", "999"],
        }
        answer, cited = _normalize_ai_answer(parsed, {"100", "101"})
        self.assertEqual(cited, ["100", "101"])
        self.assertNotIn("999", answer["overview"])

        retrieved = [
            {
                "post": {
                    "id": post_id,
                    "content": f"post {post_id}",
                    "category": "test",
                    "user": "user",
                    "time": "2026-06-07 12:00:00",
                    "comments_count": 2,
                    "stars": 1,
                },
                "body_match_terms": ["正文"],
                "comment_match_count": 1,
                "matched_comments": [{"detail": "评论"}],
            }
            for post_id in ("100", "101", "102")
        ]
        stats, evidence = _ai_evidence_payload(retrieved, cited)
        self.assertEqual([post["id"] for post in evidence], cited)
        self.assertEqual(stats["candidate_posts"], 3)
        self.assertEqual(stats["cited_posts"], 2)
        self.assertEqual(stats["matched_comments"], 3)

    def test_legacy_summary_is_still_supported(self):
        answer, cited = _normalize_ai_answer(
            {"summary": "旧格式 [#100]", "cited": ["100"]}, {"100"}
        )
        self.assertEqual(answer["overview"], "旧格式 [#100]")
        self.assertEqual(cited, ["100"])


if __name__ == "__main__":
    unittest.main()
