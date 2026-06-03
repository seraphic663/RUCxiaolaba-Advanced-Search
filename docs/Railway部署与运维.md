# Railway 部署与运维

## 服务组成

推荐 Railway 项目中至少有：

```text
Web Service      跑 server.py
Volume           挂载 /app/data
Cron: new        定时补新帖
Cron: refresh    定时补评论/活跃帖
Cron: backfill   低频补历史
```

## Volume

挂载路径：

```text
/app/data
```

线上必要文件：

```text
/app/data/posts.db
/app/data/config.txt
/app/data/admin_password.txt
/app/data/feedback.jsonl
/app/data/checkin_count.json
```

不要上传：

```text
data/railway_sync/*
temp/*
tests/*
docs/*
```

代码随 Git 部署，运行数据留在 Volume。

## Web 启动

`railway.toml`：

```toml
[deploy]
  startCommand = "bash start.sh"
  healthcheckPath = "/healthz"
```

`start.sh` 会：

1. 找到 `/app/data/posts.db`
2. 尝试查询 `posts`
3. 打印帖子数和最新时间
4. 启动 `server.py`

## 环境变量

推荐：

```text
SQLITE_DB=/app/data/posts.db
```

`DATA_BACKEND` 已不需要，当前只支持 SQLite。

## Cron 命令

Railway 官方 Cron 机制是：给某个服务设置 Cron Schedule 后，Railway 会按 crontab 表达式启动该服务的 Start Command；任务应执行完就退出。若上一次执行仍处于 Active，下一次计划执行会被跳过。Cron 使用 UTC 时间，最短间隔不能小于 5 分钟。

因此本项目不要在 Web 服务里写内置定时器，而是建 3 个独立 Cron 服务，共用同一个 `/app/data` Volume。

### Cron 服务 1：新帖更新

新帖：

```bash
python crawler_db.py new --db-path /app/data/posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

建议 Cron Schedule：

```text
*/20 * * * *
```

含义：每 20 分钟执行一次。

### Cron 服务 2：评论/活跃帖更新

评论/活跃：

```bash
python crawler_db.py refresh --db-path /app/data/posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

建议 Cron Schedule：

```text
10,40 * * * *
```

含义：每小时第 10、40 分钟执行。

### Cron 服务 3：历史补全

历史：

```bash
python crawler_db.py backfill --endpoint lists --db-path /app/data/posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600
```

建议 Cron Schedule：

```text
30 19 * * *
```

含义：UTC 19:30，即北京时间次日 03:30 左右执行。

## Railway UI 配置步骤

每个 Cron 服务都按同样方式创建：

1. `Add New Service`。
2. 选择同一个 GitHub Repo。
3. 给服务改名，例如：

```text
crawler-new
crawler-refresh
crawler-backfill
```

4. 给该服务挂载同一个 Volume：

```text
Mount Path: /app/data
```

5. 在服务的 Deploy/Settings 中设置 Start Command：

