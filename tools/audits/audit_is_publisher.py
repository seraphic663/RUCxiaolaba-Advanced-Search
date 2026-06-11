#!/usr/bin/env python3
"""对比不同 cookie 下 API 返回的 is_publisher 值是否一致。

用法:
  # 两组 cookie 对比
  python -m tools.audits.audit_is_publisher <post_id> <cookie_a> <cookie_b>

  # 三组对比（第三组用 none 表示无 cookie）
  python -m tools.audits.audit_is_publisher <post_id> <cookie_a> <cookie_b> none

  # 从文件读取 cookie
  python -m tools.audits.audit_is_publisher <post_id> "$(cat cookie_a.txt)" "$(cat cookie_b.txt)"

输出:
  - 每组 cookie 下每条评论的 (comment_id, show_user_name, is_publisher)
  - 不一致的评论高亮标记
"""
import sys

import requests
import urllib3

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


def fetch(post_id: str, cookie: str | None, label: str) -> dict | None:
    """请求帖子详情 API 并返回 data 部分。"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    if cookie:
        s.cookies.set("ys7_ysxy_session", cookie, domain="ys.qimiaoyuanfen.com")

    try:
        resp = s.get(
            f"{BASE}/article/article/info",
            params={"community_id": CID, "id": post_id},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        print(f"[{label}] 请求失败: {exc}")
        return None

    code = data.get("code")
    if code != "0000":
        print(f"[{label}] API 错误: code={code} msg={data.get('message', '')}")
        return None

    post = data.get("data", {})
    print(
        f"\n[{label}] 帖子作者: {post.get('show_user_name')} "
        f"(show_uid={post.get('show_user_id')}, real_uid={post.get('real_user_id')})"
    )
    print(f"[{label}] 标题: {(post.get('title') or '')[:60]}")
    print(f"[{label}] 内容: {(post.get('detail') or '')[:120]}")
    return post


def flatten(comments: list, depth: int = 0) -> list[dict]:
    """递归展平评论树。"""
    result = []
    for c in (comments or []):
        if not isinstance(c, dict):
            continue
        result.append({
            "comment_id": str(c.get("id") or "?"),
            "show_user_name": c.get("show_user_name", "?"),
            "show_user_id": str(c.get("show_user_id") or "?"),
            "is_publisher": int(c.get("is_publisher") or 2),
            "detail": str(c.get("detail") or "")[:100],
            "depth": depth,
        })
        replies = c.get("reply_comment_list") or []
        if isinstance(replies, list):
            result.extend(flatten(replies, depth + 1))
    return result


def print_comments(comments: list[dict]):
    """打印评论列表，OP 用 ★ 标记。"""
    for c in comments:
        indent = "  " * min(c["depth"], 4)
        op_tag = " ★ [楼主]" if c["is_publisher"] == 1 else ""
        print(f"  {indent}#{c['comment_id']} {c['show_user_name']}{op_tag}")
        detail = c["detail"].replace("\n", " ")
        print(f"  {indent}  {detail}")


def compare(results: dict[str, list[dict]]):
    """对比多组结果的 is_publisher 一致性。"""
    labels = list(results.keys())
    if len(labels) < 2:
        return

    print(f"\n{'=' * 60}")
    print("🔍 is_publisher 对比:")

    all_comment_ids = set()
    for comments in results.values():
        all_comment_ids.update(c["comment_id"] for c in comments)

    diffs = []
    for cid in sorted(all_comment_ids):
        pubs = {}
        for label, comments in results.items():
            match = next((c for c in comments if c["comment_id"] == cid), None)
            pubs[label] = match["is_publisher"] if match else "缺失"
        if len(set(pubs.values())) > 1:
            diffs.append((cid, pubs))

    if diffs:
        print(f"  ❌ 发现 {len(diffs)} 条评论 is_publisher 不一致:\n")
        for cid, pubs in diffs:
            print(f"  #{cid}:")
            for label, val in pubs.items():
                print(f"    {label}: is_publisher={val}")
            # 显示评论内容
            for label, comments in results.items():
                match = next((c for c in comments if c["comment_id"] == cid), None)
                if match:
                    print(f"    [{label}] {match['show_user_name']}: {match['detail'][:80]}")
                    break
            print()
    else:
        print(f"  ✅ 所有 {len(all_comment_ids)} 条评论的 is_publisher 完全一致")

    # 统计每组 OP 评论数
    print("\n📊 各组 OP 标记统计:")
    for label, comments in results.items():
        op_count = sum(1 for c in comments if c["is_publisher"] == 1)
        print(f"  {label}: {op_count}/{len(comments)} 条被标为楼主")


def main():
    if len(sys.argv) < 4:
        print("用法: python audit_is_publisher.py <post_id> <cookie_a> <cookie_b> [cookie_c|none]")
        print()
        print("示例:")
        print("  python audit_is_publisher.py 3858923 'abc123' 'def456'")
        print("  python audit_is_publisher.py 3858923 'abc123' 'def456' none")
        print()
        print("cookie 为 'none' 时表示不带 cookie 请求")
        sys.exit(1)

    post_id = sys.argv[1]
    cookie_args = sys.argv[2:]

    cookies: list[tuple[str | None, str]] = []
    for i, arg in enumerate(cookie_args):
        label = chr(65 + i)  # A, B, C, ...
        cookies.append((None if arg.lower() == "none" else arg, f"Cookie-{label}"))

    print(f"帖子 ID: {post_id}")
    for cookie, label in cookies:
        cookie_display = "[无]" if cookie is None else f"{cookie[:20]}..."
        print(f"  {label}: {cookie_display}")

    results: dict[str, list[dict]] = {}
    for cookie, label in cookies:
        print(f"\n{'=' * 60}")
        post = fetch(post_id, cookie, label)
        if post is None:
            continue
        comments = flatten(post.get("comment_list", []))
        results[label] = comments
        print(f"\n[{label}] 共 {len(comments)} 条评论:\n")
        print_comments(comments)

    if len(results) >= 2:
        compare(results)
    else:
        print("\n⚠️ 有效结果不足 2 组，无法对比")


if __name__ == "__main__":
    main()
