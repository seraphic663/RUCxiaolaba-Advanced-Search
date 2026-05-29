"""Web search for RUC-Xiaolaba — reads posts CSV directly."""
import json
import csv
import os
import hashlib
import secrets
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_DANGER = os.path.join(DATA_DIR, "posts_danger.csv")
CSV_LEGACY = os.path.join(DATA_DIR, "posts_full.csv")

# --- admin password ---
PASSWORD_FILE = os.path.join(DATA_DIR, "admin_password.txt")
DEFAULT_PASSWORD = "xlbadmin"
_admin_sessions = set()  # in-memory session tokens


def get_password():
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return DEFAULT_PASSWORD


def load_posts():
    """Load posts, preferring danger CSV if available (has show_user_id), fallback to legacy."""
    csv_path = CSV_DANGER if os.path.exists(CSV_DANGER) else CSV_LEGACY
    if not os.path.exists(csv_path):
        return [], None, None, None

    csv.field_size_limit(10 ** 7)
    seen, posts, crawl_time = set(), [], None
    has_danger = (csv_path == CSV_DANGER)

    with open(csv_path, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            aid = r.get("id", "")
            if aid and aid not in seen:
                seen.add(aid)
                cmts_raw = r.get("comments_json", "[]")
                try:
                    comment_list = json.loads(cmts_raw)
                except Exception:
                    comment_list = []
                post = {
                    "id": aid,
                    "content": r.get("content", ""),
                    "category": r.get("category_name", ""),
                    "user": r.get("user_name", ""),
                    "time": r.get("create_time", ""),
                    "comments": int(r.get("comment_count", 0)),
                    "stars": int(r.get("star_count", 0)),
                    "trace": int(r.get("trace_count", 0)),
                    "views": int(r.get("views", 0)),
                    "hot": int(r.get("hot", 0)),
                    "comment_list": comment_list,
                }
                if has_danger:
                    post["show_user_id"] = r.get("show_user_id", "")
                    post["show_user_head"] = r.get("show_user_head", "")
                    post["real_user_id"] = r.get("real_user_id", "0")
                posts.append(post)

    posts.sort(key=lambda x: int(x["id"]), reverse=True)
    crawl_time = datetime.fromtimestamp(os.path.getmtime(csv_path)).strftime("%Y-%m-%d %H:%M")
    return posts, crawl_time, has_danger, csv_path


# ==================== HTML TEMPLATES ====================

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RUC小喇叭 搜索</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f5f5; }}
.header {{ background: #8b0012; color: #fff; padding: 20px; text-align: center; position: relative; }}
.header h1 {{ font-size: 1.5em; }}
.header p {{ opacity: 0.8; margin-top: 4px; font-size: 0.9em; }}
.header .admin-link {{ position: absolute; right: 16px; top: 50%; transform: translateY(-50%); color: rgba(255,255,255,0.5); text-decoration: none; font-size: 0.85em; }}
.header .admin-link:hover {{ color: #fff; }}
.search-box {{ max-width: 760px; margin: 20px auto; padding: 0 16px; }}
.search-box input {{ width: 100%; padding: 14px 18px; font-size: 16px; border: 2px solid #ddd; border-radius: 12px; outline: none; }}
.search-box input:focus {{ border-color: #8b0012; }}
.results {{ max-width: 760px; margin: 0 auto; padding: 0 16px 40px; }}
.post {{ background: #fff; border-radius: 12px; padding: 18px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.post .meta {{ color: #888; font-size: 0.85em; margin-bottom: 4px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px; }}
.post .meta .left {{ display: flex; align-items: center; gap: 8px; }}
.post .meta .stats {{ color: #aaa; white-space: nowrap; }}
.post .time {{ color: #bbb; font-size: 0.8em; }}
.post .content {{ line-height: 1.7; white-space: pre-wrap; word-break: break-word; margin-top: 8px; }}
.post .toggle-cmts {{ margin-top: 10px; font-size: 0.85em; color: #8b0012; cursor: pointer; user-select: none; }}
.post .toggle-cmts:hover {{ text-decoration: underline; }}
.post .cmts {{ margin-top: 10px; border-left: 3px solid #eee; padding-left: 14px; }}
.cmt {{ padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 0.9em; }}
.cmt:last-child {{ border-bottom: none; }}
.cmt .cmt-meta {{ color: #999; font-size: 0.8em; margin-bottom: 2px; }}
.cmt .cmt-text {{ line-height: 1.5; white-space: pre-wrap; word-break: break-word; }}
.cmt .replies {{ margin-left: 16px; border-left: 2px solid #e8e8e8; padding-left: 12px; margin-top: 4px; }}
.cmt .reply {{ padding: 6px 0; font-size: 0.88em; }}
.cmt .reply .cmt-meta {{ font-size: 0.78em; }}
mark {{ background: #fff3b0; padding: 1px 3px; border-radius: 2px; }}
.info {{ color: #888; text-align: center; margin-top: 30px; font-size: 0.9em; }}
.empty {{ text-align: center; color: #999; padding: 40px; }}
.sort-bar {{ max-width: 760px; margin: 0 auto 10px; padding: 0 16px; display: flex; gap: 8px; font-size: 0.85em; flex-wrap: wrap; }}
.sort-bar button {{ padding: 6px 14px; border: 1px solid #ddd; border-radius: 16px; background: #fff; cursor: pointer; }}
.sort-bar button.active {{ background: #8b0012; color: #fff; border-color: #8b0012; }}
</style>
</head>
<body>
<div class="header">
  <h1>RUC小喇叭 搜索</h1>
  <p>中国人民大学匿名论坛 · {total} 条帖子 · 爬取于 {crawl_time} · 最新帖 {latest_time}</p>
  <a class="admin-link" href="/admin" title="管理面板">&#128274;</a>
</div>
<div class="search-box">
  <input type="text" id="q" placeholder="搜索关键词（空格分隔多个词），空着即显示最新..." autofocus>
</div>
<div class="sort-bar">
  <button id="btn-time" class="active" onclick="sortBy('time')">按时间</button>
  <button id="btn-stars" onclick="sortBy('stars')">按点赞</button>
  <button id="btn-views" onclick="sortBy('views')">按浏览</button>
  <button id="btn-hot" onclick="sortBy('hot')">按热度</button>
  <span style="margin-left:auto;color:#888;" id="count"></span>
</div>
<div class="results" id="results">
  <div class="empty">输入关键词搜索，或清空搜索框看最新帖子</div>
</div>
<script>
let all = {all_json};
let currentSort = 'time';

function sortBy(field) {{
  currentSort = field;
  document.querySelectorAll('.sort-bar button').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + field).classList.add('active');
  doSearch();
}}

function formatTime(t) {{
  if (!t) return '';
  return t.replace('2026-', '').replace('-05-','/5/').replace(' ',' ');
}}

function renderComments(cmts, kw) {{
  if (!cmts || cmts.length === 0) return '';
  let num = 0;
  return cmts.map(c => {{
    num++;
    let txt = (c.detail || '');
    kw.forEach(k => {{
      let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
      txt = txt.replace(re, '<mark>$1</mark>');
    }});
    let replies = (c.reply_comment_list || []);
    let replyHTML = '';
    if (replies.length > 0) {{
      replyHTML = '<div class="replies">' + replies.map(rp => {{
        let rt = (rp.detail || '');
        kw.forEach(k => {{
          let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
          rt = rt.replace(re, '<mark>$1</mark>');
        }});
        return '<div class="reply"><div class="cmt-meta"><span style="color:#bbb">' + formatTime(rp.create_time) + '</span> ' + (rp.show_user_name || '') + '</div><div class="cmt-text">' + rt + '</div></div>';
      }}).join('') + '</div>';
    }}
    return '<div class="cmt"><div class="cmt-meta"><b style="color:#8b0012;">#' + num + '</b> ' + (c.show_user_name || '') + (c.is_publisher==1?' <span style="color:#8b0012;">楼主</span>':'') + '</div><div class="cmt-text">' + txt + '</div>' + replyHTML + '</div>';
  }}).join('');
}}

function doSearch() {{
  let kw = document.getElementById('q').value.trim().split(/\\s+/).filter(Boolean);
  let found;
  if (kw.length === 0) {{
    found = all;
  }} else {{
    found = all.filter(a => {{
      let cmtText = (a.comment_list || []).map(c => (c.detail||'') + ' ' + (c.reply_comment_list||[]).map(r => r.detail||'').join(' ')).join(' ');
      let searchText = a.content + ' ' + cmtText + ' #' + a.id;
      return kw.every(k => searchText.toLowerCase().includes(k.toLowerCase()));
    }});
  }}
  if (currentSort === 'stars') found = [...found].sort((a,b) => b.stars - a.stars);
  else if (currentSort === 'views') found = [...found].sort((a,b) => b.views - a.views);
  else if (currentSort === 'hot') found = [...found].sort((a,b) => b.hot - a.hot);

  document.getElementById('count').textContent = found.length + ' 条结果';
  if (found.length === 0) {{
    document.getElementById('results').innerHTML = '<div class="empty">没有找到匹配的帖子</div>';
  }} else {{
    document.getElementById('results').innerHTML = found.slice(0, 200).map((a, idx) => {{
      let text = a.content;
      kw.forEach(k => {{
        let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
        text = text.replace(re, '<mark>$1</mark>');
      }});
      let nComments = (a.comment_list || []).length;
      let cmtLabel = nComments > 0 ? ('<span class="toggle-cmts" onclick="toggleCmts(' + idx + ')">&#9654; 展开 ' + nComments + ' 条评论</span>') : '';
      let cmtsHTML = '<div class="cmts" id="cmts-' + idx + '" style="display:none">' + renderComments(a.comment_list || [], kw) + '</div>';
      return '<div class="post"><div class="meta"><div class="left"><span>[' + a.category + '] ' + a.user + '</span><span class="time">#' + a.id + ' &middot; ' + (a.time || '') + '</span></div><span class="stats">&#10084;' + a.stars + ' &#128172;' + a.comments + ' &#128099;' + a.trace + ' &#128065;' + a.views + '</span></div><div class="content">' + text + '</div>' + cmtLabel + cmtsHTML + '</div>';
    }}).join('');
  }}
}}

window.toggleCmts = function(idx) {{
  let el = document.getElementById('cmts-' + idx);
  if (el.style.display === 'none') {{
    el.style.display = 'block';
    let toggle = el.parentElement.querySelector('.toggle-cmts');
    if (toggle) toggle.innerHTML = toggle.innerHTML.replace('&#9654;', '&#9660;');
  }} else {{
    el.style.display = 'none';
    let toggle = el.parentElement.querySelector('.toggle-cmts');
    if (toggle) toggle.innerHTML = toggle.innerHTML.replace('&#9660;', '&#9654;');
  }}
}};

document.getElementById('q').addEventListener('input', function() {{
  clearTimeout(this._timer);
  this._timer = setTimeout(doSearch, 200);
}});

doSearch();
</script>
</body>
</html>"""

ADMIN_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理面板 - 登录</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #1a1a2e; color: #eee; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.login-box { background: #16213e; padding: 40px; border-radius: 12px; width: 360px; box-shadow: 0 4px 20px rgba(0,0,0,.5); }
.login-box h2 { text-align: center; margin-bottom: 24px; color: #e94560; }
.login-box input { width: 100%; padding: 12px; margin-bottom: 16px; border: 1px solid #333; border-radius: 8px; background: #0f3460; color: #fff; font-size: 14px; outline: none; }
.login-box input:focus { border-color: #e94560; }
.login-box button { width: 100%; padding: 12px; border: none; border-radius: 8px; background: #e94560; color: #fff; font-size: 16px; cursor: pointer; }
.login-box button:hover { background: #d63850; }
.login-box .error { color: #e94560; text-align: center; margin-top: 12px; font-size: 0.9em; }
.login-box .back { text-align: center; margin-top: 16px; }
.login-box .back a { color: #888; text-decoration: none; font-size: 0.85em; }
.login-box .back a:hover { color: #ccc; }
</style>
</head>
<body>
<div class="login-box">
  <h2>&#128274; 管理面板</h2>
  <form method="post" action="/admin">
    <input type="password" name="password" placeholder="输入管理密码" autofocus>
    <button type="submit">登 录</button>
  </form>
  {error_html}
  <div class="back"><a href="/">&#8592; 返回搜索</a></div>
</div>
</body>
</html>"""

ADMIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理面板 - 用户追踪</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }
.header { background: #16213e; padding: 20px; border-radius: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.header h2 { color: #e94560; }
.header .nav a { color: #888; text-decoration: none; margin-left: 16px; font-size: 0.9em; }
.header .nav a:hover { color: #ccc; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: #16213e; padding: 16px; border-radius: 8px; text-align: center; }
.stat-card .num { font-size: 2em; color: #e94560; font-weight: bold; }
.stat-card .label { color: #888; font-size: 0.85em; margin-top: 4px; }
.search-box { margin-bottom: 20px; }
.search-box input { width: 100%; padding: 12px; border: 1px solid #333; border-radius: 8px; background: #0f3460; color: #fff; font-size: 14px; outline: none; }
.search-box input:focus { border-color: #e94560; }
.user-list { display: grid; gap: 8px; }
.user-row { background: #16213e; padding: 14px 18px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; cursor: pointer; transition: background .2s; }
.user-row:hover { background: #1a2a4a; }
.user-row .uid { color: #e94560; font-family: monospace; font-size: 0.95em; }
.user-row .uname { color: #ccc; margin-left: 12px; }
.user-row .count { color: #888; font-size: 0.85em; }
.user-row .cats { color: #666; font-size: 0.8em; margin-left: 8px; }
.user-detail { background: #16213e; border-radius: 8px; margin-top: 4px; padding: 16px; display: none; }
.user-detail.open { display: block; }
.user-detail .post-item { padding: 12px 0; border-bottom: 1px solid #222; }
.user-detail .post-item:last-child { border-bottom: none; }
.user-detail .post-id { color: #888; font-size: 0.8em; }
.user-detail .post-cat { color: #e94560; font-size: 0.85em; }
.user-detail .post-time { color: #666; font-size: 0.8em; }
.user-detail .post-content { margin-top: 6px; line-height: 1.6; color: #ccc; white-space: pre-wrap; word-break: break-word; }
.user-detail .post-meta-row { display: flex; gap: 12px; flex-wrap: wrap; }
.comment-item { padding: 8px 0 8px 16px; border-left: 2px solid #333; margin: 8px 0; font-size: 0.9em; }
.comment-item .cmt-from { color: #e94560; font-size: 0.8em; }
.comment-item .cmt-text { color: #aaa; margin-top: 4px; }
.no-data { text-align: center; color: #555; padding: 40px; }
</style>
</head>
<body>
<div class="header">
  <h2>&#128274; 管理面板</h2>
  <div class="nav">
    <span style="color:#888;font-size:0.85em;">{csv_source} · {total}帖 · {unique_users}用户</span>
    <a href="/">返回搜索</a>
    <a href="/admin?logout=1">退出</a>
  </div>
</div>
<div class="stats">
  <div class="stat-card"><div class="num">{unique_users}</div><div class="label">唯一用户 (show_user_id)</div></div>
  <div class="stat-card"><div class="num">{total}</div><div class="label">总帖子</div></div>
  <div class="stat-card"><div class="num">{multi_post_users}</div><div class="label">发帖≥2的用户</div></div>
  <div class="stat-card"><div class="num">{total_comments}</div><div class="label">总评论</div></div>
  <div class="stat-card"><div class="num">{unique_commenters}</div><div class="label">唯一评论者</div></div>
  <div class="stat-card"><div class="num">{danger_label}</div><div class="label">数据来源</div></div>
</div>
<div class="search-box">
  <input type="text" id="filter" placeholder="筛选: 输入 show_user_id 或用户名..." oninput="filterUsers()">
</div>
<div class="user-list" id="user-list">
{user_rows}
</div>
<script>
function toggleUser(uid) {{
  let el = document.getElementById('detail-' + uid);
  el.classList.toggle('open');
}}

function filterUsers() {{
  let kw = document.getElementById('filter').value.toLowerCase();
  document.querySelectorAll('.user-row').forEach(row => {{
    let text = (row.getAttribute('data-text') || '').toLowerCase();
    row.style.display = (text.includes(kw) || !kw) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


def build_user_rows(posts):
    """Group posts by show_user_id and build user row HTML."""
    from collections import defaultdict
    user_posts = defaultdict(list)
    user_names = {}
    user_heads = {}

    for p in posts:
        uid = p.get("show_user_id", "")
        if not uid:
            continue
        user_posts[uid].append(p)
        user_names[uid] = p.get("user", "?")
        user_heads[uid] = p.get("show_user_head", "")

    rows = []
    for uid in sorted(user_posts.keys(), key=lambda u: -len(user_posts[u])):
        p_list = user_posts[uid]
        name = user_names.get(uid, "?")
        cats = sorted(set(p.get("category", "?") for p in p_list))
        cats_str = ", ".join(cats[:5])
        if len(cats) > 5:
            cats_str += f" +{len(cats)-5}"

        # Build detail HTML
        detail_parts = []
        for p in p_list:
            detail_parts.append(
                f'<div class="post-item">'
                f'<div class="post-meta-row">'
                f'<span class="post-cat">[{p.get("category","?")}]</span>'
                f'<span class="post-id">#{p.get("id","?")}</span>'
                f'<span class="post-time">{p.get("time","?")}</span>'
                f'<span style="color:#666;font-size:0.8em;">&#10084;{p.get("stars",0)} &#128172;{p.get("comments",0)}</span>'
                f'</div>'
                f'<div class="post-content">{p.get("content","")[:300]}</div>'
                f'</div>'
            )

            # Also show comments from OTHER users directed at this user
            for c in p.get("comment_list", []):
                # Commenter info
                c_uid = c.get("show_user_id", "")
                if c_uid and c_uid != uid:
                    c_detail = c.get("detail", "")[:120]
                    detail_parts.append(
                        f'<div class="comment-item">'
                        f'<div class="cmt-from">&#8592; {c.get("show_user_name","?")} (ID:{c_uid})</div>'
                        f'<div class="cmt-text">{c_detail}</div>'
                        f'</div>'
                    )
                # Nested replies
                for nr in c.get("reply_comment_list", []):
                    nr_uid = nr.get("show_user_id", "")
                    if nr_uid and nr_uid != uid:
                        nr_detail = nr.get("detail", "")[:120]
                        reply_to_uid = nr.get("reply_show_user_id", "")
                        arrow = "&#8594; " + nr.get("reply_show_user_name", "?") if reply_to_uid == uid else "&#8592; "
                        detail_parts.append(
                            f'<div class="comment-item">'
                            f'<div class="cmt-from">{arrow} {nr.get("show_user_name","?")} (ID:{nr_uid})</div>'
                            f'<div class="cmt-text">{nr_detail}</div>'
                            f'</div>'
                        )

        detail_html = "".join(detail_parts)
        data_text = f"{uid} {name} {cats_str}"

        img_tag = ""
        head_url = user_heads.get(uid, "")
        if head_url:
            img_tag = f'<img src="{head_url}" style="width:24px;height:24px;border-radius:50%;margin-right:8px;vertical-align:middle;" onerror="this.style.display=\'none\'">'

        rows.append(
            f'<div>'
            f'<div class="user-row" onclick="toggleUser(\'{uid}\')" data-text="{data_text}">'
            f'<div>{img_tag}<span class="uid">ID:{uid}</span><span class="uname">{name}</span><span class="cats">{cats_str}</span></div>'
            f'<span class="count">{len(p_list)}帖</span>'
            f'</div>'
            f'<div class="user-detail" id="detail-{uid}">{detail_html}</div>'
            f'</div>'
        )

    return "\n".join(rows)


def count_stats(posts):
    """Count unique users, commenters, etc."""
    uids = set()
    commenter_ids = set()
    total_comments = 0
    multi_post = 0

    uid_counts = {}
    for p in posts:
        uid = p.get("show_user_id", "")
        if uid:
            uids.add(uid)
            uid_counts[uid] = uid_counts.get(uid, 0) + 1
        for c in p.get("comment_list", []):
            total_comments += 1
            c_uid = c.get("show_user_id", "")
            if c_uid:
                commenter_ids.add(c_uid)
            for nr in c.get("reply_comment_list", []):
                total_comments += 1
                nr_uid = nr.get("show_user_id", "")
                if nr_uid:
                    commenter_ids.add(nr_uid)

    multi_post = sum(1 for c in uid_counts.values() if c >= 2)

    return len(uids), multi_post, total_comments, len(commenter_ids)


# ==================== HANDLER ====================

class Handler(BaseHTTPRequestHandler):
    def _set_cookie(self, name, value):
        self.send_header("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; Max-Age=86400")

    def _get_cookie(self, name):
        cookie_hdr = self.headers.get("Cookie", "")
        for item in cookie_hdr.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                if k == name:
                    return v
        return None

    def _is_admin(self):
        token = self._get_cookie("admin_token")
        return token in _admin_sessions

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_html(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    # ---- MAIN PAGE ----
    def _handle_main(self):
        posts, crawl_time, has_danger, csv_path = load_posts()
        latest_time = max((p["time"] for p in posts), default="?") if posts else "?"

        # Strip admin-only fields before sending to client (for legacy CSV, they don't exist anyway)
        safe_posts = []
        for p in posts:
            sp = {k: v for k, v in p.items() if k not in ("show_user_id", "show_user_head", "real_user_id")}
            safe_posts.append(sp)

        html = MAIN_HTML.format(
            total=len(safe_posts),
            crawl_time=crawl_time or "?",
            latest_time=latest_time,
            all_json=json.dumps(safe_posts, ensure_ascii=False),
        )
        self._serve_html(html)

    # ---- ADMIN ----
    def _handle_admin_get(self):
        # Logout
        if "logout" in (self.path or ""):
            self._set_cookie("admin_token", "deleted")
            self._redirect("/admin")
            return

        # Check login
        if not self._is_admin():
            self._serve_html(ADMIN_LOGIN_HTML.format(error_html=""))
            return

        # Admin dashboard
        self._serve_admin_dashboard()

    def _handle_admin_post(self):
        # Read password from form data
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        params = parse_qs(body)
        password = params.get("password", [""])[0]

        if password == get_password():
            token = secrets.token_hex(32)
            _admin_sessions.add(token)
            self._set_cookie("admin_token", token)
            self._redirect("/admin")
        else:
            html = ADMIN_LOGIN_HTML.format(
                error_html='<div class="error">密码错误</div>'
            )
            self._serve_html(html)

    def _serve_admin_dashboard(self):
        posts, crawl_time, has_danger, csv_path = load_posts()
        unique_users, multi_post_users, total_comments, unique_commenters = count_stats(posts)

        user_rows = ""
        if has_danger:
            user_rows = build_user_rows(posts)
        else:
            user_rows = '<div class="no-data">当前数据源是旧版CSV (posts_full.csv)，缺少 show_user_id 字段。<br><br>请运行 <code>python spider_danger.py</code> 采集完整数据。</div>'

        csv_name = os.path.basename(csv_path) if csv_path else "?"
        danger_label = "完整数据 (含ID)" if has_danger else "旧版数据 (无ID)"

        html = ADMIN_DASHBOARD_HTML.format(
            csv_source=csv_name,
            total=len(posts),
            unique_users=unique_users,
            multi_post_users=multi_post_users,
            total_comments=total_comments,
            unique_commenters=unique_commenters,
            danger_label=danger_label,
            user_rows=user_rows,
        )
        self._serve_html(html)

    # ---- ROUTING ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/admin":
            self._handle_admin_get()
        else:
            self._handle_main()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/admin":
            self._handle_admin_post()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[{args[0]}] {args[1]} {args[2]}")


if __name__ == "__main__":
    port = 8080
    print(f"http://127.0.0.1:{port}")
    print(f"Admin: http://127.0.0.1:{port}/admin")
    print(f"Default password: {DEFAULT_PASSWORD}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
