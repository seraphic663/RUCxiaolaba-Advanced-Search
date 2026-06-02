# 架构迁移草案：从 CSV 单体搜索切到统一爬虫 + SQLite

> 状态：草案。本文只描述迁移方案，不代表当前项目已经迁移。现有 `server.py`、CSV 主站、爬虫脚本仍保持可用。

## 1. 背景与目标

当前项目已经形成两套并行逻辑：

1. 旧主站：`server.py` 启动后读取 CSV 到内存，通过 `/api/search`、`/api/comments`、`/admin` 提供搜索与管理页面。
2. 架构 demo：`demo/architecture_switch_demo.py` 展示统一爬虫入口、CSV/SQLite 存储切换、`/demo` 从 SQLite 读取数据。

当前 demo 已证明：

```text
真实 CSV data/posts_final.csv
  -> import-csv
  -> demo/runtime/posts.demo.db
  -> /demo 页面搜索、排序、评论展开
```

迁移目标不是一次性重写，而是把这个链路逐步变成主链路：

```text
采集器 crawler.py
  -> 统一 Post / Comment 数据模型
  -> SQLite 主存储
  -> Web API 查询 SQLite
  -> CSV/GZ 仅作为导出快照
```

核心目标：

1. 统一数据源，避免 `posts_scan.csv`、`posts_final.csv`、`posts_danger.csv` 混用。
2. 统一爬虫入口，减少 `spider.py`、`spider_danger.py`、`scan_full.py`、`crawl_detail.py`、`update_full.py` 的重复逻辑。
3. 让服务端从 DB 查询，避免启动时全量加载大 CSV。
4. 明确公开数据、内部数据、admin 数据的边界。
5. 支持可回滚迁移，保证主站不中断。

## 2. 当前架构梳理

### 2.1 当前数据文件

| 文件 | 当前角色 | 问题 |
|---|---|---|
| `data/posts_list.csv` | `spider.py` 列表缓存 | legacy，中间文件 |
| `data/posts_full.csv` | `spider.py` 详情结果 | legacy，字段不完整 |
| `data/posts_danger_list.csv` | `spider_danger.py` 列表缓存 | 中间文件 |
| `data/posts_danger.csv` | 含用户标识字段的早期完整数据 | 被新流水线覆盖，仍被 fallback 使用 |
| `data/posts_scan.csv` | ID 扫描结果 | 中间扫描产物，可能很大，也可能损坏 |
| `data/posts_final.csv` | 合并后的最终 CSV | 应成为当前 CSV 主数据源 |
| `demo/runtime/posts.demo.db` | demo SQLite | 只用于验证新架构 |

### 2.2 当前脚本

| 脚本 | 当前角色 | 迁移后建议 |
|---|---|---|
| `spider.py` | 简单列表 + 详情爬虫 | 标记 legacy，只保留参考 |
| `spider_danger.py` | 含 ID 字段采集 | 合并进统一 crawler，修复后废弃入口 |
| `crawl_detail.py` | 对 danger list 补详情 | 合并为 `detail-fill` 模式 |
| `scan_full.py` | 多线程 ID 扫描 | 合并为 `full-scan` 模式 |
| `update_full.py` | 当前主更新流水线 | 拆成统一 crawler 的 `full-scan`、`incremental`、`refresh-comments` |
| `mitm_filter.py` | 抓包辅助 | 保留本地工具，默认脱敏 |
| `demo/architecture_switch_demo.py` | 架构验证 | 作为新 crawler 雏形，不直接生产使用 |

### 2.3 当前服务端

`server.py` 当前仍是单文件 HTTP 服务，关键行为：

```text
启动
  -> 生成/读取 admin 密码
  -> refresh_cache()
  -> 读取 CSV 到内存
  -> 构建 post_index
  -> 启动 HTTPServer
```

主要问题：

