"""Web search for RUC-Xiaolaba — reads posts_full.csv directly."""
import json
import csv
import os
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV = os.path.join(DATA_DIR, "posts_full.csv")


def load_posts():
    if not os.path.exists(CSV):
        return [], None
    csv.field_size_limit(10 ** 7)
    seen, posts, crawl_time = set(), [], None
    with open(CSV, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            aid = r.get("id", "")
            if aid and aid not in seen:
                seen.add(aid)
                cmts_raw = r.get("comments_json", "[]")
                try:
                    comment_list = json.loads(cmts_raw)
                except Exception:
                    comment_list = []
                posts.append({
                    "id": aid,
                    "content": r.get("content", ""),
                    "category": r.get("category_name", ""),
                    "user": r.get("user_name", ""),
                    "time": r.get("create_time", ""),
                    "comments": int(r.get("comment_count", 0)),
                    "stars": int(r.get("star_count", 0)),
                    "views": int(r.get("views", 0)),
                    "hot": int(r.get("hot", 0)),
                    "comment_list": comment_list,
                })
    posts.sort(key=lambda x: int(x["id"]), reverse=True)
    crawl_time = datetime.fromtimestamp(os.path.getmtime(CSV)).strftime("%Y-%m-%d %H:%M")
    return posts, crawl_time


HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RUC小喇叭 搜索</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f5f5; }}
.header {{ background: #8b0012; color: #fff; padding: 20px; text-align: center; }}
.header h1 {{ font-size: 1.5em; }}
.header p {{ opacity: 0.8; margin-top: 4px; font-size: 0.9em; }}
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
    let txt = c.detail || '';
    kw.forEach(k => {{
      let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
      txt = txt.replace(re, '<mark>$1</mark>');
    }});
    let replies = (c.reply_comment_list || []);
    let replyHTML = '';
    if (replies.length > 0) {{
      replyHTML = '<div class="replies">' + replies.map(rp => {{
        let rt = rp.detail || '';
        kw.forEach(k => {{
          let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
          rt = rt.replace(re, '<mark>$1</mark>');
        }});
        return '<div class="reply"><div class="cmt-meta"><span style="color:#bbb">' + formatTime(rp.create_time) + '</span> ' + rp.show_user_name + ' · ⭐' + (rp.count_star||0) + '</div><div class="cmt-text">' + rt + '</div></div>';
      }}).join('') + '</div>';
    }}
    return '<div class="cmt"><div class="cmt-meta"><b style="color:#8b0012;">#' + num + '</b> <span style="color:#bbb">' + formatTime(c.create_time) + '</span> ' + c.show_user_name + (c.is_publisher==1?' <span style="color:#8b0012;">楼主</span>':'') + ' · ⭐' + (c.count_star||0) + '</div><div class="cmt-text">' + txt + '</div>' + replyHTML + '</div>';
  }}).join('');
}}

function doSearch() {{
  let kw = document.getElementById('q').value.trim().split(/\\s+/).filter(Boolean);
  let found;
  if (kw.length === 0) {{
    found = all;
  }} else {{
    found = all.filter(a => {{
      let cmtText = (a.comment_list || []).map(c => c.detail + ' ' + (c.reply_comment_list||[]).map(r => r.detail).join(' ')).join(' ');
      let searchText = a.content + ' ' + cmtText;
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
      let cmtLabel = nComments > 0 ? ('<span class="toggle-cmts" onclick="toggleCmts(' + idx + ')">▶ 展开 ' + nComments + ' 条评论</span>') : '';
      let cmtsHTML = '<div class="cmts" id="cmts-' + idx + '" style="display:none">' + renderComments(a.comment_list || [], kw) + '</div>';
      return '<div class="post"><div class="meta"><div class="left"><span>[' + a.category + '] ' + a.user + '</span><span class="time">' + (a.time || '') + '</span></div><span class="stats">💬' + a.comments + ' ⭐' + a.stars + ' 👁' + a.views + ' 🔥' + a.hot + '</span></div><div class="content">' + text + '</div>' + cmtLabel + cmtsHTML + '</div>';
    }}).join('');
  }}
}}

window.toggleCmts = function(idx) {{
  let el = document.getElementById('cmts-' + idx);
  if (el.style.display === 'none') {{
    el.style.display = 'block';
    let toggle = el.parentElement.querySelector('.toggle-cmts');
    if (toggle) toggle.textContent = toggle.textContent.replace('▶', '▼');
  }} else {{
    el.style.display = 'none';
    let toggle = el.parentElement.querySelector('.toggle-cmts');
    if (toggle) toggle.textContent = toggle.textContent.replace('▼', '▶');
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        posts, crawl_time = load_posts()
        latest_time = max((p["time"] for p in posts), default="?") if posts else "?"
        html = HTML.format(
            total=len(posts),
            crawl_time=crawl_time or "?",
            latest_time=latest_time,
            all_json=json.dumps(posts, ensure_ascii=False),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        print(f"[{args[0]}] {args[1]} {args[2]}")


if __name__ == "__main__":
    port = 8080
    print(f"http://127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
