# RUC小喇叭 高级搜索

中国人民大学 RUC 小喇叭匿名论坛的 SQLite 数据库爬取与搜索工具。

线上地址：[https://rucxlb.up.railway.app](https://rucxlb.up.railway.app)

当前版本是 DB-only 架构：

- 网站只读取 `data/posts.db`
- 爬虫只通过 `crawler_db.py` 写 SQLite
- 不再使用 CSV 作为运行时数据源

## 本地启动

```powershell
python server.py
```

若存在 `data/bigram_index.db`，启动时会自动启用 Bigram 搜索；无需额外参数。
两字及以上关键词使用 Bigram，单字关键词回退 `LIKE`。

首次构建本地索引：

```powershell
python -m tools.benchmarks.benchmark_bigram_index --db-path data\posts.db --output-dir data --sample-mod 1 --only-bigram
```

正确性与速度对比：

```powershell
python -m tools.benchmarks.benchmark_search_backends
```

慢速单字和复杂 Admin 搜索使用按页游标扫描：先按所选排序读取候选，找到一页
结果即返回。页面显示已检查数量，上一页会恢复已展开正文和已加载评论。

游标首屏 benchmark：

```powershell
python -m tools.benchmarks.benchmark_cursor_pagination --repeats 3
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
python crawler_db.py scan-id-range --from-date 2026-06-01 --db-path data\posts.db
```

也可明确指定范围：

```powershell
python crawler_db.py scan-id-range --start-id 5004321 --end-id 5066654 --db-path data\posts.db
```

补新帖：

```powershell
python crawler_db.py sync-latest --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补新回复/活跃帖：

```powershell
python crawler_db.py sync-active --db-path data\posts.db --pages 500 --min-pages 20 --stop-unchanged 300
```

补历史旧页：

```powershell
python crawler_db.py scan-history --endpoint lists --db-path data\posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600
```

更多说明见 [docs/operations/crawler.md](docs/operations/crawler.md)。

## 项目结构

```text
server.py                 Web 兼容启动入口
crawler_db.py             爬虫兼容 CLI 入口
app/                      Web、Repository、Service、AI 与 HTTP 路由
crawler/                  API Client、规范化、扫描策略与执行服务
storage/post_writer.py     SQLite 写入与搜索索引维护
jobs/                     Railway 调度与运行时备份
tools/                    迁移、审计、性能和运维工具
tests/                    单元、集成、契约和性能测试
data/posts.db             主数据库（不进入 Git）
data/bigram_index.db      可重建 Bigram 搜索旁路索引（不进入 Git）
```

## Railway

Volume 挂载：

```text
/app/data
```

线上 DB：

```text
/app/data/posts.db
/app/data/bigram_index.db
```

启动命令：

```bash
bash start.sh
```

当前由 `start.sh` 在同一服务中启动 `jobs.scheduler`。SQLite 和 Railway
Volume 不适合未经验证地由多个服务同时挂载写入，因此暂不拆成多个 Cron 服务。

旧命令 `new`、`refresh`、`backfill`、`phase1`、`detail-fill` 仍可使用，
但新文档统一采用语义更明确的正式命令。

## License

MIT。
