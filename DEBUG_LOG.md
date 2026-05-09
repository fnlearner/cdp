# Chrome CDP 抓包工具调试结论

## 背景

目标：让 AI Agent 通过 Chrome DevTools Protocol (CDP) 直连浏览器，实现在对话中实时捕获和分析网络流量。

应用场景：用户对 AI 说"帮我看看这个网站加载了哪些资源、哪些请求失败了"，AI 通过 CDP 连接浏览器、抓取 Network 事件，分析后直接回复用户。

## 我们在做什么

构建一个 Python CDP 客户端（cdp.py），支持两种模式：

**单次模式**（single capture）：传入 URL，连接浏览器，导航，等待加载完成，导出所有网络请求，关闭连接。

**交互模式**（interactive mode）：维护一个长连接的命令行 REPL，支持连续命令：
```
connect --port 9223
navigate --url https://example.com
wait_navigate
get_requests
```

核心挑战：CDP 是长连接协议，命令和事件都在同一个 WebSocket 通道上传输。需要后台线程跑事件循环，主线程发命令、读结果。

## 遇到的问题

单次模式完全正常。交互模式 `connect` → `navigate` → `wait_navigate` → `get_requests` 全流程，返回结果永远是空的——`get_requests` 返回 `[]`，`wait_navigate` 超时。

## 排查过程

### 阶段 1：怀疑 asyncio 协程卡死

加了大量 DEBUG 日志追踪 cdp_send、schedule_coro、reader_coro、do_nav 的执行。

发现：navigate 命令成功拿到了 frameId，说明 WebSocket 双向通信正常、命令确实送达 Chrome 并被处理。但 do_nav 内部的 DEBUG 从未打印。

**错误推理**：协程在 `_reader_loop` 底部的 `await` 处卡死，无法调度 do_nav。

### 阶段 2：重构双线程架构

花了大量精力调整 pump_thread + bg_thread 的调度关系：
- 改 `run_until_complete` 为 `run_forever`
- 调整 reader 启动顺序
- 加 `_exit_signal` 让 reader 永久等待

同样无效。

### 阶段 3：发现 pump_thread 绑定错误的 queue

connect handler 里 `result_q = queue.Queue()` 创建了新对象，遮蔽了外层 `_result_queue`。pump_thread 绑定外层 queue（永远为空），bg_thread 写新 queue，两个线程操作不同对象。

修了，但依然无效。

### 阶段 4：发现真正根因

用户指出：navigate 拿到了 frameId 是铁证——命令执行成功了，do_nav 确实被调度并发送了数据。

真正的死因：**Chrome CDP 的静默白名单机制**。

单次模式在 `asyncio.run()` 内 connect 后发送了：
```python
await ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.enable", "params": {}}))
await ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Page.enable", "params": {}}))
```

交互模式的 connect handler **从未发送这两个 enable 命令**。

Chrome CDP 不显式开启 Domain，就静默丢弃所有相关事件——不报错，不提示。导致：
- 没有 Page.enable → Page.loadEventFired 永远不推送 → wait_navigate 超时
- 没有 Network.enable → 所有网络事件静默丢弃 → get_requests 永远为空

所有 DEBUG 日志和架构调整都是在治标，漏掉的是最基础的**业务逻辑**。

## 修复

在 connect handler 中，建立连接后立即订阅事件：

```python
async def enable_domains():
    if _ws:
        await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.enable", "params": {}}))
        await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Page.enable", "params": {}}))
schedule_coro(enable_domains())
```

修复后完整流程通过：
```
connect → {"ok": true, "note": "connected and domains enabled"}
navigate → Page.frameStartedNavigating + Network.requestWillBeSent 收到
wait_navigate → {"ok": true, "note": "导航完成"}
get_requests → {"ok": true, "requests": [...], "count": 1}
```

## 经验教训

1. **Chrome CDP 是白名单订阅制**——不显式 enable 就不推送事件，Chrome 不会报错或提示。任何人接这个协议都要先开 Domain。

2. **命令响应正常 ≠ 事件订阅正常**。收到 frameId 只说明命令发送成功了，但 Chrome 是否向你推送事件是另一回事。

3. **架构问题会掩盖业务问题**。在修复 pump_thread queue 遮蔽之前，所有事件都丢了，根本没有机会观察到"开了 enable 就有事件"这个现象。修完架构才能看清业务逻辑缺失。

4. **DEBUG 日志会误导**。大量追踪日志让人聚焦在"协程是否被调度"，而忽略了"Chrome 是否愿意推送事件"这个更上层的问题。

## 附录

- `cdp.py` — CDP 客户端（含 enable_domains 修复）
- `cdp_browser.py` — Chrome 浏览器进程管理（支持远程调试端口拉起）
- `SKILL.md` — 技能说明文档
- `DEBUG_LOG.md` — 本文档
