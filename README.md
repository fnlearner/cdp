# chrome-cdp

AI Agent 网络流量捕获工具 —— 通过原生 Chrome DevTools Protocol 直连浏览器，实时抓取和分析 Network 流量。

## 是什么

这是一个 Python CDP 客户端，让 AI Agent 能够在对话中直接操控浏览器、捕获任意网站的请求/响应数据。

应用场景：
- "帮我看看这个网站加载了哪些资源"
- "哪些请求返回了 404 或 500？"
- "这个页面加载最慢的 API 是哪个？"
- "抓一下登录过程中的网络请求"

## 架构

```
AI Agent  ←→  cdp.py  ←→  Chrome (CDP over WebSocket)
                      ↓
                 bg_thread: asyncio event loop
                      ↓
                 pump_thread: 结果转发到 stdout
```

两种运行模式：

**单次模式**：传入 URL → 连接 → 导航 → 捕获 → 导出 → 退出。适合一次性抓包。

**交互模式**：维护长连接 REPL，AI 可以连续发送命令（navigate、get_requests、get_response_body 等）。适合 AI 深度分析。

## 前置依赖

- Chrome / Chromium
- Python 3.12+

安装 Python 依赖：

```bash
python -m venv venv
venv/bin/pip install websockets requests
```

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

或使用脚本自动拉起：

```bash
python cdp_browser.py --port 9222
```

### 2. 单次捕获

```bash
venv/bin/python cdp.py --url "https://example.com"
```

输出：

```
 connect  →  ws://localhost:9222/devtools/page/xxx
 navigate →  https://example.com
 loading  →  ✓ 完成 (1.2s)
 
 请求列表 (1 个):
 [200]  https://example.com  (HTML,  12.3KB, 234ms)
```

导出完整 JSON：

```bash
venv/bin/python cdp.py --url "https://example.com" --export-json
```

### 3. 交互模式（AI 使用）

```bash
venv/bin/python cdp.py --interactive --port 9222
```

AI 通过 stdin 发送 JSON 命令，结果从 stdout JSON 返回：

```bash
# 连接
{"cmd": "connect", "params": {"port": 9222}}
# → {"ok": true, "ws_url": "ws://localhost:9222/devtools/page/xxx"}

# 导航
{"cmd": "navigate", "params": {"url": "https://httpbin.org/get"}}
# → {"ok": true, "note": "navigate 已提交"}

# 等待加载完成
{"cmd": "wait_navigate", "params": {"timeout": 15}}
# → {"ok": true, "note": "导航完成"}

# 获取所有请求
{"cmd": "get_requests", "params": {}}
# → {"ok": true, "requests": [...], "count": 1}

# 获取响应体
{"cmd": "get_response_body", "params": {"requestId": "xxx"}}
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `connect` | 连接浏览器调试端口 |
| `disconnect` | 断开连接 |
| `navigate` | 导航到指定 URL |
| `wait_navigate` | 等待页面加载完成（Page.loadEventFired） |
| `enable_network` / `disable_network` | 开启/关闭网络监听 |
| `get_requests` | 返回所有 Network.requestWillBeSent 事件 |
| `get_responses` | 返回所有 Network.responseReceived 事件 |
| `get_failures` | 返回所有 Network.loadingFailed 事件 |
| `get_request_body` | 获取指定请求的 POST body |
| `get_response_body` | 获取指定响应的 body（自动 Base64 解码） |
| `get_har` | 返回 HAR 格式数据 |

## 文件结构

```
chrome-cdp/
├── cdp.py              # 核心客户端（单次 + 交互两种模式）
├── cdp_browser.py      # Chrome 进程管理（自动拉起/关闭浏览器）
├── SKILL.md            # Hermes Agent 技能说明
├── DEBUG_LOG.md        # 调试结论（包含踩坑记录）
├── README.md           # 本文件
└── venv/               # Python 虚拟环境
```

## 核心设计决策

### 1. 交互模式双线程架构

```
主线程: 读 stdin → cdp_send() → 从 result_queue.get() 等待响应
bg_thread: asyncio event loop (run_forever) → 收发 WebSocket 消息
pump_thread: 从 result_queue 读结果 → 写到 stdout（daemon 线程）
```

主线程和 bg_thread 通过 `asyncio.run_coroutine_threadsafe` 通信，避免了跨线程 await 的坑。

### 2. CDP 事件订阅制

Chrome CDP 是**白名单订阅制**。不显式发送 `Network.enable` / `Page.enable`，Chrome 就会静默丢弃所有相关事件。connect 后立即订阅所有必要域。

### 3. navigate 异步等待

`Page.navigate` 只发命令不等待页面加载完成。通过 `threading.Event` 同步——navigate 命令清除事件，Page.loadEventFired 事件设置事件，wait_navigate 命令等待它。

## 调试结论

调试过程中踩过的坑记录在 [DEBUG_LOG.md](DEBUG_LOG.md)，包括：

- `run_until_complete` vs `run_forever` 的行为差异
- 跨线程 Future 的正确用法（`run_coroutine_threadsafe` + `wrap_future`）
- 全局变量在子线程里 `= {}` 会导致引用分裂
- pump_thread 高频抢 GIL 导致 bg_loop 饿死
- Queue 对象在闭包中被遮蔽导致双端通信失效

## License

MIT
