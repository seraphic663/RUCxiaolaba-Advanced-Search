# is_publisher 字段可靠性审计与修复方案（历史证据）

> 状态：本文保留当时的 API 对比和修复推理，旧文件位置与行号只对应历史版本。当前字段标准化入口以 `crawler/normalizer.py`、当前展示逻辑和相关测试为准；重新审计时使用 `tools/audits/audit_is_publisher.py` 采集新证据。

## 1. 问题概述

数据库 `comments.is_publisher` 字段用于标识评论者是否为帖子作者（楼主）。该字段直接影响 Web 前端 [楼主] 标签的显示。

经系统性核查，发现 `is_publisher` 存在三类错误：

| # | 问题 | 影响范围 | 严重度 |
|---|------|----------|--------|
| A | 同一帖子下多个不同 `show_user_id` 被标为楼主 | 814 帖 | ⭐⭐⭐⭐ |
| B | 非匿名帖 OP 评论 uid ≠ 帖子 uid | 2,355/14,696（16%） | ⭐⭐⭐⭐⭐ |
| C | `is_publisher=1` 的评论内容自证不是楼主（称呼"楼主"为第三方） | 待量化 | ⭐⭐⭐⭐ |

**典型病例 — 帖子 #3858923**：

| show_user_id | 显示名 | is_publisher | 评论数 | 实际身份 |
|---|---|---|---|---|
| 17809717 | 某同学AGnFbZyh | 1 | 20 | **真楼主**（与帖子作者同 uid，人大21级转码生） |
| 17809834 | 某同学1 | 1 | 87 | 可能为同一人换 session 后评论 |
| **107607** | **momo** | **1** | **11** | ❌ **非楼主**（自称法大法硕、准备申港三 CS——与帖子内容矛盾） |

同帖中 "momo" 被标为楼主，但其评论内容（"中国政法大学非法本法硕""申港三cs"）与帖子主题（"人大21级经管倒数人二学位转码"）完全无法对应为同一人。

---

## 2. 数据流分析

### 2.1 爬虫链路

```
API: /article/article/info?community_id=4&id=<post_id>
  │
  ▼
crawler_db.py::fetch_detail()
  → api_get()  使用 config.txt 中的 ys7_ysxy_session cookie
  → 返回 JSON: { code: "0000", data: { ..., comment_list: [...] } }
  │
  ▼
crawler_db.py::normalize_detail()
  → 提取 post 字段（show_user_id, real_user_id, content ...）
  → 提取 comment_list 数组（每层含嵌套 reply_comment_list）
  │
  ▼
storage/post_writer.py::SQLitePostStore.upsert_post()
  → replace_comments()
    → flatten_comments()  递归展平评论树
      → comment_row()  逐条读取 item.get("is_publisher")
    → DELETE FROM comments WHERE post_id=?
    → INSERT INTO comments  全量覆盖
```

### 2.2 关键发现

1. **爬虫对 `is_publisher` 零校验**：`comment_row()` 第 75 行直接 `safe_int(item.get("is_publisher"))`，API 返回什么就存什么。

2. **全量覆盖策略**：`replace_comments()` 第 278 行先 DELETE 再 INSERT。如果 API 某次返回了错误的 `is_publisher`，会直接覆盖掉之前正确的值。

3. **使用真实用户 cookie**：爬虫通过 `data/config.txt` 中的 `ys7_ysxy_session` 发起请求。该 cookie 属于一个已登录微信用户，API 可能在计算 `is_publisher` 时受此 cookie 影响。

4. **匿名帖 ID 每次随机**：522,436 条匿名帖的 `show_user_id` 在每次发帖/评论时随机分配，帖子的 uid 和楼主评论的 uid 必然不同——这是匿名机制的设计特征而非 bug。但 `is_publisher` 仍应正确指向楼主。

---

## 3. 根因假设

### 假设 1（最可能）：API 根据请求者 Cookie 计算 is_publisher

```
服务端逻辑（推测）:
  if comment.real_user_id == request.cookie_user.real_user_id:
      is_publisher = 1   // "你"就是楼主
  else:
      is_publisher = 2
```

如果 API 将 `is_publisher` 计算为"评论者是否是**发起请求的用户**"而非"评论者是否是**帖子作者**"，则爬虫使用的 cookie 身份会污染所有请求：

- Cookie 用户恰好在帖子 A 中评论过 → 该评论被标 `is_publisher=1`
- Cookie 用户未评论的帖子 → 没有人被标 `is_publisher=1`
- Cookie 用户在不同帖子的不同 session 中评论 → 多处错误标记

