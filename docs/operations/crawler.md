# 爬虫运行与调度

本文档是爬虫命令、停止条件、队列、配额和 Railway 调度的当前唯一运维事实源。`crawler/README.md` 只说明模块边界，不再复制运行手册。

> 合规边界：只能使用本人合法取得且有权使用的 cookie，不得规避登录、验证码、签名、限流或权限检查。持续抓取、全量扫描、公开部署或共享真实数据前，应取得平台运营方的书面授权。无法确认授权范围时，不要连接真实接口。

## 运行主线

当前推荐路径是“快发现、慢补详情”：

```text
lists / lists2
  -> bootstrap 20 pages / discover-latest / discover-active
  -> post_id_ledger + crawler_queue
  -> posts(list_only) (only when normal discovery is configured to write stubs)
  -> trickle-fill
  -> posts(full) + comments + search indexes
```

- `discover-latest` 扫新帖流，只发现候选，不在列表循环中拉详情。
- `discover-active` 扫活跃/新回复流，只在本地缺详情或列表评论数大于数据库评论数时入队。
- 首次线上启动先运行 `bootstrap_new`：固定扫 `lists` 20 页，只写 `post_id_ledger` 和去重详情队列，不写正文 stub；初始详情队列完成或明确不可用后才开启两个增量监视任务。
- `trickle-fill` 按优先级小批量补详情，一次详情请求返回正文和完整评论/回复结构。
- 每次 list page 还会写入 `post_id_ledger`；`lists2` 的事件键写入 `list2_observation_log`，首次基线不批量触发详情，后续新事件才进入同一个 `crawler_queue`。
- `plan-gaps` 只规划低密度 ID 区间；未指定结束 ID 时会用一次 `lists?page=1` 探测最新 ID。
- `probe-gaps` 用详情接口低频抽样缺口，命中真实帖子后只记录并入队；默认每日预算为 0。

旧 `sync-latest`、`sync-active`、`scan-history`、`scan-id-range` 仍由 CLI 保留，用于兼容和明确的人工修复，不是 Railway quota-friendly 模式的日常主线。

## 配置与 Cookie

Cookie 存放在 `data/config.txt`，爬虫只读取 `ys7_ysxy_session`：

```text
ys7_ysxy_session=你的cookie
```

抓包只能用于取得本人当前登录会话中的 cookie，不得截获他人流量、收集他人 cookie 或把 cookie 提交到仓库。认证失败、限流或平台要求停止时必须停止任务，不得为了规避限制临时更换账号、使用代理或提高并发。

如果确实有多个固定、本人有权使用的会话，可以使用 cookie 池；池文件只保存“配置文件路径”和每个 lane 的每日硬上限，不保存 cookie 值。示例见 `data/cookie_pool.example.json`：

```powershell
Copy-Item data\cookie_pool.example.json data\cookie_pool.json
python crawler_db.py trickle-fill --cookie-pool data\cookie_pool.json --limit 5 --min-delay 8 --max-delay 14
```

`daily_budgets` 的键是 `new_list`、`active_list`、`detail`、`probe`。当前示例把新 cookie lane 配置为 `detail: 500`、旧 cookie lane 配置为 `detail: 500`，详情任务会在一个共享队列中按剩余 lane 配额路由；不是启动两个 crawler，也不是并发请求。池模式下这些 lane 的显式上限优先于单 cookie 的全局默认值，quota 文件会同时保留总计数和 `cookie_lanes` 分 lane 计数。`source_quota_*` 只表示该 lane 的本地配额或释放窗口已到上限，路由器可以选择另一个仍有预算的 lane；真实 `rate_limited:*` 或 `cookie_expired` 仍会触发现有停止/暂停语义，不用切换身份掩盖上游限制。

池模式也会把兼容的 `scan-id-range` 强制为单 worker；如果需要日常自动调度，应使用上面的 `trickle` 主线，避免旧的并发扫描路径绕开这套逐请求配额。

生产环境将池文件和各个 `config*.txt` 放在 Railway Volume 的 `/app/data/` 下，再设置 `CRAWLER_COOKIE_POOL=/app/data/cookie_pool.json`。池文件、cookie 配置和 quota 文件都不能提交 Git、写入日志或上传到公共服务。

