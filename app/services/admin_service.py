"""Administrator dashboard statistics and HTML row rendering."""

from __future__ import annotations

import html
from pathlib import Path

from app.repositories.connections import connect_readonly


class AdminService:
    def __init__(self, posts_db: str | Path):
        self.posts_db = Path(posts_db)

    def dashboard(self, limit: int = 40) -> dict:
        if not self.posts_db.exists():
            return {
                "total": 0,
                "unique_users": 0,
                "multi": 0,
                "total_comments": 0,
                "unique_commenters": 0,
                "user_rows": '<div class="no-data">SQLite DB not found.</div>',
            }
        with connect_readonly(self.posts_db) as conn:
            stats = conn.execute(
                """
                select
                  (select count(*) from posts) as total,
                  (select count(distinct show_user_id) from posts
                   where show_user_id != '') as unique_users,
                  (select count(*) from (
                       select show_user_id from posts where show_user_id != ''
                       group by show_user_id having count(*) >= 2
                   )) as multi,
                  (select count(*) from comments) as total_comments
                """
            ).fetchone()
            users = conn.execute(
                """
                select show_user_id, max(user_name) as user_name,
                       count(*) as post_count,
                       group_concat(distinct category_name) as categories
                from posts
                where show_user_id != ''
                group by show_user_id
                order by post_count desc
                limit ?
                """,
                (limit,),
            ).fetchall()
            user_ids = [user["show_user_id"] for user in users]
            posts_by_uid = {user_id: [] for user_id in user_ids}
            if user_ids:
                placeholders = ",".join("?" for _ in user_ids)
                rows = conn.execute(
                    f"""
                    select show_user_id, id, content, category_name,
                           create_time, star_count, comment_count
                    from posts
                    where show_user_id in ({placeholders})
                    order by show_user_id, create_time desc,
                             cast(id as integer) desc
                    """,
                    user_ids,
                ).fetchall()
                for post in rows:
                    bucket = posts_by_uid.get(post["show_user_id"])
                    if bucket is not None and len(bucket) < 12:
                        bucket.append(post)
        return {
            "total": stats["total"],
            "unique_users": stats["unique_users"],
            "multi": stats["multi"],
            "total_comments": stats["total_comments"],
            "unique_commenters": "按需检索",
            "user_rows": self._render_users(users, posts_by_uid),
        }

    @staticmethod
    def _render_users(users, posts_by_uid) -> str:
        rows = []
        for user in users:
            user_id = user["show_user_id"]
            name = html.escape(user["user_name"] or "?")
            categories = ", ".join((user["categories"] or "").split(",")[:5])
            details = []
            for post in posts_by_uid.get(user_id, []):
                details.append(
                    '<div class="post-item"><div class="post-meta-row">'
                    f'<span class="post-cat">[{html.escape(post["category_name"] or "?")}]</span> '
                    f'<span class="post-id">#{html.escape(post["id"])}</span> '
                    f'<span class="post-time">{html.escape((post["create_time"] or "")[:19])}</span> '
                    f'<span style="color:#666;font-size:0.8em;">L{post["star_count"]} C{post["comment_count"]}</span>'
                    '</div>'
                    f'<div class="post-content">{html.escape((post["content"] or "")[:300])}</div>'
                    '</div>'
                )
            escaped_id = html.escape(user_id, quote=True)
            data_text = html.escape(
                f'{user_id} {user["user_name"] or ""} {categories}',
                quote=True,
            )
            rows.append(
                "<div>"
                f'<div class="user-row" onclick="toggleUser(\'{escaped_id}\')" '
                f'data-text="{data_text}">'
                f'<div><span class="uid">ID:{html.escape(user_id)}</span>'
                f'<span class="uname">{name}</span>'
                f'<span class="cats">{html.escape(categories)}</span></div>'
                f'<span class="count">{user["post_count"]} post(s)</span></div>'
                f'<div class="user-detail" id="detail-{escaped_id}">'
                f'{"".join(details)}</div></div>'
            )
        return "\n".join(rows) if rows else (
            '<div class="no-data">No data with show_user_id found.</div>'
        )
