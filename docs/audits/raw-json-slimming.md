# raw_json 冗余字段精简（历史迁移记录）

> 状态：本文记录已完成的库瘦身依据和结果，不是日常运行步骤。当前 schema 以 `storage/post_writer.py` 和 [数据模型](../architecture/data-model.md) 为准；恢复旧库时再参考 `tools/migrations/README.md` 的生命周期说明。

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
| 历史保留 | 1 | reply_comment_list，曾作为嵌套回复安全网 |

精简后 DB 从 ~3.8GB 降到 ~2.0GB，省了 46%。

## 当前策略

`flatten_comments()` 递归扁平化所有层级后，`reply_comment_list` 不再作为数据库列保留。展示层由后端按 `parent_comment_id` 递归组装 `children`。
