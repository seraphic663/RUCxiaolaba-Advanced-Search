import unittest

from app.ai.prompts import build_prompt


class AIPromptTest(unittest.TestCase):
    def test_only_exact_publisher_value_gets_label(self):
        retrieved = [
            {
                "post": {
                    "id": "1",
                    "content": "正文",
                    "category": "测试",
                    "user": "作者",
                    "time": "2026-06-01 00:00:00",
                    "comments_count": 2,
                    "stars": 0,
                },
                "matched_comments": [
                    {
                        "user_name": "楼主",
                        "detail": "作者回复",
                        "is_publisher": 1,
                    },
                    {
                        "user_name": "访客",
                        "detail": "普通回复",
                        "is_publisher": 2,
                    },
                ],
            }
        ]
        _, prompt = build_prompt(
            "测试",
            retrieved,
            context_limit=10,
            char_limit=6000,
        )
        self.assertIn("楼主 [楼主]: 作者回复", prompt)
        self.assertIn("访客: 普通回复", prompt)
        self.assertNotIn("访客 [楼主]", prompt)


if __name__ == "__main__":
    unittest.main()
