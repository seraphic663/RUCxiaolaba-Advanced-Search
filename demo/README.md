# 演示数据库

本目录中的 `posts.db` 和 `bigram_index.db` 只包含虚构数据，用于 README 快速启动、测试搜索和检查界面。它们不包含从 RUC 小喇叭提取的真实帖子、评论、用户标识或头像。

重新生成：

```powershell
python -m tools.demo.build_demo_data
```

生成器不会读取 `data/posts.db`。
