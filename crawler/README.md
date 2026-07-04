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

因此新的可靠路径是“快发现、慢补详情”：

1. 发现阶段：只扫列表，写入候选队列；同时把列表里能拿到的标题/摘要/分类/时间/点赞/评论数写成 `list_only` 快照，让网站先看到近似结果。
2. 慢填阶段：从候选队列小批量打开详情，更新帖子、评论和搜索索引；详情成功后 `crawl_status` 变回 `full`。
3. 缺口阶段：如果断爬太久，当前 `lists` / `lists2` 只能覆盖最新窗口，就按数据库 ID 密度找稀疏区间，低频抽样详情接口；抽到真实帖子只入队，不直接大批写库。

## 列表快照与 `crawl_status`

发现阶段默认会写列表快照：

- 新帖子先进入 `posts`，`crawl_status='list_only'`，正文来自列表接口的 `title/detail`，评论暂时为空。
- 后续 `trickle-fill` 拿到详情后会覆盖为完整正文和评论，`crawl_status='full'`。
- 如果某条已经是 `list_only`，即使队列丢失，下一次 discover 仍会重新入队补详情。
- 如果详情接口明确返回 `not_found` / `foreign_or_invalid`，队列会标记 `skipped`，后续 discover 不会把它自动复活，避免无意义循环。

如果只想做纯发现、不写列表快照，可以加：

```bash
python crawler_db.py discover-latest --since "2026-06-25 00:00:00" --no-write-stubs
python crawler_db.py discover-active --since "2026-06-25 00:00:00" --no-write-stubs
```

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

按 ID 密度规划历史缺口：

```bash
python crawler_db.py plan-gaps --since "2026-06-25 00:00:00" --chunk-size 1000 --density-threshold 0.35
```

低频抽样缺口。抽到真实帖子只记录到 `crawler_id_probe` 并入 `crawler_queue`，不直接写帖子详情：

```bash
python crawler_db.py probe-gaps --range-limit 1 --samples-per-range 12 --min-delay 8 --max-delay 15
```

Railway 上使用虚拟环境解释器：

```bash
railway ssh /opt/venv/bin/python /app/crawler_db.py discover-latest --db-path /app/data/posts.db --config /app/data/config.txt --since "2026-06-25 00:00:00" --max-pages 180
railway ssh /opt/venv/bin/python /app/crawler_db.py discover-active --db-path /app/data/posts.db --config /app/data/config.txt --since "2026-06-25 00:00:00" --max-pages 120
railway ssh /opt/venv/bin/python /app/crawler_db.py trickle-fill --db-path /app/data/posts.db --config /app/data/config.txt --limit 40 --min-delay 5 --max-delay 10
railway ssh /opt/venv/bin/python /app/crawler_db.py plan-gaps --db-path /app/data/posts.db --config /app/data/config.txt --since "2026-06-25 00:00:00" --chunk-size 1000 --density-threshold 0.35
railway ssh /opt/venv/bin/python /app/crawler_db.py probe-gaps --db-path /app/data/posts.db --config /app/data/config.txt --range-limit 1 --samples-per-range 12 --min-delay 8 --max-delay 15
```

如果 Railway SSH 对 `python -c` 引号处理异常，不要在远端写临时文件；用本地 stdin 喂给 `/opt/venv/bin/python -` 查询 SQLite。

## 候选优先级

候选写入 `crawler_queue`，按优先级慢填：

- `0`：`lists2` 中已有帖子评论数变化，优先补新回复。
- `10`：`lists` 中数据库不存在的新帖。
- `20`：`lists2` 中数据库不存在的活跃帖。
- `30`：`lists2` 中更新时间较新但评论数一致的帖子。
- `15`：ID 缺口抽样命中的真实帖子，优先级低于明确的新回复，高于普通活跃兜底。

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

如果详情是 `not_found` / `foreign_or_invalid`，会记为 `skipped` 并继续下一条；这类结果通常不是限流，不应该触发整轮停止。

