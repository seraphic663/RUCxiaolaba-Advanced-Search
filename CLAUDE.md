# RUCxiaolaba-Advanced-Search

RUC小喇叭（中国人民大学匿名论坛）第三方搜索工具。含爬虫、Web 搜索界面、管理面板、API 逆向分析。

## 项目结构

```
spider.py           - 基础爬虫 → data/posts_full.csv（不含 show_user_id）
spider_danger.py    - 完整爬虫 → data/posts_danger.csv（含 show_user_id/real_user_id + 断点续爬）
crawl_detail.py     - 辅助：为列表 CSV 中缺失详情的帖子补抓详情
server.py           - Web 搜索 + Admin 面板（端口 8080）
mitm_filter.py      - mitmproxy 抓包插件（捕获微信小程序 API 请求）
test_api.py         - API 端点快速测试
captured_requests.jsonl - 抓包原始数据（2,691 条 API 响应，已在 .gitignore）
data/
  config.txt        - Cookie（ys7_ysxy_session=...）
  posts_danger.csv  - 完整数据（14 字段，含 show_user_id/real_user_id/comments_json）
  posts_full.csv    - 旧版数据（11 字段，不含持久化 ID）
  posts_danger_list.csv - 列表爬取中间数据（9 字段，无详情）
  .crawl_checkpoint.json    - spider_danger.py 断点
  .detail_checkpoint.json   - crawl_detail.py 断点
CLAUDE.md           - 本文件
隐私泄露报告.md      - 匿名机制分析报告（结论：匿名模式有效）
```

## API 架构

### 基础信息

- **Base URL**: `https://ys.qimiaoyuanfen.com`
- **Community ID**: `4`（RUC 社区）
- **认证**: Cookie `ys7_ysxy_session`（从微信小程序抓包获取）
- **User-Agent**: 微信小程序环境（`MicroMessenger MiniProgramEnv`）
- **Referer**: `https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html`

### 响应格式

```json
{"code": "0000", "message": "NO Message", "data": {...}}
```

- `0000` = 成功
- `1000` = Cookie 过期
- `0100` = 参数错误/功能禁用
- `0102` = 帖子不存在或已下架

### 已知端点清单

#### 文章列表类

| 端点 | 方法 | 参数 | 说明 |
|------|------|------|------|
| `/article/article/lists` | GET | community_id, page | 主列表（时间倒序），~4 天深度，~200 页 |
| `/article/article/lists2` | GET | community_id, page | 第二列表，与 lists 高度重叠，~4 天深度 |
| `/article/article/lists3` | GET | community_id, page | 用户个人发帖历史（依赖 Cookie），跨年度 |
| `/article/article/lists4` | GET | community_id, page | 小列表，仅 2 页 |
| `/article/article/lists5` | GET | community_id, page | 小列表，仅 1 页 |

#### 热门/时间类

| 端点 | 参数 | 说明 |
|------|------|------|
| `/article/article/datehot` | community_id, page | 当日热帖，10 条/页 |
| `/article/article/dayhot` | community_id, page | 日热榜，20 条/页 |
| `/article/article/weekhot` | community_id, page | 周热榜 |
| `/article/article/monthhot` | community_id, page | 月热榜 |
| `/article/article/yearhot` | community_id, page | 年热榜 |
| `/article/article/totalhot` | community_id, page | 总热榜 |

#### 文章详情与搜索

| 端点 | 参数 | 说明 |
|------|------|------|
| `/article/article/info` | community_id, id | 单帖详情（含完整评论树） |
| `/article/article/search` | community_id, keyword, page | 搜索（代码 0100，单字搜索禁用） |
| `/article/article/userarticles` | community_id, show_user_id, page | 指定用户的所有帖子，**跨年度** |
| `/article/article/categoryarticles` | community_id, category_id, page | 按分类浏览 |

#### 分类

| 端点 | 说明 |
|------|------|
| `/article/category/lists` | 分类列表（6 个分类：失物招领、日常投稿、二手闲置、吃瓜爆料、恋爱交友、选课互助） |

#### 用户/个人信息类（依赖 Cookie，返回当前登录用户数据）

