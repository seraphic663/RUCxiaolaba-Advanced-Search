# 工程化重构实施说明

## 状态

本轮工程化重构已经实施。外部 HTTP API、SQLite schema、Railway Volume 路径以及旧 CLI 命令保持兼容。

## 完成内容

- `server.py` 改为兼容启动器，实际应用位于 `app/`。
- 配置集中到 `app/config.py`。
- SQLite 读取拆为 Post 和 Search Repository。
- 搜索、Admin、鉴权和模板拆为 Service。
- HTTP 路由拆为 Public 和 Admin 两组。
- `crawler_db.py` 改为兼容入口，实际爬虫位于 `crawler/`。
- 爬虫拆为 API Client、Normalizer、写锁、Strategy 和 Service。
- 正式爬虫命令改为 `sync-latest`、`sync-active`、`scan-history`、`scan-id-range`、`fill-details`。
- 旧命令继续作为 argparse alias。
- 生产任务移动到 `jobs/`。
- 迁移、审计、性能和运维工具移动到 `tools/`。
- 运维命令直接使用 `python -m jobs...` 或 `python -m tools...`，不再维护 `scripts/` 包装层。

## 保持不变

- `python server.py`
- `python crawler_db.py ...`
- `/`、`/admin`、`/api/search`、`/api/comments`、`/api/categories`
- `data/posts.db`
- `/app/data/posts.db`
- `SQLITE_DB` 与 `BIGRAM_DB` 旧环境变量
- `posts`、`comments`、`search_index`、`crawl_state` 表
- 历史 `crawler_db_phase1_*` 断点键

新增环境变量别名：

```text
POSTS_DB_PATH
BIGRAM_DB_PATH
```

旧变量优先级保持兼容。

本地未设置 Bigram 环境变量时，会自动探测 `data/bigram_index.db`；文件不存在则回退 `LIKE`。Railway 仍建议显式配置 `BIGRAM_DB=/app/data/bigram_index.db`。

## 测试层次

```text
tests/test_*.py              单元与集成测试
tests/test_http_contract.py  HTTP 响应契约
tests/test_cli_contract.py   新旧 CLI 兼容
tests/test_crawler_service.py 新增、更新和停止条件
tests/performance/           手动性能测试
```

常规验证：

```powershell
python -m pytest -q
python -m compileall -q app crawler storage jobs tools
python server.py --help
python crawler_db.py --help
```

## 当前约束

1. 仍使用标准库 `http.server`，本轮没有同时更换 Web 框架。
2. Web 和 Scheduler 仍运行在同一 Railway Service，避免多服务共享写入
   SQLite Volume 带来的不确定性。
3. `posts.db` 与 Bigram 旁路库不是跨库原子事务；Bigram 必须视为可重建索引。
4. 在线 API 测试依赖有效 Cookie，不属于默认离线测试。

## 迁移工具生命周期

| 工具 | 当前运行时是否调用 | 建议 |
|---|---:|---|
| `tools/migrations/build_slim_sqlite.py` | 否 | 保留，用于从旧全量库恢复或重新生成瘦身主库 |
| `tools/migrations/migrate_slim_raw_json.py` | 否 | 历史一次性迁移；当前库与新爬虫已不需要 |
| `tools/migrations/add_admin_search_indexes.py` | 否 | 历史旧库补索引；索引定义已进入 `SQLitePostStore.init_schema()` |
| `tools/migrations/rebuild_sqlite_search_index.py` | 否 | 保留，用于 FTS 损坏或缺行时重建 |

后两项可以删除而不影响当前网站、爬虫和 Railway；只有再次导入未经升级的旧库时才可能用到。即使删除，代码仍可从 Git 历史恢复。

## 本轮验收记录

2026-06-11 本地验收：

- `pytest`：35 项通过。
- `compileall`：全部新模块通过。
- Pyflakes：核心模块无未定义名称或未使用导入。
- `git diff --check`：无空白错误。
- Markdown 相对链接检查：无失效链接。
- 本地真实主库 `pragma quick_check`：`ok`。
- 主库记录：544,993 帖、2,252,543 条评论。
- 真实主库启动：主页、Admin、健康检查、分类和搜索 API 均返回 200。
- 短词“六一”正文搜索：LIKE 后端约 1.14 秒，返回 2,701 帖。
- 在线爬虫探测因当前网络到上游 API 的 TLS/连接中断未完成。CLI 已改为在全部页面请求失败时返回非零退出，避免 Railway 误报成功。

## 后续开发规则

- HTTP 层不得直接新增 SQL。
- Service 不得读取 HTTP Cookie 或写响应流。
- Repository 不得依赖 `server.py`。
- 爬虫 Strategy 只决定扫描范围与停止条件。
- 远程字段变化只在 Client/Normalizer 处理。
- 一次性数据处理必须放入 `tools/`，不能重新堆入根目录。
