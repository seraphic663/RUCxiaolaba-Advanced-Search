# API 参考

> 本页是基于现有 Client 和历史正常小程序流量整理的端点参考。Base URL 与 Community ID 以 `crawler/client.py` 当前配置为准；本页不作为 crawler 调度参数的事实源。

> 本页记录通过正常小程序流量观察到的内部业务接口，仅用于理解现有代码，不代表平台将其作为第三方开放 API，也不构成调用授权。正式帖子爬虫只使用文章列表、活跃列表和文章详情接口。私信、账户资料、手机号检查及写操作端点不得在没有明确授权时调用。

## 本站搜索 API

`GET /api/search` 保持原分页参数，并支持以下可选游标参数：

| 参数 | 说明 |
|---|---|
| `cursor=1` | 允许慢速 LIKE 查询按页扫描 |
| `scan_offset` | 已扫描候选位置，第一页为 `0` |
| `matched_before` | 前面页面累计命中数量 |

Bigram/trigram 等快速查询仍返回 `pagination_mode=numbered`。单字 LIKE 和复杂 Admin 查询返回 `pagination_mode=cursor`，附带：

```json
{
  "candidate_total": 544993,
  "scanned": 16654,
  "matched_so_far": 50,
  "next_offset": 16654,
  "has_more": true,
  "total_exact": false
}
```

候选总数是在分类、日期、匿名/实名等非文本条件过滤后统计的。游标模式先按时间、点赞、评论或综合排序，再扫描关键词，因此返回页面顺序与完整查询一致。

### Admin 上游预览与人工现爬

以下接口仅接受有效管理员会话。两个 POST 接口还要求一次性 `X-CSRF-Token`，令牌由后台页面及每次 POST 响应返回。

| 接口 | 方法 | 用途 |
|---|---|---|
| `/api/admin/upstream-preview` | POST | 从 `search`、`lists` 或 `lists2` 获取最多 3 页候选，只保存 10 分钟预览，不写帖子库 |
| `/api/admin/live-crawl` | POST | 对本次预览中勾选的最多 10 个帖子执行 `smart`、`force` 或 `queue` |
| `/api/admin/live-crawl?id=任务号` | GET | 查询逐帖状态；不会向 public 页面暴露 |
| `/api/admin/crawl-status` | GET | 只读聚合 full/list_only、评论、队列、quota/pause 和最近帖子状态 |

`smart` 和 `force` 创建任务后立即由单线程 worker 串行拉取并逐帖保存；`queue` 只写最高优先级候选，不调用详情 API。候选 ID 必须属于未过期的服务端预览，客户端不能提交任意批量 ID。

## 认证

```
Cookie: ys7_ysxy_session=...
User-Agent: MicroMessenger MiniProgramEnv
Referer: https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html
```

## 响应格式

```json
{"code": "0000", "message": "NO Message", "data": {...}}
```

| code | 含义 |
|------|------|
| 0000 | 成功 |
| 1000 | Cookie 过期 |
| 0100 | 参数错误/功能禁用 |
| 0102 | 帖子不存在或已下架 |

## 文章列表

| 端点 | 参数 | 说明 |
|------|------|------|
| `/article/article/lists` | community_id, page | 主列表（时间倒序），~4 天深度 |
| `/article/article/lists2` | community_id, page | 活跃列表，与 lists 高度重叠 |
| `/article/article/lists3` | community_id, page | 用户个人发帖历史（依赖 Cookie） |
| `/article/article/lists4` | community_id, page | 小列表，仅 2 页 |
| `/article/article/lists5` | community_id, page | 小列表，仅 1 页 |

## 热门

| 端点 | 参数 | 说明 |
|------|------|------|
| `/article/article/datehot` | community_id, page | 当日热帖 |
| `/article/article/dayhot` | community_id, page | 日热榜 |
| `/article/article/weekhot` | community_id, page | 周热榜 |
| `/article/article/monthhot` | community_id, page | 月热榜 |
| `/article/article/yearhot` | community_id, page | 年热榜 |
| `/article/article/totalhot` | community_id, page | 总热榜 |

## 文章详情与搜索

| 端点 | 参数 | 说明 |
|------|------|------|
| `/article/article/info` | community_id, id | 单帖详情（含完整评论树） |
| `/article/article/search` | community_id, keyword, page | 搜索（单字搜索禁用） |
| `/article/article/userarticles` | community_id, show_user_id, page | 指定用户所有帖子，**跨年度** |
| `/article/article/categoryarticles` | community_id, category_id, page | 按分类浏览 |

## 分类

| 端点 | 说明 |
|------|------|
| `/article/category/lists` | 分类列表（6 个分类） |

## 用户/个人信息（依赖 Cookie）

| 端点 | 说明 |
|------|------|
| `/ysxy/user/my` | 当前用户完整信息 |
| `/ysxy/user/personal?uid=X` | 查看其他用户主页 |
| `/ysxy/user/mystat` | 当前用户统计 |
| `/friend/chat/count` | 聊天未读数 |
| `/message/message/count` | 私信未读数 |
| `/message/message/lists` | 私信列表（含内容、收发方 ID） |
| `/base/user/checkPhone` | 检查手机号 |

## 其他

| 端点 | 说明 |
|------|------|
| `/base/community/info` | 社区信息 |
| `/article/article/star` | 点赞 |
| `/article/article/trace` | 关注 |
| `/article/article_comment/comment` | 发评论 |
| `/article/article_comment/star` | 评论点赞 |
| `/article/article/checkauth` | 检查发帖权限 |
| `/article/for_image/lists` | 带图帖列表 |
