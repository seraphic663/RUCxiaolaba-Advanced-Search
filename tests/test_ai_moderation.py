import json
import unittest
from unittest.mock import Mock, patch

import requests

from server import _moderate_ai_query, _validate_ai_query


def fake_response(payload, ok=True):
    response = Mock()
    response.ok = ok
    response.json.return_value = payload
    return response


class AIModerationTest(unittest.TestCase):
    def test_query_shape_validation_remains_local(self):
        self.assertFalse(_validate_ai_query("a")[0])
        self.assertFalse(_validate_ai_query("x" * 501)[0])
        self.assertTrue(_validate_ai_query("食堂最近怎么样")[0])

    @patch("server.requests.post")
    def test_model_allows_normal_campus_query(self, post):
        post.return_value = fake_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"allowed": True, "reason": "普通校园生活问题"},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )
        self.assertEqual(_moderate_ai_query("食堂最近怎么样"), (True, None))

    @patch("server.requests.post")
    def test_model_rejects_personal_information_hunting(self, post):
        post.return_value = fake_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"allowed": False, "reason": "涉及定位他人宿舍"},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )
        self.assertEqual(
            _moderate_ai_query("帮我找这个人住哪个宿舍"),
            (False, "涉及定位他人宿舍"),
        )

    @patch("server.requests.post")
    def test_invalid_or_failed_moderation_fails_closed(self, post):
        post.return_value = fake_response(
            {"choices": [{"message": {"content": '{"reason":"missing flag"}'}}]}
        )
        self.assertFalse(_moderate_ai_query("普通问题")[0])

        post.side_effect = requests.exceptions.SSLError("closed")
        self.assertEqual(
            _moderate_ai_query("普通问题"),
            (False, "安全审核服务暂时不可用"),
        )


if __name__ == "__main__":
    unittest.main()
