from __future__ import annotations

from crawler.service import CrawlerService


class DetailClient:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error

    def article(self, post_id):
        return self.payload, self.error


def payload(post_id: str, *, comments: int = 1, community_id: str = "4") -> dict:
    return {
        "community_id": community_id,
        "title": f"post {post_id}",
        "detail": "body",
        "show_user_name": "user",
        "create_time": "2026-09-01 10:00:00",
        "count_comment": comments,
        "comment_list": [
            {
                "id": f"comment-{post_id}-{index}",
                "detail": f"comment {index}",
            }
            for index in range(comments)
        ],
    }


def service() -> CrawlerService:
    return CrawlerService(db_path="posts.db", cookie="test", lock_timeout=1)


def test_detail_contract_returns_normalized_payload_for_valid_response():
    parsed, error = service().fetch_detail_with_error(
        DetailClient(payload("101")),
        "101",
    )

    assert error is None
    assert parsed is not None
    post, comments = parsed
    assert post["id"] == "101"
    assert post["content"] == "post 101 body"
    assert len(comments) == 1


def test_detail_contract_preserves_foreign_community_error():
    parsed, error = service().fetch_detail_with_error(
        DetailClient(payload("102", community_id="999")),
        "102",
    )

    assert parsed is None
    assert error == "foreign_or_invalid"


def test_detail_contract_keeps_partial_payload_for_safe_merge():
    partial = payload("103", comments=0)
    partial["count_comment"] = 1
    parsed, error = service().fetch_detail_with_error(
        DetailClient(partial),
        "103",
    )

    assert parsed is not None
    assert error == "suspicious_payload:empty_comments"
