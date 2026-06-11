# API 参考

> 从 CLAUDE.md 提取，覆盖所有已知端点。Base URL: `https://ys.qimiaoyuanfen.com`，Community ID: `4`。

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
