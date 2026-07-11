"""Administrator dashboard statistics and HTML row rendering."""

from __future__ import annotations

import html
import json
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

    def crawler_status(self, recent_limit: int = 30) -> dict:
        """Return aggregate crawler state without exposing a public endpoint."""
        if not self.posts_db.exists():
            return {"ok": False, "error": "posts database not found"}
        with connect_readonly(self.posts_db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            post_columns = {
                row[1] for row in conn.execute("pragma table_info(posts)")
            }
            status_sql = (
                "crawl_status" if "crawl_status" in post_columns else "'full'"
            )
            queue_status_sql = (
                "p.crawl_status" if "crawl_status" in post_columns else "'full'"
            )
            updated_sql = "p.updated_at" if "updated_at" in post_columns else "''"
            posts = dict(
                conn.execute(
                    f"select count(*) total, "
                    f"sum(({status_sql})='full') as full_count, "
                    f"sum(({status_sql})='list_only') as list_only_count, "
                    "min(nullif(create_time,'')) earliest, "
                    "max(nullif(create_time,'')) latest, "
                    "max(cast(id as integer)) max_id from posts"
                ).fetchone()
            )
            posts["full"] = posts.pop("full_count")
            posts["list_only"] = posts.pop("list_only_count")
            comments = dict(
                conn.execute(
                    "select count(*) total, count(distinct post_id) posts_with_comments, "
                    "max(nullif(create_time,'')) latest_comment_time from comments"
                ).fetchone()
            )
            queue_status = []
            queue_pending_priority = []
            queue_pending_age = {}
            queue_pending_crawl_status = []
            list_only_queue_coverage = {}
            if "crawler_queue" in tables:
                queue_status = [
                    dict(row)
                    for row in conn.execute(
                        "select status,count(*) n from crawler_queue "
                        "group by status order by status"
                    )
                ]
                queue_pending_priority = [
                    dict(row)
                    for row in conn.execute(
                        "select priority,reason,count(*) n from crawler_queue "
                        "where status='pending' group by priority,reason "
                        "order by priority,n desc"
                    )
                ]
                queue_pending_age = dict(
                    conn.execute(
                        "select count(*) n,min(created_at) oldest,max(created_at) newest "
                        "from crawler_queue where status='pending'"
                    ).fetchone()
                )
                queue_pending_crawl_status = [
                    dict(row)
                    for row in conn.execute(
                        f"select coalesce({queue_status_sql},'missing') crawl_status,"
                        "count(*) n from crawler_queue q left join posts p "
                        "on p.id=q.post_id where q.status='pending' "
                        f"group by coalesce({queue_status_sql},'missing') "
                        "order by n desc,crawl_status"
                    )
                ]
                list_only_queue_coverage = dict(
                    conn.execute(
                        f"select count(*) total,sum(exists(select 1 from "
                        "crawler_queue q where q.post_id=p.id and "
                        "q.status='pending')) pending from posts p "
                        f"where ({status_sql})='list_only'"
                    ).fetchone()
                )
            queue_join = (
                "left join crawler_queue q on q.post_id=p.id"
                if "crawler_queue" in tables
                else ""
            )
            queue_columns = (
                "q.status queue_status,q.priority,q.reason"
                if "crawler_queue" in tables
                else "null queue_status,null priority,null reason"
            )
            recent_posts = [
                dict(row)
                for row in conn.execute(
                    f"select p.id,p.create_time,{updated_sql} updated_at,"
                    f"{status_sql} crawl_status,p.comment_count,"
                    "(select count(*) from comments c where c.post_id=p.id) "
                    f"comment_rows,{queue_columns} from posts p {queue_join} "
                    "order by p.create_time desc,cast(p.id as integer) desc limit ?",
                    (max(1, min(int(recent_limit), 100)),),
                )
            ]
            crawl_state = []
            if "crawl_state" in tables:
                crawl_state = [
                    dict(row)
                    for row in conn.execute(
                        "select key,value,updated_at from crawl_state "
                        "where key like 'crawler_%' order by updated_at desc limit 20"
                    )
                ]
        quota = self._read_runtime_json(".crawler_quota.json")
        pause = self._read_runtime_json(".crawler_pause.json")
        return {
            "ok": True,
            "posts": posts,
            "comments": comments,
            "queue_status": queue_status,
            "queue_pending_priority": queue_pending_priority,
            "queue_pending_age": queue_pending_age,
            "queue_pending_crawl_status": queue_pending_crawl_status,
            "list_only_queue_coverage": list_only_queue_coverage,
            "recent_posts": recent_posts,
            "crawl_state": crawl_state,
            "quota": quota,
            "pause": pause,
            "database_bytes": self.posts_db.stat().st_size,
        }

    def _read_runtime_json(self, name: str) -> dict:
        path = self.posts_db.with_name(name)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

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
