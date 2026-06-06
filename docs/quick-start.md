# 快速启动与部署

当前项目是 DB-only 架构：网站、admin、爬虫都使用 SQLite `posts.db`，不再使用 CSV。

## 本地启动

默认读取 `data/posts.db`：

```powershell
python server.py
```

指定 DB 或端口：

```powershell
python server.py --sqlite-db data\posts.db --port 8099
```

兼容参数 `--db` 可以保留，但没有实际切换作用：

```powershell
python server.py --db --sqlite-db data\posts.db
```

启动日志应出现：

```text
[init] SQLite backend: ... posts from ...\data\posts.db
Backend: sqlite (...\data\posts.db)
```

## 数据更新

唯一爬虫入口是 `crawler_db.py`。它直接写 SQLite，并用 `data/posts.db.crawler.lock` 防止多个定时任务同时写库。

Phase 1 连续扫描全部 ID，可按日期自动确定范围：

```powershell
python crawler_db.py phase1 --from-date 2026-06-01 --db-path data\posts.db
```

使用 `--to-date` 可以限制结束日期；也可以直接传入 `--start-id` 和
`--end-id`。相同范围中断后再次执行会自动续扫。

补新帖，范围保守偏大：

```powershell
python crawler_db.py new --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补新回复/活跃旧帖，范围保守偏大：

```powershell
python crawler_db.py refresh --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补历史旧页：

```powershell
python crawler_db.py backfill --endpoint lists --db-path data\posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600
```

指定 ID 修复：

```powershell
python crawler_db.py detail-fill --db-path data\posts.db --ids 5014356,5018419
```

小范围验证：

```powershell
python crawler_db.py new --db-path data\posts.db --pages 20 --min-pages 3 --stop-unchanged 80 --max-details 100 --dry-run
python crawler_db.py refresh --db-path data\posts.db --pages 20 --min-pages 3 --stop-unchanged 80 --max-details 100 --dry-run
```

判断逻辑：

```text
DB 没有该 id -> 抓详情 -> 写 posts/comments/search_index
DB 有该 id，但 comment_count 变化 -> 抓详情 -> 覆盖帖子和评论
DB 有该 id，comment_count 相同 -> unchanged
连续 unchanged 达到阈值，且已扫描 min-pages 后停止
```

## Railway 部署

Volume：

```text
Mount Path: /app/data
Size: 5GB
```

线上必须存在：

```text
/app/data/posts.db
/app/data/config.txt          # 线上爬虫需要 cookie
```

环境变量：

```text
SQLITE_DB=/app/data/posts.db
ADMIN_PASSWORD=<固定强密码>
CRAWLER_ENABLED=1
```

启动命令由 `railway.toml` 指向：

```bash
bash start.sh
```

`start.sh` 会检查 DB 是否存在、是否能查询，然后启动：

```bash
python -u server.py --db --sqlite-db "$DB_PATH"
```

## Railway 自动爬取

设置 `CRAWLER_ENABLED=1` 后，`start.sh` 会启动同服务后台调度器：

```text
new       每 30 分钟
refresh   每 60 分钟
backfill  每 24 小时
```

任务直接更新 `/app/data/posts.db`，不需要重新上传 DB。

更详细步骤见 `docs/Railway部署与运维.md`。

## 运行时备份

小文件备份，适合高频：

```bash
python scripts/backup_runtime.py --data-dir /app/data --keep 72
```

默认只备份 `config.txt`。

完整 DB 备份需要额外空间，5GB Volume 下不建议频繁使用：

```bash
python scripts/backup_runtime.py --data-dir /app/data --include-db --keep 2
```

更推荐把 DB 备份放到 Railway Bucket / S3 / R2，而不是长期堆在 Volume。

## 常见 QA

### 为什么页面还是旧时间？

确认 server 指向的 DB：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/posts.db'); print(c.execute(\"select id, create_time from posts order by create_time desc, id desc limit 5\").fetchall())"
```

如果 DB 是新的，server 也指向同一个文件，一般不需要重启。

### CSV 还能用吗？

不能。当前运行路径已经删除 CSV 后端。历史 CSV 流程只作为 git/legacy 归档参考。

### 二字搜索为什么慢？

SQLite trigram FTS 对 3 字及以上中文效果更好。1-2 字查询会回退 `LIKE`，会慢。

### 哪些文件不要上传 Volume？

```text
legacy/*
data/railway_sync/*
data/railway_rescue/*
temp/*
```

Volume 只保留 DB、cookie、反馈、人数和 admin 密码。
