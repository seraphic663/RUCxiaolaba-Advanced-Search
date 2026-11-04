# Admin 搜索、运行汇报与人工补爬

- `/admin`：管理搜索，保留原搜索字段、日期筛选和评论交互。
- `/admin/reports`：ID 积存趋势、每日流入/详情处理、新旧 cookie 请求日账。
- `/admin/crawl`：原人工候选预览和补爬；自定义数量、只记账、不设限的行为保持一致。
- `/api/admin/crawler-report`：需要 admin 登录的只读接口，支持 `from`、`to` ISO 时间；无时区时按北京时间解释。结束输入只有日期/分钟时包含最后一整天/分钟。不调用源 API。

## 数据来源与口径

趋势读取 Volume 的 `crawler_metrics/crawler_history.csv`（或 `CRAWLER_METRICS_PATH`），仅取 `metric_name=id_pending`。按 `record_id` 去重，保留所有实际时间点；不会重采样、回推或补点。服务按文件修改时间缓存筛选后的时序，不向浏览器发送原始大 CSV。

部署携带 `app/static/crawler_legacy.json`，由现有 `metrics/crawler_history.csv` 中的旧报告 ID 池系列摘取：`points.id_pending`、`points.id_pool`，以及明确标为 ID 池报告的 `points.pending/value`。保留原 `record_id`、时间、方法、质量和来源；旧报告的目标时刻/估算值不升级为实时精确观测。覆盖 2026-09-02 至 2026-10-09，共 367 条。原始记录没有时间或数量的点未导入。

蓝线为 `method=exact` 的观测，旧报告记录单独显示为橙色。无记录间隔超过 6 小时断线，孤立记录仍显示为点。悬停或左右方向键查看实际时间、数量、来源与 run ID。

快捷范围随时间滚动；自定义起止时间变化后自动请求和重绘。自定义结束留空时持续到现在。所选范围保存在 URL 中；页面每分钟读取新记录，后台采样仍沿用每轮结束及调度器定期快照。

每日流入/处理使用 `crawler_run_history`，按运行结束时间归日，仅统计列表和 ID 路由详情（排除明确标注的 `history_detail`）。未标路由的旧详情请求单列，不猜测属于哪个队列。跨日运行不能按每次请求时间拆分。失败轮请求数仍计入，但缺失的出队和保存结果标为不完整，不能默认为 0 或声称已完整对账。

配额日账使用 `.crawler_quota_history.jsonl` 的 `day_rollover` 和当日 `.crawler_quota.json`，按 `date` 归属请求日，不按次日的 `recorded_at`。它覆盖所选日期的整日/今日累计，区别于流入表首尾可能不足整天的范围。列表次数不扣本地详情配额，但仍消耗上游 session；个人小程序用量不在统计内。

移出 pending 不等于抓取完整：成功、源不可获取、转 deferred 都可能使其减少。`completed_details` 包括部分响应合并成功的情况，与 `partial_saved` 不是互斥分类。

## 验证

`tests/test_crawler_reports.py` 覆盖真实时刻保留、自定义范围、持续追加、两类队列分离、失败轮、日结归属与只读行为。`tests/test_http_contract.py` 覆盖鉴权与三个页面分离。线上验证只访问这些页面/只读报表接口，不调用预览、创建任务或人工爬取 API。