## 源 API 与请求成本

| 类型 | 端点 | 用途 | 调度计费 |
|---|---|---|---:|
| 新帖列表 | `/article/article/lists?page=N` | 发现新帖 ID、时间和评论数 | 每页 1 次 new-list |
| 活跃列表 | `/article/article/lists2?page=N` | 发现评论增量和活跃帖子 | 每页 1 次 active-list |
| 详情 | `/article/article/info?id=ID` | 正文、评论和回复 | 每帖 1 次 detail |
| 最新 ID 探测 | `lists?page=1` | `plan-gaps` 确定规划上界 | 1 次 new-list |
| 缺口抽样 | `info?id=ID` | `probe-gaps` 验证某 ID | 每个样本 1 次 probe |
| Admin 候选预览 | `search/lists/lists2` | 先展示上游候选供管理员勾选 | 每页 1 次 admin-preview 独立额度 |
| Admin 人工现爬 | `info?id=ID` | 勾选后立即补全并保存正文、评论和回复 | 每帖 1 次 admin-detail 独立额度 |

评论不是逐条请求；一个成功的详情请求同时返回帖子正文和当时可见的评论/回复。

## 手动小范围验证

先使用低预算发现，再补少量详情：

```powershell
$since = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd HH:mm:ss")
python crawler_db.py discover-latest --db-path data\posts.db --since $since --max-pages 5 --min-pages 3 --no-action-page-threshold 3
python crawler_db.py discover-active --db-path data\posts.db --since $since --max-pages 5 --min-pages 3 --no-action-page-threshold 3
python crawler_db.py trickle-fill --db-path data\posts.db --limit 5 --min-delay 8 --max-delay 14
```

这组命令最多规划 10 次列表请求和 5 次详情请求；列表扫描可能提前停止。手动 SSH 大跑不会经过 scheduler 的配额窗口和暂停保护，不应用于日常补爬。

只检查候选、不写数据库时使用 `--dry-run`。发现阶段默认写 `list_only` 快照；如不希望写快照，可加 `--no-write-stubs`。

## `crawl_status` 与运行表

- 新发现帖子先以 `posts.crawl_status='list_only'` 写入，正文来自列表快照，评论尚未补全。
- 详情成功后帖子更新为 `crawl_status='full'`，同时刷新 `comments`、SQLite FTS 和旁路索引。
- `crawler_queue` 保存详情候选、优先级、原因、状态、尝试次数、最后错误以及 `in_progress` 认领的 owner/lane/租约；租约过期会在下一轮恢复为 pending。
- `crawler_gap_ranges` 保存低密度 ID 区间。
- `crawler_id_probe` 保存缺口抽样结果，避免重复探测相同 ID。
- `crawl_state` 保存各命令最近一次统计。

旧数据库首次运行新命令时，`SQLitePostStore.ensure_runtime_schema()` 会补齐这些运行字段和表。

## 队列优先级

| 优先级 | 候选 | 原因 |
|---:|---|---|
| 0 | `lists2` 中已有帖评论数增加 | 新回复优先 |
| 10 | `lists` 中缺失且有评论的新帖 | 有正文和评论收益 |
| 15 | 缺口抽样命中的真实帖子 | 已付出探测成本，但低于明确新回复 |
| 20 | `lists2` 中缺失且有评论的活跃帖 | 活跃流兜底 |
| 40 | `lists` 中缺失但零评论的新帖 | 只有正文收益 |
| 50 | `lists2` 中缺失但零评论的活跃帖 | 最低常规优先级 |

priority 大于 0 的 coverage 任务还按 `crawler_queue.queue_order` 升序处理；初始 20 页按 list1 返回顺序进入队列，后续 list1/list2 新 ID 追加到队尾。`lists2` 已知 ID 的新事件进入 priority 0 刷新车道，并受 `CRAWLER_TRICKLE_REFRESH_LIMIT` 限制，所以不会长期饿死初始 coverage 队列。仅更新时间变化且事件键已消费、评论数没有增加时不重复进入详情队列。

