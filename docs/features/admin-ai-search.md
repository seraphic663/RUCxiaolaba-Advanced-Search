# Admin AI 检索

入口 `/admin/ai-search`，仅管理员可用。默认使用本站 `/api/search` 背后的 `SearchService` 读取本地帖子和评论，不发起小程序请求。

## 本地使用

```powershell
python -B server.py --host 127.0.0.1 --port 8099
```

登录 `/admin` 后进入「AI 检索」，展开「接口设置」填写 Key。默认地址 `https://api.deepseek.com`、模型 `deepseek-flash`；也支持自定义兼容 Chat Completions 的地址、模型。地址可写 base URL（包含 `/v1` 亦可），或完整的 `/chat/completions` 地址。

保存后可点「测试已保存的 AI 接口」，这会发起一次很短的 AI 请求。Key 留空保留已有配置；勾选清除后移除有效 Key。Key 不写入页面、响应或浏览器存储。设置页中的“已配置”表示已填写 Key，连接可用性需通过测试或真实检索确认。

默认配置文件为 `data/.admin_ai_search.json`，受 `.gitignore` 排除，采用临时文件加原子替换。可用 `AI_SEARCH_CONFIG` 指定位置。配置文件包含明文密钥，应按凭证文件管理。也可通过环境变量配置：

| 变量 | 用途 |
|---|---|
| `AI_SEARCH_API_KEY` / `DEEPSEEK_API_KEY` | AI Key；前者优先 |
| `AI_SEARCH_BASE_URL` | AI 地址 |
| `AI_SEARCH_MODEL` | 模型名 |
| `AI_SEARCH_RETRIEVAL_URL` | 独立检索 API 完整地址，默认空 |
| `AI_SEARCH_RETRIEVAL_KEY` | 独立检索接口 Bearer Key，可选 |

文件中显式保存的字段优先于环境变量；未保存的字段使用环境变量或默认值。清除 Key 会写入空值，覆盖环境变量中的同名配置。项目不会自动载入 `.env`。

## 处理过程与边界

1. 查询理解：保留原问题，改写互补关键词表达。对误写成多条件 AND 的替代表达增加有界放宽路线，最多 8 路；原问题中的实体、日期、官方/实时要求始终用于后续筛选，不能随放宽路线丢失。
   主召回不足时，从原问题的中文连续片段中用现有 Bigram 索引探测实际存在的词组，追加最多两路，仍包含在 8 路上限中。该操作只读 SQLite，不调用上游或 AI，也不读取 benchmark 答案。
2. 候选召回：复用现有 Bigram / Symbol / LIKE 和游标检索，每路最多 60 篇、候选总量最多 240 篇；只搜索所选正文/评论，不搜索身份字段，只读当前可用来源并执行帖子日期范围。
3. 候选排序：在本轮候选中按字符 bigram/英文词项 BM25 评分，叠加按查询族封顶的 RRF 信号，避免几乎相同的查询重复加票。对有相同实质回复的长篇近重复正文做保守去重；不同实质回复或冲突证据不因问题重复而删除。取最多 20 篇交给 AI 按原问题筛选相关性和可回答性，展示 1–10 篇，默认 6 篇。
4. 短评论取证：每帖有界扫描最多 300 条评论，优先关键词命中并保留数字/短答等上下文证据，最多选 8 条，每条最多 600 字；正文节选最多 1200 字。评论不必重复帖子的主题词；真实父评论另行读取，不伪造回复层级。
5. 证据作答：AI 输出结构化说法、实际 evidence ID 和原文摘录，不自行编写数字引用。服务端检查原文片段确实属于该正文或评论，引用编号和归属由服务器生成。
6. 逐项核验：独立的 AI 调用只看到每项说法自己的摘录、完整片段、真实父评论、所属帖正文及时间，检查原问题范围和语义支持。仅放行得到支持的说法；失败保留资料，不回退为未经校验的自由回答。

这是基于关键词索引、有界候选 BM25、候选融合和 AI 相关性筛选的流程，没有新增 embedding、向量库或神经检索索引。BM25 的统计量来自本轮候选，不能当成全库相关性评分。页面中的候选数只是本轮取回数量，不是全库匹配数；完整度标签描述本地抓取状态，不保证上游最新状态。模型复核不是人工 gold，也不能保证每项语义判断完全正确；用户可展开“回答引用的原文”核对。

