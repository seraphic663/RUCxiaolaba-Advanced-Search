# crawler 模块

`crawler/` 只负责上游 API 客户端、响应标准化、扫描策略、写锁和爬取流程编排；SQLite schema 与写入由 `storage/post_writer.py` 负责，Railway 时间表、配额和暂停由 `jobs/scheduler.py` 负责。

## 文件职责

| 文件 | 职责 |
|---|---|
| `client.py` | session cookie、上游请求和错误语义映射 |
| `normalizer.py` | 把源 API 响应标准化为帖子和评论结构 |
| `service.py` | discover、trickle-fill、gap plan/probe 与兼容扫描流程 |
| `strategies/page_scan.py` | 页扫描的最小页数和连续无收益停止状态 |
| `lock.py` | SQLite 跨进程写锁 |
| `config.py` | cookie 配置读取 |
| `cli.py` | 当前命令和兼容别名 |

依赖方向：

```text
crawler.cli / jobs.scheduler
  -> crawler.service
  -> crawler.client + crawler.normalizer
  -> storage.post_writer
```

新逻辑不要写回根兼容入口 `crawler_db.py`，不要在 `jobs.scheduler` 中复制爬取判定，也不要让 Strategy 直接写 SQLite。

## 运维事实源

命令、请求成本、队列优先级、停止条件、每日配额、限流暂停和 Railway 查询方法统一维护在 [docs/operations/crawler.md](../docs/operations/crawler.md)。本文件不复制参数表，避免模块说明与线上运行手册再次分叉。

CLI 契约可用以下命令核对：

```powershell
python crawler_db.py --help
python crawler_db.py discover-latest --help
python crawler_db.py trickle-fill --help
python -B -m pytest tests/test_cli_contract.py tests/test_crawler_service.py tests/test_crawler_strategies.py -q
```
