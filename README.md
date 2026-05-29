# RUC小喇叭 高级搜索

中国人民大学"RUC小喇叭"（云上校友圈）匿名论坛的数据爬取与搜索工具。

## 项目来源

Fork 自 [revalue-o/RUCxiaolaba-Advanced-Search](https://github.com/revalue-o/RUCxiaolaba-Advanced-Search)，感谢学长的开创性工作。

**原项目（2024-2025）**：基于旧版 API（`ruc.yunshangxiaoyuan.cn`），Flask + DuckDB + 阿里百炼 AI。旧版 API 已停止响应。

**本 fork（2026.05）**：通过 mitmproxy 代理逆向发现新版 API（`ys.qimiaoyuanfen.com`），完全重写爬虫和搜索前端，适配新鉴权方式与数据结构。

## 快速开始

```bash
pip install requests urllib3

# 1. 配置 Cookie
cp data/config.example.txt data/config.txt
# 编辑 data/config.txt 填入 session cookie
# 获取方式：双击 start_proxy.bat → 开微信小程序 → mitmweb 面板复制

# 2. 爬取数据
python spider.py 30        # 爬最近30页帖子+评论+赞+浏览

# 3. 启动搜索
python server.py            # → http://127.0.0.1:8080
```

## 项目结构

```
├── spider.py          # 爬虫：帖子列表 → 逐条详情（含评论、赞、浏览量）
├── server.py          # Web 搜索界面（单文件，数据内嵌，纯前端搜索）
├── test_api.py        # API 连通性测试
├── mitm_filter.py     # mitmproxy 抓包过滤脚本
├── start_proxy.bat    # 一键启动代理（获取 Cookie 用）
├── captured_requests.jsonl  # 抓包原始数据（gitignored）
├── data/
│   ├── config.txt           # Session cookie（gitignored）
│   ├── config.example.txt   # 配置模板
│   ├── posts_list.csv       # 帖子列表（基础信息）
│   └── posts_full.csv       # 完整数据（含 comments_json）
└── README.md
```

## Web 搜索功能

- 多关键词搜索（空格分隔，AND 逻辑）
- 搜索范围包括帖子正文 + 评论内容
- 四种排序：按时间 / 点赞 / 浏览 / 热度
- 关键词高亮
- 展开评论：显示序号、时间、点赞数、嵌套回复、楼主标记
- 全部数据内嵌 HTML，瞬间响应

## API 架构

| 端点 | 用途 |
|------|------|
| `article/article/lists2` | 帖子列表（分页） |
| `article/article/info` | 帖子详情（内嵌评论列表+嵌套回复） |
| `article/article/datehot` | 热门帖子 |
| `article/article/star` | 点赞帖子 |
| `article/article_comment/star` | 点赞评论 |
| `base/community/info` | 社群信息 |

鉴权方式：Cookie `ys7_ysxy_session`，有时效限制。

## Cookie 维护

Cookie 过期（API 返回 `code: 1000`）时更新：

1. 双击 `start_proxy.bat`
2. Windows 代理设为 `127.0.0.1:8899`
3. 微信打开"云上校友圈"小程序
4. 浏览器 `http://127.0.0.1:8900/?token=xlb123` 复制 Cookie
5. 更新 `data/config.txt`

## License

MIT。原仓库作者声明"代码可以随便用，包括二次开发"。