**验证方式**：用两个不同 cookie 请求同一个帖子详情 API，对比返回的 `is_publisher` 值是否一致。

### 假设 2：匿名 Session 过期导致 OP 身份丢失

匿名模式依赖服务端 session 追踪"谁发了帖"。如果 session 在发帖和评论之间过期（用户清缓存、换设备、长时间未访问），服务端无法再将匿名评论关联到原帖作者，`is_publisher` 回退为 2 或被错误分配。

**验证方式**：找一个发帖后长时间未评论、后来才回复的匿名帖，查看 API 返回的 `is_publisher` 是否正确。

### 假设 3：API 服务端 Bug

不依赖 cookie 或 session，纯粹是 API 服务端在特定并发/缓存条件下返回了错误的 `is_publisher`。

**验证方式**：多次请求同一个帖子详情 API（间隔数秒），对比返回值是否一致。

---

## 4. 数据库核查方案

### 4.1 非匿名帖交叉验证（Gold Standard）

非匿名帖（`real_user_id ≠ 0`）的 OP 身份是已知真值——`show_user_id` 持久不变。

```sql
-- 找出所有非匿名帖中 is_publisher 与帖子作者不匹配的
SELECT p.id, p.show_user_id AS post_uid, p.user_name AS post_author,
       p.real_user_id,
       c.show_user_id AS labeled_op_uid, c.show_user_name AS labeled_op_name,
       substr(c.detail, 1, 200) AS comment_text,
       c.comment_id
FROM posts p
JOIN comments c ON c.post_id = p.id AND c.is_publisher = 1
WHERE p.real_user_id NOT IN ('0', '')
  AND p.show_user_id != c.show_user_id
ORDER BY p.id
LIMIT 100;
```

对结果抽样人工判断：`labeled_op_uid` 的评论内容是否为楼主在说话。

- 是 → `is_publisher` 正确，但 uid 因匿名 session 随机化（说明该帖实际上用了匿名模式发帖，尽管 `real_user_id ≠ 0`）
- 否 → `is_publisher` 标错（API bug）

### 4.2 多 OP ID 帖内矛盾检测

```sql
-- 定位所有多 OP 帖
SELECT post_id, COUNT(DISTINCT show_user_id) AS uid_count, COUNT(*) AS total
FROM comments WHERE is_publisher = 1
GROUP BY post_id HAVING uid_count > 1
ORDER BY uid_count DESC;
```

对每个多 OP 帖，提取不同 uid 的评论样本：

```sql
SELECT show_user_id, show_user_name, COUNT(*) AS cnt,
       GROUP_CONCAT(substr(detail, 1, 100), ' ||| ') AS samples
FROM comments
WHERE post_id = ? AND is_publisher = 1
GROUP BY show_user_id;
```

对比不同 uid 的评论是否来自同一语境/同一身份。人工标注真伪。

### 4.3 反向引用检测

真楼主不会用第三人称称呼自己。

```sql
-- is_publisher=1 却称呼"楼主/lz/帖主" → 自证不是楼主
SELECT c.post_id, c.comment_id, c.show_user_name, c.show_user_id,
       substr(c.detail, 1, 200) AS detail
FROM comments c
WHERE c.is_publisher = 1
  AND (lower(c.detail) LIKE '%楼主%'
    OR lower(c.detail) LIKE '%lz%'
    OR c.detail LIKE '%帖主%'
    OR c.detail LIKE '%博主%')
ORDER BY c.post_id
LIMIT 200;
```

### 4.4 瞬移异常检测

同一个 `show_user_id` 不可能在短时间内跨帖当楼主。

```sql
-- 同一 ID 在 5 分钟内出现在两个不同帖子的 is_publisher=1 中
SELECT a.show_user_id,
       a.post_id AS post_a, a.create_time AS time_a,
       b.post_id AS post_b, b.create_time AS time_b,
       ABS(strftime('%s', b.create_time) - strftime('%s', a.create_time)) AS gap_sec
FROM comments a
JOIN comments b ON b.show_user_id = a.show_user_id
  AND b.post_id > a.post_id AND b.is_publisher = 1
WHERE a.is_publisher = 1
  AND ABS(strftime('%s', b.create_time) - strftime('%s', a.create_time)) < 300
ORDER BY gap_sec
LIMIT 50;
```

### 4.5 全局 is_publisher 分布