1. 启动依赖 CSV 全量加载。
2. 搜索是内存 list 扫描。
3. 评论通过原始 `comment_list` 返回，公开/API/admin 边界不够清晰。
4. 更新 CSV 时缺少原子切换，服务可能读到坏文件。
5. 数据文件优先级历史上不一致，容易读错源。

## 3. 目标架构

### 3.1 目录草案

```text
crawler/
  __init__.py
  cli.py                 # 统一 CLI 入口
  client.py              # API 请求封装、Cookie、重试、TLS
  models.py              # Post、Comment、CrawlState
  normalize.py           # 上游 JSON -> 内部模型
  modes.py               # full-scan / incremental / detail-fill / refresh-comments
  checkpoint.py          # 断点读写

storage/
  __init__.py
  sqlite_store.py        # SQLite upsert/query/export
  csv_store.py           # CSV 导入/导出兼容层
  schema.sql             # posts/comments/crawl_state/fts

web/
  api.py                 # 后续可迁到 Flask/FastAPI；短期可仍由 server.py 调用
  serializers.py         # public/admin 脱敏序列化

data/
  posts.db               # 未来主存储
  posts_final.csv        # 兼容导出快照
  posts_final.csv.gz     # 发布快照
```

短期不一定真的拆这么细，可以先新增：

```text
crawler.py
storage_sqlite.py
```

等稳定后再拆目录。

### 3.2 数据模型

#### posts 表

```sql
create table posts (
  id text primary key,
  content text not null,
  category_name text not null,
  user_name text not null,
  show_user_id text not null,
  show_user_head text not null,
  real_user_id text not null,
  create_time text not null,
  comment_count integer not null,
  star_count integer not null,
  trace_count integer not null,
  views integer not null,
  hot integer not null,
  raw_json text not null,
  updated_at text not null
);
```

#### comments 表

```sql
create table comments (
  id text primary key,
  post_id text not null,
  parent_comment_id text not null default '',
  detail text not null,
  show_user_name text not null,
  show_user_id text not null,
  real_user_id text not null,
  reply_show_user_name text not null default '',
  reply_show_user_id text not null default '',
  is_publisher integer not null default 0,
  create_time text not null default '',
  raw_json text not null,
  updated_at text not null,
  foreign key(post_id) references posts(id)
);
```

#### crawl_state 表

```sql
create table crawl_state (
  key text primary key,
  value text not null,
  updated_at text not null
);
```

#### FTS 表，可选

SQLite FTS5 可用于正文搜索：

```sql
create virtual table posts_fts using fts5(
  id unindexed,
  content,
  category_name,
  user_name,
  tokenize='unicode61'
);
```

中文 FTS 效果未必完美，初期可以保留 `like` 搜索；数据量继续增长后再评估分词方案。

## 4. 统一爬虫模式设计

统一入口建议：

```powershell
python crawler.py full-scan --start-id 5000000 --end-id 4000000
python crawler.py incremental
python crawler.py detail-fill --ids 5005107 5005045
python crawler.py refresh-comments --pages 50
python crawler.py import-csv --csv-path data/posts_final.csv
python crawler.py export-csv --out data/posts_final.csv
python crawler.py verify
```

### 4.1 `full-scan`

用途：历史全量补齐。

流程：

```text
读取 crawl_state.full_scan_last_id
  -> 从 start_id 向 end_id 扫描
  -> GET /article/article/info?id={id}&community_id=4
  -> code=0000 且 community_id=4：normalize + upsert posts/comments
  -> code=1000：保存断点并退出
  -> 定期保存 checkpoint
```

要求：

1. 所有 worker 只负责请求和解析。
2. 写 DB 必须集中处理，或每线程独立连接并使用事务。
3. 不再多线程直接追加同一个 CSV。
4. 每 N 条提交一次事务，降低锁竞争。

### 4.2 `incremental`

用途：日常更新新帖。

流程：

```text
GET /article/article/lists?page=1
  -> 拿最新 ID
  -> 从最新 ID 往下扫
  -> 遇到连续 N 条已存在且更新时间无变化，停止
  -> 新帖 upsert
```

