"""AI input sanitization and evidence/citation validation."""

from __future__ import annotations

import re


PII_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "<PHONE>"),
    (re.compile(r"\d{3}-\d{4}-\d{4}"), "<PHONE>"),
    (re.compile(r"\d{17}[\dXx]"), "<ID_NUM>"),
    (
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "<EMAIL>",
    ),
    (re.compile(r"\b\d{10,12}\b"), "<STUDENT_ID>"),
]


def scrub_pii(text: str) -> str:
    for pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def validate_query(query: str) -> tuple[bool, str | None]:
    value = query.strip()
    if not value or len(value) < 2:
        return False, "请输入至少两个字的搜索内容"
    if len(value) > 500:
        return False, "搜索内容过长，请精简至500字以内"
    return True, None


def verify_cited_ids(raw_cited: list, allowed_ids: set[str]) -> list[str]:
    if not isinstance(raw_cited, list):
        return []
    return list(
        dict.fromkeys(
            str(cited_id)
            for cited_id in raw_cited
            if str(cited_id) in allowed_ids
        )
    )


def sanitize_summary_citations(summary: str, allowed_ids: set[str]) -> str:
    return re.sub(
        r"\[#(\d+)\]",
        lambda match: match.group(0) if match.group(1) in allowed_ids else "",
        summary,
    )


def normalize_answer(
    parsed: dict,
    allowed_ids: set[str],
) -> tuple[dict, list[str]]:
    overview = str(parsed.get("overview") or parsed.get("summary") or "")[:1800]
    overview = sanitize_summary_citations(overview, allowed_ids)
    findings = []
    raw_findings = parsed.get("findings", [])
    if isinstance(raw_findings, list):
        for item in raw_findings[:6]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "相关发现")[:80]
            detail = sanitize_summary_citations(
                str(item.get("detail") or "")[:1000], allowed_ids
            )
            item_cited = verify_cited_ids(item.get("cited", []), allowed_ids)
            inline = re.findall(r"\[#(\d+)\]", f"{title}\n{detail}")
            item_cited = verify_cited_ids([*item_cited, *inline], allowed_ids)
            if detail:
                findings.append(
                    {"title": title, "detail": detail, "cited": item_cited}
                )
    caveat = sanitize_summary_citations(
        str(parsed.get("caveat") or "")[:800], allowed_ids
    )
    all_raw_cited = parsed.get("cited", [])
    if not isinstance(all_raw_cited, list):
        all_raw_cited = []
    inline_all = re.findall(
        r"\[#(\d+)\]",
        "\n".join([overview, caveat, *[item["detail"] for item in findings]]),
    )
    finding_cited = [cited for item in findings for cited in item["cited"]]
    cited = verify_cited_ids(
        [*all_raw_cited, *finding_cited, *inline_all], allowed_ids
    )
    return {
        "overview": overview,
        "findings": findings,
        "caveat": caveat,
    }, cited


def evidence_payload(
    retrieved: list[dict],
    cited: list[str],
    *,
    context_limit: int,
) -> tuple[dict, list[dict]]:
    by_id = {str(item["post"]["id"]): item for item in retrieved}
    evidence_posts = []
    for post_id in cited:
        item = by_id.get(post_id)
        if not item:
            continue
        post = item["post"]
        evidence_posts.append(
            {
                "id": post["id"],
                "content": post["content"],
                "category": post["category"],
                "user": post["user"],
                "time": post["time"],
                "comments": post["comments_count"],
                "stars": post["stars"],
                "body_match_terms": item.get("body_match_terms", []),
                "comment_match_count": item.get("comment_match_count", 0),
                "matched_comments": item.get("matched_comments", []),
            }
        )
    stats = {
        "candidate_posts": len(retrieved),
        "context_posts": min(len(retrieved), context_limit),
        "body_matched_posts": sum(
            1 for item in retrieved if item.get("body_match_terms")
        ),
        "comment_matched_posts": sum(
            1 for item in retrieved if item.get("comment_match_count", 0) > 0
        ),
        "matched_comments": sum(
            int(item.get("comment_match_count", 0)) for item in retrieved
        ),
        "cited_posts": len(evidence_posts),
    }
    return stats, evidence_posts