```sql
-- OP 评论总量与匹配率
SELECT
  COUNT(*) AS total_op_comments,
  SUM(CASE WHEN p.show_user_id = c.show_user_id THEN 1 ELSE 0 END) AS matching_uid,
  SUM(CASE WHEN p.show_user_id != c.show_user_id THEN 1 ELSE 0 END) AS mismatching_uid,
  ROUND(100.0 * SUM(CASE WHEN p.show_user_id = c.show_user_id THEN 1 ELSE 0 END) / COUNT(*), 1) AS match_pct
FROM comments c
JOIN posts p ON p.id = c.post_id
WHERE c.is_publisher = 1;
```

匿名帖（522,436 帖）的 uid 不匹配是预期行为。非匿名帖的不匹配才是真实错误。

---

## 5. API 端验证实验

### 5.1 实验设计

**目的**：验证 `is_publisher` 是否受请求者 cookie 影响。

**步骤**：

1. 选取 3 个测试帖子：
   - 帖子 T1：你自己的匿名帖，你知道自己是楼主
   - 帖子 T2：你朋友的**非匿名帖**，朋友确认了身份
   - 帖子 T3：帖子 #3858923（已知存在 is_publisher 错误）

2. 准备 3 组 cookie：
   - Cookie-A：你的 `ys7_ysxy_session`（发 T1 的账号）
   - Cookie-B：爬虫 `data/config.txt` 中的 cookie
   - Cookie-C：如果可能，一个未登录/游客态的请求（不带 cookie）

3. 用每组 cookie 分别请求 T1/T2/T3 的详情 API：
   ```
   GET https://ys.qimiaoyuanfen.com/article/article/info?community_id=4&id=<post_id>
   Header: ys7_ysxy_session=<cookie>
   ```

4. 对比三组返回的 `comment_list` 中每条评论的 `is_publisher`、`show_user_id`、`show_user_name`。

**预期结果判断**：

| 场景 | 结论 |
|------|------|
| 三组 cookie 返回的 `is_publisher` **完全一致** | 假设 1 不成立，问题出在服务端 session 管理（假设 2）或其他 |
| 三组 cookie 返回的 `is_publisher` **不一致** | 假设 1 成立，需修改爬虫请求策略 |
| Cookie-C（无登录态）返回的 `is_publisher` **最准确** | 爬虫应改用无状态请求 |

### 5.2 实验脚本

审计工具位于 `tools/audits/audit_is_publisher.py`：

```python
#!/usr/bin/env python3
"""对比不同 cookie 下 API 返回的 is_publisher 值是否一致。

用法:
  python -m tools.audits.audit_is_publisher <post_id> <cookie_a> <cookie_b> [cookie_c]

输出:
  - 每组 cookie 下每条评论的 (comment_id, show_user_name, is_publisher)
  - 不一致的评论高亮标记
"""
import json, sys, requests, urllib3
urllib3.disable_warnings()

BASE = "https://ys.qimiaoyuanfen.com"
CID = 4
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "MicroMessenger/7.0.20.1781 MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": "https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html",
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}

def fetch(post_id: str, cookie: str | None, label: str) -> dict:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    if cookie:
        s.cookies.set("ys7_ysxy_session", cookie)

    resp = s.get(f"{BASE}/article/article/info",
                 params={"community_id": CID, "id": post_id}, timeout=15)
    data = resp.json()
    if data.get("code") != "0000":
        print(f"[{label}] API error: code={data.get('code')} msg={data.get('message')}")
        return {}

    post = data.get("data", {})
    print(f"[{label}] post_author: {post.get('show_user_name')} "
          f"(uid={post.get('show_user_id')}, real_uid={post.get('real_user_id')})")
    return post

def flatten_comments(comments: list, depth: int = 0) -> list[dict]:
    result = []
    for c in (comments or []):
        if not isinstance(c, dict):
            continue
        result.append({
            "comment_id": str(c.get("id")),
            "show_user_name": c.get("show_user_name", ""),
            "is_publisher": c.get("is_publisher", 0),
            "detail": str(c.get("detail", ""))[:80],
            "depth": depth,
        })
        replies = c.get("reply_comment_list") or []
        if isinstance(replies, list):
            result.extend(flatten_comments(replies, depth + 1))
    return result

def main():
    if len(sys.argv) < 4:
        print("用法: python audit_is_publisher.py <post_id> <cookie_a> <cookie_b> [cookie_c]")
        sys.exit(1)

    post_id = sys.argv[1]
    cookies = []
    for i, arg in enumerate(sys.argv[2:]):
        label = chr(65 + i)  # A, B, C
        cookies.append((arg if arg != "none" else None, f"Cookie-{label}"))

    results = {}
    for cookie, label in cookies:
        print(f"\n{'='*60}")
        print(f"请求: {label}")
        post = fetch(post_id, cookie, label)
        if not post:
            continue
        comments = flatten_comments(post.get("comment_list", []))
        results[label] = comments
        for c in comments:
            prefix = "  " * c["depth"]
            op_tag = " ★[楼主]" if c["is_publisher"] == 1 else ""
            print(f"  {prefix}#{c['comment_id']} {c['show_user_name']}{op_tag}")
            print(f"  {prefix}  {c['detail']}")

    # 对比
    if len(results) >= 2:
        labels = list(results.keys())
        print(f"\n{'='*60}")
        print("对比结果:")
        set_a = {(c["comment_id"], c["is_publisher"]) for c in results[labels[0]]}
        set_b = {(c["comment_id"], c["is_publisher"]) for c in results[labels[1]]}
        diff = set_a.symmetric_difference(set_b)
        if diff:
            print(f"  ❌ {labels[0]} vs {labels[1]}: {len(diff)} 条不一致")
            for cid, is_pub in sorted(diff):
                a_pub = next((c["is_publisher"] for c in results[labels[0]] if c["comment_id"] == cid), "?")
                b_pub = next((c["is_publisher"] for c in results[labels[1]] if c["comment_id"] == cid), "?")
                print(f"    #{cid}: {labels[0]}={a_pub}, {labels[1]}={b_pub}")
        else:
            print(f"  ✅ {labels[0]} vs {labels[1]}: 完全一致")

if __name__ == "__main__":
    main()
```