停止条件建议：

```text
连续 50 个 ID 未发现新 RUC 帖或已命中 DB
并且已经扫过至少 latest_page_size * 2 的范围
```

当前 `UNCHANGED_STOP=10` 偏激进，容易漏边界情况。

### 4.3 `refresh-comments`

用途：更新热门/近期帖评论。

流程：

```text
遍历 lists2 第 1..N 页
  -> 对比 comment_count
  -> 变化则重抓 info
  -> upsert post + replace comments for post
```

评论更新要注意：

1. 重抓详情后，建议先删除该帖旧评论，再插入新评论。
2. 如果详情请求失败，不要删除旧评论。
3. `comment_count` 和实际 `comments` 长度可能不一致，要记录但不要崩溃。

### 4.4 `detail-fill`

用途：对已有 ID 补详情，或修复单帖。

```text
ids -> info -> normalize -> upsert
```

这会替代 `crawl_detail.py`。

### 4.5 `import-csv`

用途：迁移初始导入。

```text
posts_final.csv -> normalize csv row -> SQLite posts/comments
```

这里有两种策略：

1. 保守策略：把 `comments_json` 整体放入 `posts.raw_json` 或 `posts.comments_json`，不拆评论表。
2. 完整策略：拆成 `comments` 表，同时保留原始 JSON。

建议迁移第一阶段采用完整策略，因为评论区是用户关心功能。

### 4.6 `export-csv`

用途：保留当前发布方式。

```text
SQLite -> data/posts_final.csv.tmp -> 校验 -> os.replace -> gzip
```

要求：

1. 必须写临时文件。
2. 校验行数、表头、UTF-8 编码。
3. 校验通过后原子替换。
4. gzip 也写临时文件再替换。

## 5. Web 迁移设计

### 5.1 阶段一：双读，不切主站

保留：

```text
/           仍读 CSV 内存缓存
/api/search 仍读 CSV 内存缓存
/demo       读 SQLite
```

新增：

```text
/api/demo/search
/api/demo/comments
```

用途：让 `/demo` 完整模拟主站。

验收：

1. `/demo` 搜索结果和 `/` 对同一 query 基本一致。
2. `/demo` 评论展开正常。
3. `/demo` 排序、分页正常。
4. SQLite 数据来自真实 CSV 或真实爬虫。

### 5.2 阶段二：主站可配置数据源

新增环境变量：

```text
DATA_BACKEND=csv|sqlite
SQLITE_PATH=data/posts.db
CSV_PATH=data/posts_final.csv
```

`server.py` 启动时：

```python
if DATA_BACKEND == 'sqlite':
    use sqlite query functions
else:
    use existing csv cache
```

这一阶段主站仍默认 `csv`，本地或测试环境切 `sqlite`。

验收：

```powershell
$env:DATA_BACKEND='sqlite'
python server.py
```

打开：

```text
http://127.0.0.1:8080/
```

功能与 CSV 版本一致。

### 5.3 阶段三：默认切 SQLite

条件：

1. SQLite 版主站连续多次本地测试通过。
2. 数据导入、增量更新、评论更新稳定。
3. 线上环境有 DB 文件准备流程。
4. CSV fallback 可用。

切换：

```text
DATA_BACKEND 默认 sqlite
CSV fallback 保留一个版本周期
```

### 5.4 阶段四：CSV 降级为快照

最终：

```text
SQLite 是主数据库
CSV/GZ 是发布快照、备份、兼容导出
```

这时 `server.py` 不再在生产路径读取 `posts_scan.csv`。

## 6. API 与脱敏边界

迁移时要顺手修正数据边界。

### 6.1 Public Post

公开搜索只返回：

```json
{
  "id": "5005107",
  "content": "...",
  "category": "日常投稿",
  "user": "某同学...",
  "time": "2026-06-01 07:25:00",
  "comments": 1,
  "stars": 0,
  "trace": 0,
  "views": 45,
  "hot": 0
}
```