```bash
python crawler_db.py new --db-path /app/data/posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

或对应的 `refresh/backfill` 命令。

6. 在服务 Settings 的 `Cron Schedule` 填 crontab 表达式。
7. 保存后手动触发一次或等待下一次 Cron。
8. 看 Logs，确认最后出现类似：

```text
[incremental] done {"pages": ..., "seen": ..., "new": ..., "updated": ...}
```

## 错峰建议

```text
new      00, 20, 40 分
refresh 10, 40 分
backfill 凌晨低频
```

即使爬虫有锁，也不要让多个任务同一分钟启动。

## Cron 常见问题

### 为什么 Cron 没有按时跑？

Railway Cron 使用 UTC，不是北京时间。

如果上一次执行没有退出，下一次会跳过。`crawler_db.py` 是短任务，正常会退出；如果 Logs 显示服务一直 Active，要检查是否卡在网络请求、cookie 过期或 DB 锁。

### 为什么提示 cookie_expired？

更新 Volume 中的：

```text
/app/data/config.txt
```

内容：

```text
ys7_ysxy_session=新的cookie
```

### 多个 Cron 同时写 DB 会怎样？

`crawler_db.py` 会创建：

```text
/app/data/posts.db.crawler.lock
```

其他爬虫会等待锁。但为了减少等待和 Railway 跳过执行，仍应错峰。

### Web 服务需要重启吗？

通常不需要。Web 服务每次查询都读同一个 SQLite 文件，爬虫写入后搜索会读到新数据。

## 上传 DB

直接上传大 DB 容易中断。推荐：

1. 本地确认 DB 完整。
2. 使用 Railway Volume 上传。
3. 上传后 SSH 验证：

```bash
python - <<'PY'
import sqlite3, os
p="/app/data/posts.db"
print(os.path.getsize(p))
c=sqlite3.connect(p)
print(c.execute("select count(*) from posts").fetchone())
print(c.execute("select id, create_time from posts order by create_time desc, id desc limit 1").fetchone())
c.close()
PY
```

如果出现：

```text
database disk image is malformed
```

说明上传文件损坏，需要重新上传。

## 上传运行态文件

Railway 上有两类文件：

```text
代码文件：server.py / crawler_db.py / templates / docs 等
运行态文件：posts.db / config.txt / admin_password.txt / feedback.jsonl / checkin_count.json
```

代码文件不要传到 Volume，随 Git 部署即可。Volume 只放运行态文件。

### 什么时候不用重传 DB

如果线上 `/app/data/posts.db` 和本地 `data/posts.db` 哈希一致，就不要重传大 DB。

本地检查：

```powershell
python -c "import sqlite3,os,hashlib;p='data/posts.db';print(os.path.getsize(p));print(hashlib.file_digest(open(p,'rb'),'sha256').hexdigest());c=sqlite3.connect(p);print(c.execute('select count(*) from posts').fetchone());print(c.execute('select id, create_time from posts order by create_time desc, id desc limit 1').fetchone());print(c.execute('pragma integrity_check').fetchone()[0]);c.close()"
```

线上轻量检查：

```powershell
railway ssh "python - <<'PY'
import sqlite3, os
p='/app/data/posts.db'
print(os.path.getsize(p))
c=sqlite3.connect(p)
print(c.execute('select count(*) from posts').fetchone())
print(c.execute('select id, create_time from posts order by create_time desc, id desc limit 1').fetchone())
c.close()
PY"
```

不要在线上频繁执行 `sha256` 或 `pragma integrity_check`。这两个操作会顺序读取整个 3-4GB SQLite 文件，Railway 的内存图会把文件页缓存也计入 cgroup memory，Estimated Usage 会短时间飙高。

只有在刚上传大 DB 且怀疑文件损坏时，才执行一次完整校验：

```powershell
railway ssh "python - <<'PY'
import sqlite3, hashlib
p='/app/data/posts.db'
print(hashlib.file_digest(open(p,'rb'), 'sha256').hexdigest())
c=sqlite3.connect(p)
print(c.execute('pragma integrity_check').fetchone()[0])
c.close()
PY"
```

校验后如果 Railway RAM 图长期停在 4GB 左右，可重启 Web Service 释放文件页缓存：

```powershell
railway restart --yes
```

若 Railway API 超时，也可临时使用 SSH 让平台拉起新实例：

```powershell
railway ssh "kill -9 1"
```

### 上传小文件

当前必要小文件：

```powershell
railway volume files --volume rucxiaolaba-advanced-search-volume upload data\config.txt /config.txt --overwrite
railway volume files --volume rucxiaolaba-advanced-search-volume upload data\admin_password.txt /admin_password.txt --overwrite
railway volume files --volume rucxiaolaba-advanced-search-volume upload data\checkin_count.json /checkin_count.json --overwrite
railway volume files --volume rucxiaolaba-advanced-search-volume upload data\feedback.jsonl /feedback.jsonl --overwrite
```

上传后确认：

```powershell
railway ssh "ls -lah /app/data && python - <<'PY'
from pathlib import Path
for name in ['posts.db','config.txt','admin_password.txt','feedback.jsonl','checkin_count.json']:
    p=Path('/app/data')/name
    print(name, p.exists(), p.stat().st_size if p.exists() else None)
PY"
```

## 运行时同步

本地拉取 feedback/checkin：

```powershell
.\scripts\sync_railway_runtime.ps1 -Volume rucxiaolaba-advanced-search-volume
```

默认覆盖：

```text
data/railway_sync/feedback.latest.jsonl
data/railway_sync/checkin_count.latest.json
```

需要留档时：

```powershell
.\scripts\sync_railway_runtime.ps1 -Volume rucxiaolaba-advanced-search-volume -Archive
```

## 备份

小文件备份：

```bash
python scripts/backup_runtime.py --data-dir /app/data --keep 72
```

完整 DB 备份：

```bash
python scripts/backup_runtime.py --data-dir /app/data --include-db --keep 2
```

注意：5GB Volume 下不要频繁保留完整 DB 备份。更推荐外部对象存储。
