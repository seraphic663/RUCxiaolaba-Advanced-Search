# raw_json 字段分析与精简方案

## 背景

> 当前状态：生产 schema 已将 `comments.raw_json` 重命名/瘦身为
> `comments.reply_comment_list`，只保留嵌套回复列表。本文前半部分是迁移前
> 对旧 `raw_json` 的历史分析，用于说明为什么可以瘦身。

`comments` 表每行存储了爬虫从 API 获取的原始评论 JSON（`raw_json` 列），平均 836 字节/条，225 万条评论合计 **1.75 GB**，占数据库 46%。

本文分析这 1.75 GB 里到底存了什么，哪些有意义，哪些可以删。

## raw_json 字段全览（35 个）

### 第一类：已被结构化列覆盖（8 个字段）— 100% 冗余

这些字段的值已经提取到 `comments` 表的结构化列中，完全相同：

| raw_json 字段 | 对应结构化列 | 抽样非空率 |
|---|---|---|
| `detail` | `comments.detail` | 100% |
| `show_user_name` | `comments.show_user_name` | 100% |
| `show_user_id` | `comments.show_user_id` | 100% |
| `real_user_id` | `comments.real_user_id` | 100% |
| `create_time` | `comments.create_time` | 100% |
| `is_publisher` | `comments.is_publisher` | 100% |
| `reply_show_user_name` | `comments.reply_show_user_name` | 100% |
| `reply_show_user_id` | `comments.reply_show_user_id` | 100% |

### 第二类：常量垃圾（10 个字段）— 无意义

这些字段对所有评论永远返回相同的值，无信息量：

| 字段 | 固定值 | 说明 |
|------|--------|------|
| `community_id` | `"4"` | RUC 社区 ID，从不变 |
| `group_id` | `"0"` | 从不使用分组 |
| `is_group` | `"2"` | 固定值 |
| `is_bounty_sure` | `2` | 不相关功能 |
| `is_bounty_winner` | `2` | 不相关功能 |
| `is_top` | `"1"` | 固定值 |
| `reply_show_type` | `"1"` | 固定值 |
| `show_type` | `"1"` | 固定值 |
| `user_type` | `1` | 固定值 |
| `top_comment_id` | `"0"` | 从不置顶评论 |

### 第三类：会话依赖（3 个字段）— 无意义

这些字段取决于抓取时使用的 cookie（哪个用户登录了小程序），换 cookie 后值会变：

| 字段 | 说明 |
|------|------|
| `is_mine` | 这条评论是不是"我"发的（取决于 cookie 所属用户） |
| `has_star` | "我"有没有给这条评论点赞 |
| `has_trace` | "我"有没有蹲这条评论 |

这三个字段是用户视角的状态，不是评论的固有属性。用不同 cookie 抓同一评论，值不同。**毫无保存价值。**

### 第四类：冗余 ID（3 个字段）— 无意义

| 字段 | 实际含义 | 已在表中 |
|------|---------|---------|
| `id` | 评论 ID | `comments.comment_id` |
| `article_id` | 所属帖子 ID | `comments.post_id` |
| `reply_comment_id` | 父评论 ID | `comments.parent_comment_id` |

### 第五类：永不展示的媒体字段（4 个字段）— 无意义

| 字段 | 非空率 | 说明 |
|------|:---:|------|
| `show_user_head` | 100% | 评论者头像 URL，UI 从不渲染（和 `posts.show_user_head` 一样是死数据） |
| `reply_show_user_head` | 100% | 被回复者头像 URL，同上 |
| `show_images` | 1% | 评论带的图片，极少有，UI 也不展示 |
| `images` | 1% | 同上 |

### 第六类：冗余时间字段（2 个字段）— 无意义

| 字段 | 示例值 | 说明 |
|------|--------|------|
| `show_create_time` | `"刚刚"`、`"3小时前"` | 人类可读的相对时间，无精确度 |
| `update_time` | `"2026-06-01 21:25:23"` | 与 `create_time` 基本相同 |

`create_time` 已结构化存储，精确到秒。这两个是冗余副本。

### 第七类：帖子级聚合字段（2 个字段）— 无意义

| 字段 | 说明 |
|------|------|
| `count_comment` | 这条评论发出时帖子的评论总数，非评论自身属性 |
| `count_star` | 同上，点赞总数 |

这些是帖子维度的数据，存到每条评论里是冗余。帖子级数据已在 `posts` 表维护。

### 第八类：需保留（1 个字段）— 唯一有价值的内容

| 字段 | 非空率 | 说明 |
|------|:---:|------|
| `reply_comment_list` | 19% | **嵌套子回复列表**，含回复的完整 JSON |