不返回：

```text
show_user_id
real_user_id
show_user_head
raw_json
内部状态字段
```

### 6.2 Public Comment

公开评论只返回：

```json
{
  "detail": "...",
  "show_user_name": "某同学",
  "create_time": "...",
  "is_publisher": 0,
  "reply_comment_list": [ ... ]
}
```

不返回：

```text
show_user_id
real_user_id
reply_show_user_id
头像 URL
内部 flags
```

### 6.3 Admin API

Admin 单独走：

```text
/api/admin/search
/api/admin/comments
```

必须要求 admin session。

不要继续让同一个 `/api/search` 根据 cookie 自动改变返回字段。这个设计短期可以保留，但迁移后应该拆开。

## 7. 测试计划

### 7.1 数据导入测试

```powershell
python crawler.py import-csv --csv-path data/posts_final.csv --limit 1000
python crawler.py verify
```

检查：

1. posts 数量正确。
2. comments 数量合理。
3. `comments_json` 坏 JSON 不导致整体失败。
4. 中文、emoji 正常。
5. ID、时间、分类字段完整。

### 7.2 查询测试

固定 query 对比 CSV 与 SQLite：

```text
毕业
打印
5005045
食堂
```

对比项：

1. total 是否接近或一致。
2. 前 20 条 ID 是否一致。
3. 排序是否一致。
4. 评论数量是否一致。

### 7.3 Web 测试

页面：

```text
/
/demo
/admin
/api/search
/api/comments
```

检查：

1. 首页可打开。
2. `/demo` 可搜索、排序、展开评论。
3. 中文不是问号。
4. 评论不泄露用户 ID。
5. admin 登录仍可用。

### 7.4 性能测试

对比：

| 场景 | CSV 当前 | SQLite 目标 |
|---|---:|---:|
| 服务启动 | 需要读完整 CSV | 只打开 DB，启动快 |
| 空 query 最新帖 | 内存排序/切片 | DB order by limit |
| 关键词搜索 | Python 全表扫描 | LIKE 或 FTS |
| 评论加载 | 内存 post_index | comments 表按 post_id 查询 |
| 数据更新 | 写 CSV，重载全量 | upsert DB，按需查询 |

注意：如果 SQLite 仅用 `%like%`，全文搜索未必比内存快很多；真正性能提升来自启动速度、增量更新、评论按需查询。搜索性能要靠索引/FTS 进一步优化。

## 8. 回滚策略

任何阶段都必须能回滚到 CSV。

### 8.1 运行时回滚

```powershell
$env:DATA_BACKEND='csv'
python server.py
```

### 8.2 数据回滚

保留：

```text
data/posts_final.csv
data/posts_final.csv.gz
data/posts.db.backup
```

迁移写 DB 前先备份：

```powershell
copy data\posts.db data\posts.db.bak
```

### 8.3 代码回滚

迁移期间不要删除旧 CSV 读取逻辑。至少保留一个完整版本周期。

## 9. 迁移前后对比

### 9.1 数据源

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 主数据源 | CSV 文件 | SQLite DB |
| 中间文件 | 多个 CSV 混用 | crawler 状态 + DB |
| 发布格式 | CSV/GZ | DB 为主，CSV/GZ 为快照 |
| 原子更新 | 弱 | 强，事务 + os.replace 导出 |
| 坏文件影响 | 服务可能启动失败 | DB 事务失败可回滚 |

### 9.2 爬虫

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 入口 | 多脚本 | 单入口多模式 |
| 断点 | 分散 JSON | 统一 crawl_state |
| 写入 | 多线程追加 CSV | upsert DB |
| 去重 | 读 CSV set | primary key / unique key |
| 评论更新 | 覆盖 CSV 行困难 | replace comments by post_id |

