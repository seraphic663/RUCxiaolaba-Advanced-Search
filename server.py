"""Minimal web search for RUC-Xiaolaba new data."""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from search_new import load_deduped

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
.search-box {{ max-width: 700px; margin: 20px auto; padding: 0 16px; }}
.search-box input {{ width: 100%; padding: 14px 18px; font-size: 16px; border: 2px solid #ddd; border-radius: 12px; outline: none; }}
.search-box input:focus {{ border-color: #8b0012; }}
.results {{ max-width: 700px; margin: 0 auto; padding: 0 16px; }}
.post {{ background: #fff; border-radius: 12px; padding: 18px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.post .meta {{ color: #888; font-size: 0.85em; margin-bottom: 6px; display: flex; justify-content: space-between; }}
.post .content {{ line-height: 1.7; white-space: pre-wrap; word-break: break-word; }}
.post .id {{ color: #bbb; font-size: 0.75em; }}
mark {{ background: #fff3b0; padding: 1px 3px; border-radius: 2px; }}
.info {{ color: #888; text-align: center; margin-top: 30px; font-size: 0.9em; }}
.empty {{ text-align: center; color: #999; padding: 40px; }}
</style>
</head>
<body>
<div class="header">
  <h1>RUC小喇叭 搜索</h1>
  <p>中国人民大学匿名论坛 · {total} 条帖子</p>
</div>
<div class="search-box">
  <input type="text" id="q" placeholder="搜索关键词，空格分隔多个词..." autofocus>
</div>
<div class="results" id="results">
  <div class="empty">输入关键词开始搜索</div>
</div>
<div class="info">数据更新于 {updated}</div>
<script>
let all = {all_json};
let timer = null;
document.getElementById('q').addEventListener('input', function() {{
  clearTimeout(timer);
  timer = setTimeout(() => {{
    let kw = this.value.trim().split(/\\s+/).filter(Boolean);
    if (kw.length === 0) {{
      document.getElementById('results').innerHTML = '<div class="empty">输入关键词开始搜索</div>';
      return;
    }}
    let found = all.filter(a => kw.every(k => a.content.toLowerCase().includes(k.toLowerCase())));
    if (found.length === 0) {{
      document.getElementById('results').innerHTML = '<div class="empty">没有找到匹配的帖子</div>';
    }} else {{
      document.getElementById('results').innerHTML = found.slice(0, 100).map(a => {{
        let text = a.content;
        kw.forEach(k => {{
          let re = new RegExp('(' + k.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
          text = text.replace(re, '<mark>$1</mark>');
        }});
        return '<div class="post"><div class="meta"><span>[' + a.category + '] ' + a.user + '</span><span class="id">#' + a.id + '</span></div><div class="content">' + text + '</div></div>';
      }}).join('');
    }}
  }}, 200);
}});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        articles = load_deduped()
        updated = max((a.get("time", "?") for a in articles), default="?")
        html = HTML.format(
            total=len(articles),
            updated=updated,
            all_json=json.dumps(
                [{"id": a["id"], "content": a["content"],
                  "category": a["category"], "user": a["user"]}
                 for a in articles], ensure_ascii=False),
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