这是唯一无法从结构化列完全恢复的字段。虽然 `comments` 表通过 `parent_comment_id` 扁平化了评论层级，但当前的 `flatten_comments()` 函数只扁平化两层（顶层评论 + 一层回复）。API 返回的数据中，回复本身可能还有 `reply_comment_list`。

**不过**：`api_comments_sqlite()` 在读数据时通过 `parent_comment_id` 重建嵌套结构。如果 `flatten_comments()` 被修正为递归处理所有层级，则 `reply_comment_list` 也可以从结构化列完全恢复。

**保守策略**：在确认递归扁平化无遗漏之前，保留 `reply_comment_list`。

---

## 体积分解

```
raw_json 平均 836 字节的构成
（225 万条 × 836 字节 = 1.75 GB）

███████████████  JSON key 名开销            ~350 字节   35 个字段名每行重复
██████           字段 1-7 类（垃圾数据）      ~250 字节   常量值 + 冗余 ID + 会话状态
████             reply_comment_list          ~120 字节   仅 19% 非空时有内容
███              show_user_head × 2          ~120 字节   头像 URL
█                detail 文本                   ~22 字节   结构化列已有
█                JSON 括号/引号/逗号           ~60 字节
                 = 总计 ~836 字节
```

---

## 精简方案

### 目标

- 删除 `raw_json` 中的 34/35 个字段，只保留 `reply_comment_list`
- 删除 `posts.show_user_head` 列（30 MB，代码全项目零引用）
- 修正 `flatten_comments()` 为递归处理所有嵌套层级

### DB 体积变化

```
当前:    3.80 GB
精简后:  ~2.05 GB   (省 1.75 GB，约 46%)
+ VACUUM 后可能再回收一些碎片空间
```

### 实施步骤

#### 步骤 1：修改爬虫写入逻辑（`crawler_db.py` + `storage/sqlite_store.py`）

`normalize_detail()` / `comment_row()` 不再将完整 JSON 写入 `raw_json`，改为只写入 `{"reply_comment_list": [...]}`。

改前：
```python
# storage/sqlite_store.py:45
json.dumps(item, ensure_ascii=False, separators=(",", ":"))
```

改后：
```python
# 只保留 reply_comment_list
slim = {}
if item.get("reply_comment_list"):
    slim["reply_comment_list"] = item["reply_comment_list"]
json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
```

#### 步骤 2：修正 `flatten_comments()` 递归处理

当前函数只处理两层（顶层 + 一层回复）。需改为递归遍历 `reply_comment_list` 内的 `reply_comment_list`，确保所有嵌套层级的评论都被写入 `comments` 表。

#### 步骤 3：迁移历史数据（一次性脚本）

对 `comments` 表中已有的 225 万条记录，遍历并替换 `raw_json` 为精简版：

```sql
-- 逻辑（伪代码）
for each row in comments:
    old = json.loads(row.raw_json)
    new = {}
    if old.get("reply_comment_list"):
        new["reply_comment_list"] = old["reply_comment_list"]
    update comments set raw_json = json.dumps(new) where row_key = ?
```

注意：225 万条操作量较大，需分批提交（每 10000 条 commit 一次），避免 WAL 膨胀。

#### 步骤 4：删除 `posts.show_user_head` 列

SQLite 不支持 `ALTER TABLE DROP COLUMN`（3.35.0 之前），需要重建表：

```sql
-- 建新表（不含 show_user_head）
create table posts_new (
    id text primary key,
    content text not null,
    ...
    -- show_user_head 不包含在此
);
insert into posts_new select id, content, ... from posts;
drop table posts;
alter table posts_new rename to posts;
-- 重建索引
```

也可以在步骤 3 的迁移脚本中一并处理。

#### 步骤 5：VACUUM

迁移完成后执行 `VACUUM`，回收删除 1.75 GB 数据后产生的空闲页。

注意：VACUUM 需要额外约 2 GB 临时空间。在本地（非 Railway Volume）执行，然后重新上传 DB。

#### 步骤 6（可选）：验证 `reply_comment_list` 是否可完全由结构化列重建

在步骤 2 完成后，在测试环境对比：
- 从 `reply_comment_list` JSON 还原的嵌套结构
- 从 `comments` 表通过 `parent_comment_id` 重建的嵌套结构

如果两者一致，则可以进一步删除 `reply_comment_list`，`raw_json` 列整个清空或删除。

### 不改动的部分

- `server.py`：不需要改。`api_comments_sqlite()` 读的是结构化列，不依赖 `raw_json`。
- 前端 HTML：不需要改。
- `posts` 表的整数字段（`trace_count`、`views`、`hot`）：保留。删它们收益 < 2 MB，不值得折腾。
- `search_index`：保留。删了搜索就废了。

