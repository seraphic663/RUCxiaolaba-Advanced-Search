"""Build the small synthetic databases used by the public-page quick start.

The generated records are fictional. This module never reads ``data/posts.db``
or any other production data source.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from storage.bigram_index import build_bigram_index
from storage.post_writer import SQLitePostStore

POST_FIXTURES = [
    ("99000001", "图书馆期末周开放时间在哪里看？想找一个安静的自习区域。", "学习互助", "2026-01-03 09:15:00", 8, ["学校官网和图书馆公众号都会更新通知。", "楼上的阅览区通常更安静。"]),
    ("99000002", "求推荐校区附近的食堂窗口，最好有清淡一点的晚饭。", "日常投稿", "2026-01-05 18:20:00", 15, ["清淡口味可以试试二楼的示例窗口。", "番茄鸡蛋面也不错，关键词搜索可以搜到这条评论。"]),
    ("99000003", "数据库课程项目想做校园信息检索，有没有适合入门的 SQLite 资料？", "选课互助", "2026-01-08 14:30:00", 6, ["先从 SQLite 官方文档和 FTS5 示例开始。", "项目里可以同时比较 LIKE 和 Bigram。"]),
    ("99000004", "失物招领：教学楼捡到一把蓝色雨伞，请描述伞柄特征后认领。", "失物招领", "2026-01-10 12:05:00", 3, ["已转发到演示失物群。"]),
    ("99000005", "期末复习搭子招募，每晚七点在图书馆复习统计学。", "学习互助", "2026-01-12 16:45:00", 11, ["统计学复习搭子加一。", "图书馆见，先复习概率分布。"]),
    ("99000006", "二手闲置：转让一本干净的高等数学教材，仅用于演示分类筛选。", "二手闲置", "2026-01-15 11:10:00", 2, ["教材状态已确认。"]),
    ("99000007", "校园网今晚有点慢，切换网络后恢复了，大家可以先检查代理设置。", "日常投稿", "2026-01-18 21:05:00", 9, ["关闭代理后校园网恢复正常。", "也可以检查一下 DNS 设置。"]),
    ("99000008", "机器学习课程如何安排复习顺序？目前准备先看线性模型和决策树。", "选课互助", "2026-01-20 13:25:00", 7, ["建议先线性模型，再看树模型和集成学习。", "课程重点还是以老师讲义为准。"]),
    ("99000009", "周末想约羽毛球搭子，水平普通，运动时间可以再商量。", "日常投稿", "2026-01-23 10:00:00", 5, ["周日下午可以一起打球。"]),
    ("99000010", "食堂新窗口的番茄鸡蛋面不错，中午排队时间大约十分钟。", "日常投稿", "2026-01-26 12:40:00", 18, ["这个食堂窗口确实出餐很快。", "晚饭时间排队更短。"]),
    ("99000011", "求助：SQLite 的 FTS 和普通 LIKE 搜索分别适合什么场景？", "学习互助", "2026-01-28 17:30:00", 10, ["短词可以用 Bigram，单字查询回退 LIKE。", "先保证正确性，再做 benchmark。"]),
    ("99000012", "一月演示数据最后一帖：搜索、分类、排序和评论展开都可以测试。", "日常投稿", "2026-01-31 20:26:00", 4, ["这是完全虚构的演示评论。"]),
]


def demo_comments(post_id: str, create_time: str, bodies: list[str]) -> list[dict]:
    return [
        {
            "id": f"{post_id}-c-{index}",
            "detail": body,
            "show_user_name": "匿名用户",
            "show_user_id": "",
            "real_user_id": "0",
            "create_time": create_time,
            "is_publisher": 0,
        }
        for index, body in enumerate(bodies, start=1)
    ]


def build_posts_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with SQLitePostStore(path) as store:
        store.init_schema()
        for post_id, content, category, created, stars, bodies in POST_FIXTURES:
            comments = demo_comments(post_id, created, bodies)
            store.upsert_post(
                {
                    "id": post_id,
                    "content": content,
                    "category_name": category,
                    "user_name": "匿名用户",
                    "show_user_id": "",
                    "real_user_id": "0",
                    "create_time": created,
                    "comment_count": len(comments),
                    "star_count": stars,
                    "trace_count": 0,
                },
                comments,
                commit=False,
            )
        store.set_state("demo.synthetic", "true", commit=False)
        store.conn.execute("insert into search_index(search_index) values ('optimize')")
        store.conn.commit()
    with sqlite3.connect(path) as conn:
        conn.execute("pragma journal_mode=delete")
        conn.execute("vacuum")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="demo")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    posts_path = output_dir / "posts.db"
    bigram_path = output_dir / "bigram_index.db"
    build_posts_db(posts_path)
    stats = build_bigram_index(
        posts_path,
        bigram_path,
        source_label="demo/posts.db",
        built_at="2026-01-31T20:30:00",
    )
    print(f"built {posts_path} ({posts_path.stat().st_size:,} bytes)")
    print(f"built {bigram_path} ({bigram_path.stat().st_size:,} bytes)")
    print(f"synthetic posts={len(POST_FIXTURES)} search_rows={stats.rows}")


if __name__ == "__main__":
    main()
