# RUC 小喇叭高级搜索

面向 RUC 小喇叭内容的非官方 SQLite 搜索与更新工具，支持帖子、评论、分类和排序。

> 本项目不是中国人民大学或 RUC 小喇叭官方项目。仓库不分发真实论坛数据库，自带的演示数据库只包含虚构内容。

## 功能

- 搜索帖子正文和评论，支持分类、日期、热度、点赞和评论数排序
- 两字及以上关键词可使用 Bigram 索引，单字关键词回退 SQLite `LIKE`
- 按需展开正文和评论，慢查询使用游标分页
- 爬虫直接增量写入 SQLite，可补新帖、活跃帖和历史范围

## 五分钟启动

要求 Python 3.10 或更高版本。

```powershell
git clone https://github.com/seraphic663/RUCxiaolaba-Advanced-Search.git
cd RUCxiaolaba-Advanced-Search

py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python server.py --sqlite-db demo\posts.db --bigram-db demo\bigram_index.db --port 8099
```

打开 <http://127.0.0.1:8099>，可以尝试搜索“食堂”“图书馆”“SQLite”。demo 使用独立端口，避免误连到已在 8080 运行的真实数据库服务。

`demo/posts.db` 和 `demo/bigram_index.db` 总计不到 200 KiB，包含 12 篇虚构帖子和 20 条虚构评论。它们只用于验证公开主页、评论搜索、分类、排序和 Bigram，不包含用户身份数据。重新生成演示数据：

```powershell
python -m tools.demo.build_demo_data
```

## 使用自己的数据库

主数据库默认路径是 `data/posts.db`：

```powershell
python server.py
```

也可以明确指定数据库、索引和端口：

```powershell
python server.py --sqlite-db D:\data\posts.db --bigram-db D:\data\bigram_index.db --port 8099
```

Bigram 索引是可选的。未提供索引时，搜索自动回退到 SQLite FTS/`LIKE`。数据库表结构见 [数据模型](docs/architecture/data-model.md)，索引构建和性能验证见 [benchmark 说明](tools/benchmarks/README.md)。

## 更新数据

只有在你确认具备相应授权并理解数据处理责任时，才应连接真实接口。先复制配置：

> 使用者必须使用本人合法取得且有权使用的 cookie，不得绕过登录、验证码、签名、限流或权限检查。持续抓取、全量扫描、公开部署或共享真实数据前，应取得平台运营方的书面授权。无法确认授权范围时，请只使用仓库自带的合成 demo。

```powershell
Copy-Item data\config.example.txt data\config.txt
```

在 `data/config.txt` 中填写你有权使用的 cookie，然后执行一次小范围同步：

```powershell
python crawler_db.py sync-latest --db-path data\posts.db --pages 20 --min-pages 3 --stop-unchanged 80
```

该命令可以从空路径创建数据库。正式运行前先阅读：

- [爬虫命令、停止条件和写锁](docs/operations/crawler.md)
- [项目如何抓取、是否获得授权、是否合法](docs/legal-and-data.md)

根 README 只保留最小可运行流程，批量补历史、日期范围扫描和自动调度参数均在爬虫文档中维护。

## 可选配置

| 配置 | 用途 | 默认值/替代方式 |
|---|---|---|
| `POSTS_DB_PATH` / `SQLITE_DB` | 主数据库路径 | `data/posts.db` |
| `BIGRAM_DB_PATH` / `BIGRAM_DB` | Bigram 索引路径 | 自动探测 `data/bigram_index.db` |
| `HOST`, `PORT` | 监听地址和端口 | `0.0.0.0:8080` |

配置文件、cookie、密码和真实数据库均已被 `.gitignore` 排除，不应提交。

## 开发与验证

```powershell
python -m pip install -r requirements-dev.txt
pytest
ruff check .
```

性能测试不会在普通测试中自动运行。命令、前置数据库和结果解释统一维护在 [tools/benchmarks/README.md](tools/benchmarks/README.md)。

## 项目结构

```text
server.py                  Web 兼容启动入口
crawler_db.py              爬虫兼容 CLI 入口
app/                       Web、Repository、Service 与 HTTP 路由
crawler/                   API Client、规范化、扫描策略与执行服务
storage/post_writer.py      SQLite 写入与搜索索引维护
demo/                      可提交的合成演示数据库
jobs/                      调度与运行时备份
tools/                     迁移、审计、性能和运维工具
tests/                     单元、集成、契约和性能测试
docs/                      架构、功能、运维和数据合规文档
```

完整入口见 [文档地图](docs/index.md)。

## 数据与授权

“能访问”不等于“可以批量抓取或公开再分发”。平台授权、个人信息处理依据、用户内容著作权和安全义务是不同问题，需要分别判断。仓库的 MIT License 只授权本项目代码和文档，不授权任何第三方论坛数据。

该接口是微信小程序正常使用的内部业务 API，不是后门，但也不是面向第三方开放的公开 API。复现正常客户端请求不等于获得自动化抓取许可，使用者必须自行确认账号权限、平台规则和书面授权范围。

关于项目如何获取数据、当前是否具备可验证授权以及为什么不能笼统宣称“项目合法”，见 [项目数据来源、授权与合法性 QA](docs/legal-and-data.md)。该文档提供工程风险控制建议，不替代针对具体用途的法律意见。

## License

代码和项目文档采用 [MIT License](LICENSE)。合成演示数据可随本项目使用；真实论坛内容、用户数据、平台名称和第三方素材不因本许可证获得授权。
