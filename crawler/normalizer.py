"""Convert remote API payloads into the stable storage model."""

from __future__ import annotations

from crawler.config import COMMUNITY_ID
from storage.post_writer import extract_media_json, has_media_json, safe_int


def count_comment_nodes(comments: list[dict]) -> int:
    total = 0
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        total += 1
        replies = comment.get("reply_comment_list") or []
        if isinstance(replies, list):
            total += count_comment_nodes(replies)
    return total


def normalize_detail(
    post_id: str,
    data: dict,
) -> tuple[dict, list[dict]] | None:
    if str(data.get("community_id", "")) != str(COMMUNITY_ID):
        return None
    comments = data.get("comment_list", [])
    if not isinstance(comments, list):
        comments = []
    post = {
        "id": str(post_id),
        "content": (f"{data.get('title') or ''} {data.get('detail') or ''}".strip()),
        "media_json": extract_media_json(data),
        "category_name": data.get("category_name", ""),
        "user_name": data.get("show_user_name", ""),
        "show_user_id": data.get("show_user_id", ""),
        "real_user_id": data.get("real_user_id", 0),
        "create_time": data.get("create_time", ""),
        "comment_count": safe_int(data.get("count_comment")),
        "star_count": safe_int(data.get("count_star")),
        "trace_count": safe_int(data.get("count_trace")),
    }
    return post, comments


def validate_normalized_detail(
    post: dict,
    comments: list[dict],
) -> str | None:
    """Reject partial upstream payloads that would destroy good local data."""
    if (
        not str(post.get("content") or "").strip()
        and not has_media_json(post.get("media_json"))
    ):
        return "empty_content"
    declared = safe_int(post.get("comment_count"))
    returned = count_comment_nodes(comments)
    if declared > 0 and returned == 0:
        return "empty_comments"
    if declared > returned:
        return f"incomplete_comments:declared={declared},returned={returned}"
    return None
