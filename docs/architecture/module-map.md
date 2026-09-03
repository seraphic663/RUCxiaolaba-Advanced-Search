# Python 模块用途与用法

本文是当前生产代码和兼容入口的模块地图。它覆盖会被 Web、crawler 或 scheduler 直接调用的 Python 文件；测试、一次性迁移、审计、抓包和性能脚本按各自目录 README 管理，不在这里复制一份容易过期的清单。

## 先记住三条调用规则

1. Web 从 `server.py` 进入，实际实现位于 `app/`；不要把业务逻辑重新写回根入口。
2. crawler 从 `crawler_db.py` 或 `jobs.scheduler` 进入；详情响应先经过 `crawler.detail_pipeline`，数据库写入经 `storage.post_writer` facade。
3. `storage` 会产生 SQLite 写入副作用；`app.repositories` 默认只读；`app.services.search_request` 和 `crawler.detail_pipeline` 是纯处理边界。

## 根兼容入口

| 文件 | 用途 | 用法与边界 | 主要测试 |
|---|---|---|---|
| `server.py` | 启动 Web 服务并转发兼容导入 | `python server.py`；保留旧参数和 monkey-patch 兼容，不在此添加业务逻辑 | `tests/test_http_contract.py` |
| `crawler_db.py` | 启动 crawler CLI 并转发兼容导入 | `python crawler_db.py --help`；新命令实现放在 `crawler/cli.py` | `tests/test_cli_contract.py` |

## app/

| 文件 | 用途 | 用法与边界 | 主要测试 |
|---|---|---|---|
| `app/config.py` | 读取路径、端口、密码和运行配置 | 由 Web 启动入口创建配置；不读取 crawler cookie 内容以外的业务数据 | `tests/test_config.py`、HTTP 契约 |
| `app/domain/search.py` | 搜索查询类型、Bigram/Symbol 分词和 `SearchQuery` 数据模型 | 纯函数和不可变查询模型；不连接数据库 | `tests/test_search_bigram.py`、`tests/test_cursor_search.py` |
| `app/http/server.py` | HTTP handler、请求上下文和兼容服务函数 | 组装路由、service 和 template；不要在这里写 SQL | `tests/test_http_contract.py` |
| `app/http/router.py` | 根据路径分发 public/admin 路由 | 只做路由匹配和 handler 调用 | `tests/test_http_contract.py` |
| `app/http/routes/public.py` | 首页、搜索、评论、分类和公开健康接口 | 公开响应不得返回真实用户 ID；查询参数转换后交给 `SearchService` | `tests/test_http_contract.py` |
| `app/http/routes/admin.py` | 管理登录、Admin 搜索和管理员状态接口 | 只在已认证上下文中返回管理字段；状态变更应遵守认证/CSRF 契约 | `tests/test_http_contract.py` |
| `app/http/routes/admin_crawl.py` | Admin 上游预览、选中任务和人工抓取接口 | 通过 `AdminCrawlService`，不直接发 API 或写主库 | `tests/test_admin_crawl_service.py`、HTTP 契约 |
| `app/repositories/connections.py` | 创建主库和 sidecar 的只读连接 | 由 Repository 使用；连接必须只读，不能在这里加入写入逻辑 | Repository/search tests |
| `app/repositories/post_repository.py` | 读取帖子概览、时间范围和分类统计 | 只读主库；不负责搜索 SQL 编译 | HTTP/Repository tests |
| `app/repositories/search_repository.py` | 编译并执行帖子、评论、sidecar 搜索及评论树查询 | 接收 `SearchQuery`；当前仍是较大的读模型，修改搜索语义必须补 numbered/cursor 对照测试 | `tests/test_search_bigram.py`、`tests/test_cursor_search.py` |
| `app/repositories/admin_crawl_repository.py` | 保存 Admin 预览和人工任务 sidecar | 只处理 `.admin_crawl.db` 的任务状态，不直接请求上游 | `tests/test_admin_crawl_service.py` |
| `app/services/search_request.py` | 构造统一的 `SearchQuery` | 调用 `build_search_query(...)`；同时服务 numbered 和 cursor 搜索；不访问数据库 | `tests/test_search_request.py` |
| `app/services/search_service.py` | 搜索应用服务 facade | 调用 `search()`、`search_cursor()`、`comments()`；不拼重复的 `SearchQuery` | 搜索和 HTTP 契约测试 |
| `app/services/admin_service.py` | 管理状态和本地数据查询 | 只组织 Admin 读操作；不负责上游详情抓取 | HTTP/Admin tests |
| `app/services/admin_crawl_service.py` | Admin 预览、策略判断、任务 worker 和详情写入 | 通过 `crawler.detail_pipeline` 校验详情；`smart/force/queue` 是公开策略边界 | `tests/test_admin_crawl_service.py` |
| `app/services/auth_service.py` | Admin session、CSRF 和认证状态 | 当前是单实例内存 session；不要在多副本部署前假设它可共享 | `tests/test_http_contract.py` |
| `app/services/template_service.py` | 读取和渲染 HTML 模板 | 只负责模板文件，不放业务查询 | HTTP 契约测试 |

## crawler/

