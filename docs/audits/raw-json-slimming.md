# raw_json 冗余字段精简

> 状态: **已完成**。生产 schema 已精简为 `comments.reply_comment_list`，原始 raw_json 不再保留。

## 结论

`comments` 表每行曾存储完整 API 原始评论 JSON（`raw_json`），平均 836 字节，225 万条评论合计 **1.75 GB**，占数据库 46%。

经分析，35 个字段中 **34 个是冗余的**：

| 类别 | 字段数 | 说明 |
|------|--------|------|
| 结构化列已覆盖 | 8 | detail, show_user_name, is_publisher 等 |
| 常量值 | 10 | community_id, is_top 等永不变的值 |
| 会话依赖 | 3 | is_mine, has_star, has_trace — 取决于 cookie |
| 冗余 ID | 3 | id, article_id, reply_comment_id |
| 媒体字段 | 4 | 头像 URL、图片 — UI 不渲染 |
| 冗余时间 | 2 | show_create_time, update_time |
| 帖子级聚合 | 2 | count_comment, count_star |
| 嵌套数据 | 1 | count_star, count_trace |
| **唯一保留** | **1** | **reply_comment_list**（嵌套回复，无法从结构化列重建） |

精简后 DB 从 ~3.8GB 降到 ~2.0GB，省了 46%。

## 当前保守策略

`reply_comment_list` 仍保留，因为 `flatten_comments()` 只扁平化两层。如果后续修正为递归处理所有层级，这个字段也可以删。
