"""AI retriever — natural-language query → keyword extraction → FTS bm25 ranking → top posts with matched comments.

Design:
  1. Strip question words / stop-words from the natural-language query.
  2. Segment (jieba if available, else character bigrams).
  3. Build FTS5 OR query → bm25() rank → keep top 100 candidates.
  4. Fetch post details + score comments by keyword hits → return top-N.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

# ── optional jieba ────────────────────────────────────────────────
try:
    import jieba

    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

# ── stop patterns / words stripped before keyword extraction ──────
_STOP_PATTERNS = [
    "请问", "有没有", "是不是", "怎么样", "如何", "什么", "怎么",
    "大家", "最近", "我想", "帮我", "可以", "能否", "哪些",
    "哪个", "哪里", "谁", "什么时候", "多少", "为什么",
    "？", "？", "。", "，", "！", "？", "：", "“", "”",
    "的", "了", "吗", "呢", "吧", "啊", "嗯", "哦",
    "很", "非常", "比较", "特别", "真的", "可能",
    "一下", "一些", "一个", "这个", "那个", "这些",
    "我", "你", "他", "她", "它",
]

# ── minimum CJK codepoint range for character filtering ───────────
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]{2,}")
_ALNUM_RE = re.compile(r"[a-zA-Z0-9]{2,}")


def _clean_query(text: str) -> str:
    for pat in _STOP_PATTERNS:
        text = text.replace(pat, " ")
    return text


def _segment(text: str) -> list[str]:
    """Return a deduplicated list of 2+-char tokens from *text*."""
    if _HAS_JIEBA:
        words = [w.strip() for w in jieba.cut(text) if len(w.strip()) >= 2]
    else:
        # character bigrams + trigrams
        compact = re.sub(r"\s+", "", text)
        words = []
        for i in range(len(compact) - 1):
            words.append(compact[i : i + 2])
        for i in range(max(0, len(compact) - 2)):
            words.append(compact[i : i + 3])

    # also keep alpha-numeric tokens
    words.extend(_ALNUM_RE.findall(text))
    # deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        w = w.lower()
        if w not in seen and len(w) >= 2:
            seen.add(w)
            out.append(w)
    return out


def extract_keywords(query: str) -> list[str]:
    """Public: natural-language query → list of meaningful keywords."""
    return _segment(_clean_query(query))[:12]


def build_fts_query(keywords: list[str]) -> str | None:
    """Build FTS5 OR match expression, e.g.  "kw1" OR "kw2" OR "kw3"."""
    if not keywords:
        return None
    parts = []
    for kw in keywords:
        safe = kw.replace('"', '""')
        parts.append(f'"{safe}"')
    return " OR ".join(parts)


# ── main retrieve ──────────────────────────────────────────────────


def retrieve_ai(
    query: str, db_path: str | Path, limit: int = 20
) -> list[dict]:
    """Return *limit* posts sorted by bm25 score, each with best matching comments.

    Returns list of::

        {
            "post": {
                "id", "content", "category", "user", "time",
                "comments_count", "stars"
            },
            "matched_comments": [
                {"detail": str, "user_name": str, "time": str, "is_publisher": int},
                ...
            ],
            "body_match_terms": list[str],
            "comment_match_count": int,
            "bm25_score": float,   # lower = better
        }
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    keywords = extract_keywords(query)
    fts_keywords = [kw for kw in keywords if len(kw) >= 3]
    fts_expr = build_fts_query(fts_keywords)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=on")
    try:
        has_fts = (
            conn.execute(
                "select 1 from sqlite_master where name='search_index' and type='table'"
            ).fetchone()
            is not None
        )

        fts_scores: dict[str, float] = {}
        matched_keywords: dict[str, set[str]] = {}
        body_matched_keywords: dict[str, set[str]] = {}
        candidate_ids: set[str] = set()

        # ── FTS path ──────────────────────────────────────────
        if has_fts and fts_expr:
            try:
                rows = conn.execute(
                    """
                    select post_id, min(score) as score
                    from (
                        select post_id, bm25(search_index, 0.0, 0.0, 10.0) as score
                        from search_index
                        where body match ?
                        order by score
                        limit 500
                    )
                    group by post_id
                    order by score
                    limit 300
                    """,
                    (fts_expr,),
                ).fetchall()
                for row in rows:
                    pid = str(row["post_id"])
                    candidate_ids.add(pid)
                    fts_scores[pid] = float(row["score"])
            except sqlite3.OperationalError:
                pass

        # FTS trigram cannot reliably recall common two-character Chinese words.
        # Add bounded lexical candidates from both post bodies and comments.
        for kw in keywords:
            like = f"%{kw}%"
            try:
                post_rows = conn.execute(
                    """select id from posts
                       where content like ? or id = ?
                       order by create_time desc limit 100""",
                    (like, kw),
                ).fetchall()
                comment_rows = conn.execute(
                    """select distinct post_id from comments
                       where detail like ?
                       order by create_time desc limit 100""",
                    (like,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in [*post_rows, *comment_rows]:
                pid = str(row[0])
                candidate_ids.add(pid)
                matched_keywords.setdefault(pid, set()).add(kw)

        if not candidate_ids:
            return []

        # ── fetch post details ────────────────────────────────
        candidate_list = list(candidate_ids)
        ph = ",".join("?" for _ in candidate_list)
        post_rows = conn.execute(
            f"""select id, content, category_name, user_name, create_time,
                       comment_count, star_count
                from posts
                where id in ({ph})""",
            candidate_list,
        ).fetchall()

        posts_by_id: dict[str, dict] = {}
        for r in post_rows:
            content = r["content"] or ""
            body_hits = {kw for kw in keywords if kw in content}
            body_matched_keywords[str(r["id"])] = body_hits
            matched_keywords.setdefault(str(r["id"]), set()).update(body_hits)
            posts_by_id[r["id"]] = {
                "id": r["id"],
                "content": content,
                "category": r["category_name"] or "",
                "user": r["user_name"] or "",
                "time": r["create_time"] or "",
                "comments_count": int(r["comment_count"] or 0),
                "stars": int(r["star_count"] or 0),
            }

        def combined_score(pid: str) -> tuple[float, str]:
            hits = matched_keywords.get(pid, set())
            # Longer terms carry more intent than generic two-character words.
            # Example: "电脑系统" must outweigh "专业" / "建议".
            lexical_weight = sum(max(1, len(term) - 1) ** 2 for term in hits)
            fts_score = fts_scores.get(pid, 0.0)
            create_time = posts_by_id.get(pid, {}).get("time", "")
            return (-100.0 * lexical_weight + fts_score, create_time)

        selected = sorted(
            posts_by_id,
            key=lambda pid: (combined_score(pid)[0], combined_score(pid)[1]),
            reverse=False,
        )[: max(1, limit)]
        # For equal relevance, prefer newer posts.
        selected.sort(
            key=lambda pid: combined_score(pid)[1],
            reverse=True,
        )
        selected.sort(key=lambda pid: combined_score(pid)[0])

        # ── comment scoring ───────────────────────────────────
        results: list[dict] = []
        for pid in selected:
            post = posts_by_id.get(pid)
            if post is None:
                continue

            predicates = " or ".join("detail like ?" for _ in keywords)
            params = [pid, *[f"%{kw}%" for kw in keywords]]
            count_row = conn.execute(
                f"""select count(*) as total
                    from comments
                    where post_id = ? and detail != ''
                      and ({predicates})""",
                params,
            ).fetchone()
            cmt_rows = conn.execute(
                f"""select detail, show_user_name, create_time, is_publisher
                    from comments
                    where post_id = ? and detail != ''
                      and ({predicates})
                    order by create_time desc limit 100""",
                params,
            ).fetchall()

            scored: list[tuple[int, dict]] = []
            for c in cmt_rows:
                detail = c["detail"] or ""
                hits = sum(1 for kw in keywords if kw in detail)
                if hits > 0:
                    scored.append(
                        (
                            hits,
                            {
                                "detail": detail[:300],
                                "user_name": c["show_user_name"] or "",
                                "time": c["create_time"] or "",
                                "is_publisher": int(c["is_publisher"] or 0),
                            },
                        )
                    )
            scored.sort(key=lambda x: x[0], reverse=True)

            results.append(
                {
                    "post": post,
                    "matched_comments": [sc[1] for sc in scored[:3]],
                    "body_match_terms": sorted(body_matched_keywords.get(pid, set())),
                    "comment_match_count": int(count_row["total"] or 0),
                    "bm25_score": combined_score(pid)[0],
                }
            )

        return results

    finally:
        conn.close()
