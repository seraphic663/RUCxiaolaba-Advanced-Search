# 架构切换 Demo

这个 demo 是旁路样例，不修改现有 `server.py`、爬虫脚本或生产 CSV。目标是展示如何把现在分散的爬虫和 CSV 流程收敛成：

```text
统一入口 crawler.py
  ├─ full-scan      历史 ID 扫描
  ├─ incremental    新帖补扫 + 评论更新
  ├─ detail-fill    对已有 ID 补详情
  ├─ import-csv     从 CSV 快照导入主存储
  ├─ verify         数据完整性检查
  └─ export-csv     从主存储导出发布 CSV

统一数据模型 Post
统一存储接口 Store
  ├─ CsvStore       兼容当前 CSV 发布方式
  └─ SqliteStore    后续切 DB 的目标形态
```

## 运行

SQLite 模式：

```bash
python3 demo/architecture_switch_demo.py full-scan --store sqlite
python3 demo/architecture_switch_demo.py incremental --store sqlite
python3 demo/architecture_switch_demo.py verify --store sqlite
python3 demo/architecture_switch_demo.py export-csv --store sqlite
```

CSV 模式：

```bash
python3 demo/architecture_switch_demo.py full-scan --store csv
python3 demo/architecture_switch_demo.py incremental --store csv
python3 demo/architecture_switch_demo.py verify --store csv
```

CSV 转 SQLite：

```bash
rm -rf demo/runtime/*
python3 demo/architecture_switch_demo.py full-scan --store csv
python3 demo/architecture_switch_demo.py import-csv --store sqlite --csv-path demo/runtime/posts_final.demo.csv
python3 demo/architecture_switch_demo.py verify --store sqlite
```

默认输出在 `demo/runtime/`，该目录有独立 `.gitignore`，不影响现有数据。

本地网站测试：

```bash
python3 server.py
# 打开 http://127.0.0.1:8080/demo
```

`/demo` 页面读取 `demo/runtime/posts.demo.db`，用于验证网站层已经可以从新架构的 SQLite 存储取数。

## 对当前项目的迁移含义

短期可以只借鉴两个点：

1. `server.py` 统一读一个最终数据源，例如 `posts_final.csv`。
2. 所有爬虫结果先标准化成同一套 `Post` 字段，再写入目标存储。

中期把 `spider.py`、`spider_danger.py`、`scan_full.py`、`crawl_detail.py`、`update_full.py` 收敛成一个入口：

```bash
python crawler.py full-scan
python crawler.py incremental
python crawler.py detail-fill --ids 4990001 4990002
python crawler.py import-csv --csv-path data/posts_final.csv
python crawler.py verify
python crawler.py export-csv
```

长期把主存储切到 SQLite：

```text
采集脚本 -> SQLite upsert -> 服务端查询 SQLite
                    └── export-csv/export-gz 用于发布快照
```

这样 CSV 就从“运行时主数据库”降级为“发布/备份格式”，能减少文件写坏、全量重载、数据源混乱和权限脱敏混乱的问题。
