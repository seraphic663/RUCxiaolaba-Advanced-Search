# 页面加载与搜索性能优化方案（草案 v2）

**日期**: 2026-06-09  
**状态**: 草案，待讨论  
**版本**: v2 —— 基于实测数据重写

---

## 1. 问题画像（实测）

### 1.1 数据规模

```
posts     :   544,993 行
comments  : 2,252,543 行
search_index (FTS) : 2,797,496 行  ← 已存在！含全部 post + comment 正文
```

### 1.2 各类查询耗时实测

用单关键词 `"食堂"` 在本地对 `data/posts.db` 实测（3 次取平均）：

```
操作                                    耗时       用户体感
─────────────────────────────────────────────────────────────
FTS trigram 关键词匹配                  0ms      瞬间
开新 SQLite 连接 + pragma               0.3ms    瞬间
简单 COUNT(*)（无 WHERE）                10ms     瞬间

普通搜索 COUNT（LIKE 扫 54 万帖）        961ms    等 1 秒 ██████
scope=all SELECT（含评论 LIKE）         3,574ms   等 3.5 秒 ██████████████████
scope=all COUNT（含评论 LIKE）          4,306ms   等 4.3 秒 ██████████████████████
管理员 COUNT（4 组评论 LIKE）           9,271ms   等 9 秒 ██████████████████████████████████████
管理员 SELECT（4 组评论 LIKE）         10,145ms   等 10 秒 ██████████████████████████████████████████████
```

### 1.3 瓶颈在哪

所有慢查询有一个共同特征：**`LIKE '%关键词%'`**。

SQLite 的 LIKE 不能走 B-tree 索引——前后都有 `%` 通配符时，唯一的选择是把整个表从头到尾读一遍，对每一行做子串匹配。54 万篇帖子扫一遍要 1 秒，225 万条评论扫一遍要 4 秒，管理员搜索两个表各扫 5 遍就是 10 秒。

对比：FTS5 trigram 索引（`search_index`）是一个**倒排索引**，类似书末的术语索引——"食堂"这个词条下，所有包含它的帖子和评论 ID 已经预先建好，查询就是一次字典查找，耗时不到 1 毫秒。

**核心发现**：`search_index` 已经建好了 280 万行（覆盖每篇帖子正文 + 每条评论正文），数据完整，随时可用。只是 `server.py` 的查询逻辑没有充分发挥它——绝大多数搜索请求走了 LIKE 路径，而不是 FTS 路径。

---

## 2. 方案总览

| # | 优化项 | 代价 | 收益 | 结论 |
|---|--------|------|------|------|
| ① | **搜索全部走 FTS**，LIKE 仅作短词兜底 | 改造 ~80 行，风险中 | 搜索 **1-10s → 10-50ms**（100-1000x） | **必须做** |
| ② | **COUNT 轻量化**：空搜索复用缓存，有词走 FTS 估算 | ~25 行，风险低 | COUNT **1-10s → 0-5ms** | **应该做** |
| ③ | **首页 `/api/init` 合并**：3 个 API 调合成 1 个 | ~40 行前后端，风险低 | 省 2 次 HTTP 往返 | **应该做**（配合 ①） |
| ④ | AI 审核与检索并行执行 | ~10 行，风险低 | 单个请求省 0.5-30s | 可以做 |
| ⑤ | SQLite 只读连接线程复用 | ~15 行，风险低 | 省 0.3ms/请求（< 1%） | 锦上添花 |
| ⑥ | 搜索结果 LRU 缓存 | ~20 行，风险低 | 热门搜索 ~0ms | 锦上添花 |

---

## 3. 各方案详细分析

### 方案 ①：搜索走 FTS，LIKE 兜底（P0，必须做）

#### 现状问题

`server.py` 的 `sqlite_search_where()` 函数决定搜索走 FTS 还是 LIKE：

