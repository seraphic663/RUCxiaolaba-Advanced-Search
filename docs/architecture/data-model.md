# SQLite 数据模型

## 主库

默认路径：

```text
data/posts.db
```

Railway 路径：

```text
/app/data/posts.db
```

当前本地库规模约：

```text
posts:       544,993
comments:  2,252,543
大小:        约 1.8GB
```

## 表：posts

用途：帖子主记录。

关键字段：

```text
id              帖子 ID，主键
content         标题 + 正文拼接后的搜索内容
category_name   分类/tag
user_name       展示昵称
show_user_id    匿名展示 ID
real_user_id    真实用户 ID，0 通常表示匿名
create_time     发帖时间
comment_count   评论数
star_count      点赞数
trace_count     蹲蹲数
updated_at      本库最后更新时间
```

## 表：comments

用途：评论和楼中楼回复，统一扁平化存储。

关键字段：

```text
row_key              本地唯一键
comment_id           评论 ID
post_id              所属帖子 ID
parent_comment_id    父评论 ID，空表示顶层评论
detail               评论正文
show_user_name       评论展示昵称
show_user_id         评论匿名展示 ID
real_user_id         评论真实 ID
reply_show_user_name 被回复人展示昵称
reply_show_user_id   被回复人展示 ID
is_publisher         是否楼主
create_time          评论时间
updated_at           本库最后更新时间
```

## 表：search_index

用途：兼容的 FTS5 trigram 全文索引。当前两字及以上搜索优先使用独立 `bigram_index.db`，该表仍用于无 Bigram 环境下的长词回退与 AI 检索。

```text
post_id
kind     post/comment
body     正文或评论文本
```

限制：

- Bigram 可用时，两字及以上中文关键词使用旁路索引。
- 单字中文搜索回退 `LIKE`，会慢。
- Bigram 不可用时，满足条件的长词可使用 trigram FTS，其余回退 `LIKE`。

## 表：crawl_state

用途：记录爬虫状态或统计。

当前主要写入最近一次 `crawler_db` 的运行统计。

## 为什么不再保留 posts.comments_json

旧 CSV/完整 DB 里曾经在帖子表保存完整嵌套评论 JSON。当前主模型是：

```text
comments 表结构化字段
comments.parent_comment_id 表示评论树层级
```

这样可以减少重复存储，同时保留当前搜索、评论展示、admin 检索所需字段。API 展示时递归组装 `children`，并暂时提供 `reply_comment_list` 兼容字段给旧前端逻辑。

代价：

- 这不是 API 全字段无损归档；评论头像、图片、会话态字段、浏览数和 hot 等原始字段已不再保留。
- 如果以后要恢复新的原始 API 字段，可能需要重新爬取或从旧备份恢复。
- 当前网站功能不依赖完整整包 JSON。

## 索引与性能

已有索引方向：

```text
posts.create_time
posts.star_count
posts.category_name
comments.post_id
comments.create_time
comments(post_id, create_time, row_key)
search_index FTS5
```

建议后续增加或确认：

```sql
create index if not exists idx_posts_show_user_id on posts(show_user_id);
create index if not exists idx_comments_show_user_id on comments(show_user_id);
```

admin 按用户聚合、ID 检索会受益。
