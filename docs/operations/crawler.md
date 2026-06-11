# 爬虫指南

## 前置条件

Cookie 存放在 `data/config.txt`：

```text
ys7_ysxy_session=你的cookie
```

获取方式：微信小程序抓包，复制请求头中的 `ys7_ysxy_session` 值。

## API 端点

爬虫使用的接口：

| 端点 | 用途 |
|------|------|
| `/article/article/lists?community_id=4&page=N` | 新帖流（`sync-latest`） |
| `/article/article/lists2?community_id=4&page=N` | 活跃/更新流（`sync-active`） |
| `/article/article/info?community_id=4&id=ID` | 帖子详情 + 完整评论树 |

返回码：

```text
code=0000  成功
code=1000  cookie 过期
code=0102  帖子不存在
```

完整 API 端点清单见 [architecture/api-reference](../architecture/api-reference.md)。

## 子命令

### sync-latest — 补新帖

```powershell
python crawler_db.py sync-latest --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

扫 `/article/article/lists`，发现新 ID 或评论数变化的帖子。

### sync-active — 补活跃帖

```powershell
python crawler_db.py sync-active --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

扫 `/article/article/lists2`，更新近期有评论变化的旧帖。

### scan-history — 补历史

```powershell
python crawler_db.py scan-history --endpoint lists --db-path data\posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600
```

从指定页向后扫，低频运行。

### scan-id-range — ID 全量扫描

```powershell
# 按日期
python crawler_db.py scan-id-range --from-date 2026-06-01 --db-path data\posts.db

# 按 ID 范围
python crawler_db.py scan-id-range --start-id 5004321 --end-id 5066654 --db-path data\posts.db

# 中断后自动续扫；加 --restart 从头重扫
```

### detail-fill — 指定 ID 修复

```powershell
python crawler_db.py detail-fill --db-path data\posts.db --ids 5014356,5018419
```

### 验证（dry-run）

```powershell
python crawler_db.py sync-latest --pages 20 --min-pages 3 --stop-unchanged 80 --max-details 100 --dry-run
```

## 判断逻辑

```
DB 无该 ID        → 抓详情 → 写入 posts/comments/search_index
DB 有该 ID，评论数变化 → 抓详情 → 覆盖帖子和评论 → 刷新 search_index
DB 有该 ID，评论数相同 → unchanged（不重抓）
连续 unchanged ≥ stop-unchanged 且已扫 ≥ min-pages → 停止
```

## 写锁

爬虫启动时创建 `data/posts.db.crawler.lock`（含 PID），防止多个进程同时写 SQLite。即使有锁也建议错峰运行。

## Railway 调度

`jobs.scheduler` 在 Web 服务内运行（非独立 Cron）：

```
sync-latest   每 8 小时
sync-active   每 8 小时（错开 4 小时）
scan-history  每 24 小时
scan-id-range 每 7 天
```

间隔可通过环境变量覆盖（单位：秒）：

```text
CRAWLER_NEW_INTERVAL=28800
CRAWLER_REFRESH_INTERVAL=28800
CRAWLER_BACKFILL_INTERVAL=86400
CRAWLER_PHASE1_INTERVAL=604800
```

`CRAWLER_ENABLED=1` 时调度器随 `start.sh` 启动。部署重启后首轮
`sync-latest` 等 4 小时、`sync-active` 等 8 小时，避免部署风暴。

旧命令名仍作为兼容别名保留。