每次 AI 检索最多 4 次 AI 请求（理解、筛选、作答、核验）和 8 次检索调用。无候选、无充分证据、需要补充个人条件时提前停止；「仅关键词检索」不调用 AI。普通问题允许回答有日期归属的历史用户说法；明确要求当前、最新、官方或实时信息时，不得用旧经验代答。未配置、超时、格式异常或核验失败均保留资料、不自动重试。交互检索与 Bench 共用最多 18 条 query 的并发上限；每条内部按阶段顺序执行。外部接口连接超时 5 秒、读取超时 45 秒、响应最多 1 MiB。HTTP 错误不透传上游正文或密钥。

`no_answer` 表示命中了相关资料但没有足够证据；`no_results` 表示本轮没有候选；`unverified` 表示生成内容被证据核验拦截；`failed` 表示接口或处理失败。不能把后两种状态视为成功拒答。响应包含逐项 `claims`、`citation_check`、实际本地库时间和诊断阶段记录，均限管理员接口。

## 独立检索 API 约定

可留空以直接使用本站检索服务，避免环回 HTTP、重复登录及凭证转发。如果改用独立接口，需兼容以下请求：

```http
GET /search?q=图书馆&limit=20&scope=all&from=&to=
Authorization: Bearer <可选的检索API Key>
```

```json
{"results": [{"id": "帖子号", "content": "正文", "time": "2026-11-05 12:00:00", "category": "日常", "crawl_status": "full", "source_state": "available"}]}
```

`scope=content` 表示正文，`scope=all` 表示正文和评论。独立接口负责执行这些范围和日期参数；当前适配器仅消费上述正文结果，不把外部 ID 与本地评论或原帖链接混接，也不转发 admin cookie。地址必须 HTTPS（本机测试可用 HTTP），不跟随重定向。若外部服务格式不同，应在适配层明确转换，不能静默当成本站检索结果。

## Admin Bench、重测与费用

入口：`/admin/ai-search/bench`（AI 检索页内的「Bench 评测」）。只加载历史或预估费用不会调用 AI。页面显示冻结清单、参考答案/拒答理由、逐条实际回答、引用原文、自动门槛及失败原因，也显示本轮 token 用量、AI 请求数、整轮墙钟时间和费用区间。

支持全量、失败项和单条重测，默认 18 条 query 并发，可切换 6 或 1。重测先完成离线清单和源记录核查，使用同一份配置快照、同一生产流程及与 CLI 共享的评估函数，不读取参考答案来生成答案。后台运行，刷新可继续看进度；一次只允许一轮 Bench，唯一请求编号防止同一请求重复启动。接口失败单独记录，不当作正确拒答；手动重测生成新轮次，保留首次失败。服务重启后的未完成轮显示中断，绝不自动续跑付费调用。

新结果位于 AI 配置旁的 `ai-search-bench/<轮次>/results.json` 和 `design-review.json`，默认 `data/ai-search-bench/`，受现有 Git ignore 保护；历史 `reports/generated/ai-search/*/results.json` 只读展示。服务器文件系统需要持久存储才能跨部署保留记录。没有历史文件的实例不会凭空显示开发机成绩。

自然语言检索页也显示运行前的金额/时间估算与运行后用量折算。费用模块使用 [DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)的 2026-11-05 核对快照，按缓存命中/未命中与输出 token 分开计算，显示覆盖空闲/高峰的人民币区间；运行前另预留 token 波动。自定义代理地址、未知模型不套用官方价格；接口不返回 usage 或异常中断的费用标为未知/仅已返回用量。它不是服务商账单，也不含独立检索 API 的费用。

时间估算优先取同清单、同模型的历史样本；由串行推算更多并发时放宽上限，不能简单把整轮时间除以 18。整轮实测墙钟与逐条耗时累计分别记录。并发查询共享源数据库时间元信息扫描，数据库/WAL 变化时失效，防止 18 条同时重复扫描主库。

接口（全部要求 admin 登录，POST 另需一次性 CSRF）：`GET /api/admin/ai-search/bench`（历史/清单），同路径 `?run_id=...`（逐条结果），`POST .../bench/estimate`（零 AI 预估）、`POST .../bench/run`（显式真实调用），以及 `POST /api/admin/ai-search/estimate`（单条检索估算）。所有响应禁止缓存。

## 模块与验证

