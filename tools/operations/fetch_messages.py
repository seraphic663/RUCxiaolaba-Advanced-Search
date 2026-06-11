#!/usr/bin/env python3
"""抓取个人私聊记录。

用法:
  # 从文件读取 cookie（推荐）
  python scripts/fetch_messages.py --cookie-file data/config.txt

  # 直接传 cookie
  python scripts/fetch_messages.py --cookie "ys7_ysxy_session=xxxx"

  # 指定输出文件（默认 data/messages.json）
  python scripts/fetch_messages.py --cookie-file data/config.txt --output my_messages.json

  # 只输出文本摘要（不用 JSON）
  python scripts/fetch_messages.py --cookie-file data/config.txt --output messages.txt --format text

  # 先探测 API 格式再决定（dry-run，不保存）
  python scripts/fetch_messages.py --cookie-file data/config.txt --dry-run

获取 Cookie:
  1. 在电脑上安装微信 + mitmproxy
  2. 打开微信小程序 "RUC小喇叭"
  3. 浏览页面时抓取请求，复制 cookie 中的 ys7_ysxy_session 值
  4. 将 cookie 写入 data/config.txt（格式: ys7_ysxy_session=你的值）

输出文件:
  - JSON 格式: 完整 API 原始响应 + 结构化摘要
  - Text 格式: 人类可读的对话记录
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

# ============================================================
# 常量
# ============================================================

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

# 所有已知的消息相关端点
MESSAGE_ENDPOINTS = {
    "message_count": "/message/message/count",
    "message_list": "/message/message/lists",
    "chat_count": "/friend/chat/count",
}

# 列表端点默认分页参数
DEFAULT_PAGE_SIZE = 20

# 请求间隔（秒），避免触发频率限制
MIN_DELAY = 0.3
MAX_DELAY = 0.8

# ============================================================
# Cookie 加载
# ============================================================

def load_cookie(config_path: str | Path) -> str:
    """从配置文件或直接传入的 cookie 字符串中提取 ys7_ysxy_session 值。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"cookie 文件不存在: {path}")

    text = path.read_text(encoding="utf-8").strip()

    # 情况 1: 文件内容就是 ys7_ysxy_session=xxx 格式
    if text.startswith("ys7_ysxy_session="):
        return text.split("=", 1)[1]

    # 情况 2: 文件内容就是纯 cookie 值（不含 key）
    if "=" not in text and len(text) > 10:
        return text

    # 情况 3: 多行文件，搜索包含 ys7_ysxy_session= 的行
    for line in text.splitlines():
        if "ys7_ysxy_session=" in line:
            return line.strip().split("=", 1)[1]

    raise RuntimeError(
        "未找到有效的 cookie。请确保文件包含 ys7_ysxy_session=你的cookie"
    )


# ============================================================
# API 调用
# ============================================================

