# RUC小喇叭 高级搜索

中国人民大学"RUC小喇叭"（云上校友圈/奇喵缘分）匿名论坛的高级搜索工具。

## 项目来源

本项目 fork 自 [revalue-o/RUCxiaolaba-Advanced-Search](https://github.com/revalue-o/RUCxiaolaba-Advanced-Search)，感谢学长的开创性工作。

**原项目（2024-2025）**：
- 爬虫目标：`ruc.yunshangxiaoyuan.cn`（旧版 API）
- 鉴权方式：请求体中的 `openid`
- 技术栈：Flask + DuckDB + 阿里百炼 AI
- 功能：多关键词搜索、评论搜索、AI 总结

**2026.05 更新（本 fork）**：
- 旧版 API 已不可用，重新逆向发现新版 API
- 新版 API 域名：`ys.qimiaoyuanfen.com`
- 新版鉴权：Cookie session（`ys7_ysxy_session`）
- 通过 mitmproxy 抓包完成 API 逆向
- 新增 `spider_new.py`（新版爬虫）、`test_api.py`（API 测试）、`mitm_filter.py`（抓包工具）

## 快速开始

```bash
# 1. 安装依赖
pip install requests

# 2. 配置 Cookie
cp data/config.example.txt data/config.txt
# 编辑 data/config.txt，填入你的 session cookie
# 获取方式：mitmproxy 抓包 或 WeChat PC DevTools

# 3. 爬取数据
python spider_new.py

# 4. 测试 API 连通性
python test_api.py
```

## 项目结构

```
├── spider.py            # 旧版爬虫（ruc.yunshangxiaoyuan.cn，已失效，保留供参考）
├── spider_new.py        # 新版爬虫（ys.qimiaoyuanfen.com，当前可用）
├── test_api.py          # API 连通性测试
├── mitm_filter.py       # mitmproxy 抓包过滤脚本
├── app.py               # Flask 搜索服务（待适配新版 API）
├── utils.py             # DuckDB 查询 + AI 搜索逻辑（待适配）
├── init_duckdb.py       # 数据库初始化
├── data/                # 数据存储目录（gitignored）
│   ├── config.example.txt  # 配置文件模板
│   └── config.txt          # 实际配置（含 cookie，不提交）
├── templates/           # 前端页面
├── static/              # 静态资源
└── README.md
```

## Cookie 维护

Session cookie 有时效限制。过期后 API 返回 `code: 1000`，需更新 `data/config.txt`：

1. 打开微信 → 进入"云上校友圈"小程序
2. mitmweb 面板中复制任意 `ys.qimiaoyuanfen.com` 请求的 Cookie
3. 更新 `data/config.txt`

## TODO

- [ ] `spider_new.py` 接入 DuckDB 替换 CSV
- [ ] 新版 API 评论接口适配
- [ ] Flask 搜索适配新版数据格式
- [ ] Cookie 过期自动提醒
- [ ] 定时爬取（cron / GitHub Actions）

## License

MIT。原仓库作者声明"代码可以随便用，包括二次开发"。
