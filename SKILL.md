---
name: chrome-cdp
description: 使用原生 Chrome DevTools Protocol (CDP) 让 AI Agent 直接连接浏览器、捕获并分析 Network 流量的技能。
category: software-development
tags: [cdp, chrome, devtools, network, websockets, asyncio]
---

# chrome-cdp

使用原生 Chrome DevTools Protocol (CDP) 让 AI Agent 直接连接浏览器、捕获并分析 Network 流量的技能。

## 触发条件

- 用户提到"抓取网络请求"、"监听网络流量"、"CDP"、"Chrome 调试"、"network 抓包"时使用
- 用户要求分析某个网站的请求/响应、排查 404/500 问题、查看资源加载情况

## 前置依赖

- Chrome/Chromium 已安装
- Python venv: `~/.hermes/skills/software-development/chrome-cdp/venv`
- 依赖: `websockets`, `requests`

## 快速开始

### 1. 启动 Chrome 调试模式

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-cdp-profile

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-cdp-profile
```

### 2. 快速捕获

```bash
~/.hermes/skills/software-development/chrome-cdp/venv/bin/python \
  ~/.hermes/skills/software-development/chrome-cdp/cdp.py \
  --url "https://example.com"
```

输出请求列表: URL、状态码、耗时、MIME 类型。

### 3. 交互式分析（AI 使用）

CDP 脚本接收命令并通过 stdin/stdout JSON 通信，供 AI Agent 调用。

## 核心脚本用法

```bash
cdp.py [OPTIONS]

Options:
  --url TEXT        目标 URL，导航并捕获
  --keep-open       捕获完成后保持浏览器连接，不关闭
  --timeout SECS    页面加载超时，默认 30s
  --pattern TEXT    只捕获 URL 匹配该正则的请求（可多次指定）
  --export-json     输出完整 JSON 报告（包含 requestId、timing 等）
  --interactive     交互模式：监听 stdin，持续接收 AI 命令
```

### 交互模式示例

```bash
# 启动交互模式（浏览器已运行）
cdp.py --interactive

# AI 通过 stdin 发送命令（如 navigate、get_requests、get_body）
# 脚本通过 stdout 返回 JSON 结果
```

## CDP 命令协议（交互模式）

所有命令通过 stdin JSON 发送，结果从 stdout JSON 返回。

### 启动连接

```json
{"cmd": "connect"}
{"cmd": "connect", "port": 9222}
{"cmd": "connect", "browser_url": "ws://localhost:9222/..."}
```

### 页面操作

```json
{"cmd": "navigate", "url": "https://example.com"}
{"cmd": "enable_network"}
{"cmd": "disable_network"}
```

### 数据获取

```json
{"cmd": "get_requests"}       // 返回所有捕获的请求
{"cmd": "get_responses"}      // 返回所有响应
{"cmd": "get_request_body", "requestId": "..."}
{"cmd": "get_response_body", "requestId": "..."}
{"cmd": "get_har"}            // 返回 HAR 格式数据
```

### 响应格式

```json
// 成功
{"ok": true, "data": {...}}

// 错误
{"ok": false, "error": "message"}

// 事件推送（Network.requestWillBeSent 等）
{"event": "Network.requestWillBeSent", "data": {...}}
```

## 文件结构

```
chrome-cdp/
├── SKILL.md              # 本文件
├── cdp.py                # 核心 CDP 客户端脚本
├── cdp_browser.py        # 浏览器进程管理（启动/关闭 Chrome）
└── venv/                 # Python 虚拟环境
```

## 典型 AI 对话场景

**用户**: "抓取 https://news.ycombinator.com 的所有网络请求"

AI 内部流程:
1. 检查浏览器是否已启动，未启动则拉起进程
2. 获取 WebSocket URL
3. 建立连接，发送 `Network.enable`
4. 发送 `Page.navigate` 到目标 URL
5. 等待 `Page.loadEventFired`
6. 收集 `Network.requestWillBeSent` / `Network.responseReceived`
7. 整理数据返回给用户

**用户**: "哪个脚本加载失败了？"

AI 从已捕获数据中筛选 `status >= 400` 或 `Network.loadingFailed` 事件。

## 注意事项

- macOS Chrome 路径: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
- `--user-data-dir` 必须使用独立目录，避免覆盖正常浏览器配置
- 响应体可能很大，谨慎调用 `getResponseBody`
- 捕获 HTTPS 时 Chrome 会报证书警告（正常现象）

## ⚠️ 陷阱与已知问题

以下问题均通过长时间调试发现并修复，供参考避免重蹈覆辙。

### 1. `run_until_complete` 会导致 Reader 协程提前退出

**问题**：`run_until_complete(reader_coro())` 在协程遇到第一个 `await`（如 `await ws.recv()` 或 `await exit_signal.wait()`）时就会返回，整个事件循环退出。

**修复**：改用 `run_forever()` + `asyncio.ensure_future()` 组合，reader 协程永久运行直到外部调用 `loop.stop()`。

```python
# ❌ 错误：reader 在第一个 await 就退出
loop.run_until_complete(reader_coro())

# ✅ 正确：loop 持续运行，reader 永久等待
asyncio.ensure_future(reader_coro())
loop.run_forever()
```

### 2. 跨线程 `await` 一个 `concurrent.futures.Future` 会崩溃

**问题**：主线程 `cdp_send` 创建 `concurrent.futures.Future`，子线程填充它。主线程直接 `await future` 报错 `"SystemError: Future await doesn't support concurrent.futures.Future"`。

**修复**：用 `asyncio.wrap_future()` 将其转为 `asyncio.Future` 后再 `await`。

```python
# ❌ 错误
future = _pending_futures[cmd_id]  # concurrent.futures.Future
result = await future  # 崩溃

