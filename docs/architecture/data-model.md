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

2026-06-20 本地快照规模约为：

```text
posts:       544,993
comments:  2,252,543
大小:        约 1.8GB
```

该数字只描述仓库作者当时的本地快照，不代表 Railway 当前线上数量。

2026-09-04 本地只读审计快照约为：

```text
posts:                 545,429
comments:            2,254,450
searchable rows:     2,799,839
crawler_queue:             82
```

这组数字只描述当前 workspace 中 `data/posts.db` 的本地运行库，不代表 Railway 当前线上数量。sidecar 的生成时间和 `source_rows` 还必须单独核对，不能从主库行数推断 sidecar 已同步。

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
crawl_status    `list_only` 表示只有列表快照，`full` 表示详情已补全
list_update_time 最近一次列表更新时间
source_state    `available` 或 `deleted_or_unavailable`，与详情完整度分离
source_state_changed_at 最近一次源端可用状态变化时间
source_state_reason 状态证据，例如 `not_found`
source_observed_at 最近一次列表或详情观测时间
```

`comment_count` 保存最近一次源端报告值；`count(*) from comments` 是本站实际保存的评论/回复记录行数，两者允许不同。删帖后冻结最后一次观察值，Admin 同时展示源端值和本地保存行数。`crawl_status` 只描述本站是否抓到详情，不能替代 `source_state`。

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

用途：兼容的 FTS5 trigram 全文索引。当前两字及以上搜索优先使用独立 `bigram_index.db`，该表仍用于无 Bigram 环境下的长词回退。

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

## crawler 运行表

这些表由 `SQLitePostStore.ensure_runtime_schema()` 为旧数据库按需补齐：

| 表 | 用途 |
|---|---|
| `crawler_queue` | 保存待补详情帖子、`task_type` 路由、priority、reason、状态、列表/数据库评论数、重试信息和带 owner/token 的原子认领租约/lane |
| `post_id_ledger` | 保存 ID 首次/最近由 list1 或 list2 观察的来源、事件键、详情状态、详情时间线和帖子存在性 |
| `list2_observation_log` | 保存 list2 事件键，避免同一活跃观察重复触发详情 |
| `crawler_run_history` | 保存 scheduler/crawler 运行的开始、结束、来源请求和错误摘要；没有记录时不能据此证明最近没有运行 |
| `crawler_quarantine_posts` | 保存被可疑详情响应校验拒绝覆盖的帖子及原因，供人工复核 |

它们是 crawler 的持久运行状态，不等于帖子内容覆盖率。完整度需要同时查看 `posts.crawl_status`、`comments`、queue 状态和各命令统计。由 queue 认领的详情 worker 在完成、失败或重试时必须带回本次 claim token；租约过期后旧 worker 的终态写入应被拒绝，不能覆盖重新认领任务。

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

`SQLitePostStore.init_schema()` 当前会创建 admin 身份检索索引，包括：

```sql
create index if not exists idx_posts_show_user_id on posts(show_user_id);
create index if not exists idx_posts_real_user_id on posts(real_user_id);
create index if not exists idx_comments_show_user_id on comments(show_user_id);
create index if not exists idx_comments_real_user_id on comments(real_user_id);
```

旧库通过当前 `SQLitePostStore.init_schema()` 补齐这些索引；已删除的一次性旧索引脚本仍可从 Git 历史恢复。
