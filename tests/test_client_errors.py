from __future__ import annotations

import unittest
from unittest.mock import Mock

from crawler.client import MiniProgramClient


class ClientErrorMappingTest(unittest.TestCase):
    def test_upstream_login_required_is_cookie_expired(self) -> None:
        response = Mock()
        response.json.return_value = {"code": "7001", "message": "请先登录1"}
        client = MiniProgramClient("cookie")
        client.automatic_quota = None
        client.session.get = Mock(return_value=response)

        data, error = client.get("/article/article/lists", {"page": 1})

        self.assertIsNone(data)
        self.assertEqual(error, "cookie_expired")