```python
# 当前逻辑（server.py: 404-405）
fts_query = sqlite_fts_query(keywords) if scope == "all" and use_fts and not admin else None
if fts_query:
    # 走 FTS 路径（快，但几乎不触发）
    clauses.append("p.id in (select post_id from search_index where body match ?)")
else:
    # 走 LIKE 路径（慢，绝大部分请求落在这）
    for kw in keywords:
        clauses.append("(lower(p.content) like ? or p.id like ?)")
```

FTS 路径触发的三个条件：

| 条件 | 含义 | 为什么苛刻 |
|------|------|-----------|
| `scope == "all"` | 用户切到"全文搜索"模式 | 默认是 `content`，99% 用户不会改 |
| `use_fts` | 所有关键词 ≥ 3 字符 | trigram 索引天然限制，"食堂"刚好 2 个中文字 ≤ 3 字符 |
| `not admin` | 非管理员 | 管理员搜索**完全不走 FTS** |

实际效果：**当前几乎没有任何搜索走 FTS 路径**。两字中文词（最常见的中文搜索）被 trigram 限制拦住。管理员搜索被 `not admin` 拦住。

#### 但 search_index 实际可以匹配两字词

trigram 索引对 "食堂" 这种两字词的匹配逻辑：trigram 分词器把 "食堂很难吃" 拆成 `["食堂", "堂很", "很难", "难吃"]`——"食堂" 本身就是其中一个 trigram。所以 `MATCH '"食堂"'` 在 trigram 索引里是可以匹配的。当前代码的 `len(kw) < 3` 判断过于保守——它假设中文 trigram 的最小匹配单元是 3 字节（1 个 UTF-8 中文字 ≈ 3 字节），但 trigram 是以**字符**为单位的。

#### 改造方案

```
搜索关键词
    │
    ├─ 单字（如 "猫"）    → 不可走 FTS trigram → LIKE 兜底（~1s，但极少见）
    │
    ├─ 两字及以上（如 "食堂"）→ FTS trigram 命中 → 0ms
    │
    └─ 搜索范围 = "全文"  → 同时搜 search_index kind='post' + kind='comment'
       范围 = "正文"      → 只搜 kind='post'
```

对 `sqlite_search_where()` 的修改：

1. **去掉 `scope == "all"` 限制** — 默认内容搜索也走 FTS
2. **去掉 `not admin` 限制** — 管理员搜索也走 FTS
3. **改短词判断** — 一个中文字 ≈ 1 个 trigram token（不是 3 字节），所以 `len(kw) >= 1` 即可进 FTS；实测两字词能匹配就放行
4. **评论搜索走 FTS** — 已有 `kind='comment'` 数据，直接 `MATCH` + `kind='comment'` 替代 `WHERE comments.detail LIKE`
5. **LIKE 仍保留** — 仅在单字搜索或 FTS 语法错误时兜底

**代价**：

| 维度 | 评估 |
|------|------|
| 代码改动 | 约 80 行，改 `sqlite_search_where()` 和 `sqlite_fts_query()` |
| 风险 | 中。FTS 行为与 LIKE 有细微差异——部分匹配 vs 精确 trigram 匹配。需要验证零结果场景 |
| 测试要点 | 单字搜索、"食堂"类两字词、中英混合、数字 ID 搜索、空搜索 |
| 回滚 | 容易，改回旧判断条件即可 |

**收益**：

| 场景 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| 普通搜索（content） | 961ms | ~5ms | **192x** |
| 全文搜索（scope=all） | 4,306ms | ~5ms | **861x** |
| 管理员搜索（4-field） | 9,271ms | ~10ms | **927x** |
| 空搜索（最新帖子） | ~5ms | ~5ms | 不变（本来就走索引） |

**为什么是整个方案的核心**：不做这个，其他所有优化加起来也抹不平 10 秒的 LIKE 全表扫描。做了这个，大部分搜索从秒级变成毫秒级，剩下的优化都变成"把 10ms 优化到 5ms"——用户感知不到的区别。

