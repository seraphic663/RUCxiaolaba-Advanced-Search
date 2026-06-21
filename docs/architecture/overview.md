# 项目总览

## 当前定位

本项目是 RUC 小喇叭数据的本地/线上搜索服务。当前架构已经从 CSV 运行时切换为 DB-only：

- 主站只读取 SQLite：`data/posts.db`
- 爬虫只通过 `crawler_db.py` 写入 SQLite
- Web 服务由 `server.py` 提供页面和 JSON API
- Railway 线上通过 Volume 持久化 `/app/data/posts.db`
- CSV 已退出运行路径，不再作为服务输入

## 核心模块

```text
server.py                     Web 兼容启动入口
crawler_db.py                 爬虫兼容 CLI 入口
app/config.py                 集中配置和路径解析
app/repositories/             SQLite 读取、搜索和 AI 权限数据
app/services/                 搜索、Admin、鉴权、模板和 AI 编排
app/http/routes/              公开、Admin、AI 路由
app/ai/                       检索、审核、模型客户端、提示词和证据校验
crawler/client.py             小程序 API Client
crawler/normalizer.py         API 数据标准化
crawler/service.py            爬取执行与断点状态
crawler/strategies/           页面流和 ID 范围扫描策略
storage/post_writer.py        SQLite 写入、FTS 与 Bigram 同步
jobs/scheduler.py             Railway 自动更新调度器
jobs/backup.py                运行时备份
tools/                        迁移、审计、性能和运维工具
templates/                    主页和管理后台模板
```

## 数据流

```text
小程序 API
  -> crawler.client.MiniProgramClient
  -> crawler.normalizer
  -> crawler.service.CrawlerService
  -> storage.post_writer.SQLitePostStore
  -> data/posts.db + bigram_index.db

浏览器
  -> app.http.router
  -> app.http.routes
  -> app.services
  -> app.repositories
  -> SQLite
```

## 运行时数据

```text
data/posts.db             主数据库
data/bigram_index.db      Bigram 旁路索引；存在时本地自动启用
data/config.txt           小程序 cookie，爬虫需要
Railway ADMIN_PASSWORD    admin 固定密码环境变量
```

`posts.db-shm` 和 `posts.db-wal` 是 SQLite WAL 辅助文件，访问 DB 后可能自动出现，通常不需要手动管理。

## 已废弃内容

以下路径已经不属于当前运行架构：

- CSV 数据文件
- 旧 CSV 爬虫
- demo 架构切换页
- legacy 旧脚本归档

这些内容可从 git 历史恢复，但不应再作为当前部署或开发依据。

## 当前风险点

1. `data/posts.db` 很大，Railway 5GB Volume 下不要频繁做完整 DB 备份。
2. 单字中文搜索回退 `LIKE`；两字及以上在 Bigram 可用时走旁路索引。
3. admin 的复杂筛选如果勾选评论/ID/昵称，会比正文搜索慢。
4. 定时爬虫需要错峰，虽然已有跨进程锁，但不建议多个服务同时写 DB。
5. Bigram 是可重建的旁路索引库，更新由 PostWriter 同步执行。

## 依赖方向

```text
HTTP / CLI / Jobs
        ↓
     Services
        ↓
Repositories / API Client
        ↓
 SQLite / Remote API
```

根目录入口仅用于兼容。新代码不得从 Service 或 Repository 反向导入 `server.py`、`crawler_db.py`。
