# 更新记录

## 2026-06-11：本地默认启用 Bigram

- 本地存在 `data/bigram_index.db` 时，`python server.py` 自动挂载旁路索引。
- 环境变量和 `--bigram-db` 仍可覆盖默认路径；索引不存在时回退 `LIKE`。
- Bigram benchmark 与正式写入器共用同一分词实现，避免算法漂移。
- 更新本地构建、Railway 上传、变量设置和搜索后端核验文档。

## 2026-06-11：删除过渡层与生成文件

- 删除 `scripts/` 薄包装层，运维命令统一使用 `python -m jobs...` 或
  `python -m tools...`。
- 删除 `ai_retriever.py`、`storage/ai_store.py`、`storage/sqlite_store.py`
  兼容模块，测试和文档统一引用正式模块。
- 删除未被调用的爬虫 ID 范围模型和页面策略常量，保留实际使用的停止策略。
- 清理 Python/test 缓存和空临时日志，不改动正式数据库与本地运行配置。

## 2026-06-11：工程化模块重构

- 保留 `server.py`、`crawler_db.py` 兼容入口，实际实现迁入包结构。
- Web 拆分为配置、Repository、Service、HTTP Routes 和 AI 子模块。
- 爬虫拆分为 API Client、Normalizer、写锁、扫描策略、Service 和 CLI。
- 正式爬虫命令统一为 `sync-latest`、`sync-active`、`scan-history`、
  `scan-id-range`、`fill-details`，旧命令保留兼容。
- 生产任务迁入 `jobs/`，迁移、审计、性能和运维工具迁入 `tools/`。
- `scripts/` 改为兼容包装器，Railway 调度改为 `python -m jobs.scheduler`。
- 新增 HTTP、CLI、爬虫停止机制和 AI 楼主标记契约测试。
- 修复 `is_publisher=2` 被 AI 路径错误视为楼主的问题。
- 爬虫在所有页面请求失败时改为非零退出，避免 Scheduler 误报成功。

## 2026-06-03：DB-only 收敛与文档补齐

- 主站切换为 SQLite-only，移除 CSV 后端、CSV cache、CSV fallback。
- `server.py` 默认读取 `data/posts.db`，Railway 读取 `/app/data/posts.db`。
- admin 搜索改为 SQLite 高级搜索，支持正文、评论、ID、昵称、匿名/实名筛选。
- admin 评论展开改为按需请求 `/api/comments`，admin 登录态返回用户 ID，公开页面不暴露 ID。
- 删除 `/demo` 页面、demo 模板和 demo 数据目录。
- 测试文件整理到 `tests/`。
- Railway runtime 同步改为默认覆盖 `feedback.latest.jsonl` 和 `checkin_count.latest.json`，只在 `-Archive` 时保留时间戳目录。
- 新增运行时备份脚本 `scripts/backup_runtime.py`。
- 补充中文文档：
  - `docs/项目总览.md`
  - `docs/API与数据来源.md`
  - `docs/SQLite数据模型.md`
  - `docs/爬虫与更新策略.md`
  - `docs/搜索排序分类分页建议.md`
  - `docs/Railway部署与运维.md`
  - `docs/安全隐私与备份.md`

## 2026-06-02：SQLite 主库与 Railway Volume

- 从 CSV 主流程迁移到 SQLite 主库。
- 构建瘦身但保留评论原始 JSON 的 `posts.db`。
- 将 `posts.comments_json` 的重复大字段移除，评论统一由 `comments` 表承载。
- `crawler_db.py` 开始直接写 SQLite，支持增量更新和指定 ID 补详情。
- Railway 部署改为依赖 Volume：`/app/data/posts.db`。
- `start.sh` 增加 DB 存在性和基础查询检查，避免线上误启动空数据。
- 讨论并确认 5GB Volume 下不适合同时保留 CSV 与完整 DB。

## 2026-06-01：架构切换 Demo 与统一爬虫方向

- 编写架构切换 demo，用于验证“统一爬虫入口 + Store 接口 + SQLite 读取”思路。
- 验证 CSV 转 SQLite、SQLite 页面读取、评论展示等关键路径。
- 发现并修复 CSV 编码/坏行导致的启动异常。
- 明确后续方向：CSV 降级为历史导入/备份材料，运行时以 DB 为唯一数据源。

## 2026-05-31：Railway 适配与线上运行数据

- 增加 Railway 部署配置和健康检查。
- 调整启动脚本权限和 Railway 构建流程。
- 增加反馈入口和“到此一游”人数记录。
- 增加本地脚本拉取 Railway Volume 中的反馈和人数文件。

## 2026-05-30：全量数据扫描与最终 CSV 阶段

- 通过多阶段 CSV 流程补全历史帖子和当日新增帖子。
- 形成 `posts_final.csv` 作为当时网站主数据源。
- 处理大 CSV、坏行、评论 JSON 超长等问题。
- 这一阶段后续被 SQLite 主库替代。

## 2026-05-28 至 2026-05-29：新版 API 与网站恢复

- 通过抓包确认新版小程序 API：`ys.qimiaoyuanfen.com`。
- 适配新版 cookie 鉴权 `ys7_ysxy_session`。
- 重做基础爬虫与搜索页面。
- 初步恢复帖子搜索、评论展示、排序和高亮能力。

## 更早阶段

- 原仓库基于旧版 API、Flask、DuckDB 和早期搜索逻辑。
- 旧版 API 停止响应后，本 fork 逐步转向新版 API 和当前 DB-only 架构。
