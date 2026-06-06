# AI 智能搜索 + 邀请码方案

> 状态: **规划中，未实施**  
> 日期: 2026-06-06  
> 目标: 在现有搜索基础上增加 AI 驱动的对话式搜索与总结

---

## 目录

1. [技术架构总览](#1-技术架构总览)
2. [数据库变更](#2-数据库变更)
3. [后端新增 / 变更](#3-后端新增--变更)
4. [前端 UI 变更](#4-前端-ui-变更)
5. [AI 调用流程与 Prompt 设计](#5-ai-调用流程与-prompt-设计)
6. [安全与滥用防护](#6-安全与滥用防护)
7. [成本估算](#7-成本估算)
8. [实施步骤](#8-实施步骤)
9. [文件变更清单](#9-文件变更清单)

---

## 1. 技术架构总览

```
┌──────────────────────────────────────────────────┐
│                   main.html                       │
│  ┌─────────────────────────────────────────┐     │
│  │ 常规搜索 (不变)                          │     │
│  └─────────────────────────────────────────┘     │
│  ┌─────────────────────────────────────────┐     │
│  │ AI 对话面板 (新增)                       │     │
│  │  ┌──────────────┐  ┌──────────────────┐ │     │
│  │  │ 邀请码入口   │  │ 对话列表 (侧栏)  │ │     │
│  │  └──────────────┘  └──────────────────┘ │     │
│  │  ┌──────────────────────────────────────┐ │     │
│  │  │ 对话消息流 (用户 ↔ AI)              │ │     │
│  │  │ 支持追问、多轮对话                  │ │     │
│  │  └──────────────────────────────────────┘ │     │
│  └─────────────────────────────────────────┘     │
└──────────────────────────────────────────────────┘
         │
         ▼
    server.py  ─── /api/ai/search       (AI 搜索 + 总结)
               ─── /api/ai/chat         (多轮对话追问)
               ─── /api/ai/conversations (对话列表管理)
               ─── /api/ai/invite       (邀请码激活)
               ─── /api/ai/status       (查看配额)
               ─── /api/ai/delete-conv  (删除对话)
         │
         ▼
    DeepSeek V4 Flash API  (默认, 非思考模式降低成本)
    DeepSeek V4 Pro   API  (复杂分析场景, 可选升级)
         │
         ▼
    SQLite posts.db  (不变, 现有 FTS 粗筛 → LLM 精加工)
```

**核心设计原则**：
- 现有搜索**完全不变**，AI 作为独立功能面板附加
- 两阶段检索：SQLite FTS 粗筛 → DeepSeek 精加工总结
- 对话持久化，支持多轮追问和多对话管理
- 邀请码制，无需注册系统，零资质成本

---

## 2. 数据库变更

### 文件: `storage/sqlite_store.py` → `init_schema()` 新增 DDL

在 `posts.db` 中新增 4 张表（不影响现有表）：

```sql
-- 邀请码表（由管理员在 admin 面板或命令行管理）
create table if not exists invite_codes (
    code            text primary key,           -- 如 "XLB-A1B2C3D4"
    created_at      text not null,              -- 生成时间
    daily_quota     integer not null default 30, -- 每日 AI 搜索次数
    max_quota       integer not null default 0,  -- 0=无限总次数
    used_total      integer not null default 0,  -- 已使用总次数
    is_active       integer not null default 1,  -- 0=禁用
    note            text not null default ''     -- 管理员备注（谁在使用）
);

-- 对话表
create table if not exists ai_conversations (
    conv_id         text primary key,           -- UUID
    invite_code     text not null,              -- 关联的邀请码
    title           text not null default '新对话', -- 对话标题（用户可修改）
    created_at      text not null,
    updated_at      text not null,
    is_deleted      integer not null default 0, -- 软删除
    foreign key (invite_code) references invite_codes(code)
);

-- 对话消息表
create table if not exists ai_messages (
    msg_id          integer primary key autoincrement,
    conv_id         text not null,
    role            text not null,              -- 'user' | 'assistant' | 'system'
    content         text not null,
    search_query    text,                       -- 本次使用的搜索词（user 消息用）
    cited_posts     text,                       -- 引用的帖子 ID 列表，JSON array
    token_count     integer,                    -- 该消息消耗的 token 数
    created_at      text not null,
    foreign key (conv_id) references ai_conversations(conv_id)
);

create index if not exists idx_ai_messages_conv on ai_messages(conv_id, msg_id);
create index if not exists idx_ai_conv_code on ai_conversations(invite_code);

-- 每日使用统计（按邀请码 + 日期）
create table if not exists ai_daily_usage (
    invite_code     text not null,
    usage_date      text not null,              -- '2026-06-06'
    query_count     integer not null default 0,
    token_input     integer not null default 0,
    token_output    integer not null default 0,
    primary key (invite_code, usage_date),
    foreign key (invite_code) references invite_codes(code)
);
```

### 管理工具: 新增 `scripts/manage_invites.py`

```text
# 批量生成邀请码
python scripts/manage_invites.py generate --count 50 --daily 30 --prefix XLB

# 列出所有邀请码及使用情况
python scripts/manage_invites.py list

# 禁用/启用某个邀请码
python scripts/manage_invites.py disable XLB-A1B2C3D4
python scripts/manage_invites.py enable XLB-A1B2C3D4

# 修改配额
python scripts/manage_invites.py set-quota XLB-A1B2C3D4 --daily 50 --max 500

# 查看每日使用统计
python scripts/manage_invites.py stats
```

---

## 3. 后端新增 / 变更

### 3.1 server.py — 新增路由

在 `do_GET` 中新增：

```python
elif path == "/api/ai/search":
    self._handle_ai_search()
elif path == "/api/ai/chat":
    self._handle_ai_chat()
elif path == "/api/ai/conversations":
    self._handle_ai_conversations()
elif path == "/api/ai/invite":
    self._handle_ai_invite()
elif path == "/api/ai/status":
    self._handle_ai_status()
```

在 `do_POST` 中新增：

```python
elif path == "/api/ai/search":
    self._handle_ai_search()
elif path == "/api/ai/chat":
    self._handle_ai_chat()
elif path == "/api/ai/delete-conv":
    self._handle_ai_delete_conv()
elif path == "/api/ai/invite":
    self._handle_ai_invite()
```

### 3.2 邀请码管理（`_handle_ai_invite`）

```
POST /api/ai/invite
Body: {"code": "XLB-A1B2C3D4"}

返回:
- 成功: {"ok": true, "session_token": "<UUID>"}
- 失败: {"ok": false, "error": "邀请码无效或已禁用"}
```

流程：
1. 查询 `invite_codes` 表，检查 `is_active=1`
2. 查询 `ai_daily_usage`，检查今日使用次数 `< daily_quota`
3. 生成 `session_token`（UUID），写入 Cookie（`ai_token`，30 天有效期）
4. 在内存中维护 `ai_sessions: {session_token: invite_code}` 映射

### 3.3 AI 首次搜索（`_handle_ai_search`）

```
POST /api/ai/search
Body: {"query": "最近大家怎么评价食堂的？"}

流程:
1. 验证 ai_token → 获取 invite_code
2. 检查每日配额 → 未超限则继续
3. 安全过滤检查 (见 §6.2)
4. SQLite FTS 粗筛 → top 20 帖子 + 各取 top 3 评论
5. 拼接成 AI prompt (见 §5)
6. 调用 DeepSeek API
7. 创建新对话 → 保存 user/assistant 消息
8. 更新 ai_daily_usage
9. 返回 {"conv_id": "...", "summary": "...", "posts": [...], "remaining": N}
```

### 3.4 AI 追问（`_handle_ai_chat`）

```
POST /api/ai/chat
Body: {"conv_id": "uuid", "query": "那几个帖子里有没有提到二食堂的？"}

流程:
1. 验证 ai_token
2. 安全过滤检查
3. 加载对话历史（最近 10 轮，约 8K tokens）
4. 如果 query 包含新的检索需求 → 再次 SQLite FTS 粗筛 → 追加到 context
5. 拼接完整 prompt（包含对话历史 + 新搜索结果）
6. 调用 DeepSeek API
7. 保存新消息
8. 更新 ai_daily_usage
9. 返回 {"summary": "...", "referenced_ids": [...], "remaining": N}
```

### 3.5 对话管理（`_handle_ai_conversations` / `_handle_ai_delete_conv`）

```
GET /api/ai/conversations
→ {"conversations": [{"id": "uuid", "title": "...", "updated_at": "...", "msg_count": N}, ...]}

POST /api/ai/delete-conv
Body: {"conv_id": "uuid"}
→ {"ok": true}
```

### 3.6 配额查询（`_handle_ai_status`）

```
GET /api/ai/status
→ {"daily_quota": 30, "used_today": 5, "remaining": 25}
```

### 3.7 IP 级别限流（现有架构增强）

```python
# server.py 新增——在 _handle_ai_* 所有方法中先检查
AI_RATE_LIMIT = {
    # IP → [timestamps]
}
RATE_PER_MINUTE = 6       # 同一 IP 每分钟最多 6 次 AI 请求
RATE_PER_HOUR = 100       # 同一 IP 每小时最多 100 次
```

---

## 4. 前端 UI 变更

### 4.1 `templates/main.html` — 新增 AI 对话面板

**布局**（在搜索结果区下方新增）：

```
┌─────────────────────────────────────────────┐
│  [常规搜索输入框]         (不变)             │
│  [排序 / 筛选]            (不变)             │
│  [帖子列表]               (不变)             │
├─────────────────────────────────────────────┤
│  ──────── AI 智能搜索 ────────              │
│                                              │
│  [输入邀请码] [我已经有码]                   │
│  ┌─────────────────────────────────────┐    │
│  │ 对话列表 (可折叠侧栏)               │    │
│  │ ┌─────────────────────────────────┐ │    │
│  │ │ + 新对话                        │ │    │
│  │ │ ─────────────────────────────── │ │    │
│  │ │ 📝 大家对食堂的评价... 1h ago   │ │    │
│  │ │ 📝 选课互助帖整理...   3h ago   │ │    │
│  │ │ 📝 校园网速度讨论...   1d ago   │ │    │
│  │ └─────────────────────────────────┘ │    │
│  └─────────────────────────────────────┘    │
│  ┌─────────────────────────────────────┐    │
│  │ 当前对话                            │    │
│  │ ┌──────────────┐                    │    │
│  │ │ 用户: 大家对  │                    │    │
│  │ │ 食堂怎么看？  │                    │    │
│  │ └──────────────┘                    │    │
│  │ ┌──────────────┐                    │    │
│  │ │ AI: 根据近7天 │                    │    │
│  │ │ 的讨论，总结  │                    │    │
│  │ │ 如下...       │                    │    │
│  │ │ [引用帖子ID]  │                    │    │
│  │ └──────────────┘                    │    │
│  │ ┌──────────────────────────────┐    │    │
│  │ │ 继续提问...         [发送]   │    │    │
│  │ └──────────────────────────────┘    │    │
│  │ 剩余: 25/30 次                      │    │
│  └─────────────────────────────────────┘    │
└─────────────────────────────────────────────┘
```

### UI 设计要点

1. **颜色**: 沿用 `#8b0012` (RUC 红) 作为主色调，`#f5f5f5` 背景
2. **字体**: `-apple-system, "Microsoft YaHei", sans-serif`
3. **AI 消息气泡**: 白色卡片 + 圆角 (`border-radius: 12px`)，阴影同现有 `.post`
4. **用户消息**: 右侧对齐，浅红底 `#fff0f0`
5. **AI 消息**: 左侧对齐，白底
6. **引用样式**: 蓝色可点击链接，如 `[查看原帖 #12345]`
7. **邀请码输入**: 居中模态框，类似 admin 登录页风格
8. **对话列表**: 左侧窄栏，最大宽度 280px，可折叠
9. **配额指示器**: 页脚轻量提示，`color: #999; font-size: 0.8em`
10. **响应式**: 移动端对话列表收起到顶部汉堡菜单

### 4.2 `templates/admin_dashboard.html` — 新增邀请码管理

在管理面板增加一个 tab 或 section：

```
┌──────────────────────────────────────┐
│ [统计] [帖子搜索] [用户分析] [邀请码] │  ← 新增 [邀请码]
└──────────────────────────────────────┘

邀请码管理区:
  - 一键生成 10/50/100 个邀请码
  - 表格列出所有邀请码（码、每日配额、今日用量、总用量、状态、备注、操作）
  - 禁用/启用按钮
  - 修改配额按钮
  - 查看该邀请码下的对话列表
```

---

## 5. AI 调用流程与 Prompt 设计

### 5.1 系统 Prompt（`server.py` 中定义）

```python
SYSTEM_PROMPT = """你是 RUC小喇叭 的 AI 搜索助手。你会收到来自中国人民大学匿名论坛（RUC小喇叭）的帖子和评论数据。

你的职责:
1. 根据用户的问题，从提供的帖子数据中提炼信息，给出准确、有条理的总结
2. 引用帖子 ID 时，使用格式「[#帖子ID]」让用户可以直接定位
3. 如果数据不足以回答某个问题，诚实告知"根据现有数据无法确定"，不要编造
4. 如果有人询问具体个人信息或敏感内容，礼貌拒绝并说明"该问题涉及个人隐私，无法提供相关信息"
5. 保持中立、客观，不评判帖子观点，只做事实性总结
6. 对于涉及色情、暴力、违法内容的问题，拒绝回答并提示用户遵守社区规范
7. 回答尽量简洁，关键信息优先，复杂问题可适当分段

关于论坛数据:
- 论坛为匿名论坛，帖子发布者的显示名是系统随机生成的，不代表真实身份
- 帖子时间覆盖范围有限，不代表完整的历史数据
- 如果用户要求的数据不在当前搜索结果中，建议用户尝试更换搜索词

禁止行为:
- 严禁尝试猜测、推断帖子发布者的真实身份
- 严禁输出任何鼓励犯罪、伤害他人、破坏校园秩序的内容
- 严禁编造不存在的帖子或数据
- 严禁泄露 show_user_id、real_user_id 等内部标识
"""
```

### 5.2 首次搜索 Prompt 结构

```
SYSTEM_PROMPT
---
用户问题: {query}
---
以下是搜索到的相关帖子（按相关度排序）:

[帖子 #123456] 分类:日常投稿 | 时间:2026-06-05 14:30 | 👍3 💬5
内容: 热哭了。。学校什么时候给装空调啊，图书馆人满了根本抢不到位置

热门评论:
- 某同学A: 宿舍也是，风扇根本不够
- 某同学B: 据说今年夏天比往年都热...

[帖子 #123457] ...
---
请根据以上数据回答用户的问题。引用帖子时使用 [#ID] 格式。如果数据不足，诚实说明。
```

### 5.3 追问 Context 结构

```
SYSTEM_PROMPT

<对话历史>
用户: 大家对食堂怎么看？
助手: 根据近期的帖子，同学对食堂的讨论主要集中在...[#1001][#1002]...
用户: 那几个帖子里有没有提到二食堂的？
</对话历史>

用户新问题: {query}

<补充搜索结果（如有重新检索）>
...
</补充搜索结果>

请结合对话历史和补充数据回答。
```

### 5.4 安全过滤层（独立函数 `check_content_safety`）

```python
def check_content_safety(query: str) -> tuple[bool, str | None]:
    """
    返回 (is_safe, rejection_reason)
    
    检查维度:
    1. 个人信息搜索: 匹配手机号/学号/身份证/宿舍号等模式
    2. 色情/低俗: 关键词匹配 + 简单启发式
    3. 违法/暴力: 关键词匹配
    4. 过度具体的人身搜索: "XX学院那个女生""XX老师的联系方式"等
    
    Safe → (True, None)
    Unsafe → (False, "rejection message")
    """
```

拒绝话术示例：

> "抱歉，该搜索涉及他人个人隐私信息。RUC小喇叭 AI 助手不会尝试识别或关联论坛用户的真实身份。如果你有需要帮助的一般性问题，请换个话题试试。"

> "抱歉，该搜索包含不当内容。请遵守社区规范，使用论坛搜索合理的信息。"

### 5.5 DeepSeek API 调用参数

```python
DEEPSEEK_CONFIG = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",          # 默认
    "thinking": {"type": "disabled"},       # 非思考模式降成本
    "max_tokens": 2048,                     # 总结够用
    "temperature": 0.3,                     # 低温度保稳定性
    "timeout": 30,                          # 超时保护
}

# 可选: Pro 模式（用户可在对话中请求"用更深入的分析"）
DEEPSEEK_PRO_CONFIG = {
    "model": "deepseek-v4-pro",
    "thinking": {"type": "enabled"},
    "max_tokens": 4096,
}
```

---

## 6. 安全与滥用防护

### 6.1 多层限流体系

```
第1层: IP 频率限制
  - 每分钟 6 次 AI 请求 / IP
  - 每小时 100 次 AI 请求 / IP
  - 内存状态，重启清零（可接受）

第2层: 邀请码每日配额
  - 默认 30 次/天/码
  - 存储于 SQLite，跨重启持久

第3层: 邀请码总量上限
  - max_quota=0 表示无限制
  - 可给活跃用户单独提额

第4层: 并发对话限制
  - 每个邀请码最多 20 个活跃对话
  - 超过时提示删除旧对话
```

### 6.2 内容安全

| 类别 | 处理方式 | 示例 |
|------|---------|------|
| 搜索他人隐私 | 前端+后端双重过滤，直接拒绝 | "XX学院王同学是谁""查手机号138xxxx" |
| 色情/低俗 | 关键词过滤，拒绝 | 关键词表维护 |
| 违法内容 | 直接拒绝，不搜索也不调 AI | 毒品、暴力、诈骗相关 |
| 过度推测身份 | Prompt 约束 + 后端关键词 | "帮我把这个帖子的发帖人找出来" |
| 间接骚扰 | AI Prompt 禁止推测 + 后端模式检测 | "最近和李XX教授有关的帖子" → 如果是搜教授个人信息则拒绝 |

### 6.3 对话数据隐私

- 对话存储于 SQLite（同现有 DB），不额外传输
- 对话历史仅发送给 DeepSeek API（数据传输加密）
- 对话数据不包含 `show_user_id` / `real_user_id`
- 30 天不活跃对话自动清理（cron 任务，但内测阶段可手动）
- 管理员可查看对话但看不到用户的 IP 或设备信息

### 6.4 防止滥用注册的思路

**邀请码 天然解决"滥注册"问题**：

1. 没有注册入口 → 不存在"注册 100 个账号"
2. 邀请码由管理员线下分发 → 每个码分给一个真实用户
3. 即使码被分享 → 分享者自己的每日额度被摊薄 → 社会抑制
4. 如果某个码被公开（如发到群里）→ 管理员可在 admin 面板一键禁用

**内测分发路径**：
- 项目 GitHub README 放少量邀请码（先到先得）
- RUC 相关微信群/朋友圈分发
- 从 admin 面板中查看活跃用户（按 show_user_id 聚合），给高频优质用户私发

---

## 7. 成本估算

### 7.1 单次对话成本（V4 Flash，非思考模式）

| 场景 | 输入 tokens | 输出 tokens | 单次成本 |
|------|-----------|-----------|---------|
| 首次搜索 (20帖+评论) | ~3,500 | ~800 | **$0.00071** |
| 追问 (对话历史+结果) | ~5,000 | ~600 | **$0.00087** |
| 简短追问 (无新搜索) | ~2,500 | ~400 | **$0.00046** |

### 7.2 月度成本（V4 Flash）

| 日活用户 | 人均日查询 | 月总量 | 月成本 (Flash) |
|---------|-----------|--------|---------------|
| 10 | 10 | 3,000 | ~$2.50 |
| 30 | 15 | 13,500 | ~$11.00 |
| 50 | 20 | 30,000 | ~$24.00 |
| 100 | 25 | 75,000 | ~$60.00 |

### 7.3 对比 V4 Pro

Pro 成本约 Flash 的 **3.1 倍**。建议默认 Flash，少数深度分析场景开放 Pro 开关。

### 7.4 Context Caching

DeepSeek 支持 KV Cache（cache hit 时输入成本降 50 倍）。对话中 SYSTEM_PROMPT 部分可以被缓存：
- 首次请求: 全部按 cache miss 计
- 追问: system prompt 命中缓存，只有增量输入计费
- **追问的实际输入成本再降 30-50%**

---

## 8. 实施步骤

### 阶段 A: 基础架构（1-2 天）

1. **`storage/sqlite_store.py`** — `init_schema()` 新增 4 张表
2. **`scripts/manage_invites.py`** — 邀请码管理 CLI
3. **`server.py`** — 新增 `ai_sessions` 内存管理 + IP 限流装饰器
4. **`server.py`** — 新增 `/api/ai/invite` 和 `/api/ai/status`

### 阶段 B: AI 集成（2-3 天）

5. **`server.py`** — 实现 `check_content_safety()` 安全过滤
6. **`server.py`** — 实现 `/api/ai/search` (首次搜索)
7. **`server.py`** — 实现 `/api/ai/chat` (追问)
8. **`server.py`** — 实现 `/api/ai/conversations` (对话列表)
9. **`server.py`** — 实现 `/api/ai/delete-conv` (删除对话)

### 阶段 C: 前端（2-3 天）

10. **`templates/main.html`** — AI 面板 HTML + CSS
11. **`templates/main.html`** — 对话交互 JS（发送/接收/渲染）
12. **`templates/main.html`** — 邀请码输入模态框
13. **`templates/main.html`** — 对话列表侧栏 + 新对话 / 删除
14. **`templates/main.html`** — 配额显示 + 响应式适配

### 阶段 D: 管理界面（1 天）

15. **`templates/admin_dashboard.html`** — 邀请码管理 section
16. **`server.py`** — admin API: 生成 / 禁用 / 修改邀请码

### 阶段 E: 打磨上线（1 天）

17. 环境变量 `DEEPSEEK_API_KEY` + `AI_ENABLED` 开关
18. `start.sh` 中不需要额外启动（AI 功能同 server.py 进程）
19. Railway 环境变量配置
20. `README.md` 更新使用说明

---

## 9. 文件变更清单

| 文件 | 变更类型 | 变更内容 |
|------|---------|---------|
| `storage/sqlite_store.py` | **修改** | `init_schema()` 新增 4 张表 |
| `server.py` | **修改** | +~300 行: AI 路由、安全过滤、邀请码逻辑、DeepSeek 调用、内存 session、IP 限流 |
| `templates/main.html` | **大量修改** | +~500 行: AI 对话面板 HTML/CSS/JS |
| `templates/admin_dashboard.html` | **修改** | +~150 行: 邀请码管理 section |
| `scripts/manage_invites.py` | **新增** | ~120 行: CLI 邀请码管理 |
| `docs/AI搜索方案.md` | **新增** | 本文件 |
| `.env.example` (可选) | **新增** | 环境变量参考 |
| `README.md` | **修改** | AI 搜索功能说明 |

**不涉及变更的文件**（保证现有功能不被破坏）：
- `crawler_db.py` — 不变
- `scripts/railway_scheduler.py` — 不变
- `data/` 下所有现有文件 — 不变

---

## 备注

- **DeepSeek API Key** 从 [platform.deepseek.com](https://platform.deepseek.com) 获取，个人即可注册
- **无需中国手机号** (DeepSeek 支持国际注册)
- **Railway 上无需额外 Service**，AI 逻辑在同一个 server.py 进程内
- **不依赖任何第三方认证服务** (OAuth/手机/邮箱验证一概不需要)