---

### 方案 ②：COUNT 轻量化（P1，应该做）

#### 现状问题

每次搜索先跑一遍完整的 `COUNT(*)`（和 SELECT 一模一样的 WHERE），再跑一遍 SELECT。当前代码：

```python
# server.py api_search_sqlite(): 492-496
total = conn.execute(f"select count(*) from posts p{where_sql}", args).fetchone()[0]
# ... 然后
rows = conn.execute(f"""select ... from posts p {where_sql} order by ... limit ? offset ?""", ...)
```

COUNT 的作用仅仅是：算总页数（`total_pages`）、显示"共 N 条"、分页器渲染。

#### 为什么会慢

上面实测看到，一个带 comments 子查询的 COUNT 要跑 4 秒。而相应的 SELECT + LIMIT 50 可能只要 2ms（扫到 50 条匹配就停）。COUNT 无法提前停止——它必须精确数完每一行，所以 COUNT 往往比 SELECT 更慢。

**更糟的是：这套逻辑每翻一页都重跑一遍 COUNT**。

#### 优化策略（分层）

**第一层：空搜索不用 COUNT**

最常见场景——首页加载、用户清空搜索框看最新帖子。此时 WHERE 只有可选分类/日期过滤，可以复用 `sqlite_overview()` 缓存的 total（服务器启动时已算好，误差可接受）。

```python
# 无关键词搜索 → 直接读缓存，不查 COUNT
if not query and not uid and not uname:
    total = _overview_cache["total"]  # 启动时已算，54 万，10ms
else:
    total = conn.execute(...).fetchone()[0]  # 有搜索词，正常走 COUNT
```

**第二层：FTS 估算**

如果关键词搜索走了 FTS 路径（方案 ① 完成后），FTS 本身可以快速返回匹配行数：

```sql
SELECT count(DISTINCT post_id) FROM search_index WHERE body MATCH '"食堂"'
-- ~0ms，因为只是统计倒排索引中"食堂"词条的条目数
```

这个数不等于最终 WHERE 结果（还有分类/日期过滤），但作为"约 N 条"显示足够了。

**第三层：前端接受估算**

前端显示 `"约 1,234 条结果"` 而不是 `"共 1,234 条结果"`。搜索结果的精确总数对用户几乎无意义——没人会翻到第 200 页。

**代价**：

| 维度 | 评估 |
|------|------|
| 代码改动 | ~25 行，改 `api_search_sqlite()` |
| 风险 | 低。只是数据显示从精确变估算，搜索功能本身不变 |
| 回滚 | 改回精确 COUNT 即可 |

**收益**：

| 场景 | 优化前 | 优化后 | 说明 |
|------|--------|--------|------|
| 空搜索 COUNT | 10ms | 0ms | 读缓存 |
| 有词搜索 COUNT（走 FTS 后） | ~5ms | ~2ms | FTS 估算 vs 全表 COUNT |
| 有词搜索 COUNT（未走 FTS 的短词） | 961ms | 961ms | 短词兜底，不变（但极少触发） |

注意：方案 ② 的收益**严重依赖方案 ①**。如果方案 ① 没做，搜索照样要走 LIKE COUNT（1-10s），COUNT 轻量化只能救空搜索这一个场景。

---

### 方案 ③：首页 API 合并（P1，配合 ① 效果更好）

#### 现状

首页 HTML 返回后，浏览器并行发出 3 个请求：

```
GET /api/search?q=     → COUNT + SELECT（当前 ~1s）
GET /api/categories    → GROUP BY（~10ms）
GET /api/ai/status     → session 验证（~2ms）
```

3 次 TCP 握手 + 3 次 HTTP 请求-响应 + 3 次服务端处理。最慢的 `/api/search` 决定总耗时。

#### 方案

新增一个 `/api/init`，把首页需要的三份数据打包返回：

