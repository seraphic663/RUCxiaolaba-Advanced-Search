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