def make_session(cookie: str) -> requests.Session:
    """创建带 cookie 的 requests Session。"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("ys7_ysxy_session", cookie, domain="ys.qimiaoyuanfen.com")
    s.verify = False
    return s


def api_get(session: requests.Session, path: str, params: dict | None = None) -> dict | None:
    """调用 API 并返回 data 字段。失败时打印错误并返回 None。"""
    try:
        resp = session.get(f"{BASE}{path}", params=params, timeout=15)
        payload = resp.json()
    except Exception as exc:
        print(f"  [错误] 请求失败: {exc}", file=sys.stderr)
        return None

    code = payload.get("code")
    if code == "0000":
        return payload.get("data", {})
    if code == "1000":
        print("  [错误] cookie 已过期 (code=1000)，请重新获取", file=sys.stderr)
        return None
    print(f"  [错误] API 返回 code={code} message={payload.get('message', '')}", file=sys.stderr)
    return None


# ============================================================
# 消息抓取
# ============================================================

def fetch_user_info(session: requests.Session) -> dict | None:
    """获取当前用户信息（用于展示抓取的是谁的私信）。"""
    data = api_get(session, "/ysxy/user/my")
    if not data:
        return None
    return {
        "uid": data.get("uid", "?"),
        "name": data.get("name", "?"),
        "phone": data.get("phone", "")[:3] + "****",  # 脱敏
        "gender": {1: "男", 2: "女"}.get(data.get("gender"), "?"),
    }


def fetch_message_count(session: requests.Session) -> dict | None:
    """获取未读消息数统计。"""
    return api_get(session, MESSAGE_ENDPOINTS["message_count"])


def fetch_chat_count(session: requests.Session) -> dict | None:
    """获取聊天未读数。"""
    return api_get(session, MESSAGE_ENDPOINTS["chat_count"])


def fetch_message_list(
    session: requests.Session, page: int = 1, limit: int = DEFAULT_PAGE_SIZE
) -> dict | None:
    """获取一页私信列表。"""
    return api_get(
        session,
        MESSAGE_ENDPOINTS["message_list"],
        params={"community_id": CID, "page": page, "limit": limit},
    )


def fetch_all_messages(session: requests.Session, max_pages: int = 500) -> list[dict]:
    """翻页抓取全部私信记录。

    自动检测最后一页（返回空列表或数量不足）。
    """
    all_items: list[dict] = []
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        time.sleep(0.3 + (page % 10) * 0.05)  # 渐进延迟

        data = fetch_message_list(session, page=page)
        if data is None:
            print(f"  第 {page} 页请求失败，跳过", file=sys.stderr)
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        # 尝试多种可能的列表字段名
        items = (
            data.get("list")
            or data.get("lists")
            or data.get("data")
            or data.get("messages")
            or []
        )
        if not isinstance(items, list):
            items = []

        if not items:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print(f"  连续 {consecutive_empty} 页为空，停止翻页")
                break
            continue

        consecutive_empty = 0
        all_items.extend(items)
        print(f"  第 {page} 页: +{len(items)} 条 (累计 {len(all_items)})", flush=True)

        # 如果返回数量少于预期，说明是最后一页
        if len(items) < DEFAULT_PAGE_SIZE:
            break

    return all_items


# ============================================================
# 数据格式化
# ============================================================

def _truncate(text: str, max_len: int = 120) -> str:
    """截断长文本。"""
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def format_message_summary(item: dict, index: int) -> str:
    """将单条私信记录格式化为人类可读文本。

    由于 API 返回格式未知，这里用通用逻辑尝试解析。
    """
    lines = [f"--- 消息 #{index + 1} ---"]

    # 尝试提取关键字段
    msg_id = item.get("id") or item.get("message_id") or "?"
    lines.append(f"ID: {msg_id}")

    # 发送方
    from_name = item.get("from_user_name") or item.get("show_user_name") or "?"
    from_uid = item.get("from_user_id") or item.get("from_show_user_id") or "?"
    lines.append(f"发送方: {from_name} (uid={from_uid})")

    # 接收方
    to_name = item.get("to_user_name") or item.get("reply_show_user_name") or "?"
    to_uid = item.get("to_user_id") or item.get("reply_show_user_id") or "?"
    lines.append(f"接收方: {to_name} (uid={to_uid})")

    # 内容
    detail = item.get("detail") or item.get("content") or item.get("message") or ""
    lines.append(f"内容: {detail}")

    # 时间
    create_time = item.get("create_time") or item.get("show_create_time") or item.get("time") or "?"
    lines.append(f"时间: {create_time}")

    # 其他可能有用的字段（展示但不展开）
    extra_fields = {k: v for k, v in item.items()
                    if k not in ("id", "message_id", "from_user_name", "show_user_name",
                                 "from_user_id", "from_show_user_id", "to_user_name",
                                 "reply_show_user_name", "to_user_id", "reply_show_user_id",
                                 "detail", "content", "message", "create_time",
                                 "show_create_time", "time", "reply_comment_list")}
    if extra_fields:
        lines.append(f"其他字段: {json.dumps(extra_fields, ensure_ascii=False)}")

    return "\n".join(lines)


def format_message_list_text(raw_items: list[dict]) -> str:
    """将原始消息列表格式化为文本。"""
    header = f"# 私信记录\n# 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n# 共 {len(raw_items)} 条\n\n"
    bodies = [format_message_summary(item, i) for i, item in enumerate(raw_items)]
    return header + "\n\n".join(bodies)


def build_output(raw_items: list[dict], user_info: dict | None,
                 counts: dict | None, api_format_hint: str = "") -> dict:
    """构建最终的输出数据结构（JSON 格式）。"""
    return {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": user_info,
        "counts": counts,
        "total_messages": len(raw_items),
        "api_response_format": api_format_hint,
        "raw_messages": raw_items,
    }


# ============================================================
# 格式探测（dry-run）
# ============================================================

def probe_api_format(session: requests.Session) -> str:
    """探测 API 返回的消息列表格式，给出字段说明。

    打印第一页原始 JSON 的前两条记录，帮助用户理解数据结构。
    """
    print("\n" + "=" * 60)
    print("🔍 探测 /message/message/lists 返回格式")
    print("=" * 60)

    data = fetch_message_list(session, page=1)
    if data is None:
        return "API 请求失败"

    # 找到列表
    items = (
        data.get("list")
        or data.get("lists")
        or data.get("data")
        or data.get("messages")
        or []
    )
    if not isinstance(items, list):
        print("⚠️ 未找到消息列表字段。原始响应顶层键:", list(data.keys()))
        print("\n原始响应 (前 2000 字符):")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return "格式未知（见上方原始输出）"

    print(f"共找到 {len(items)} 条消息（第 1 页）")
    print(f"\n消息顶层字段 ({len(items[0]) if items else 0} 个):")
    if items:
        for k, v in items[0].items():
            sample = _truncate(str(v), 60)
            print(f"  {k}: {sample}")

    print("\n第 1 条消息 (完整 JSON):")
    if items:
        print(json.dumps(items[0], ensure_ascii=False, indent=2)[:1500])

    if len(items) > 1:
        print("\n第 2 条消息 (完整 JSON):")
        print(json.dumps(items[1], ensure_ascii=False, indent=2)[:1500])

    # 分析格式
    if items:
        first = items[0]
        has_detail = "detail" in first
        has_content = "content" in first
        has_from = any(k in first for k in ("from_user_name", "from_user_id"))
        has_to = any(k in first for k in ("to_user_name", "to_user_id", "reply_show_user_name"))

        hints = []
        if has_detail or has_content:
            hints.append("含消息正文")
        if has_from and has_to:
            hints.append("含收发双方标识")
        elif not has_from and not has_to:
            hints.append("可能为单用户视角（需检查 is_mine 字段）")

        return "探测结果: " + (", ".join(hints) if hints else "字段类型待确认")

    return "无消息数据"


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="抓取 RUC 小喇叭个人私聊记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/fetch_messages.py --cookie-file data/config.txt
  python scripts/fetch_messages.py --cookie-file data/config.txt --dry-run
  python scripts/fetch_messages.py --cookie "ys7_ysxy_session=xxx" --format text
        """,
    )
    # Cookie 来源（二选一）
    cookie_group = parser.add_mutually_exclusive_group(required=True)
    cookie_group.add_argument("--cookie-file", help="包含 cookie 的文件路径")
    cookie_group.add_argument("--cookie", help="直接传入 cookie 字符串")
    # 输出
    parser.add_argument("--output", default="", help="输出文件路径 (默认: data/messages.json 或 data/messages.txt)")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="输出格式 (默认 json)")
    parser.add_argument("--dry-run", action="store_true", help="仅探测格式，不保存")
    parser.add_argument("--max-pages", type=int, default=500, help="最大翻页数 (默认 500)")
    parser.add_argument("--no-user-info", action="store_true", help="跳过获取用户信息")

    args = parser.parse_args()

    # 解析 cookie
    if args.cookie_file:
        cookie = load_cookie(args.cookie_file)
    else:
        cookie = args.cookie
        if cookie.startswith("ys7_ysxy_session="):
            cookie = cookie.split("=", 1)[1]

    print(f"Cookie: {cookie[:12]}...{cookie[-8:] if len(cookie) > 20 else ''}")
    session = make_session(cookie)

    # 获取用户信息
    user_info = None
    if not args.no_user_info:
        print("\n获取用户信息...")
        user_info = fetch_user_info(session)
        if user_info:
            print(f"  用户: {user_info['name']} (uid={user_info['uid']}, {user_info['gender']})")
            print(f"  手机: {user_info['phone']}")
        else:
            print("  ⚠️ 无法获取用户信息（cookie 可能已过期）")

    # 获取未读计数
    print("\n获取消息统计...")
    msg_count = fetch_message_count(session)
    chat_count = fetch_chat_count(session)
    counts = {
        "message": msg_count,
        "chat": chat_count,
    }
    if msg_count:
        print(f"  私信: {json.dumps(msg_count, ensure_ascii=False)}")
    if chat_count:
        print(f"  聊天: {json.dumps(chat_count, ensure_ascii=False)}")

    # Dry-run 模式：探测格式后退出
    if args.dry_run:
        probe_api_format(session)
        print("\n✅ 探测完成。如需正式抓取，去掉 --dry-run 参数。")
        return

    # 正式抓取
    print(f"\n开始抓取私信列表（最多 {args.max_pages} 页）...")
    raw_items = fetch_all_messages(session, max_pages=args.max_pages)
    print(f"\n共抓取 {len(raw_items)} 条私信记录")

    if not raw_items:
        print("⚠️ 未抓取到任何消息。建议先用 --dry-run 探测 API 格式。")
        return

    # 确定输出路径
    if args.output:
        output_path = args.output
    else:
        ext = "txt" if args.format == "text" else "json"
        output_path = str(Path(__file__).resolve().parents[2] / "data" / f"messages.{ext}")

    # 写入输出
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if args.format == "text":
        text_output = format_message_list_text(raw_items)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text_output)
    else:
        api_hint = f"{len(raw_items[0]) if raw_items else 0} fields: " + ", ".join(raw_items[0].keys())[:200] if raw_items else ""
        output_data = build_output(raw_items, user_info, counts, api_hint)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 已保存到: {output_path}")
    print(f"   格式: {args.format}")
    print(f"   条数: {len(raw_items)}")


if __name__ == "__main__":
    main()