| 端点 | 说明 |
|------|------|
| `/ysxy/user/my` | 当前用户完整信息（uid, name, phone, gender, birthday, head_img...） |
| `/ysxy/user/personal?uid=X` | 查看其他用户主页 |
| `/ysxy/user/mystat` | 当前用户统计（post_num, comment_num, points_num） |
| `/friend/chat/count` | 聊天未读数 |
| `/message/message/count` | 私信未读数 |
| `/message/message/lists` | 私信列表（含内容、收发方 ID） |
| `/base/user/checkPhone` | 检查手机号 |

#### 其他

| 端点 | 说明 |
|------|------|
| `/base/community/info` | 社区信息 |
| `/article/article/star` | 点赞 |
| `/article/article/trace` | 关注 |
| `/article/article_comment/comment` | 发评论 |
| `/article/article_comment/star` | 评论点赞 |
| `/article/article/checkauth` | 检查发帖权限 |
| `/article/for_image/lists` | 带图帖列表 |

### 数据模型

#### 文章对象（来自 lists/lists2/info）

```json
{
  "id": "<article_id>",         // 文章 ID（递增数字）
  "title": "",
  "detail": "热哭了。。",
  "category_id": "<category_id>",
  "category_name": "日常投稿",
  "show_user_id": "<show_user_id>", // ★ 持久化用户标识（匿名帖为随机值，非匿名帖为固定值）
  "show_user_name": "某同学",    // 显示名（匿名帖随机生成，非匿名帖固定）
  "show_user_head": "https://...",
  "real_user_id": 0,            // 真实用户 ID（匿名帖=0，非匿名帖=show_user_id）
  "create_time": "<create_time>",
  "views": 123,
  "count_comment": 0,
  "count_star": 0,
  "count_trace": 0,
  "hot": 0,
  "comment_list": [...]
}
```

#### 评论对象（来自 comment_list / reply_comment_list）

```json
{
  "id": "<comment_id>",
  "article_id": "<article_id>",
  "detail": "<comment_text>",
  "show_user_id": "<show_user_id>",    // 评论者持久 ID
  "show_user_name": "某同学1",
  "reply_show_user_id": "<reply_show_user_id>", // 被回复者持久 ID
  "reply_show_user_name": "某同学ncwkJdHr",
  "reply_comment_list": [...]          // 嵌套回复
}
```

### 匿名机制实测结论

| 发帖模式 | show_user_id | show_user_name | real_user_id | 跨帖可关联 |
|----------|-------------|---------------|-------------|-----------|
| 匿名 | 每次随机 | 每次随机 | 始终 0 | **否** |
| 非匿名 | 持久不变 | 固定 | = show_user_id | **是**（用户主动选择） |

**匿名有效**。匿名帖之间无法通过 API 数据关联。非匿名帖的持久 ID 是用户主动行为，非漏洞。

## 爬虫设计

### spider_danger.py

- 只爬 `/article/article/lists`（主列表）
- 随机延迟 0.5-1.5s（列表）/ 1.0-2.5s（详情）
- 断点续爬：每页保存 checkpoint，中断后自动续爬
- 两阶段：先爬列表（存 posts_danger_list.csv），再补详情（存 posts_danger.csv）
- 自动检测末尾：连续 3 页 0 新帖即停止

### 局限性

- `lists` 端点仅返回约 4 天帖子（~200 页，去重后 ~2000 帖）
- 历史数据不在主列表端点中
- `lists3` 返回当前登录用户的个人历史（非通用数据源）
- 搜索端点对短关键词禁用（返回 0100）
- 直接 ID 枚举可行但效率极低（ID 空间 ~200 万，大量下架/不存在）
- **目前唯一可跨年度的通用端点**：`/article/article/userarticles?show_user_id=X`

## 运行

```bash
python spider_danger.py          # 全量爬取（自动续爬）
python crawl_detail.py           # 补抓详情
python server.py                 # 启动 Web 服务器
# http://127.0.0.1:8080         主页
# http://127.0.0.1:8080/admin   管理面板（密码见 data/admin_password.txt）
python test_api.py               # API 连通性测试
```

## 服务器

- 普通模式：不暴露 show_user_id/real_user_id
- Admin 模式（/admin，密码登录）：显示 show_user_id，按用户分组
- 优先加载 posts_danger.csv，fallback 到 posts_full.csv
