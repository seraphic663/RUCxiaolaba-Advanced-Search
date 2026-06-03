# API 与数据来源

## 小程序 API

当前爬虫使用的基础域名：

```text
https://ys.qimiaoyuanfen.com
```

鉴权方式：

```text
Cookie: ys7_ysxy_session=...
```

本地和 Railway 爬虫默认从以下文件读取 cookie：

```text
data/config.txt
```

格式：

```text
ys7_ysxy_session=你的cookie
```

## 当前使用的接口

### 列表流：新帖

```text
GET /article/article/lists
参数：
community_id=4
page=N
```

用途：

- 更适合发现最新发布的帖子
- `crawler_db.py new` 默认使用它

### 列表流：活跃/评论更新

```text
GET /article/article/lists2
参数：
community_id=4
page=N
```

用途：

- 更适合发现近期有评论变化或活跃变化的帖子
- `crawler_db.py refresh` 默认使用它

### 详情接口

```text
GET /article/article/info
参数：
community_id=4
id=帖子ID
```

用途：

- 获取帖子正文、分类、匿名展示 ID、真实 ID、浏览、点赞、蹲蹲、评论列表
- 所有写入 DB 的帖子都应该来自详情接口标准化后的结果

## 返回码

`crawler_db.py` 目前处理：

```text
code=0000  成功
code=1000  cookie 过期
code=0102  帖子不存在或不可访问
其他       记录错误并跳过
```

## 规范化字段

详情接口数据会被规范化成：

```text
posts.id
posts.content
posts.category_name
posts.user_name
posts.show_user_id
posts.show_user_head
posts.real_user_id
posts.create_time
posts.comment_count
posts.star_count
posts.trace_count
posts.views
posts.hot
```

评论被展开成 `comments` 表。当前生产库不再保留完整 `raw_json`，只在
`comments.reply_comment_list` 中保留嵌套回复列表，其他评论字段依赖结构化列。

## 新增与更新判断

当前策略：

```text
DB 没有该帖子 ID
  -> 抓详情
  -> 写入 posts/comments/search_index

DB 有该帖子 ID，但 comment_count 不同
  -> 抓详情
  -> 覆盖帖子和评论
  -> 刷新 search_index

DB 有该帖子 ID，comment_count 相同
  -> 判定 unchanged
```

说明：

- 这能覆盖“新帖”和“新评论/删评论”。
- 如果只是浏览、点赞、蹲蹲变化，但评论数不变，当前不会主动刷新详情。
- 如果未来要精确维护点赞/蹲蹲，可以增加轻量刷新模式，按列表返回值比较 `star_count/trace_count/views/hot` 后决定是否抓详情或只更新计数字段。
