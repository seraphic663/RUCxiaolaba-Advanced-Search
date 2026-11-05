"""Read bounded, matching comment excerpts without identity metadata."""

from __future__ import annotations

import math
import re
import sqlite3
import threading
from pathlib import Path

from app.domain.ai_search import excerpt
from app.domain.search import bigram_query
from app.repositories.connections import connect_readonly


class AiEvidenceRepository:
    def __init__(self, posts_db: str | Path):
        self.posts_db = Path(posts_db)
        self._dataset_lock = threading.Lock()
        self._dataset_cache = None

    def dataset_info(self) -> dict:
        # All concurrent queries share one source timestamp scan. Invalidate on
        # database or WAL changes so a live writer cannot leave it stale.
        if not self.posts_db.exists():
            return {"latest_post_time": "", "scope": "saved historical user posts, not official/live data"}
        with self._dataset_lock:
            signature = []
            for path in (self.posts_db, Path(str(self.posts_db) + "-wal")):
                try:
                    stat = path.stat()
                    signature.append((stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    signature.append(None)
            if self._dataset_cache and self._dataset_cache[0] == signature:
                return dict(self._dataset_cache[1])
            result = self._dataset_info_uncached()
            self._dataset_cache = (signature, result)
            return dict(result)

    def _dataset_info_uncached(self) -> dict:
        with connect_readonly(self.posts_db) as conn:
            columns = {row[1] for row in conn.execute("pragma table_info(posts)")}
            latest = conn.execute("select max(create_time) from posts").fetchone()[0] if "create_time" in columns else ""
        return {"latest_post_time": str(latest or ""), "scope": "saved historical user posts, not official/live data"}

    def anchor_candidates(self, question: str, bigram_db: Path | None) -> list[str]:
        """Recover existing literal phrases from unsegmented Chinese questions.

        Counts use the existing sidecar, never the benchmark or a subject list.
        The full original question still governs relevance and answerability.
        """
        if not bigram_db or not Path(bigram_db).exists():
            return []
        runs = re.findall(r"[\u3400-\u9fff]{3,}", question)
        phrases = []
        for width in (6, 5, 4, 3):
            group = list(dict.fromkeys(run[i:i + width] for run in runs for i in range(len(run) - width + 1)))
            phrases.extend(group[:14])
        counts = {}
        try:
            with connect_readonly(self.posts_db, bigram_db) as conn:
                for phrase in phrases:
                    count = conn.execute(
                        "select count(distinct m.post_id) from bigram.search_bigram f "
                        "join bigram.search_rows m on m.row_id=f.rowid where f.tokens match ?",
                        (bigram_query(phrase),),
                    ).fetchone()[0]
                    if count:
                        counts[phrase] = count
        except sqlite3.Error:
            return []
        if not counts:
            return []
        best = max(counts, key=lambda phrase: len(phrase) ** 2 / math.log(2 + counts[phrase]))
        particles = set("的了呢吗呀啊哪什怎么何请问到底现在个想能可到里")
        subjects = [phrase for phrase in counts if len(phrase) == 3 and not particles.intersection(phrase)]
        core = max(subjects, key=lambda phrase: counts[phrase]) if subjects else best
        return list(dict.fromkeys([best, core]))

    def comments(self, post_id: str, terms: list[str], limit: int = 8) -> list[dict]:
        if not self.posts_db.exists():
            return []
        terms = list(dict.fromkeys(terms))[:24]
        with connect_readonly(self.posts_db) as conn:
            # Keep short answers even when the subject appears only in the post.
            # Prefer direct matches before the bounded per-thread scan, rather
            # than throwing away all replies without a repeated query keyword.
            order = " + ".join("(instr(lower(detail), lower(?)) > 0)" for _ in terms) or "0"
            rows = conn.execute(
                f"select comment_id, parent_comment_id, detail, create_time from comments "
                f"where post_id=? order by ({order}) desc, create_time desc, row_key limit 300",
                [post_id, *terms],
            ).fetchall()
            by_id = {str(row["comment_id"]): row for row in rows}
            parent_ids = {str(row["parent_comment_id"]) for row in rows if row["parent_comment_id"]} - by_id.keys()
            if parent_ids:
                placeholders = ",".join("?" for _ in parent_ids)
                for row in conn.execute(
                    f"select comment_id, detail from comments where post_id=? and comment_id in ({placeholders})",
                    [post_id, *sorted(parent_ids)],
                ):
                    by_id[str(row["comment_id"])] = row

        def score(row):
            text = str(row["detail"] or "").strip()
            hits = sum(term.casefold() in text.casefold() for term in terms if term)
            numeric_answer = bool(re.search(r"\d|[一二三四五六七八九十]点|不外借|押证", text))
            generic = text in {"谢谢", "谢谢！", "好的谢谢！", "谢", "蹲", "同问", "顶", "dd"}
            return (hits * 3 + numeric_answer * 2 - generic * 10, str(row["create_time"] or ""))

        selected = sorted(rows, key=score, reverse=True)[:limit]
        return [
            {"id": str(row["comment_id"] or ""), "parent_id": str(row["parent_comment_id"] or ""),
             "content": excerpt(row["detail"], terms, 600), "time": str(row["create_time"] or ""),
             "parent_content": str(by_id[str(row["parent_comment_id"])]["detail"] or "")[:600]
             if str(row["parent_comment_id"] or "") in by_id else ""}
            for row in selected if str(row["detail"] or "").strip()
        ]
