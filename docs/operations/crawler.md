# 爬虫运行与调度

本文档是爬虫命令、停止条件、队列、配额和 Railway 调度的当前唯一运维事实源。`crawler/README.md` 只说明模块边界，不再复制运行手册。

> 合规边界：只能使用本人合法取得且有权使用的 cookie，不得规避登录、验证码、签名、限流或权限检查。持续抓取、全量扫描、公开部署或共享真实数据前，应取得平台运营方的书面授权。无法确认授权范围时，不要连接真实接口。

## 运行主线

当前推荐路径是“快发现、慢补详情”：

```text
lists / lists2
  -> discover-latest / discover-active
  -> posts(list_only) + crawler_queue
  -> trickle-fill
  -> posts(full) + comments + search indexes
```

- `discover-latest` 扫新帖流，只发现候选，不在列表循环中拉详情。
- `discover-active` 扫活跃/新回复流，只在本地缺详情或列表评论数大于数据库评论数时入队。
- `trickle-fill` 按优先级小批量补详情，一次详情请求返回正文和完整评论/回复结构。
- `plan-gaps` 只规划低密度 ID 区间；未指定结束 ID 时会用一次 `lists?page=1` 探测最新 ID。
- `probe-gaps` 用详情接口低频抽样缺口，命中真实帖子后只记录并入队；默认每日预算为 0。

旧 `sync-latest`、`sync-active`、`scan-history`、`scan-id-range` 仍由 CLI 保留，用于兼容和明确的人工修复，不是 Railway quota-friendly 模式的日常主线。

## 配置与 Cookie

Cookie 存放在 `data/config.txt`，爬虫只读取 `ys7_ysxy_session`：

```text
ys7_ysxy_session=你的cookie
```

抓包只能用于取得本人当前登录会话中的 cookie，不得截获他人流量、收集他人 cookie 或把 cookie 提交到仓库。认证失败、限流或平台要求停止时必须停止任务，不得通过更换账号、代理或提高并发规避限制。

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
- `crawler_queue` 保存详情候选、优先级、原因、状态、尝试次数和最后错误。
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

同一优先级内再按评论增量、列表更新时间、评论数和入队时间排序。`lists2` 更新时间变化但评论数没有增加时不进入详情队列。

## 列表停止条件

`discover-latest`：

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
CRAWLER_DAILY_DETAIL_BUDGET=450
CRAWLER_DAILY_PROBE_BUDGET=0
CRAWLER_DAILY_ADMIN_PREVIEW_BUDGET=20
CRAWLER_DAILY_ADMIN_DETAIL_BUDGET=10
CRAWLER_TRICKLE_LIMIT_CAP=12
CRAWLER_TRICKLE_MIN_DELAY=8
CRAWLER_TRICKLE_MAX_DELAY=14
CRAWLER_QUOTA_RELEASE_STEPS=11=0.20,14=0.35,17=0.50,20=0.70,21=0.85,22=1.00
CRAWLER_QUOTA_ADAPTIVE_ENABLED=1
CRAWLER_QUOTA_ADAPTIVE_SAFETY=0.80
CRAWLER_QUOTA_ADAPTIVE_LOOKBACK_DAYS=14
```

自动调度主额度上限为每天 690 次源请求：80 次新帖列表、160 次活跃列表、450 次详情、0 次缺口探测。阶梯累计上限在未触发自适应缩放时约为 11:00 的 138 次、14:00 的 241 次、17:00 的 345 次、20:00 的 483 次、21:00 的 586 次、22:00 的 690 次。最后 30% 分两小时释放，是为了让串行详情任务在午夜前实际使用额度，同时仍把早间额度留给用户本人。

Admin 使用独立额外额度：每天 20 次候选预览和 10 次人工详情，不扣减 new-list、active-list 或 detail 主计数，也不受主额度阶梯释放约束；因此配置请求上界是 690 次自动主额度加 30 次人工额度，共 720 次。人工调用仍读取同一个全局 pause，发生 `rate_limited` 时会和 scheduler 一起暂停；人工计数也会进入 quota history 的真实 `source_calls`，不能在限流分析中漏算。一次预览最多 3 页，一次任务最多 10 个帖子；详情任务第一个帖子立即请求，后续帖子继续使用 8–14 秒串行间隔。

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
CRAWLER_DISCOVER_INTERVAL=1800
CRAWLER_TRICKLE_INTERVAL=600
CRAWLER_GAP_PLAN_INTERVAL=21600
CRAWLER_GAP_PROBE_INTERVAL=7200
```

`probe-gaps` 即使被调度，也会在每日 probe budget 为 0 时跳过。不要通过手动 SSH 大跑绕过这一保护。

## 限流、Cookie 失效与暂停

- `code == "1000"` 映射为 `cookie_expired`，通常需要人工替换 cookie。
- “今天刷得太久”“休息一下”“操作频繁”“稍后再试”“访问频繁”等文本映射为 `rate_limited:*`。
- `rate_limited` 发生后当前候选保持 `pending`，本轮立即停止；scheduler 暂停全部爬虫到下一个北京时间 00:05。
- 暂停结束不代表立即放量，主动请求仍受当天 release step 约束。
- `cookie_expired` 默认暂停 6 小时，但恢复通常依赖人工更新 `/app/data/config.txt`。
- 最近 14 天发生过 `rate_limited` 时，有效总预算按最近触顶时已预留源请求数的 80% 缩小。

运行文件位于主库旁：

```text
/app/data/.crawler_quota.json
/app/data/.crawler_quota_history.jsonl
/app/data/.crawler_pause.json
/app/data/.admin_crawl.db
/app/data/.crawler_scheduler_heartbeat.json
```

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
