# 爬虫同步策略

这个目录里的爬虫面对的核心问题不是“能不能请求接口”，而是长期同步时如何避免漏抓和触发小程序风控。

## 两类列表入口

- `lists`：更接近新发帖流，用来发现新帖子。
- `lists2`：更接近活跃/新回复流，用来发现评论数变化、老帖被回复、新帖在活跃流中出现。

两者不能互相替代。长时间断爬虫后，必须分别扫描两条流。

## 为什么不能直接跑旧的 `sync-latest` / `sync-active`

旧命令是“扫列表时立刻打开详情并写库”。这有两个问题：

1. `--max-details` 达到上限后会停止整轮扫描，深页候选没有机会被发现。
2. 如果先大量补新帖，数据库里的评论数会被写成当前值；随后再跑新回复同步时，旧逻辑可能把这些帖子判成 `unchanged`，从而影响活跃流的停止判断。

因此新的可靠路径是两阶段：

1. 发现阶段：只扫列表，写入候选队列，不打开详情。
2. 慢填阶段：从候选队列小批量打开详情，更新帖子、评论和搜索索引。

## 新命令

发现新帖候选：

```bash
python crawler_db.py discover-latest --since "2026-06-25 00:00:00" --max-pages 180
```

发现新回复/活跃候选：

```bash
python crawler_db.py discover-active --since "2026-06-25 00:00:00" --max-pages 120
```

慢慢补详情：

```bash
python crawler_db.py trickle-fill --limit 40 --min-delay 5 --max-delay 10
```

Railway 上使用虚拟环境解释器：

```bash
railway ssh /opt/venv/bin/python /app/crawler_db.py discover-latest --db-path /app/data/posts.db --config /app/data/config.txt --since "2026-06-25 00:00:00" --max-pages 180
railway ssh /opt/venv/bin/python /app/crawler_db.py discover-active --db-path /app/data/posts.db --config /app/data/config.txt --since "2026-06-25 00:00:00" --max-pages 120
railway ssh /opt/venv/bin/python /app/crawler_db.py trickle-fill --db-path /app/data/posts.db --config /app/data/config.txt --limit 40 --min-delay 5 --max-delay 10
```

## 候选优先级

候选写入 `crawler_queue`，按优先级慢填：

- `0`：`lists2` 中已有帖子评论数变化，优先补新回复。
- `10`：`lists` 中数据库不存在的新帖。
- `20`：`lists2` 中数据库不存在的活跃帖。
- `30`：`lists2` 中更新时间较新但评论数一致的帖子。

这样可以避免“新帖补完后，新回复同步被误判早停”。

## 停止条件

`discover-latest` 面向新帖流：

- 主停止条件：连续多页都没有 `since` 之后的创建/更新时间。
- 辅助停止条件：页面 ID 签名重复。

`discover-active` 面向活跃流：

- 主停止条件：页面 ID 签名重复。
- 硬上限：由 `--max-pages` 控制。

重复页签名不是通用真理，但对 `lists2` 很重要。实际观察中，`lists2` 到一定页数后会重复同一批 ID；继续扫只会浪费请求。

## 限流熔断

详情接口如果返回类似：

- `今天刷的太久`
- `休息一下`
- `操作频繁`
- `稍后再试`

客户端会标记为 `rate_limited`。`trickle-fill` 遇到后立即停止，并把当前候选保留为 `pending`，避免继续消耗同一 session 的额度。

如果连续多个详情失败，也会停止。失败候选会记录 `attempts` 和 `last_error`，后续可以重试。

## 长时间断爬虫后的推荐流程

以 `2026-06-25 00:00:00` 为补洞起点：

```bash
python crawler_db.py discover-latest --since "2026-06-25 00:00:00" --max-pages 180
python crawler_db.py discover-active --since "2026-06-25 00:00:00" --max-pages 120
python crawler_db.py trickle-fill --limit 40 --min-delay 5 --max-delay 10
```

重复运行 `trickle-fill`，直到队列明显变小。中途可以继续运行两个 discover 命令刷新候选。

## 日常自动化建议

恢复正常后，不建议再使用一次性大爬。更稳的调度是：

- 每 30 分钟运行一次 `discover-latest` 和 `discover-active`。
- 每 10 分钟运行一次 `trickle-fill --limit 20~40`。
- 如果触发限流，暂停 6 到 12 小时。

部署前应先在 Railway 上 dry-run 或小批量验证，避免影响小程序账号正常使用。
