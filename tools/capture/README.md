# 本地抓包辅助

这两个文件只用于在本人设备上观察小程序正常请求、确认接口格式或取得本人会话 cookie，不进入 Web、crawler 或 Railway 运行路径。

```powershell
python -m pip install mitmproxy
.\tools\capture\start_proxy.bat
```

`mitm_filter.py` 会把筛选后的请求记录到同目录的 `captured_requests.jsonl`。该文件可能包含 cookie、请求正文和个人信息，已由 `*.jsonl` 忽略规则排除；使用后应及时删除，不得提交或分享。