# ✅ 正确
loop = asyncio.get_event_loop()
asyncio_future = loop.run_in_executor(None, future.result)
result = await asyncio_future
```

### 3. `asyncio.set_event_loop` 必须在目标线程内部调用

**问题**：主线程调用 `asyncio.set_event_loop(bg_loop)` 后传给子线程 `run_loop`，子线程里 `"There is no current event loop"`。

**修复**：每个线程必须自己调用 `set_event_loop`。`run_loop` 函数内部（子线程运行时）设置。

```python
def run_loop(bg_loop, ...):
    asyncio.set_event_loop(bg_loop)  # 在子线程内设置，不是主线程
    asyncio.ensure_future(reader_coro())
    bg_loop.run_forever()
```

### 4. pump_thread 高频抢 GIL 导致 bg_loop 饿死

**问题**：pump_thread 无限循环 `queue.get(timeout=0.3)`，每 300ms 醒来抢一次 GIL，导致 bg_loop 没有足够连续时间片执行协程。

**修复**：在 pump 的 Empty 分支加 `time.sleep(0.05)` 让出 GIL。

```python
def pump_queue():
    while True:
        try:
            item = _result_queue.get(timeout=0.5)
            print(item, flush=True)
        except queue.Empty:
            time.sleep(0.05)  # 让出 GIL，给 bg_loop 调度机会
```

### 5. 全局变量在子线程里 `= {}` 会创建新对象（最关键 bug）

**问题**：子线程里 `global _pending_futures; _pending_futures = {}` 会创建新 dict 对象，主线程的 `cdp_send` 还在操作旧 dict 的旧 futures，永远收不到结果。

**修复**：原地 `.clear()` 而非赋值。

```python
# ❌ 错误：子线程创建新 dict，主线程引用分裂
global _pending_futures
_pending_futures = {}

# ✅ 正确：原地清空，保持引用不变
global _pending_futures
_pending_futures.clear()
```

### 6. 函数内部赋值会遮蔽外层全局变量（Queue 对象分裂）

**问题**：connect handler 里有 `result_q = queue.Queue()` 赋值语句，创建了新的局部 Queue 对象，遮蔽了外层 `_result_queue` 全局变量。pump_thread 绑定外层空 queue，bg_thread 写新 queue，两边操作不同对象，永远对不上。

**修复**：删除函数内部的 `result_q = queue.Queue()`，让函数直接使用外层 `_result_queue`。

### 7. `ws.close()` 用了局部变量而非全局 `_ws`

**问题**：`_reader_loop` 里 `await ws.close()` 用的是参数 `ws`（协程退出后生命周期已结束），而不是全局 `_ws`。

**修复**：统一使用全局 `_ws`，reader_coro 只负责等待退出信号。

### 8. 交互模式命令发送顺序：Reader 必须先启动

**问题**：先 `cdp_send(Network.enable)` 等响应，再 `await reader_task` 永久阻塞——但 enable 响应需要 reader 已在循环里才能被消费，形成死锁。

**修复顺序**：

```python
asyncio.ensure_future(_reader_loop(_ws, result_q, _exit_signal))  # 1. 先启动 reader
await _cdp_send_raw(...)  # 2. 再发命令（reader 已在跑，可以消费响应）
# 不要 await reader_task —— 它会永久阻塞
```

### 9. `wait_navigate` 需要 threading.Event 同步

**问题**：`navigate` 命令是异步提交（`Page.navigate` 只发命令不等待），无法知道页面何时真正加载完成。

**修复**：引入 `_nav_done = threading.Event()`，navigate 命令清除它，Page.loadEventFired 事件设置它，`wait_navigate` 命令等待它。

### 10. `connect` 后固定 `sleep` 是竞态，后续命令失败

**问题**：`connect` handler 启动 bg_thread 后 sleep 0.8s 就返回，但 bg_thread 里 `_ws` 的赋值发生在 `await websockets.connect()` 完成之后。启动时机不稳定时，0.8s 不够，导致后续 `navigate` 命令报 `"WebSocket not connected, reconnect first"`。

**修复**：cdp_send 内部自己等 `_ws` 就绪，而不是靠外部固定 sleep。具体做法：cdp_send 在发命令前轮询检查 `_ws is not None`，最多等几秒。

```python
# ❌ 错误：靠固定 sleep 赌 ws 就绪
time.sleep(0.8)
result = {"ok": True}

# ✅ 正确：cdp_send 自己等 ws 就绪
def cdp_send(method, params=None):
    if _ws is None:
        for _ in range(50):  # 最多 5s
            time.sleep(0.1)
            if _ws is not None:
                break
        else:
            raise RuntimeError("WebSocket 未在 5s 内就绪")
    # 然后再发命令...
```

**根本原因**：bg_thread 的启动和 `_ws` 的赋值都在 bg_thread 内部，主线程无法可靠地预测何时完成。正确的跨线程同步是让消费者（cdp_send）自己等资源就绪，而不是让生产者（bg_thread）通知。

```python
# navigate handler
_nav_done.clear()
await cdp_send("Page.navigate", {"url": url})

# wait_navigate handler
_nav_done.wait(timeout=timeout)

# 事件分发里
if method == "Page.loadEventFired":
    _nav_done.set()
```
