# Railway 部署与运维

## 当前架构

```text
一个 Railway Web Service
一个挂载到 /app/data 的 Volume
start.sh 启动 jobs.scheduler 后以前台进程运行 server.py
```

Web 和 crawler 保持在同一个 service，SQLite 与 Volume 由该 service 独占。不要额外创建共享同一 SQLite Volume 的 Cron Service。

## Volume 文件

首次部署或灾难恢复时准备：

```text
/app/data/posts.db
/app/data/bigram_index.db
/app/data/symbol_index.db
/app/data/config.txt
```

上传命令：

```powershell
railway volume files upload data\posts.db /posts.db --overwrite
railway volume files upload data\bigram_index.db /bigram_index.db --overwrite
railway volume files upload data\symbol_index.db /symbol_index.db --overwrite
railway volume files upload data\config.txt /config.txt --overwrite
```

`posts.db` 只在首次部署或灾难恢复时上传；日常数据由线上 crawler 直接更新。不要把本地旧快照覆盖到线上主库。

## Variables

核心变量：

```text
SQLITE_DB=/app/data/posts.db
BIGRAM_DB=/app/data/bigram_index.db
SYMBOL_INDEX_DB=/app/data/symbol_index.db
ADMIN_PASSWORD=<固定强密码>
CRAWLER_ENABLED=1
CRAWLER_TRICKLE_ENABLED=1
CRAWLER_TRICKLE_SINCE=<需要持续覆盖的起始时间>
```

不要上传 `admin_password.txt`。Bigram 用于普通中文/混合文本，Symbol 用于特殊符号、表情和符号混合查询；普通单字查询仍可能回退 `LIKE`。旁路索引由 `SQLitePostStore` 随详情写入更新，也可以从主库重建。

## 自动更新

`CRAWLER_ENABLED=1` 让 `start.sh` 启动后台 scheduler；当前线上推荐同时设置 `CRAWLER_TRICKLE_ENABLED=1`，启用 quota-friendly 模式：

```text
discover-latest  默认每 30 分钟，受 new-list budget 和 release step 限制
discover-active  默认每 30 分钟，受 active-list budget 和 release step 限制
trickle-fill     默认每 10 分钟，每轮最多 12 条详情
plan-gaps        默认每 6 小时，只规划缺口
probe-gaps       默认每 2 小时检查，但每日 probe budget 为 0 时不发请求
```

调度器顺序执行任务，并使用 `posts.db.crawler.lock` 防止并发写入。trickle 模式启动后首轮新帖发现约等 1 分钟、活跃发现约等 3 分钟、详情补全约等 5 分钟，但默认 11:00 前仍会被 release window 拦截。更新完成后 Web 无需重启。

主要间隔变量，单位为秒：

```text
CRAWLER_DISCOVER_INTERVAL=1800
CRAWLER_TRICKLE_INTERVAL=600
CRAWLER_GAP_PLAN_INTERVAL=21600
CRAWLER_GAP_PROBE_INTERVAL=7200
```

完整请求成本、队列优先级、每日预算、阶梯释放、自适应缩放和暂停语义统一见 [爬虫运行与调度](crawler.md)，本页不再复制完整参数表。

如果没有设置 `CRAWLER_TRICKLE_ENABLED=1`，scheduler 仍会运行兼容的 `sync-latest`、`sync-active`、`scan-history` 和每周 `scan-id-range`。该分支用于保持旧部署兼容，不是当前推荐配置。

## 限流与 Cookie

出现 `cookie_expired` 时，覆盖合法取得的新配置：

```powershell
railway volume files upload data\config.txt /config.txt --overwrite
```

调度器下次执行会读取新 cookie，无需上传 DB。`rate_limited` 不是 cookie 格式错误；发生后 scheduler 暂停到下一个北京时间 00:05，恢复后仍受当天阶梯释放约束。不得通过替换身份、轮换 cookie、代理或提高并发规避限制。

## 兼容的 ID 范围补扫

`scan-id-range` 可用于明确范围的人工修复。在当前 trickle 模式中它不会自动按周运行；旧 scheduler 模式才会用 `.phase1_weekly_last` 记录每周执行时间。该命令会逐 ID 消耗详情请求，不要在 quota-friendly 调度之外直接大跑。

```powershell
python crawler_db.py scan-id-range --from-date 2026-06-01 --to-date 2026-06-03 --db-path data\posts.db
python crawler_db.py scan-id-range --start-id 5004321 --end-id 5066654 --db-path data\posts.db
```

## 运行库瘦身迁移

需要删除旧字段或重建主库 schema 时，不要在线上原地 `ALTER/VACUUM`。先暂停 crawler，在 Volume 内生成替换库并验证：

```bash
python -m tools.operations.compact_runtime_db plan --db /app/data/posts.db --bigram /app/data/bigram_index.db --symbol /app/data/symbol_index.db
python -m tools.operations.compact_runtime_db migrate --db /app/data/posts.db --out /app/data/posts.next.db
python -m tools.operations.compact_runtime_db rebuild-sidecars --db /app/data/posts.next.db --bigram-out /app/data/bigram_index.next.db --symbol-out /app/data/symbol_index.next.db
python -m tools.operations.compact_runtime_db verify --db /app/data/posts.next.db --bigram /app/data/bigram_index.next.db --symbol /app/data/symbol_index.next.db
python -m tools.operations.compact_runtime_db swap --db /app/data/posts.db --next /app/data/posts.next.db
python -m tools.operations.compact_runtime_db swap-sidecars --bigram /app/data/bigram_index.db --bigram-next /app/data/bigram_index.next.db --symbol /app/data/symbol_index.db --symbol-next /app/data/symbol_index.next.db
```

`swap` 会把旧库改名为 `posts.before-时间.db`，再把 `posts.next.db` 放到 `/app/data/posts.db`。执行后重启 Railway，让已有 SQLite 连接重新打开。异常时使用命令输出的备份路径回滚：

```bash
python -m tools.operations.compact_runtime_db rollback --db /app/data/posts.db --backup /app/data/posts.before-YYYYMMDD-HHMMSS.db
```

## Railway 设置

```text
Volume Mount Path: /app/data
Start Command: bash start.sh
Healthcheck Path: /healthz
Healthcheck Timeout: 300
```

如果 Railway 开启应用休眠，后台 scheduler 也会暂停。要持续自动更新，应关闭 Serverless/App Sleeping。

## 部署验证

```powershell
railway status
railway logs --lines 120
try { (Invoke-WebRequest -UseBasicParsing https://rucxlb.up.railway.app/healthz -TimeoutSec 20).Content } catch { $_.Exception.Message; exit 1 }
```

需要同时看到：

- service 为 Online。
- 日志出现 `[boot] Using SQLite DB: /app/data/posts.db`。
- 日志出现 `[scheduler] trickle enabled ...`。
- `/healthz` 返回 `{"ok": true}`。

正常调度日志类似：

```text
[scheduler] quota discover_active active_list_calls_reserved=...
[discover-active:lists2] done {...}
[scheduler] done discover_active exit=0
```

不要仅凭 GitHub push 判断部署成功。完整 DB 备份会消耗大量 Volume 空间，5GB Volume 下应把长期备份保存到外部对象存储。