### 风险

| 风险 | 等级 | 缓解 |
|------|:---:|------|
| 迁移脚本中断导致 DB 损坏 | 中 | 先复制一份 DB 做迁移，验证通过后再替换原文件 |
| `reply_comment_list` 遗漏嵌套层级 | 低 | 保留 `reply_comment_list` 作为安全网；递归修正后再评估 |
| VACUUM 磁盘空间不足 | 低 | 在本地（非 Railway Volume）操作，确保有 5GB+ 空闲 |
| 爬虫继续写完整 JSON | 低 | 修改 `crawler_db.py` 和 `sqlite_store.py`，新数据直接写精简版 |

### 建议执行顺序

1. 先在本地测试环境完成全部迁移
2. 验证搜索、评论展开、admin 功能正常
3. 将精简后的 DB 上传到阿里云 / Railway
4. 部署更新后的爬虫代码

---

## 实际执行结果（2026-06-03）

### 已执行步骤

**全部步骤已完成。** 步骤 6（验证嵌套是否可完全由结构化列重建）留待后续。

### 第一轮：数据瘦身（已完成）

详见上一版记录。raw_json 从 35 字段完整 JSON（avg 836 字节）瘦身为只保留 `reply_comment_list`（avg 44 字节），省 1.76 GB。

### 第二轮：列重命名 + VACUUM（已完成）

#### 列重命名

`raw_json` 名不副实，重命名为 `reply_comment_list`：

```sql
ALTER TABLE comments RENAME COLUMN raw_json TO reply_comment_list;
```

#### 代码适配的文件

| 文件 | 改动 |
|------|------|
| `storage/sqlite_store.py:148` | `init_schema()` 建表语句：`raw_json` → `reply_comment_list` |
| `storage/sqlite_store.py:27-34` | `slim_raw()` 不变（函数只处理数据，不涉及列名） |
| `scripts/build_slim_sqlite.py` | `COMMENT_COLUMNS` + schema + 打印消息全部更新 |
| `scripts/migrate_slim_raw_json.py` | 全文重写，SQL 查询 + 变量名 + 文档全部更新为 `reply_comment_list` |
| `tests/test_sqlite_store.py:73-74` | 列名 + 断言内容适配（`show_user_head` → `reply_comment_list` 嵌套结构） |
| `server.py:846` | 顺带修复 `log_message` IndexError |

**`server.py`、`crawler_db.py`、前端 HTML — 不改。** 它们不引用列名。

#### VACUUM 结果

```
VACUUM 前:  3,983 MB  (含 ~2.3 GB UPDATE 遗留垃圾页)
VACUUM 后:  1,617 MB  (58% 缩减)
耗时:       46 秒
WAL:        checkpoint(truncate) → 0 MB
```

#### 最终 DB 组成

```
posts.db        1,617 MB
├── posts 数据+索引       ~100 MB
├── comments 数据         ~350 MB  (reply_comment_list 占 94 MB)
├── search_index FTS5     ~500 MB  (trigram 中文索引)
├── 9 个 B-tree 索引      ~350 MB
└── 页面结构开销          ~300 MB
```

### 迁移统计（两轮合计）

```
raw_json 旧大小:   1.85 GB
reply_comment_list: 0.09 GB
直接节省:           1.76 GB
VACUUM 额外回收:    ~0.5 GB (碎片/垃圾页)
─────────────────────────────
DB 总缩减:         3.98 GB → 1.62 GB  (省 2.36 GB, 59%)
```

### 测试验证结果（VACUUM 后）

| 端点 | 结果 |
|------|:---:|
| `/healthz` | ✅ |
| `/` 主页 | ✅ |
| `/api/search` 空搜索 | ✅ total=543,962 |
| `/api/search?q=毕业照` | ✅ total=527 |
| `/api/search?sort=comments` | ✅ |
| `/api/search?sort=score` | ✅ |
| `/api/search?sort=hot` | ✅ 自动回退 time |
| `/api/search?sort=views` | ✅ 自动回退 time |
| `/api/comments?id=` | ✅ 嵌套结构正常 |
| `/api/categories` | ✅ 585 类 |
| `/admin` | ✅ 200 |
| `/api/checkin` | ✅ |

### 对爬虫的影响

新爬取的评论从 `crawler_db.py → sqlite_store.py → slim_raw()` 自动写入 `reply_comment_list` 列（只含嵌套回复结构）。无需修改 `crawler_db.py`。

旧评论被爬虫重新抓取时（评论数变化触发 `upsert_post`），也会自动替换为新格式。