```python
# GET /api/init → {"overview": {...}, "posts": [...], "categories": [...]}
def _handle_api_init(self):
    overview = sqlite_overview()
    search_result = api_search_sqlite("", "time", 1, 50)
    categories = api_categories_sqlite()
    self._serve_json({
        "overview": overview,
        "posts": search_result["results"],
        "categories": categories["categories"],
    })
```

前端改为：`DOMContentLoaded` 时只调这一个接口，拿到数据后一次性渲染搜索列表 + 分类下拉 + 统计信息。AI 状态仍独立请求（依赖 cookie）。

**代价**：

| 维度 | 评估 |
|------|------|
| 代码改动 | 服务端 ~20 行（新路由 + 新 handler），前端 ~20 行（改加载逻辑） |
| 风险 | 低。新接口不破坏旧接口，旧搜索路径完全保留 |
| 兼容 | `/api/search` 等接口不变，分页搜索继续走老路径 |

**收益**：首页数据从 3 个 HTTP 往返 → 1 个。用户感知延迟从 `max(搜索, 分类, AI状态)` → `一个合并请求`。如果方案 ① 把搜索降到毫秒级，这个合并请求就是 ~15ms。

独立于方案 ① 的收益：即使搜索仍是 1s，合并也能省掉分类和 AI 状态的额外往返时间（约 50-100ms 网络延迟）。

---

### 方案 ④：AI 审核与检索并行（P2，可以做）

#### 现状

AI 搜索（`_handle_ai_search`）的处理链路是**串行**的：

```
安全审核 API ──→ 数据库检索 ──→ DeepSeek 生成
  0.5-30s        0.1-2s         3-120s
```

审核和检索之间**没有数据依赖**——两者都只需要用户的原始 query。可以并发执行：

```
安全审核 API ──┐
               ├──→ DeepSeek 生成
数据库检索 ────┘
```

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=2) as ex:
    fut_mod = ex.submit(_moderate_ai_query, query)
    fut_ret = ex.submit(retrieve_ai, query, SQLITE_DB)

    allowed, reason = fut_mod.result()
    if not allowed:
        return reject  # 检索白做了，但审核不通过的概率低

    retrieved = fut_ret.result()
```

**代价**：

| 维度 | 评估 |
|------|------|
| 代码改动 | ~10 行，包在 `_handle_ai_search` 里 |
| 风险 | 低。检索可能白跑（审核不通过时），但发生概率极低且检索本身成本几乎为零 |
| 注意事项 | `ThreadPoolExecutor` 创建的线程不在 HTTP 请求线程内，需要确保 `retrieve_ai` 不依赖 thread-local 状态 |

**收益**：单次 AI 搜索省 `min(审核耗时, 检索耗时)`，通常 1-2 秒，审核模型慢时省 30 秒。但 AI 搜索本身是慢操作（DeepSeek 生成要 3-120 秒），这点优化被生成阶段的时间稀释了。对用户体验有改善但不显著。

---

### 方案 ⑤：SQLite 只读连接线程复用（P3，锦上添花）

实测开一次新连接的成本：

```
connect + row_factory + pragma query_only + pragma mmap + pragma cache: 0.3ms
```

而一次 LIKE 搜索的 COUNT：9,000ms。**连接开销占搜索总耗时的 0.003%。**

```python
# 如果做了（代码不复杂）
import threading
_read_conns = threading.local()