### 9.3 Web

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 启动 | 全量加载 CSV | 打开 DB 即可 |
| 搜索 | 内存扫描 | SQL / FTS |
| 评论 | 内存字段 | comments 表按需查 |
| Admin | 与 public 混用接口 | public/admin API 分离 |
| 脱敏 | 容易遗漏 | serializer 统一控制 |

### 9.4 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| SQL 查询慢 | `%like%` 对中文和大数据不一定快 | 先保持 limit，后续 FTS/索引 |
| 迁移字段遗漏 | CSV 字段多、上游 JSON 不稳定 | 保留 raw_json，写 verify |
| 评论结构丢失 | 嵌套评论转换复杂 | comments 表保留 parent/reply 字段 + raw_json |
| 主站行为变动 | 搜索结果顺序可能不同 | 双读对比一段时间 |
| SQLite 锁 | 爬虫写入时 Web 查询 | WAL 模式、短事务、读写分离 |

## 10. 推荐执行顺序

### 第 0 步：冻结当前可用状态

1. 确认 `server.py` 当前优先读 `posts_final.csv`。
2. 保留现有 `/demo`。
3. 不删除任何旧脚本。

### 第 1 步：完善 demo 到真实数据

当前已经做到：

```text
data/posts_final.csv --limit 500
  -> demo/runtime/posts.demo.db
  -> /demo 搜索、排序、评论展开
```

下一步可做：

```powershell
python demo\architecture_switch_demo.py import-csv --store sqlite --csv-path data\posts_final.csv --limit 5000
```

观察性能。

### 第 2 步：新增生产级 SQLite Store

从 demo 中提取：

```text
storage_sqlite.py
models.py
```

不要直接让 demo 文件变生产代码。

### 第 3 步：新增 crawler.py

先只实现：

```text
import-csv
verify
export-csv
```

让 DB 能可靠从现有 CSV 生成。

### 第 4 步：让 /demo 使用生产 Store

`/demo` 不再直接写临时 SQL，而是调用 production store query 函数。

### 第 5 步：实现 sqlite backend 版主 API

新增：

```text
api_search_sqlite()
api_comments_sqlite()
```

通过 `DATA_BACKEND` 切换。

### 第 6 步：双读对比

写一个对比脚本：

```powershell
python scripts/compare_backends.py --queries 毕业 打印 5005045 食堂
```

输出：

```text
query=毕业
csv_total=...
sqlite_total=...
top20_match=18/20
```

### 第 7 步：增量爬虫迁移

把 `update_full.py` 的三阶段逻辑迁入 `crawler.py`：

1. `full-scan`
2. `incremental`
3. `refresh-comments`

先写 DB，不动 CSV。

### 第 8 步：主站切 SQLite 默认

条件满足后：

```text
DATA_BACKEND 默认 sqlite
CSV fallback 保留
```

### 第 9 步：清理旧脚本

最后再清理，而不是迁移中清理。

建议标记：

```text
spider.py              legacy
spider_danger.py       legacy
crawl_detail.py        legacy
scan_full.py           legacy, replaced by crawler.py full-scan
update_full.py         legacy, replaced by crawler.py incremental/refresh-comments
```

## 11. 当前不迁移时的近期建议

即使暂时不迁移，也建议马上做三件事：

1. 主站继续优先读 `posts_final.csv`，不要优先读 `posts_scan.csv`。
2. `posts_scan.csv` 只作为中间文件，不作为服务输入。
3. `/demo` 继续扩大真实 CSV 样本，作为迁移验证台。

推荐本地验证命令：

```powershell
Remove-Item demo\runtime\posts.demo.db
python demo\architecture_switch_demo.py import-csv --store sqlite --csv-path data\posts_final.csv --limit 5000
python demo\architecture_switch_demo.py verify --store sqlite
python -c "from server import Handler, ThreadingHTTPServer; ThreadingHTTPServer(('127.0.0.1', 8099), Handler).serve_forever()"
```

打开：

```text
http://127.0.0.1:8099/demo
```