## 列表停止条件

`discover-latest`：

- bootstrap 模式固定完成 `--min-pages=--max-pages=20`；不使用连续无收益页提前停止，也不写 list stub。
- 至少扫描 `--min-pages` 后，连续 `--no-action-page-threshold` 页没有可入队候选即可停止。
- 连续多页都早于 `--since` 时停止。
- 页面 ID 签名重复时停止。
- `--max-pages` 是硬上限。

`discover-active`：

- 页面 ID 签名重复时停止，避免上游窗口循环。
- 至少扫描 `--min-pages` 后，连续无收益页达到阈值时停止。
- `--max-pages` 是硬上限。

停止逻辑同时依赖最小页数、连续无收益页、重复页签名、时间边界和硬预算；单条重复不能作为停止条件。

## Railway quota-friendly 调度

`CRAWLER_ENABLED=1` 时 `start.sh` 启动 `jobs.scheduler`。当前推荐线上模式还需要：

```text
CRAWLER_TRICKLE_ENABLED=1
CRAWLER_TRICKLE_SINCE=<需要持续覆盖的起始时间>
```

代码默认预算：

```text
CRAWLER_DAILY_NEW_LIST_BUDGET=80
CRAWLER_DAILY_ACTIVE_LIST_BUDGET=160
CRAWLER_DAILY_DETAIL_BUDGET=1000
CRAWLER_DAILY_PROBE_BUDGET=0
CRAWLER_DAILY_ADMIN_PREVIEW_BUDGET=20
CRAWLER_DAILY_ADMIN_DETAIL_BUDGET=10
CRAWLER_TRICKLE_LIMIT_CAP=12
CRAWLER_TRICKLE_REFRESH_LIMIT=5
CRAWLER_TRICKLE_MIN_DELAY=8
CRAWLER_TRICKLE_MAX_DELAY=14
CRAWLER_QUOTA_RELEASE_STEPS=11=0.20,14=0.35,17=0.50,20=0.70,21=0.85,22=1.00
CRAWLER_DETAIL_QUOTA_RELEASE_STEPS=10=0.20,12=0.40,15=0.65,18=0.82,20=0.93,21=1.00
CRAWLER_QUOTA_ADAPTIVE_ENABLED=1
CRAWLER_QUOTA_ADAPTIVE_SAFETY=0.80
CRAWLER_QUOTA_ADAPTIVE_LOOKBACK_DAYS=14
CRAWLER_QUOTA_RATE_LIMIT_EXCLUDED_DATES=2026-08-06
CRAWLER_DETAIL_ADAPTIVE_ENABLED=1
CRAWLER_DETAIL_ADAPTIVE_MIN=900
CRAWLER_DETAIL_ADAPTIVE_START=900
CRAWLER_DETAIL_ADAPTIVE_STEP=100
CRAWLER_DETAIL_ADAPTIVE_UTILIZATION=0.95
CRAWLER_DETAIL_ADAPTIVE_SCHEDULE_UTILIZATION=0.98
# 可选：固定 cookie 池；不设置时继续使用单一 CRAWLER_CONFIG/config.txt
CRAWLER_COOKIE_POOL=/app/data/cookie_pool.json
CRAWLER_BOOTSTRAP_PAGES=20
CRAWLER_BOOTSTRAP_SINCE=1970-01-01 00:00:00
```

设置 `CRAWLER_COOKIE_POOL` 后，`CRAWLER_DAILY_*` 的单会话默认预算不再替代池文件中的 lane 预算；scheduler 会按所有 lane 预算的合计裁剪本轮 `limit/max-pages`，子进程再在每一次真实请求前原子扣减对应 lane。详情仍保持单请求、串行和 8–14 秒间隔，队列不会因为增加 lane 而复制同一帖子。

当前积压加速配置把详情目标范围设为 900–1000，从 900 起步；旧目标低于 900 时会立即抬到 900，在安全满载或时间窗满载且无限流时，次日再增加 100 到 1000。新帖列表 80、活跃列表 160、缺口探测 0，因此自动源请求配置上限为 1240。详情使用独立的提前释放曲线：10:00 释放 20%、12:00 释放 40%、15:00 释放 65%、18:00 释放 82%、20:00 释放 93%、21:00 全量释放；按 10 分钟一轮、每轮最多 12 次计算，1000 次详情在午夜前可达。列表仍使用 11:00 才开始的保守公共释放曲线。

