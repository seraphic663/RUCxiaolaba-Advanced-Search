# RUC小喇叭 高级搜索

中国人民大学 RUC 小喇叭匿名论坛的 SQLite 数据库爬取与搜索工具。

当前版本是 DB-only 架构：

- 网站只读取 `data/posts.db`
- 爬虫只通过 `crawler_db.py` 写 SQLite
- 不再使用 CSV 作为运行时数据源

## 本地启动

```powershell
python server.py
```

指定端口或 DB：

```powershell
python server.py --sqlite-db data\posts.db --port 8099
```

## 更新数据

配置 cookie：

```text
data/config.txt
ys7_ysxy_session=你的cookie
```

连续 ID 全量扫描，可按日期自动确定范围：

```powershell
python crawler_db.py phase1 --from-date 2026-06-01 --db-path data\posts.db
```

也可明确指定范围：

```powershell
python crawler_db.py phase1 --start-id 5004321 --end-id 5066654 --db-path data\posts.db
```

补新帖：

```powershell
python crawler_db.py new --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补新回复/活跃帖：

```powershell
python crawler_db.py refresh --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补历史旧页：

```powershell
python crawler_db.py backfill --endpoint lists --db-path data\posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600
```

更多说明见 [docs/quick-start.md](docs/quick-start.md)。

## 项目结构

```text
server.py                 Web 服务与 API
crawler_db.py             唯一爬虫入口，直接写 SQLite
storage/sqlite_store.py   SQLite 写入与索引维护
scripts/backup_runtime.py 运行时数据备份
data/posts.db             主数据库
```

## Railway

Volume 挂载：

```text
/app/data
```

线上 DB：

```text
/app/data/posts.db
```

启动命令：

```bash
bash start.sh
```

建议把新帖、回复刷新、历史补全拆成独立 Railway Cron 服务，并共用同一个 Volume。

## License

MIT。
