# Changelog

## 2026-05-29

### Added
- 评论展示增加序号（#1, #2...）和发布时间
- 搜索同时匹配帖子和评论内容
- "按热度"排序按钮
- 页面显示爬取时间和最新帖子时间

### Changed
- `server.py` 直接从 `posts_full.csv` 读取，移除对 `search_new.py` 的依赖

## 2026-05-28

### Added
- **`spider.py`** — 全新爬虫，两步爬取：
  1. `lists2` 扫描帖子列表（按页）
  2. `article/info` 逐条获取详情（含评论、赞数、浏览量、嵌套回复）
- **`server.py`** — 纯前端 Web 搜索，536 帖数据内嵌，多关键词搜索，3 种排序
- **`test_api.py`** — API 测试工具
- **`mitm_filter.py`** — mitmproxy 代理过滤脚本
- **`start_proxy.bat`** — 一键启动代理 + Web 面板
- `data/config.example.txt` — 配置模板

### Changed
- **API 切换**：从旧版 `ruc.yunshangxiaoyuan.cn`（已失效）迁移至 `ys.qimiaoyuanfen.com`
- **鉴权方式**：从请求体 `openid` 切换为 Cookie `ys7_ysxy_session`
- **数据格式**：适配新版 `{data: {list: [...], comment_list: [...]}}` 结构
- `README.md` 完全重写

### Removed
- 旧版代码（`app.py`, `utils.py`, `init_duckdb.py`, `spider_new.py`, `search_new.py` 等）——已归档/删除
- 旧版 API 数据文件移至 `data/archive/`

### Fixed
- CSV 字段限制溢出（post #4963315 含 64 条评论导致 JSON 超长）
- 帖子 ID 去重

## 2024-2025（上游仓库 [revalue-o/RUCxiaolaba-Advanced-Search](https://github.com/revalue-o/RUCxiaolaba-Advanced-Search)）

- Flask + DuckDB 后端
- 阿里百炼 DashScope AI 总结
- 多关键词搜索、评论搜索
- 定时爬取（每天凌晨 3 点）
- 旧版 API `ruc.yunshangxiaoyuan.cn`