## 12. 结论

建议采用渐进式迁移：

```text
先统一最终 CSV
再用 SQLite demo 验证真实数据
再抽出 Store 和 crawler
再让主站支持 DATA_BACKEND=sqlite
最后切默认值
```

不要直接删除旧 CSV 架构。当前项目数据量、脚本数量和线上发布方式都说明，最稳妥的方式是“双轨运行、逐步替换、保留回滚”。

## 11. 2026-06-01 试迁移记录

本次已经做了一个可回滚的试迁移，不改变默认启动方式。默认仍是 CSV：

```powershell
python server.py
```

SQLite 试运行需要显式打开：

```powershell
$env:DATA_BACKEND = "sqlite"
$env:SQLITE_DB = "data\posts.db"
python server.py
```

### 11.1 已生成的数据层

从 `data/posts_final.csv` 导入到 `data/posts.db`：

```powershell
python scripts\import_posts_to_sqlite.py --csv-path data\posts_final.csv --db-path data\posts.db --batch-size 5000
```

当前结果：

| 项 | 结果 |
|---|---:|
| posts | 543,601 |
| comments | 2,247,566 |
| skipped_rows | 0 |
| bad_comment_json | 1 |
| 非空时间范围 | 2023-04-19 15:25:11 ~ 2026-06-01 21:25:09 |
| DB 大小，含搜索索引 | 约 6.28GB |

评论采用两种形态保存：

1. `posts.comments_json` 保留原始嵌套 JSON，方便回放和兼容。
2. `comments` 表扁平保存评论和回复，方便按帖子读取评论区。

这会让 SQLite 比 CSV 更大。正式迁移时可以二选一：如果需要最小体积，去掉 `comments_json`；如果需要兼容和审计，保留双写。

### 11.2 已生成的搜索索引

新增 FTS5 trigram 搜索索引：

```powershell
python scripts\rebuild_sqlite_search_index.py --db-path data\posts.db --batch-size 20000
```

索引表为 `search_index`，包含帖子正文和评论正文。3 字及以上中文关键词可以走 FTS，例如：

| 查询 | 结果量 | 本地耗时 |
|---|---:|---:|
| 毕业照 | 584 | 约 0.03 ~ 0.36s |
| 打印店 | 738 | 约 0.05s |
| 免费拍 | 24 | 约 0.01s |

限制：SQLite trigram FTS 不匹配 1-2 字中文查询，例如 `毕业`。当前后端对这类短词回退到 `LIKE`，本地测试约 12 秒。正式迁移前需要决定短词策略：

1. UI 限制全文搜索至少 3 个字符，短词只搜正文或提示缩小关键词。
2. 增加二字 gram 倒排表，换取更大 DB 和更复杂增量维护。
3. 引入外部搜索引擎，例如 Meilisearch / Tantivy / SQLite 自定义 tokenizer。

### 11.3 服务端试切换

`server.py` 已增加可选 SQLite 后端：

- `DATA_BACKEND=csv`：旧逻辑，启动时加载 CSV，默认。
- `DATA_BACKEND=sqlite`：新逻辑，启动时只读取 DB 概览，不预加载 2.4GB CSV。

已接入接口：

| 路由 | SQLite 状态 |
|---|---|
| `/` | 读取 DB 概览渲染主页 |
| `/api/search` | 读取 `posts`，支持分类、日期、排序、admin 基础字段 |
| `/api/categories` | 从 DB 聚合分类 |
| `/api/comments?id=...` | 从 `comments` 表还原评论区 |
| `/demo` | 仍读取 demo DB，不受主库切换影响 |

本地接口测试结果：

| 接口 | 结果 |
|---|---:|
| `/` | 200，约 0.7s |
| `/api/categories` | 200，约 0.17s |
| `/api/search?limit=3` | 200，约 0.32s |
| `/api/search?sort=hot&limit=3` | 200，约 0.04s |
| `/api/search?q=毕业照&scope=all&limit=3` | 200，584 条，约 0.36s |
| `/api/comments?id=5014356` | 200，约 0.003s |

