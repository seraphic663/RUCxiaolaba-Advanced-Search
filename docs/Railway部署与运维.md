# Railway 部署与运维

## 唯一架构

```text
一个 Web Service
一个挂载到 /app/data 的 Volume
server.py 提供网站
railway_scheduler.py 在同一服务内自动更新 SQLite
```

不要创建三个独立 Cron Service。SQLite 与 Volume 由当前 Web Service 独占。

## 首次需要上传

```text
/app/data/posts.db
/app/data/config.txt
```

命令：

```powershell
railway volume files upload data\posts.db /posts.db --overwrite
railway volume files upload data\config.txt /config.txt --overwrite
```

`posts.db` 只在首次部署或灾难恢复时上传。以后爬虫直接更新线上 DB。

## Railway Variables

在 Web Service 的 `Variables` 设置：

```text
SQLITE_DB=/app/data/posts.db
ADMIN_PASSWORD=<固定强密码>
CRAWLER_ENABLED=1
```

不要上传 `admin_password.txt`。反馈与到访人数模块已删除，也不再需要
`feedback.jsonl` 或 `checkin_count.json`。

## 自动更新

`CRAWLER_ENABLED=1` 时，`start.sh` 会在启动网站的同时启动后台调度器：

```text
new       每 30 分钟
refresh   每 60 分钟
backfill  每 24 小时
```

调度器顺序执行任务，并使用 `posts.db.crawler.lock` 防止并发写入。
更新完成后 Web 无需重启。

API 每页最多 20 条、有效页最多约 100 页。请求超过第 100 页时会重复
末页，不会得到更旧数据。因此常规任务允许连续未变化后提前停止，每日
`backfill` 则完整扫描第 2–100 页，用于补偿移动分页或短时中断造成的遗漏。

可选间隔变量，单位为秒：

```text
CRAWLER_NEW_INTERVAL=1800
CRAWLER_REFRESH_INTERVAL=3600
CRAWLER_BACKFILL_INTERVAL=86400
```

## 一次性全量 ID 补扫

需要补齐某个日期以来所有帖子及其当前评论时，设置：

```text
CRAWLER_ID_SCAN_FROM=2026-06-01
CRAWLER_ID_SCAN_WORKERS=4
CRAWLER_ID_SCAN_CHUNK=500
```

下次部署时，调度器会从数据库中该日期的最小帖子 ID 开始，连续扫描到
API 当前最新 ID。每 500 个 ID 提交一次并记录断点；部署中断后会自动续扫，
不会从头开始。日志以 `[id-scan]` 开头。

扫描完成后状态会保存在 SQLite 的 `crawl_state` 表中，同一日期不会重复执行。
确认日志出现 `[id-scan] done` 后，可以删除上述三个变量；常规自动更新仍由
`new`、`refresh`、`backfill` 负责。

## Cookie 更新

当日志出现 `cookie_expired`，只需要覆盖：

```powershell
railway volume files upload data\config.txt /config.txt --overwrite
```

调度器下次执行会读取新 cookie，无需上传 DB。

## 代码更新

```powershell
git add -A
git commit -m "描述"
git push origin main
```

Railway 从 GitHub 自动重新部署。代码文件不要上传到 Volume。

## Railway 设置

```text
Volume Mount Path: /app/data
Start Command: bash start.sh
Healthcheck Path: /healthz
Healthcheck Timeout: 300
```

如果 Railway 开启了应用休眠，后台调度器也会暂停。要持续自动更新，应关闭
Serverless/App Sleeping。

## 日志检查

正常调度日志类似：

```text
[scheduler] start new
[incremental] done {...}
[scheduler] done new exit=0
```

完整 DB 备份会消耗大量 Volume 空间。5GB Volume 下建议把备份保存到外部对象存储。
