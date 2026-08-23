# crawler 模块

`crawler/` 只负责上游 API 客户端、响应标准化、扫描策略、写锁和爬取流程编排；SQLite schema 与写入由 `storage/post_writer.py` 负责，Railway 时间表、配额和暂停由 `jobs/scheduler.py` 负责。

## 文件职责

| 文件 | 职责 |
|---|---|
| `client.py` | session cookie、上游请求和错误语义映射 |
| `automatic_quota.py` | scheduler 子进程每次真实源请求前的原子额度领取 |
| `cookie_pool.py` | 固定 cookie lane 配置、任务路由和 lane 统计；不保存 cookie 值 |
| `task_routing.py` | 统一 list1/list2、当前 ID 表详情、历史详情和缺口探测的任务名称 |
| `id_ledger.py` | list1/list2 观察事件、首次来源和详情时间线；不发网络请求 |
| `normalizer.py` | 把源 API 响应标准化，并拒绝会破坏本地完整数据的异常详情 |
| `service.py` | discover、trickle-fill 与兼容扫描流程 |
| `strategies/page_scan.py` | 页扫描的最小页数和连续无收益停止状态 |
| `lock.py` | 带 token 和心跳租约的 SQLite 跨进程、跨容器写锁 |
| `config.py` | cookie 配置读取 |
| `cli.py` | 当前命令和兼容别名 |

依赖方向：

```text
crawler.cli / jobs.scheduler
  -> crawler.service
  -> crawler.client + crawler.cookie_pool + crawler.automatic_quota + crawler.normalizer
  -> storage.post_writer
```

默认模式仍是一个顺序请求流；启用 `CRAWLER_PARALLEL_LANES=1` 后，主 scheduler 负责新 cookie，`jobs.lane_worker` 负责旧 cookie。两个 worker 共用 SQLite 队列，先原子认领并记录 owner/lane，详情完成或失败后才释放；WAL 和短写事务允许不同 lane 的 HTTP 请求重叠，同一 ID 仍不能被两条任务同时认领。遇到上游 `rate_limited` 不会静默切换 lane，错误 lane 单独暂停；只有本地 lane 配额耗尽时才会选择另一个已配置 lane。

新逻辑不要写回根兼容入口 `crawler_db.py`，不要在 `jobs.scheduler` 中复制爬取判定，也不要让 Strategy 直接写 SQLite。

## 运维事实源

命令、请求成本、队列优先级、停止条件、每日配额、限流暂停和 Railway 查询方法统一维护在 [docs/operations/crawler.md](../docs/operations/crawler.md)。本文件不复制参数表，避免模块说明与线上运行手册再次分叉。

CLI 契约可用以下命令核对：

```powershell
python crawler_db.py --help
python crawler_db.py discover-latest --help
python crawler_db.py trickle-fill --help
python -B -m pytest tests/test_cli_contract.py tests/test_automatic_quota.py tests/test_crawler_lock.py tests/test_crawler_service.py tests/test_crawler_strategies.py -q
```

列表发现会利用已经取得的 `lists/lists2` 快照刷新所有已观察帖子的评论、点赞、蹲蹲和源端观测时间，不额外请求详情。已在列表中观察或本地已有归档的帖子若详情返回 `not_found`，保留本地内容并标记为 `deleted_or_unavailable`；public 搜索隐藏，Admin 保留查看。ID 缺口探测得到的 `not_found` 不足以证明帖子曾存在，不会创建删除记录。