### 11.4 不能直接正式切换的点

本次只是试迁移，尚不建议直接替换生产主链路：

1. 短中文关键词全文搜索仍慢，必须先确定产品策略或索引策略。
2. admin 搜索里的 `identity`、`admin_fields` 已保留基础兼容，但还没有逐项对齐旧 CSV 逻辑。
3. SQLite DB 当前保留 `comments_json` 和扁平 `comments`，体积较大，需要决定正式存储策略。
4. 增量爬虫还没有直接写 DB；现在是 CSV -> DB 的离线导入。
5. 还没有做 CSV 与 DB 的抽样一致性测试，例如同一批 post_id 的正文、计数、评论区逐字段对比。

### 11.5 下一步建议

建议下一阶段只做“双轨灰度”，不要删除 CSV：

1. 保留 `posts_final.csv` 作为回滚源。
2. 每次更新 CSV 后自动运行 `import_posts_to_sqlite.py` 和 `rebuild_sqlite_search_index.py`。
3. 本地和 Railway 都先用 `DATA_BACKEND=sqlite` 压测。
4. 补齐 admin 高级搜索一致性测试。
5. 决定短词搜索方案后，再把默认后端从 CSV 改为 SQLite。

## 12. DB-first route progress: 2026-06-02

本轮开始把路线从 `CSV -> DB -> 网站` 推进到 `爬虫 -> DB -> 网站`，但仍保持旧链路可回滚。

### 12.1 无损瘦身 DB

新增脚本：

```powershell
python scripts\build_slim_sqlite.py --source data\posts.db --target data\posts.slim.db --batch-size 20000
```

策略：

- 删除 `posts.comments_json`
- 保留 `comments.raw_json`
- 保留 `comments` 结构化字段
- 保留 `search_index`
- 保留 `show_user_head`

结果：

| DB | 大小 | posts | comments | search_index |
|---|---:|---:|---:|---:|
| `data/posts.db` | 约 6.29GB | 543,601 | 2,247,566 | 2,791,127 |
| `data/posts.slim.db` | 约 3.98GB | 543,601 | 2,247,566 | 2,791,127 |

`posts.slim.db` 低于 Railway 5GB Volume 限制，适合作为当前线上候选 DB。

### 12.2 DB writer 雏形

新增：

```text
storage/sqlite_store.py
scripts/test_sqlite_store.py
```

`SQLitePostStore` 支持：

- `init_schema()` 初始化 slim schema
- `upsert_post(post, comments)` 写入/更新帖子和评论
- `replace_comments(post_id, comments)` 刷新某帖评论
- `refresh_search_index(post_id)` 更新全文搜索索引
- `set_state(key, value)` 记录爬虫状态
- `latest_post_id()` 获取最新帖子 ID

这一步是未来让爬虫直接写 DB 的基础。当前旧爬虫还没有全部接入它。

### 12.3 旧爬虫归档

新增归档目录：

```text
legacy/20260602-pre-db-first/
```

已复制旧 CSV-first 脚本、日志和抓包文件副本。原根目录文件保留不动。大体积数据只写入 `data-inventory.json` 清单，不重复复制。

### 12.4 当前推荐 Railway 配置

优先上传瘦身库：

```text
/app/data/posts.db  <- 使用本地 data/posts.slim.db 改名上传
```

环境变量：

```text
DATA_BACKEND=sqlite
SQLITE_DB=/app/data/posts.db
```

不要把 `data/posts_final.csv` 和完整 `data/posts.db` 同时放入 5GB Volume。

## 13. DB-first crawler entrypoint: 2026-06-02

新增 `crawler_db.py`，作为不破坏旧 CSV 爬虫的 DB-first 入口。

当前支持三种模式：