如果连续多个其它详情失败，也会停止。失败候选会记录 `attempts` 和 `last_error`，后续可以重试。

## ID 缺口策略

当前列表接口有实际窗口限制：扫到一定深度后会重复页签名或只能覆盖最新一段时间。断爬时间过长时，只靠 `lists` / `lists2` 不能保证补回中间缺口。

`plan-gaps` 做的是只读规划：从 `--since` 对应的数据库起点到最新列表 ID，按 `--chunk-size` 切块，统计每块数据库已有帖子密度，低于 `--density-threshold` 的写入 `crawler_gap_ranges`。

`probe-gaps` 做的是低强度探测：每次只取少量 gap range、每段只抽少量未探测 ID，详情接口确认存在后写 `crawler_id_probe`，并把帖子 ID 放进 `crawler_queue`。它不直接批量写 `posts`，所以不会把站点瞬间塞满未经详情校验的数据，也不会和新帖/新回复队列抢最高优先级。

推荐默认强度：

- 快速发现：`discover-latest` / `discover-active` 每 30 分钟。
- 慢补详情：`trickle-fill --limit 20~40` 每 10 分钟，详情间隔 5~10 秒。
- 缺口探测：`probe-gaps --range-limit 1 --samples-per-range 12` 每 2 小时，详情间隔 8~15 秒。

这不是一次性遍历。它的目标是：当前列表窗口内尽快可搜索；详情慢慢补；历史缺口靠抽样和低速累积提高完整度。

## 长时间断爬虫后的推荐流程

以 `2026-06-25 00:00:00` 为补洞起点：

```bash
python crawler_db.py discover-latest --since "2026-06-25 00:00:00" --max-pages 180
python crawler_db.py discover-active --since "2026-06-25 00:00:00" --max-pages 120
python crawler_db.py trickle-fill --limit 40 --min-delay 5 --max-delay 10
python crawler_db.py plan-gaps --since "2026-06-25 00:00:00" --chunk-size 1000 --density-threshold 0.35
python crawler_db.py probe-gaps --range-limit 1 --samples-per-range 12 --min-delay 8 --max-delay 15
```

重复运行 `trickle-fill`，直到队列明显变小。中途可以继续运行两个 discover 命令刷新候选。`probe-gaps` 可以长期低频跑，它会跳过已探测 ID，不会每轮重复抽同一批。

## 日常自动化建议

恢复正常后，不建议再使用一次性大爬。更稳的调度是：

- 每 30 分钟运行一次 `discover-latest` 和 `discover-active`。
- 每 10 分钟运行一次 `trickle-fill --limit 20~40`。
- 每 6 小时运行一次 `plan-gaps`。
- 每 2 小时运行一次 `probe-gaps --range-limit 1 --samples-per-range 12`。
- 如果触发限流，暂停 6 到 12 小时。

Railway scheduler 的 trickle 模式支持这些环境变量：

- `CRAWLER_TRICKLE_ENABLED=1`
- `CRAWLER_TRICKLE_SINCE=2026-06-25 00:00:00`
- `CRAWLER_DISCOVER_INTERVAL=1800`
- `CRAWLER_TRICKLE_INTERVAL=600`
- `CRAWLER_TRICKLE_LIMIT=30`
- `CRAWLER_GAP_ENABLED=1`
- `CRAWLER_GAP_SINCE=2026-06-25 00:00:00`
- `CRAWLER_GAP_PLAN_INTERVAL=21600`
- `CRAWLER_GAP_PROBE_INTERVAL=7200`
- `CRAWLER_GAP_RANGE_LIMIT=1`
- `CRAWLER_GAP_SAMPLES=12`
- `CRAWLER_GAP_CHUNK_SIZE=1000`
- `CRAWLER_GAP_DENSITY_THRESHOLD=0.35`

部署前应先在 Railway 上 dry-run 或小批量验证，避免影响小程序账号正常使用。
