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
new       每 8 小时
refresh   每 8 小时，与 new 错开 4 小时
backfill  每 24 小时
phase1    每 7 天，重扫最近 8 个自然日
```

调度器顺序执行任务，并使用 `posts.db.crawler.lock` 防止并发写入。
更新完成后 Web 无需重启。服务部署或重启后不会立即扫描：首轮 `new`
等待 4 小时，首轮 `refresh` 等待 8 小时。

API 每页最多 20 条、有效页最多约 100 页。请求超过第 100 页时会重复
末页，不会得到更旧数据。因此常规任务允许连续未变化后提前停止，每日
`backfill` 则完整扫描第 2–100 页，用于补偿移动分页或短时中断造成的遗漏。

可选间隔变量，单位为秒：

```text
CRAWLER_NEW_INTERVAL=28800
CRAWLER_REFRESH_INTERVAL=28800
CRAWLER_BACKFILL_INTERVAL=86400
CRAWLER_PHASE1_INTERVAL=604800
```

## Phase 1 全量 ID 补扫

Railway 每 7 天自动重扫最近 8 个自然日。执行时间保存在 Volume 的
`.phase1_weekly_last` 标记中，服务重启不会重置周期。也可手动按日期执行：

```powershell
python crawler_db.py phase1 --from-date 2026-06-01 --db-path data\posts.db
```

限制结束日期：

```powershell
python crawler_db.py phase1 --from-date 2026-06-01 --to-date 2026-06-03 --db-path data\posts.db
```

也可以明确指定 ID：

```powershell
python crawler_db.py phase1 --start-id 5004321 --end-id 5066654 --db-path data\posts.db
```

Phase 1 每 500 个 ID 保存一次断点，重复执行相同范围会自动续扫。需要从头重扫时
加 `--restart`。线上执行可先进入持久 SSH 会话：

```powershell
railway ssh --session phase1
python crawler_db.py phase1 --from-date 2026-06-01 --db-path /app/data/posts.db --config /app/data/config.txt
```

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
