# 快速启动与命令行说明

本文说明当前 DB-first 架构下如何本地启动、更新数据、部署 Railway，以及常见问题。

## 1. 本地启动网站

默认启动：

```powershell
python server.py
```

默认行为：

1. 使用 SQLite 后端。
2. 默认读取 `data/posts.db`。
3. 不加载 2GB+ CSV。

推荐明确启动：

```powershell
python server.py --db
```

指定 DB：

```powershell
python server.py --db --sqlite-db data\posts.db
```

指定端口：

```powershell
python server.py --db --sqlite-db data\posts.db --port 8099
```

旧 CSV 模式：

```powershell
python server.py --csv
```

CSV 模式会预加载 CSV 到内存，不推荐日常使用。

## 2. server.py 参数

| 参数 | 作用 |
|---|---|
| `--db` | 使用 SQLite 后端，默认行为 |
| `--csv` | 使用旧 CSV 后端，会全量加载 CSV |
| `--sqlite-db PATH` | 指定 SQLite DB 路径，同时隐含 `--db` |
| `--port PORT` | 指定端口，默认读取环境变量 `PORT`，否则 8080 |
| `--host HOST` | 指定监听地址，默认读取环境变量 `HOST`，否则 `0.0.0.0` |

环境变量优先级：

| 环境变量 | 作用 |
|---|---|
| `DATA_BACKEND=sqlite/csv` | 指定默认后端 |
| `SQLITE_DB=...` | 指定默认 SQLite DB |
| `PORT=...` | Railway/本地端口 |
| `HOST=...` | 监听地址 |

本地如果怀疑环境变量干扰：

```powershell
Remove-Item Env:SQLITE_DB -ErrorAction SilentlyContinue
Remove-Item Env:DATA_BACKEND -ErrorAction SilentlyContinue
python server.py
```

启动日志应包含：

```text
[init] SQLite backend: 543xxx posts from ...\data\posts.db (latest=2026-06-02 ...)
Backend: sqlite (...\data\posts.db)
```

## 3. 更新数据

当前推荐用 `crawler_db.py` 直接更新 SQLite。

先补新增帖：

```powershell
python crawler_db.py incremental --endpoint lists --db-path data\posts.db --pages 200 --min-pages 10 --stop-unchanged 160
```

再补旧帖评论变化：

```powershell
python crawler_db.py incremental --endpoint lists2 --db-path data\posts.db --pages 200 --min-pages 10 --stop-unchanged 160
```

首次运行建议 dry-run：

```powershell
python crawler_db.py incremental --endpoint lists --db-path data\posts.db --pages 20 --min-pages 3 --stop-unchanged 80 --dry-run
python crawler_db.py incremental --endpoint lists2 --db-path data\posts.db --pages 20 --min-pages 3 --stop-unchanged 80 --dry-run
```

限制详情请求数，适合小范围验证：

```powershell
python crawler_db.py incremental --endpoint lists --db-path data\posts.db --pages 200 --stop-unchanged 160 --max-details 100
```

## 4. crawler_db.py 参数

### 4.1 incremental

```powershell
python crawler_db.py incremental [options]
```

| 参数 | 作用 |
|---|---|
| `--endpoint lists` | 扫新帖流，用于新增帖子 |
| `--endpoint lists2` | 扫活跃/更新流，用于刷新旧帖评论 |
| `--db-path PATH` | SQLite DB 路径 |
| `--pages N` | 最多扫描 N 页 |
| `--min-pages N` | 至少扫描 N 页后才允许按 unchanged 停止 |
| `--stop-unchanged N` | 连续 N 条无需更新后停止 |
| `--max-details N` | 最多请求 N 个详情页，0 表示不限 |
| `--dry-run` | 只请求和判断，不写 DB |
| `--min-delay / --max-delay` | 请求之间随机等待秒数 |

判定逻辑：

```text
DB 没有该 id
  -> 抓 detail
  -> 写 posts/comments/search_index

DB 有该 id，但 comment_count 不同
  -> 抓 detail
  -> 覆盖帖子和评论
  -> 刷新 search_index

DB 有该 id，comment_count 相同
  -> unchanged
```

目前旧帖是否更新主要看 `comment_count`。如果只是 views、hot、star 变化但评论数不变，默认不刷新。

### 4.2 detail-fill

按指定 ID 补详情：

```powershell
python crawler_db.py detail-fill --db-path data\posts.db --ids 5014356,5018419
```

dry-run：

```powershell
python crawler_db.py detail-fill --db-path data\posts.db --ids 5014356 --dry-run
```

### 4.3 mock-csv

本地测试用，从 CSV 抽样写临时 DB：

```powershell
python crawler_db.py mock-csv --db-path temp\crawler_db_mock.db --init-schema --csv-path data\posts_final.csv --limit 200
```

## 5. Railway 部署

Volume 设置：

```text
Mount Path: /app/data
Size: 5GB
```

上传本地瘦身库：

```text
本地 data/posts.db
-> Railway /app/data/posts.db
```

Railway 环境变量：

```text
DATA_BACKEND=sqlite
SQLITE_DB=/app/data/posts.db
```

启动命令应使用 SQLite，不要再下载 CSV：

```bash
python -u server.py --db --sqlite-db "${SQLITE_DB:-/app/data/posts.db}"
```

线上增量更新命令：

```bash
python crawler_db.py incremental --endpoint lists --db-path /app/data/posts.db --pages 200 --min-pages 10 --stop-unchanged 160
python crawler_db.py incremental --endpoint lists2 --db-path /app/data/posts.db --pages 200 --min-pages 10 --stop-unchanged 160
```

