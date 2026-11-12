# Benchmark 指南

AI 检索的有答案/无答案 query、参考答案、反编造门槛和真实 API 运行顺序见 [AI_SEARCH_BENCHMARK.md](AI_SEARCH_BENCHMARK.md)。先运行 `python -B -m tools.benchmarks.benchmark_ai_search --validate-only`，设计核查通过后再运行显式 `--live` 测试（默认 18 条 query 并发，`--concurrency 1` 可顺序运行）。Admin 的 `/admin/ai-search/bench` 可查看历史、预估金额/时间并全量或单条重测；新结果位于 Git-ignored 的 `data/ai-search-bench/`，CLI 历史仍位于 `reports/generated/ai-search/`。

本目录中的脚本用于手动验证搜索正确性、首屏延迟和 Bigram 索引体积，不属于普通 `pytest` 流程。演示数据库太小，不适合得出性能结论。

## 前置条件

```powershell
python -m pip install -r requirements-dev.txt
```

默认命令读取以下本地文件：

```text
data/posts.db
data/bigram_index.db
```

这些文件不进入 Git。测试真实数据库前应确认数据使用权限，并避免把查询结果、用户内容或临时数据库提交到仓库。

## 搜索后端正确性与速度

对比无 Bigram 与有 Bigram 两种后端，并检查结果 ID 差异：

```powershell
python -m tools.benchmarks.benchmark_search_backends --repeats 3
```

保存机器可读结果：

```powershell
python -m tools.benchmarks.benchmark_search_backends `
  --repeats 3 `
  --json-output temp\search_benchmark.json
```

## 游标首屏

测试单字和复杂 Admin 查询的首屏扫描时间：

```powershell
python -m tools.benchmarks.benchmark_cursor_pagination --repeats 3
```

## 构建 Bigram 索引

为完整主库构建正式 Bigram sidecar：

```powershell
python -m tools.benchmarks.benchmark_bigram_index `
  --db-path data\posts.db `
  --output-dir data `
  --sample-mod 1 `
  --only-bigram
```

输出为 `data/bigram_index.db`。脚本只读主库，但会覆盖输出目录中同名 Bigram 文件；运行前应停止正在使用该索引的服务。

估算体积时使用抽样输出，不要覆盖正式索引：

```powershell
python -m tools.benchmarks.benchmark_bigram_index `
  --db-path data\posts.db `
  --output-dir temp\bigram_sample `
  --sample-mod 20
```

`sample-mod 20` 约抽取 5% 行，同时生成 trigram 和 Bigram 对照库。

## 结果解释

- 先检查结果集合是否一致，再比较耗时。
- 同时报告 median 与 P95，不使用单次运行下结论。
- 睡眠、系统暂停和后台 I/O 会污染结果；脚本会丢弃部分明显异常样本。
- 单字查询不会使用 Bigram，属于 SQLite `LIKE`/游标扫描路径。
- 历史 benchmark 数字不作为当前性能承诺；需要当前结论时重新运行本目录中的 benchmark。