```powershell
# 本地测试：从 CSV 抽样写入临时 DB，不访问网络
python crawler_db.py mock-csv --db-path temp\crawler_db_mock.db --init-schema --csv-path data\posts_final.csv --limit 200

# 线上详情补齐：按帖子 ID 请求详情并 upsert DB
python crawler_db.py detail-fill --db-path data\posts.slim.db --ids 5014356

# 线上增量：扫 lists2 页，发现新帖或 comment_count 变化后抓详情写 DB
python crawler_db.py incremental --db-path data\posts.slim.db --pages 3
```

`detail-fill` 和 `incremental` 均支持 `--dry-run`，用于只请求接口、不写库。

### 13.1 已验证短链路

本地临时库验证：

```powershell
python crawler_db.py mock-csv --db-path temp\crawler_db_mock.db --init-schema --csv-path data\posts_final.csv --limit 200 --batch-size 50
python crawler_db.py detail-fill --db-path temp\crawler_db_mock.db --ids 5014356 --min-delay 0 --max-delay 0
```

结果：

| 检查项 | 结果 |
|---|---:|
| mock posts | 200 |
| mock comments | 342 |
| live detail-fill | 写入 1 帖 |
| server `/api/search?q=冰淇淋&scope=all` | 返回 #5014356 |
| server `/api/comments?id=5014356` | 返回 1 条评论 |

这证明最小 DB-first 链路已跑通：

```text
线上 detail API
  -> crawler_db.py
  -> SQLitePostStore.upsert_post()
  -> posts / comments / search_index
  -> server.py SQLite backend
  -> /api/search + /api/comments
```

### 13.2 仍保留的旧链路

旧脚本 `update_full.py`、`scan_full.py`、`crawl_detail.py` 等没有删除，也没有改成默认调用 DB。归档副本位于：

```text
legacy/20260602-pre-db-first/
```

下一步应逐步把 `update_full.py` 的 Phase 2/3 迁到 `crawler_db.py`：

1. `incremental` 作为日常更新入口。
2. `detail-fill` 作为补详情入口。
3. `mock-csv` 仅作为本地测试入口。
4. CSV 导出从主链路退出，变成备份命令。

### 13.3 小范围真实增量写入验证

在 `data/posts.slim.db` 上执行了限量真实写入：

```powershell
python crawler_db.py incremental --db-path data\posts.slim.db --pages 1 --min-pages 1 --stop-unchanged 10 --max-details 2 --min-delay 0 --max-delay 0
```

写入前后：

| 项 | 写入前 | 写入后 |
|---|---:|---:|
| posts | 543,601 | 543,603 |
| latest | #5014356 / 2026-06-01 21:25:09 | #5018419 / 2026-06-02 11:30:00 |
| details fetched | - | 2 |

写入后没有残留 `posts.slim.db-wal` / `posts.slim.db-shm`，单个 `posts.slim.db` 文件可作为上传 Railway Volume 的候选文件。

服务端回归：

| 接口 | 结果 |
|---|---|
| `/api/search?limit=3` | 返回 #5018419、#5018053、#5014356 |
| `/api/comments?id=5018419` | 返回 2 条评论 |
| `/api/search?q=绩点排名&scope=all&limit=5` | 返回 #5018419，约 1.0s |

`crawler_db.py incremental` 已加 `--max-details`，用于首次真实写入时限制详情请求数，避免一次更新过大。

## 14. Server backend CLI

`server.py` 默认已经切到 SQLite 后端，优先使用：

```text
data/posts.slim.db
```

如果不存在，再回退到：

```text
data/posts.db
```

本地启动推荐：

```powershell
python server.py
```

等价于：

```powershell
python server.py --db
```

指定 DB：

```powershell
python server.py --db --sqlite-db data\posts.slim.db
```

临时回到旧 CSV 模式：

```powershell
python server.py --csv
```

注意：`--csv` 会重新加载 2.4GB `posts_final.csv` 到内存，看到 `[init] Loading data...` 时说明正在走旧链路，启动慢是预期现象。
