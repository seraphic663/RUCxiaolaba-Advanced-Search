# 工程边界、兼容入口与文件生命周期

本文档描述当前仓库的职责边界和文件归属。历史重构过程保留在 Git 与 `docs/CHANGELOG.md`，不再把一次重构验收记录当成当前运行手册。

## 目录职责

| 路径 | 当前职责 | 已知问题 | 目标边界 | 迁移风险 |
|---|---|---|---|---|
| `server.py` | Web 兼容启动与导入入口 | 根文件看似主实现 | 保持薄入口，逻辑只进入 `app/` | 高：Railway、测试和本地命令仍引用 |
| `crawler_db.py` | crawler 兼容 CLI 与导入入口 | 容易被误认为实现文件 | 保持薄入口，命令实现在 `crawler/cli.py` | 高：运维命令和测试仍引用 |
| `app/` | 配置、Domain、Repository、Service、HTTP 与页面模板 | 搜索 SQL 仍集中在较大的 Repository | HTTP 不写 SQL，Service 不读写 HTTP，Repository 只负责读取 | 中：搜索语义和 public/admin 权限敏感 |
| `crawler/` | API client、标准化、扫描策略和流程编排 | 新旧命令同时存在 | 新逻辑走 discover/trickle，旧命令仅兼容或人工修复 | 中：涉及源 API 成本与停止条件 |
| `storage/` | SQLite schema、写入与可重建旁路索引 | `post_writer.py` 同时承担 schema 和队列写入 | 暂不拆表层，先由测试固定 schema/优先级 | 高：任何拆分都可能影响线上主库 |
| `jobs/` | Railway crawler scheduler | scheduler 同时保留新旧两种模式 | 明确 trickle 为推荐模式，旧模式只兼容 | 高：直接影响线上请求量和暂停恢复 |
| `app/templates/` | public/admin 页面和共享 UI 资源 | 两页仍有各自的搜索与渲染逻辑 | 主题、设置等共同逻辑只维护一份，权限展示允许不同 | 中：需要浏览器与 HTTP 契约回归 |
| `tools/operations/` | 操作员明确执行的维护命令 | 部分命令会处理敏感数据或替换 DB | 每个命令说明输入、输出、可逆性和权限边界 | 中到高 |
| `tools/migrations/` | 一次性迁移、旧库升级和索引重建 | 历史脚本看起来仍像日常入口 | README 标明 active/recovery/legacy 生命周期 | 中：旧备份恢复仍可能需要 |
| `tools/benchmarks/` | 手动性能基准 | 结果可能过时 | 普通测试不自动运行，文档标明数据前提 | 低 |
| `tools/capture/` | 本地抓包辅助 | 输出可能含 cookie 和个人信息 | 不进入生产路径，输出始终忽略并及时删除 | 中：敏感数据边界 |
| `docs/` | 当前架构、运维、功能与历史审计 | 爬虫文档曾出现新旧两套事实 | `docs/operations/crawler.md` 为唯一爬虫运维事实源 | 低 |
| `data/` | 本地运行数据 | 体积大、可能含密钥和个人数据 | 继续由 `.gitignore` 排除，只提交配置示例 | 高：不得误提交或覆盖线上库 |

## 依赖方向

```text
HTTP / CLI / Jobs
        -> Services / Crawler Service
        -> Repositories / API Client / Normalizer
        -> SQLite / Remote API
```

- HTTP 层不得新增 SQL。
- Service 不得直接读取 HTTP Cookie 或写响应流。
- Repository 不得依赖 `server.py`。
- crawler Strategy 只决定扫描范围和停止条件，不直接写 SQLite。
- 远端字段变化集中在 Client/Normalizer 处理。
- scheduler 只负责任务时间、配额、暂停和子进程，不复制候选判定。
- 一次性数据处理进入 `tools/`，不得重新堆到根目录。

## 必须保持的兼容面

```text
python server.py
python crawler_db.py ...
/、/admin、/api/search、/api/comments、/api/categories
data/posts.db
/app/data/posts.db
SQLITE_DB、BIGRAM_DB
posts、comments、search_index、crawl_state
历史 crawler_db_phase1_* 断点键
```

新增路径变量 `POSTS_DB_PATH`、`BIGRAM_DB_PATH`、`SYMBOL_INDEX_DB_PATH` 不取消旧变量。根入口的 `sys.modules` 转发还承担现有测试和本地工具 monkey-patch 兼容，不能直接删除。

## 冗余处理结论

| 对象 | 判断 | 本轮处理 |
|---|---|---|
| `crawler/README.md` 与 `docs/operations/crawler.md` | 内容重复且已发生新旧调度冲突 | 前者缩为模块说明，后者成为唯一运维事实源 |
| 根 README、Railway 文档中的旧 scheduler 说明 | 与当前 trickle/quota 代码不一致 | 改为推荐主线，并保留旧模式的兼容说明 |
| `server.py`、`crawler_db.py` | 不是冗余，是受支持兼容入口 | 保留，不迁移实现回根目录 |
| 根目录抓包脚本 | 有偶发调试价值，但不是生产入口 | 已迁入 `tools/capture/` 并去除个人安装路径和固定 Web 密码 |
| 论坛词频实验 | 与网站、搜索和 crawler 运行无关 | 已删除；需要时从 Git 历史恢复 |
| 已被当前 schema 覆盖的 migration | 当前运行时和恢复主线均不调用 | 已删除 `migrate_slim_raw_json.py` 与 `add_admin_search_indexes.py` |
| `tests/test_api.py` | 手工在线探测，不是 pytest 测试 | 收敛为 `tools/audits/probe_upstream.py`，只探测 crawler 使用的端点 |
| `jobs/backup.py`、`tools/build_symbol_index.py` | 有用，但归属错误 | 已迁入 `tools/operations/` |
| `data/*.db`、quota/pause/history、缓存 | 运行时产物，不属于源码结构 | 保持忽略，不移动、不提交 |
| 四个未跟踪的代课/QAC 分析脚本 | 个人一次性分析，不属于项目 | 已按用户要求删除 |

## 文档事实源

| 主题 | 唯一当前文档 |
|---|---|
| 项目启动与入口 | `README.md` |
| 代码边界与兼容面 | 本文档 |
| SQLite schema 与运行表 | `docs/architecture/data-model.md` |
| crawler 命令、配额和停止条件 | `docs/operations/crawler.md` |
| Railway 部署与验证 | `docs/operations/railway.md` |
| 工具归属与生命周期 | `tools/README.md` |
| 历史变更 | `docs/CHANGELOG.md` 与 Git history |

审计、性能和迁移文档可以保留当时证据，但必须标明它们是历史快照还是当前操作说明，不能覆盖上述事实源。

## 验证层次

```text
tests/test_*.py               单元、集成和契约测试
tests/test_http_contract.py   public/admin HTTP 与权限字段契约
tests/test_cli_contract.py    新旧 CLI、scheduler 命令和配额契约
tests/test_crawler_service.py 候选、优先级、停止条件和限流熔断
tests/performance/            手动性能测试，不进入普通测试
```

常规验证：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -B -m pytest -q
python -B -m compileall -q app crawler storage jobs tools
python server.py --help
python crawler_db.py --help
git diff --check
```

涉及入口、部署命令或 scheduler 行为时，还必须按 `docs/operations/railway.md` 验证新 deployment、启动日志和 `/healthz`。