日切升级既看详情总目标利用率，也看旧释放曲线下的理论可达容量：如果昨日没有限流，虽然未达到目标的 95%，但已经使用了当日时间窗理论容量的 98%，视为 `schedule_limited_increase`，而不是错误判为需求不足。发生 `rate_limited` 时仍由全局 80% 安全回退统一缩减，详情控制器不重复降额。每轮 12 个详情默认最多 5 个 priority 0 新回复任务，剩余至少 7 个位置继续补新帖和历史覆盖。

2026-08-05 上线前快照显示：最近无 `rate_limited`，已验证最高约 816 次源请求/天；队列仍有 20,983 个 pending，其中 priority 0 新回复 4,172 个、priority 10 有评论新帖 12,371 个。该数据说明 700 详情上限只能接近追平新增，无法明显消化积压，因此提高到 1000；如果更高请求强度触发真实限流，全局 pause 仍会立即停止当天请求并在次日按 80% 安全系数回退。

Admin 使用独立额外额度：每天 20 次候选预览和 10 次人工详情，不扣减 new-list、active-list 或 detail 主计数，也不受主额度阶梯释放约束；按当前线上配置，请求上界是 1240 次自动源额度加 30 次人工额度，共 1270 次。人工调用仍读取同一个全局 pause，发生 `rate_limited` 时会和 scheduler 一起暂停；人工计数也会进入 quota history 的真实 `source_calls`，不能在限流分析中漏算。一次预览最多 3 页，一次任务最多 10 个帖子；详情任务第一个帖子立即请求，后续帖子继续使用 8–14 秒串行间隔。

后台方案语义：

- `smart`：本地缺失、仅列表数据或上游评论数增加时立即抓详情并保存；否则跳过。
- `force`：无论本地状态，勾选后立即抓详情并保存。
- `queue`：不立刻打详情 API，只加入 priority `-10` 的人工优先队列。

预览只写主库旁的 `.admin_crawl.db`，10 分钟后失效，不会写入 `posts`。人工任务也保存在该 sidecar，服务重启后会恢复未完成任务。详情成功后在同一写入路径更新 SQLite FTS、Bigram 和可用的 Symbol sidecar；上游声称有评论却返回空评论、正文为空或社区不匹配时拒绝覆盖旧数据。

scheduler 只用剩余额度裁剪子任务的 `max-pages` 或 `limit`，不再整批预扣。scheduler 启动的子进程会在每一次真实 HTTP 请求前原子领取 1 次对应额度，因此 quota 文件记录的是实际发起的源请求；部署中断、提前停止、重复页和空页不会再虚扣整批额度。北京时间跨日后 release 重新归零，仍在运行的旧任务会在下一次请求前正常停止，不能偷吃次日 11:00 前的额度。

多个任务同时过期时按 `trickle-fill`、`discover-active`、`discover-latest` 的价值顺序运行；间隔按开始时间计算，所以“每 10 分钟详情”是接近真实的 start-to-start 节拍，不再变成“任务耗时 + 10 分钟”。列表日志保留 `queued` 作为候选观察数，同时新增 `queue_inserted`、`queue_reopened`、`queue_updated`、`queue_unchanged`；连续无收益页按真实队列变化判断。

已完成队列行在 `lists2` 发现评论数增长后会重新变成 `pending` priority 0。启动时还会修复旧版本遗留的“队列 done、列表评论数大于主库评论数”记录。自动详情与 Admin 现爬共用可疑响应校验：正文为空，或上游声明有评论但评论列表为空时，不覆盖主库已有数据。

默认调度间隔：

```text
CRAWLER_NEW_DISCOVER_INTERVAL=3600
CRAWLER_ACTIVE_DISCOVER_INTERVAL=1800
CRAWLER_DISCOVER_LATEST_PAGES=5
CRAWLER_DISCOVER_ACTIVE_PAGES=5
CRAWLER_TRICKLE_INTERVAL=600
CRAWLER_GAP_PLAN_INTERVAL=21600
CRAWLER_GAP_PROBE_INTERVAL=7200
```