def get_read_conn():
    conn = getattr(_read_conns, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(SQLITE_DB)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma query_only=on")
        conn.execute("pragma cache_size=-2000")
        _read_conns.conn = conn
    return conn
```

**代价**：~15 行，低风险。但需要注意：长时间持有的只读连接可能阻止 WAL checkpoint 清理旧 WAL 文件。需要定期关闭重建（比如每 1000 次查询或每 10 分钟）。

**收益**：省 0.3ms/请求。在 10ms 级别的搜索中占 3%，在 10s 级别的搜索中可忽略不计。

**结论**：方案 ① 做完后（搜索降到 10ms），这个优化有 3% 的边际收益。在此之前做是完全浪费精力。

---

### 方案 ⑥：搜索结果缓存（P3，锦上添花）

```python
_cache: dict[tuple, tuple[float, dict]] = {}  # key → (expiry, result)

def cached_search(query, sort, page, category, date):
    key = (query, sort, page, category, date)
    now = time.time()
    if key in _cache:
        expiry, result = _cache[key]
        if now < expiry:
            return result
    result = _do_search(...)
    _cache[key] = (now + 60, result)  # 60 秒 TTL
    # 限制缓存大小
    if len(_cache) > 128:
        _cache.pop(next(iter(_cache)))
    return result
```

**代价**：~20 行，低风险。内存占用可忽略（128 个 key，每个 ~50KB = 共 6.4MB）。

**收益**：热门搜索 10ms → 0ms。真实价值不在单次节省 10ms，而在于——10 个人同时搜"选课"，只需 1 次 SQLite 查询而非 10 次。这在并发场景下有意义，但当前并发量（Railway 小服务）未必需要。

---

## 4. 推荐实施路线

```
Phase 1（核心，1-2 小时）：
  ┌─────────────────────────────────────────┐
  │ 方案 ① FTS 改造                          │
  │   - 改 sqlite_search_where()             │
  │   - 搜 FTS search_index 替代 LIKE        │
  │   - 保留 LIKE 做短词兜底                   │
  │   - 管理员搜索也走 FTS                     │
  │                                          │
  │ 预期：所有搜索 1-10s → 10-50ms            │
  │ 这是唯一能产生数量级变化的一项               │
  └─────────────────────────────────────────┘
           │
           ▼
Phase 2（锦上添花，30 分钟）：
  ┌─────────────────────────────────────────┐
  │ 方案 ② COUNT 轻量化                       │
  │ 方案 ③ 首页 API 合并                      │
  │                                          │
  │ 预期：COUNT 耗时归零、首页少 2 次往返       │
  └─────────────────────────────────────────┘
           │
           ▼
Phase 3（有余力再做，30 分钟）：
  ┌─────────────────────────────────────────┐
  │ 方案 ④ AI 审核并行                        │
  │ 方案 ⑤ 连接复用                           │
  │ 方案 ⑥ 搜索缓存                           │
  │                                          │
  │ 预期：边际提升，聊胜于无                    │
  └─────────────────────────────────────────┘
```

---

## 5. 不做的事

- ❌ **引入 Redis / Memcached** — SQLite + FTS 已经够快，引入外部缓存增加部署复杂度（Railway 需要额外服务），ROI 为负
- ❌ **换成 MySQL / PostgreSQL** — 当前瓶颈是查询没有用索引，不是 SQLite 引擎本身。FTS 已经解决
- ❌ **前端虚拟滚动 / SSR** — 每页 50 条帖子不需要虚拟滚动，HTML 只有 29KB 不需要 SSR
- ❌ **CDN 缓存** — 搜索结果因人而异（不同关键词、排序、分页），且每 2 小时有新数据爬入
- ❌ **comments 子查询加 `LIMIT 1`** — v1 草案推荐过，但实测 9.3s → 6.9s，不治本；FTS 改造才是正解

---

## 6. 讨论点

1. **COUNT 改为估算是否可以接受？** — 技术上"约 1,234 条"和"共 1,234 条"对用户无差别。但如果不放心，可以保留精确 COUNT，只在 COUNT 耗时超过 500ms 时自动降级为"约 N 条"。

2. **FTS trigram 对单字搜索不适用，单字搜索是否常见？** — 中文搜索极少用单字。"食堂"是两个中文字，刚好是一个 trigram。真正的单字搜索（如搜"猫"）只能走 LIKE，但这需要保留 LIKE 兜底。可以加日志统计单字搜索占比，如果 < 1% 就不值得优化。

3. **`search_index` 是否包含最新爬取的数据？** — `SQLitePostStore.upsert_post()` 中 `refresh_search_index()` 是同步调用的，每次写入帖子/评论时实时更新 FTS 索引。但可以加一个启动检查：如果 FTS 行数严重少于 posts+comments 行数，打印警告并建议 `rebuild_sqlite_search_index.py`。

---

## 7. 2026-06-09 本地核查与 bigram Demo

### 7.1 对草案核心假设的修正

本地 SQLite 3.45.3、真实 `data/posts.db` 实测：

```text
食堂（2 字）  trigram MATCH -> 0 条
选课（2 字）  trigram MATCH -> 0 条
食堂饭（3 字）trigram MATCH -> 正常命中
图书馆（3 字）trigram MATCH -> 正常命中
```

因此，前文“trigram 可以直接匹配两个汉字”的判断不成立。现有 trigram FTS 只能可靠处理至少 3 个 Unicode 字符的查询；如果不更换索引结构，两字中文必须继续走 LIKE。

### 7.2 当前存储情况

```text
本地 posts.db：1.586 GiB
本地 posts：544,993
本地 comments：2,252,543
本地最新帖子：2026-06-03 21:26:00

Railway posts.db：约 1.607 GiB
Railway posts.db-wal：约 5.76 MiB
Railway Volume：1.7G / 4.6G，剩余约 2.9G
Railway posts：549,456
Railway comments：2,266,122
Railway最新帖子：2026-06-09 16:00:13
```

本地历史主体完整，但比线上少 2026-06-04 至 2026-06-09 的增量。

复制本地 DB、删除 `search_index` 后执行 `VACUUM`：

```text
删除前：1.586 GiB
删除后：0.841 GiB
现有 trigram FTS 实际占用：762.66 MiB
```

### 7.3 bigram Demo 结构

Demo 不修改主库，输出在 `temp/bigram_demo*`：

```bash
python -m tools.benchmarks.benchmark_bigram_index --sample-mod 20
python -m tools.benchmarks.benchmark_bigram_index --sample-mod 10 --output-dir temp/bigram_demo_10pct
```

索引结构：

```sql
create table search_rows(
    row_id integer primary key,
    post_id text not null,
    kind text not null
);

create virtual table search_bigram using fts5(
    tokens,
    content='',
    contentless_delete=1,
    tokenize='unicode61'
);
```

例如：

```text
学校食堂 -> 学校 校食 食堂
图书馆   -> 图书 书馆
```

查询三字及以上短语时，把查询也转换为相邻 bigram，并使用 FTS phrase，保证顺序和连续性。

`content=''` 表示 FTS 不再重复保存正文；`contentless_delete=1` 允许爬虫按 rowid 删除旧词项并重新写入，适用于增量更新。

### 7.4 容量实验

5% 真实数据样本：

```text
样本行数：139,875
trigram Demo：43.02 MiB
bigram Demo：18.81 MiB
bigram / trigram：0.44x
线性外推 bigram：376.25 MiB
```

10% 真实数据样本：

```text
样本行数：279,759
trigram Demo：83.01 MiB
bigram Demo：36.16 MiB
bigram / trigram：0.44x
线性外推 bigram：361.64 MiB
```

结合现有 trigram 的实际全库占用，预计完整 bigram 索引约为：

```text
约 335-380 MiB
```

若用 bigram 替换现有 trigram，而不是两套并存，预计完整 DB：

```text
无 FTS 主体       约 0.841 GiB
bigram FTS        约 0.33-0.37 GiB
最终 DB           约 1.17-1.21 GiB
```

比当前 1.586 GiB 预计节省约 380-430 MiB。

### 7.5 准确率和耗时抽样

在同一 5% 真实样本上对比 LIKE 与 bigram FTS：

```text
食堂：  LIKE 360 条 / 1462ms，FTS 360 条 / 6.15ms
选课：  LIKE 320 条 / 1976ms，FTS 320 条 / 3.68ms
图书馆：LIKE 419 条 / 2315ms，FTS 419 条 / 5.48ms
六一：  LIKE  15 条 / 2120ms，FTS  15 条 / 1.12ms
快乐：  LIKE 375 条 / 2053ms，FTS 375 条 / 3.67ms
```

以上样本均为：

```text
missing = 0
extra = 0
```

### 7.6 修订后的 Phase 1 建议

不应继续按前文“直接放宽 trigram 至两字”实施。推荐的新 Phase 1：

1. 在本地完整构建 contentless bigram 索引。
2. 验证完整库上的两字、三字、英文、数字、标点和混合关键词。
3. 修改查询层，使正文、评论和管理员正文/评论统一走 bigram。
4. 修改 `SQLitePostStore`，保证新增、评论更新和帖子更新同步维护映射表及 bigram FTS。
5. 保留一字查询 LIKE 兜底。
6. 验证通过后，用 bigram 替换 trigram，不在线上长期保留两套索引。

### 7.7 全量构建结果

已执行：

```bash
python -m tools.benchmarks.benchmark_bigram_index \
  --sample-mod 1 \
  --only-bigram \
  --output-dir temp/bigram_full
```

结果：

```text
文件：temp/bigram_full/bigram_index.db
有效索引行：2,797,496
文件大小：360.47 MiB
构建耗时：约 3 分 08 秒
quick_check：ok
```

全量典型搜索对照：

```text
食堂：    LIKE 5439，bigram 5439
选课：    LIKE 4878，bigram 4878
图书馆：  LIKE 6519，bigram 6519
六一：    LIKE 235， bigram 235
校园卡：  LIKE 2713，bigram 2713
abc：     LIKE 257， bigram 257
```

`研究生` 的 bigram 结果比 SQLite LIKE 多 1 条。检查原文后确认该帖子确实包含“研究生”，只是字段前部含有 `NUL (\x00)`，SQLite LIKE 在 NUL 后停止匹配。该差异属于 LIKE 漏检，不是 bigram 误检。

全量索引包含 `index_meta`，记录版本、源 DB 大小、源行数和构建时间。主库没有被修改或替换。

### 7.8 Sidecar Demo Server

`server.py` 现支持可选参数 `--bigram-db`。不传参数时仍使用原有 trigram/LIKE；传入后，两个及以上可索引字符使用 bigram，一字搜索继续走 LIKE。

启动：

```powershell
python server.py --sqlite-db data\posts.db --bigram-db temp\bigram_full\bigram_index.db --host 127.0.0.1 --port 8099
```

打开：

```text
http://127.0.0.1:8099/
http://127.0.0.1:8099/admin
```

接口测试：

```powershell
Invoke-RestMethod "http://127.0.0.1:8099/api/search?q=食堂&scope=content&limit=10"
Invoke-RestMethod "http://127.0.0.1:8099/api/search?q=食堂&scope=all&limit=10"
```

JSON 会返回实际搜索后端：

```json
{"search_backend": "bigram"}
```

一字搜索返回：

```json
{"search_backend": "like"}
```

本地真实 HTTP 测试：

```text
食堂 / 正文：bigram，3599 条，约 0.2-0.4 秒
食堂 / 全文：bigram，5439 条，约 0.2-0.3 秒
选课 / 正文：bigram，2996 条，约 0.2 秒
猫 / 正文：  LIKE，1258 条，约 1.3 秒
```

预期：

1. 主页、分页、排序、分类和时间筛选保持原样。
2. “正文”只返回正文命中的帖子。
3. “全文”同时返回正文或评论命中的帖子。
4. 管理员正文/评论复选框继续分别生效。
5. 两字及以上中文搜索明显加快。
6. 一字搜索结果不变，但没有性能改善。