### 5.3 执行

```bash
# 用你的 cookie 和爬虫的 cookie 对比
python -m tools.audits.audit_is_publisher 3858923 "你的cookie" "$(cat data/config.txt | grep ys7_ysxy_session | cut -d= -f2)"

# 如果可以抓到未登录态的请求，再加一组
python -m tools.audits.audit_is_publisher 3858923 "你的cookie" "$(cat data/config.txt | grep ys7_ysxy_session | cut -d= -f2)" "none"
```

---

## 6. 修复方案

### 6.1 短期：数据库端修正（立即可做，不依赖 API）

#### Step 1：非匿名帖修正（最可靠）

非匿名帖中 `post.show_user_id` 是楼主真值。

```sql
-- 清除错误的 OP 标记
UPDATE comments SET is_publisher = 2
WHERE is_publisher = 1
  AND post_id IN (SELECT id FROM posts WHERE real_user_id NOT IN ('0',''))
  AND show_user_id != (
    SELECT show_user_id FROM posts WHERE id = comments.post_id
  );

-- 用帖子作者 ID 重标真正的 OP
UPDATE comments SET is_publisher = 1
WHERE is_publisher != 1
  AND post_id IN (SELECT id FROM posts WHERE real_user_id NOT IN ('0',''))
  AND show_user_id = (
    SELECT show_user_id FROM posts WHERE id = comments.post_id
  );
```

#### Step 2：清除自指矛盾

```sql
UPDATE comments SET is_publisher = 2
WHERE is_publisher = 1
  AND (lower(detail) LIKE '%楼主%'
    OR lower(detail) LIKE '%lz%'
    OR detail LIKE '%帖主%');
```

#### Step 3：多 OP 帖 — 保留评论最多的 uid

```sql
-- 对每个多 OP 帖，只保留评论数最多的那个 show_user_id 的 is_publisher=1
WITH ranked AS (
  SELECT post_id, show_user_id, COUNT(*) AS cnt,
         ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY COUNT(*) DESC) AS rn
  FROM comments WHERE is_publisher = 1
  GROUP BY post_id, show_user_id
)
UPDATE comments SET is_publisher = 2
WHERE is_publisher = 1
  AND (post_id, show_user_id) IN (
    SELECT post_id, show_user_id FROM ranked WHERE rn > 1
  );
```

逻辑：真正的楼主在自己帖子里通常最活跃，评论数最多。

#### Step 4：瞬移异常修正

```sql
-- 同一 uid 在 5 分钟内出现在两个不同帖子作为楼主 → 清除较晚的那个
-- （简化版：对疑似污染的 uid，只保留其最早出现的帖子的 OP 标记）
```

