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
posts:    543,962
comments: 2,249,518
大小:     约 1.6GB
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
show_user_head  头像 URL，当前 UI 基本不用
real_user_id    真实用户 ID，0 通常表示匿名
create_time     发帖时间
comment_count   评论数
star_count      点赞数
trace_count     蹲蹲数
views           浏览数
hot             小程序原始 hot 字段
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
reply_comment_list   从旧原始评论 JSON 中保留的嵌套回复列表安全网
updated_at           本库最后更新时间
```

## 表：search_index

用途：FTS5 trigram 全文索引。

```text
post_id
kind     post/comment
body     正文或评论文本
```

限制：

- 3 字及以上中文关键词更适合 trigram FTS。
- 1-2 字中文搜索通常回退 `LIKE`，会慢。

## 表：crawl_state

用途：记录爬虫状态或统计。

当前主要写入最近一次 `crawler_db` 的运行统计。

## 为什么不再保留 posts.comments_json

旧 CSV/完整 DB 里曾经在帖子表保存完整嵌套评论 JSON。现在已改成：

```text
comments 表结构化字段
comments.reply_comment_list 仅保留嵌套回复列表
```

这样可以减少重复存储，同时保留当前搜索、评论展示、admin 检索所需字段。

代价：

- 这不是 API 全字段无损归档；评论头像、图片、会话态字段等原始字段已不再保留。
- 如果以后要恢复新的原始 API 字段，可能需要重新爬取或从旧备份恢复。
- 当前网站功能不依赖完整整包 JSON。

## 索引与性能

已有索引方向：

```text
posts.create_time
posts.hot
posts.views
posts.star_count
posts.category_name
comments.post_id
comments.create_time
search_index FTS5
```

建议后续增加或确认：

```sql
create index if not exists idx_posts_show_user_id on posts(show_user_id);
create index if not exists idx_comments_show_user_id on comments(show_user_id);
```

admin 按用户聚合、ID 检索会受益。