| 文件 | 用途 | 用法与边界 | 主要测试 |
|---|---|---|---|
| `crawler/config.py` | crawler 的 API 基础配置和请求头 | 由 client 使用；不放 cookie 值 | client/CLI tests |
| `crawler/client.py` | 小程序 HTTP client、cookie session 和错误映射 | 使用 `MiniProgramClient.article/list_page/search`；真实接口请求必须遵守授权、限流和暂停规则 | `tests/test_admin_crawl_service.py`、`tests/test_cli_contract.py`；HTTP client 专项测试仍待补 |
| `crawler/detail_pipeline.py` | 详情 payload 的标准化结果和安全校验边界 | 调用 `parse_detail_payload(post_id, data)`；返回 `DetailParseResult`；可疑 payload 保留 parsed 值供 partial merge，不写数据库 | `tests/test_detail_pipeline_contract.py` |
| `crawler/normalizer.py` | 把远端字段转换成稳定的帖子/评论结构 | 纯转换和 payload 校验；不请求网络、不提交事务 | crawler/admin tests |
| `crawler/service.py` | 列表发现、详情补全、兼容扫描和流程编排 | `CrawlerService.discover_queue()`、`trickle_fill()`、`fill_details()` 等由 CLI/scheduler 调用；负责流程，不再重复详情解析 | `tests/test_crawler_service.py` |
| `crawler/automatic_quota.py` | 每次真实源请求前领取自动额度 | 由 client/子进程环境调用；遇到额度阻断必须停止，不用于规避上游限制 | `tests/test_automatic_quota.py` |
| `crawler/manual_quota.py` | Admin 预览和人工详情额度 | 由 `AdminCrawlService` 使用；与自动额度分开记账但仍受上游 session 合规约束 | Admin/quota tests |
| `crawler/cookie_pool.py` | 固定授权 session lane、任务路由和 lane 统计 | 只读取配置文件路径，不保存 cookie 值；不能把它当作规避限流机制 | `tests/test_cookie_pool.py` |
| `crawler/task_routing.py` | 统一任务类型常量和规范化 | 只处理 `list_new/list_active/id_followup/history_detail` 等语义，不发请求 | crawler/scheduler tests |
| `crawler/id_ledger.py` | list1/list2 观察、ID 台账和详情时间线 | 只写传入的 SQLite connection；不负责 HTTP 请求 | `tests/test_id_ledger.py`、ledger integration |
| `crawler/lock.py` | SQLite 跨进程/跨容器写锁和租约 | 使用 `database_write_lock()`；锁过期和心跳语义不能随意简化 | `tests/test_crawler_lock.py` |
| `crawler/strategies/page_scan.py` | 页扫描停止条件和进度状态 | 纯策略状态；不直接写 SQLite | `tests/test_crawler_strategies.py` |
| `crawler/cli.py` | 当前 crawler 命令和旧别名 | 新功能先添加正式命令，再决定是否保留兼容别名；不要绕过 scheduler quota 做日常大跑 | `tests/test_cli_contract.py` |

## storage/

| 文件 | 用途 | 用法与边界 | 主要测试 |
|---|---|---|---|
| `storage/post_writer.py` | SQLite schema、帖子/评论写入、FTS 和 sidecar 更新 facade | 通过 `SQLitePostStore` 使用；它仍是兼容入口，不能直接替换主库或删除运行表 | `tests/test_sqlite_store.py`、crawler tests |
| `storage/queue_repository.py` | crawler queue 的 claim、token fencing 和终态转换 | 由 `SQLitePostStore` 委托；`QueueClaim` 只对当前 owner/token 有效，过期 worker 返回 `stale_claim` 或 false | `tests/test_queue_claim_fencing.py` |
| `storage/bigram_index.py` | 构建和维护普通文本 Bigram sidecar | 只在明确的临时输出路径构建并验证后替换；不要直接覆盖正在使用的 sidecar | `tests/test_search_bigram.py`、benchmark |
| `storage/symbol_index.py` | 构建特殊符号、表情和混合查询 sidecar | 与主库的 searchable row 数和 schema version 一起核对 | `tests/test_search_bigram.py` |

## jobs/

| 文件 | 用途 | 用法与边界 | 主要测试 |
|---|---|---|---|
| `jobs/scheduler.py` | Railway 调度、预算、暂停、heartbeat 和 crawler 子进程 | `python -m jobs.scheduler`；只决定何时运行和给多少预算，不复制候选判定 | `tests/test_scheduler_policy.py`、CLI/quota tests |
| `jobs/lane_worker.py` | 并行 cookie lane 的 worker 入口 | 仅在显式启用 parallel lanes 时运行；使用共享 queue claim，不创建第二套队列 | `tests/test_cookie_pool.py`、crawler integration |

## 常见错误用法

- 不要直接 import 根入口来实现新逻辑；根入口只负责兼容启动和转发。
- 不要让 `app/http` 直接读 SQLite，也不要让 `crawler/strategies` 直接写 SQLite。
- 不要把 `crawler/detail_pipeline.py` 当成入库器；它只返回解析结果和错误。
- 不要绕过 `storage/queue_repository.py` 清理或完成一个已被 claim 的队列任务。
- 不要把 `tools/` 下的迁移、抓包、性能和私有分析脚本当作 Web 主线模块。