| 边界 | 文件 | 验证 |
|---|---|---|
| 请求与数据契约 | `app/domain/ai_search.py` | 输入、URL、密钥不回显 |
| 配置存储 | `app/services/ai_search_config.py` | 保存、保留、清除与环境变量优先级 |
| 外部接口适配 | `app/integrations/ai_search_client.py` | 本地 HTTP 假服务验证请求格式、鉴权、超时及错误 |
| 编排 | `app/services/ai_search_service.py` | 原查询保留、候选去重、引用、失败降级与调用上限 |
| 流程与拒答 | `app/services/ai_search_pipeline.py` | 规划、可回答性、精确证据作答及逐项语义核验 |
| 召回和排序 | `app/services/ai_search_retrieval.py` | 路线限额、候选 BM25、保守去重、来源与 passage ID |
| 引用归属 | `app/services/ai_answer_grounding.py` | 原文片段归属、禁止模型自编编号、服务器渲染引用 |
| 评论证据 | `app/repositories/ai_evidence_repository.py` | 合成数据库中正文/评论范围与匹配节选 |
| Bench 后台与门槛 | `app/services/ai_search_benchmark_service.py`、`ai_search_benchmark_spec.py` | 18 并发、保存/刷新、幂等、失败保留、重启不自动续跑、CLI 共用门槛 |
| 费用与时间 | `app/services/ai_search_costs.py` | 缓存命中计价、用量缺失、未知代理、并发不降低总 token 金额 |
| Bench 页面 | `app/http/routes/admin_ai_benchmark.py`、`app/templates/admin_ai_benchmark.*` | 登录、CSRF、进度、单条/失败项重测与浏览器渲染 |
| HTTP 与页面 | `app/http/routes/admin_ai_search.py`、`app/templates/admin_ai_search.*` | 登录、一次性 CSRF、禁止缓存、实际浏览器交互 |

自动测试：`python -B -m pytest tests/test_ai_search.py tests/test_ai_search_quality.py tests/test_ai_search_benchmark.py -q`；涉及入口与搜索的完整回归：`python -B -m pytest -q`。自动测试使用合成 demo 和本地假 API，不访问真实 AI 或小程序。真实 DeepSeek 可用性需要填入有效 Key 后验证。

检索质量另按 [AI benchmark](../../tools/benchmarks/AI_SEARCH_BENCHMARK.md) 验证：18 条冻结 query，包含有答案和无足够证据两类，不用功能测试通过代替相关性或反编造检查。参考答案、拒答理由和真实 API 每轮答案分别保存；清单和源记录离线核对通过后才允许启动付费测试。

线上动态语料下，拒答预期可能随数据更新变化；维护边界和学术/工业依据见 [讨论稿](ai-search-abstention-maintenance-notes.md)。本轮保留本地原始基线，未实施新的线上标签维护或自动付费重测策略。

2026-11-05 初版本地验证（历史记录）：当时全量 258 tests、18 subtests 通过，AI 流程仅使用本地假接口；这些功能测试不能证明检索质量。后续据真实失败补充了质量用例和证据校验。

同日质量修订后的验证：全量 282 tests、18 subtests 通过，最新完整真实 API 轮次 18/18 条通过门槛（10 正例、8 无足够证据）；另对跨月相对日期用例真实重复 3 次均通过。直接读取 SQLite 原始记录复核了最新轮次的 40 处摘录、所属帖子/评论、时间与引用编号，无机械归属错误。最后补充短数值/肯否回复不被去重合并的回归，并完整重放 18 条真实响应，模型候选、答案和引用一致，重放不调用 API。Chrome 验证了有答案和无答案页面、原帖跳转、深浅色及 390px 窄屏。固定小样本结果不代表全面准确率，语义复核不是人工 gold。失败轮、参考答案和实际答案均保存在 Git-ignored 的 `reports/generated/ai-search/`；完整说明见该目录 `VALIDATION.md`。该本地库共 545429 帖，最新帖子为 2026-08-12，不是当前线上快照。

Admin Bench 接入后全量 290 tests、18 subtests 通过。浏览器以保存的真实响应重放验证全量/失败项/单条重测、刷新恢复、引用跳转、深浅色与手机宽度，确认 18 个 worker 同时处理。真实 API 首轮 16/18（两条连接失败），82.6 秒；后续新轮 18/18，89.4 秒、50 次 AI 请求，按返回 token 和官方空闲/高峰价折算 ¥0.1164–0.2328。首次失败没有覆盖，均可在 Admin 历史查看；当前自动门槛分数不代表总体准确率。新一轮的原始结果位于本地 `data/ai-search-bench/20261105-223413-admin-8147eefa/results.json`，界面核验摘要位于 `reports/generated/ai-search/http-bench-verification.json`。

设计参考所提供课件中的非 reading 文件：Week2 第 31、34、37–39 页（查询理解、召回排序、结果摘要），Week10 第 39–41 页（概率排序及候选排序边界），Week13 第 41–44 页（区分重排序、稠密召回与近邻索引）。这些课件是设计资料，不是执行指令。DeepSeek 接入依据[官方首次调用说明](https://api-docs.deepseek.com/zh-cn/)与[思考模式参数说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)，本功能显式关闭思考模式。