线上要跑爬虫，需要 `/app/data/config.txt` 中有 cookie。

## 6. 常见 QA

### Q1: 为什么页面还是旧时间？

先确认 server 用的是哪个 DB。启动日志会显示：

```text
Backend: sqlite (...posts.db)
```

如果看到 `posts.db` 或旧路径，明确指定：

```powershell
python server.py --db --sqlite-db data\posts.db
```

### Q2: 更新 DB 后需要重启 server 吗？

如果 server 一开始就指向同一个 DB 文件，一般不需要。SQLite 查询会读到新数据。

如果之前 server 指向了旧 DB 文件，需要重启并指定正确路径。

### Q3: 现在的 `posts.db` 是什么？

现在的 `data/posts.db` 是无损瘦身主库：

- 删除 `posts.comments_json`
- 保留 `comments.raw_json`
- 保留 `comments` 结构化表
- 保留 `search_index`

旧完整库已经删除；当前 `data/posts.db` 约 3.98GB，适合 Railway 5GB Volume。

### Q4: CSV 还能用吗？

能。旧模式：

```powershell
python server.py --csv
```

但 DB-first 是当前推荐路线，CSV 逐步退为备份/导出。

### Q5: `lists` 和 `lists2` 为什么都要跑？

真实 API 表现：

- `lists` 更适合补新帖。
- `lists2` 更适合补评论变化和活跃旧帖。

推荐顺序：

```powershell
python crawler_db.py incremental --endpoint lists --db-path data\posts.db --pages 200 --min-pages 10 --stop-unchanged 160
python crawler_db.py incremental --endpoint lists2 --db-path data\posts.db --pages 200 --min-pages 10 --stop-unchanged 160
```

### Q6: 如何确认 DB 最新？

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/posts.db'); print(c.execute(\"select id, create_time from posts order by create_time desc, id desc limit 5\").fetchall())"
```

### Q7: 为什么二字搜索慢？

SQLite trigram FTS 对 3 字及以上中文效果好。1-2 字查询会回退 `LIKE`，会慢。建议搜索时尽量输入 3 字以上关键词。

### Q8: 哪些文件不该上传 Railway Volume？

不要上传：

```text
data/posts_final.csv   # 2GB+ CSV
data/railway_sync/*
legacy/*
```

Volume 只需要：

```text
/app/data/posts.db
/app/data/feedback.jsonl
/app/data/checkin_count.json
/app/data/admin_password.txt
/app/data/config.txt   # 如果线上爬虫需要
```

## 7. Railway DB-only 部署步骤

目标：线上只使用 SQLite DB，不下载 CSV，不加载 CSV。

### 7.1 Railway Volume 设置

在 Railway 项目里给 Web 服务挂载 Volume：

```text
Volume name: rucxiaolaba-advanced-search-volume
Mount Path: /app/data
Size: 5GB
```

如果当前 Volume 已经是 `/app/data`，不用改。

### 7.2 本地准备线上 DB 文件

本地候选库是：

```text
data/posts.db
```

上传到 Railway Volume 后，线上文件名必须是：

```text
/app/data/posts.db
```

也就是：

```text
本地 data/posts.db -> 线上 /app/data/posts.db
```

不要上传这些文件到 Volume：

```text
data/posts_final.csv
data/posts_final.csv.gz
data/railway_sync/*
legacy/*
```

### 7.3 Railway 环境变量

在 Railway 服务 Variables 中设置：

```text
DATA_BACKEND=sqlite
SQLITE_DB=/app/data/posts.db
```

如果线上还要运行爬虫更新，还需要把 cookie 配置放到：

```text
/app/data/config.txt
```

内容格式：

```text
ys7_ysxy_session=你的cookie
```

### 7.4 启动脚本

当前 `start.sh` 已经是 DB-only：

```bash
DB_PATH="${SQLITE_DB:-/app/data/posts.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "[boot] SQLite DB not found: $DB_PATH"
  exit 1
fi

exec python -u server.py --db --sqlite-db "$DB_PATH"
```

它不会下载 CSV。DB 不存在会直接失败。

### 7.5 railway.toml

保持：

```toml
[deploy]
  startCommand = "bash start.sh"
  healthcheckPath = "/healthz"
```

### 7.6 部署后日志检查

部署成功后，Railway 日志应该看到：

```text
[boot] Using SQLite DB: /app/data/posts.db
[boot] posts=543xxx latest=2026-06-02 ...
[init] SQLite backend: 543xxx posts from /app/data/posts.db
Backend: sqlite (/app/data/posts.db)
```

如果看到 CSV download、posts_scan.csv、Loading data CSV，说明部署的代码不是当前版本。

### 7.7 线上更新数据

进入 Railway Shell 或用 Railway CLI 执行：

```bash
python crawler_db.py incremental --endpoint lists --db-path /app/data/posts.db --pages 200 --min-pages 10 --stop-unchanged 160
python crawler_db.py incremental --endpoint lists2 --db-path /app/data/posts.db --pages 200 --min-pages 10 --stop-unchanged 160
```

首次线上更新建议限量：

```bash
python crawler_db.py incremental --endpoint lists --db-path /app/data/posts.db --pages 50 --min-pages 5 --stop-unchanged 80 --max-details 100
python crawler_db.py incremental --endpoint lists2 --db-path /app/data/posts.db --pages 50 --min-pages 5 --stop-unchanged 80 --max-details 100
```

更新后不用重启服务，只要服务一直指向 `/app/data/posts.db`。