### 6.2 中期：前端兼容（server.py 修改）

```python
# 当前代码（server.py L1042）:
tag = " [楼主]" if c.get("is_publisher") else ""

# 改进：匿名帖下统一显示"楼主"而非随机匿名名
if c.get("is_publisher"):
    if post_is_anonymous:
        display_name = "楼主"  # 避免匿名帖名字不匹配的视觉混淆
    else:
        display_name = c.get("show_user_name")
    tag = " [楼主]"
else:
    display_name = c.get("show_user_name")
    tag = ""
```

### 6.3 长期：爬虫端修复（依赖 API 验证结论）

根据第 5 节实验结论：

- **如果假设 1 成立**：将爬虫 cookie 替换为游客态（不带 `ys7_ysxy_session`），或在请求详情时使用不带 cookie 的独立 Session
- **如果假设 2 成立**：`is_publisher` 本身不可靠，在爬虫端加入启发式校验（对比 show_user_id、交叉验证评论内容一致性），标记低置信度的 OP 标注
- **如果三者都不成立**：`is_publisher` 随机出错，建立独立的 OP 真值表，在 `upsert_post` 后执行 SQL 修正

### 6.4 建立 OP 真值表

```sql
CREATE TABLE op_ground_truth (
  post_id TEXT PRIMARY KEY,
  op_show_user_id TEXT NOT NULL,
  verification_method TEXT NOT NULL,
  confidence REAL NOT NULL,
  verified_at TEXT NOT NULL
);

-- 非匿名帖：confidence = 1.0
INSERT INTO op_ground_truth (post_id, op_show_user_id, verification_method, confidence, verified_at)
SELECT id, show_user_id, 'non_anonymous', 1.0, datetime('now')
FROM posts WHERE real_user_id NOT IN ('0','');

-- 匿名帖：如果有 is_publisher=1 的唯一评论者，标记为 0.7
INSERT INTO op_ground_truth (post_id, op_show_user_id, verification_method, confidence, verified_at)
SELECT post_id, show_user_id, 'single_op_heuristic', 0.7, datetime('now')
FROM comments WHERE is_publisher = 1
GROUP BY post_id HAVING COUNT(DISTINCT show_user_id) = 1
ON CONFLICT(post_id) DO NOTHING;
```

---

## 7. 需要你协助的事项

| 优先级 | 事项 | 目的 |
|--------|------|------|
| **P0** | 提供你的 `ys7_ysxy_session` cookie | 与爬虫 cookie 做对比实验 |
| **P0** | 确认一个你已知 OP 身份的帖子 ID | 作为 ground truth |
| **P1** | 从小程序抓一个未登录态的请求（如果存在） | 验证是否游客态 API 返回更准 |
| **P1** | 多抓几个不同用户的 cookie（问朋友要） | 扩大对比范围 |
| **P2** | 人工标注 20-50 个帖子的真实楼主 | 训练/验证修正规则 |

---

## 8. 附录：关键 SQL 速查

```sql
-- 全局统计
SELECT 'posts' tbl, count(*) FROM posts
UNION ALL SELECT 'comments', count(*) FROM comments
UNION ALL SELECT 'op_comments', count(*) FROM comments WHERE is_publisher=1
UNION ALL SELECT 'anon_posts', count(*) FROM posts WHERE real_user_id IN ('0','')
UNION ALL SELECT 'non_anon_posts', count(*) FROM posts WHERE real_user_id NOT IN ('0','');

-- 非匿名帖 OP 标签准确率
SELECT
  count(DISTINCT p.id) AS non_anon_with_op,
  count(DISTINCT CASE WHEN p.show_user_id = c.show_user_id THEN p.id END) AS correct,
  count(DISTINCT CASE WHEN p.show_user_id != c.show_user_id THEN p.id END) AS incorrect
FROM posts p
JOIN comments c ON c.post_id = p.id AND c.is_publisher = 1
WHERE p.real_user_id NOT IN ('0','');

-- 多 OP 帖 Top 10
SELECT post_id, count(DISTINCT show_user_id) AS uid_count, count(*) AS total
FROM comments WHERE is_publisher = 1
GROUP BY post_id HAVING uid_count > 1
ORDER BY uid_count DESC LIMIT 10;

-- 反向引用检测
SELECT count(*) AS self_refuting
FROM comments
WHERE is_publisher = 1
  AND (lower(detail) LIKE '%楼主%' OR detail LIKE '%lz%' OR detail LIKE '%帖主%');
```