首次启动先固定扫 20 页 list1，随后 `trickle-fill` 按 `queue_order` 串行补详情；初始行全部完成或明确不可用后，才开启正式监视。监视阶段两类列表默认至少扫描 2 页，并在连续 2 页没有队列变化或新的台账信号时停止；单轮最多 5 页。list1 每小时一次，list2 每半小时一次；只有出现新 ID、源端更新时间/评论数变化或新的 `lists2` 事件时才继续扩页。`CRAWLER_DISCOVER_INTERVAL` 仍作为旧部署的 active-list 兼容变量，新的两个变量优先级更高。

`probe-gaps` 即使被调度，也会在每日 probe budget 为 0 时跳过。不要通过手动 SSH 大跑绕过这一保护。

## 限流、Cookie 失效与暂停

- `code == "1000"` 映射为 `cookie_expired`，通常需要人工替换 cookie。
- “今天刷得太久”“休息一下”“操作频繁”“稍后再试”“访问频繁”等文本映射为 `rate_limited:*`。
- `rate_limited` 发生后当前候选保持 `pending`，本轮立即停止；scheduler 暂停全部爬虫到下一个北京时间 00:05。
- 暂停结束不代表立即放量，主动请求仍受当天 release step 约束。
- `cookie_expired` 默认暂停 6 小时，但恢复通常依赖人工更新 `/app/data/config.txt`。
- 最近 14 天发生过 `rate_limited` 时，有效总预算按最近触顶时已预留源请求数的 80% 缩小。
- 只有经人工确认由共享 session 的用户高强度浏览导致时，才可把对应北京时间日期加入 `CRAWLER_QUOTA_RATE_LIMIT_EXCLUDED_DATES`。原始 quota history 不删除、不改写；排除项只阻止该日期参与后续自动降额，其他日期的真实 crawler 限流仍正常暂停和回退。

运行文件位于主库旁：

```text
/app/data/.crawler_quota.json
/app/data/.crawler_quota_history.jsonl
/app/data/.crawler_pause.json
/app/data/.admin_crawl.db
/app/data/.crawler_scheduler_heartbeat.json
```

启用 cookie 池时，`.crawler_quota.json` 还会有 `cookie_lanes` 数字计数，例如每个 lane 的 `detail_calls`；不会写入 cookie 内容。

数据库写锁使用带 token、容器主机名和心跳的 90 秒租约；新旧 Railway 容器重叠时，新容器不会仅因为看不到旧容器 PID 就删除活锁。scheduler 还由 `start.sh` 监督，意外退出后 30 秒重启；管理员状态接口会返回 scheduler heartbeat 和终态队列中仍未补的评论差值。

## Railway 只读检查

远端容器没有 `sqlite3` CLI，Railway SSH 对 `python -c` 的引号处理也不可靠。使用 stdin 喂给虚拟环境 Python，不要在远端写临时脚本：

```powershell
@'
import sqlite3
conn = sqlite3.connect("/app/data/posts.db")
conn.row_factory = sqlite3.Row
print(conn.execute("select count(*) from posts").fetchone()[0])
'@ | railway ssh -- /opt/venv/bin/python -
```

配额文件检查：

```powershell
@'
from pathlib import Path
for name in [".crawler_quota.json", ".crawler_quota_history.jsonl", ".crawler_pause.json"]:
    path = Path("/app/data") / name
    print("\n" + str(path))
    print(path.read_text(encoding="utf-8")[-4000:] if path.exists() else "missing")
'@ | railway ssh -- /opt/venv/bin/python -
```

## 本地验证

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -B -m pytest tests/test_cli_contract.py tests/test_automatic_quota.py tests/test_crawler_lock.py tests/test_crawler_service.py tests/test_crawler_strategies.py -q
python -B -c "import jobs.scheduler, crawler.service, crawler.cli; print('import ok')"
git diff --check
```

部署后的状态、日志和健康检查见 [Railway 部署与运维](railway.md)。
