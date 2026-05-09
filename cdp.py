#!/usr/bin/env python3
"""
Chrome CDP Client — AI Agent 网络流量捕获工具
支持单次捕获（--url）和交互模式（--interactive）

交互模式架构：
  主线程 (interactive_loop)
    ├── pump_thread  ← daemon，从 result_q 读 JSON 写到 stdout
    └── bg_thread    ← daemon，跑 asyncio event loop (run_forever)
                        通过 schedule_coro() 调度协程
  cdp_send()         ← 主线程调用，从 result_q.get() 等待响应
"""

import argparse
import asyncio
import concurrent.futures
import itertools
import json
import queue
import sys
import threading
import time
import shutil
import subprocess
import urllib.request
import os
from typing import Any

try:
    import websockets
except ImportError:
    print("需要安装: pip install websockets", file=sys.stderr)
    sys.exit(1)

# ─────────────────────────────────────────────────────────
# 全局共享状态
# ─────────────────────────────────────────────────────────

_ws: websockets.WebSocketClientProtocol | None = None
_ws_url: str = ""
_cmd_id_counter = itertools.count(start=1)
_pending_futures: dict[int, concurrent.futures.Future] = {}
_events: list[dict] = []
_requests: dict[str, dict] = {}
_responses: dict[str, dict] = {}
_failures: list[dict] = []
_lock = threading.Lock()
_ws_ready = threading.Event()  # 连接就绪同步事件
_nav_done = threading.Event()
_result_queue: queue.Queue = queue.Queue()
_bg_loop: asyncio.AbstractEventLoop | None = None
_interactive = False
_exit_signal: asyncio.Event | None = None


# ─────────────────────────────────────────────────────────
# CDP 命令发送（线程安全）
# ─────────────────────────────────────────────────────────

def cdp_send(method: str, params: dict | None = None) -> Any:
    """同步发送 CDP 命令并等待响应。必须在主线程调用。"""
    if _ws is None:
        raise RuntimeError("WebSocket 未连接")
    if _bg_loop is None or _bg_loop.is_closed():
        raise RuntimeError("bg_loop is dead")

    cmd_id = next(_cmd_id_counter)
    msg = {"id": cmd_id, "method": method, "params": params or {}}
    fut = concurrent.futures.Future()

    with _lock:
        _pending_futures[cmd_id] = fut

    async def _send():
        try:
            await _ws.send(json.dumps(msg))
        except Exception as e:
            with _lock:
                if cmd_id in _pending_futures:
                    _pending_futures.pop(cmd_id).set_exception(e)

    asyncio.run_coroutine_threadsafe(_send(), _bg_loop)

    # 在主线程等待响应
    return fut.result(timeout=30)


def cdp_send_async(method: str, params: dict | None = None) -> asyncio.Future:
    """异步 CDP 命令，返回 asyncio.Future。供 bg_loop 内部协程调用。"""
    if _ws is None:
        raise RuntimeError("WebSocket 未连接")
    cmd_id = next(_cmd_id_counter)
    msg = {"id": cmd_id, "method": method, "params": params or {}}
    _ws.send(json.dumps(msg))
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    with _lock:
        _pending_futures[cmd_id] = fut
    return fut


# ─────────────────────────────────────────────────────────
# WebSocket 读取协程（在 bg_loop 中运行）
# ─────────────────────────────────────────────────────────

async def _reader_loop():
    """消费所有 WebSocket 消息，分发给 listeners。"""
    global _ws, _exit_signal
    try:
        async for raw in _ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 命令响应 → 填充 Future
            if "id" in msg:
                cmd_id = msg["id"]
                with _lock:
                    if cmd_id in _pending_futures:
                        fut = _pending_futures.pop(cmd_id)
                        loop = asyncio.get_running_loop()
                        loop.call_soon_threadsafe(fut.set_result, msg)
                        # 同时通过 result_q 通知 pump（交互模式）
                        if _interactive:
                            _result_queue.put_nowait(msg)
                        continue
            # 事件分发
            method = msg.get("method", "")
            params = msg.get("params", {})
            with _lock:
                _events.append(msg)
                if method == "Network.requestWillBeSent":
                    rid = params.get("requestId", "")
                    _requests[rid] = params
                elif method == "Network.responseReceived":
                    rid = params.get("requestId", "")
                    _responses[rid] = params
                elif method == "Network.loadingFailed":
                    _failures.append(params)
                elif method == "Network.getResponseBody":
                    rid = params.get("requestId", "")
                    _responses[f"{rid}_body"] = params
            # 导航完成
            if method == "Page.loadEventFired":
                _nav_done.set()
            # 交互模式：所有事件都经 result_q 转发
            if _interactive:
                _result_queue.put_nowait(msg)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if _exit_signal:
            _exit_signal.set()


# ─────────────────────────────────────────────────────────
# 事件循环管理
# ─────────────────────────────────────────────────────────

def _run_loop(loop: asyncio.AbstractEventLoop, url: str):
    """在独立线程中运行事件循环，直到 stop() 被调用。"""
    asyncio.set_event_loop(loop)

    async def _connect_and_read():
        global _ws
        _ws = await websockets.connect(url, max_size=10**8)
        _ws_ready.set()
        await _reader_loop()

    task = loop.create_task(_connect_and_read())
    loop.run_forever()
    # 清理
    task.cancel()
    loop.run_until_complete(asyncio.sleep(0.1))
    loop.close()
    with _lock:
        _ws = None


def schedule_coro(coro) -> concurrent.futures.Future:
    """将协程调度到 bg_loop（线程安全），异常通过 JSON 暴露给 Agent。"""
    if _bg_loop is None or _bg_loop.is_closed():
        print(json.dumps({"ok": False, "error": "bg_loop is dead"}), flush=True)
        f = concurrent.futures.Future()
        f.set_exception(RuntimeError("bg_loop is dead"))
        return f

    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)

    def _on_done(f):
        try:
            f.result()
        except Exception as e:
            import traceback
            print(json.dumps({
                "event": "InternalError",
                "error": str(e),
                "trace": traceback.format_exc()
            }, ensure_ascii=False), flush=True)

    future.add_done_callback(_on_done)
    return future


# ─────────────────────────────────────────────────────────
# 浏览器进程管理
# ─────────────────────────────────────────────────────────

def start_chrome(port: int = 9222) -> str:
    """启动 Chrome 并等待调试端口就绪。"""
    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    chrome_cmd = None
    for p in chrome_paths:
        if os.path.exists(p):
            chrome_cmd = p
            break
    if not chrome_cmd:
        chrome_cmd = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome_cmd:
        raise RuntimeError("找不到 Chrome/Chromium，请安装 Google Chrome 或 Chromium")

    profile = f"/tmp/chrome-cdp-{port}"
    os.makedirs(profile, exist_ok=True)
    subprocess.Popen(
        [chrome_cmd,
         f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}",
         "--no-first-run",
         "--silent",
         "--no-default-browser-check"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 轮询等待端口就绪（最多 20 秒）
    for i in range(40):
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1):
                return f"http://localhost:{port}"
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"Chrome 启动超时（端口 {port} 未就绪）")


def get_ws_url(cdp_host: str) -> str:
    """获取 WebSocket 调试 URL。"""
    resp = urllib.request.urlopen(f"{cdp_host}/json/list", timeout=5)
    tabs = json.loads(resp.read())
    if not tabs:
        raise RuntimeError("没有打开的标签页，请先打开一个页面")
    return tabs[0]["webSocketDebuggerUrl"]


# ─────────────────────────────────────────────────────────
# 单次捕获模式
# ─────────────────────────────────────────────────────────

async def _single_capture(url: str, ws_url: str) -> dict:
    """单次捕获：连接 → 启用 → 导航 → 等待 → 返回数据。"""
    global _ws, _ws_url, _exit_signal, _nav_done, _events, _requests, _responses, _failures
    _events.clear()
    _requests.clear()
    _responses.clear()
    _failures.clear()
    _nav_done.clear()
    _exit_signal = asyncio.Event()

    async with websockets.connect(ws_url, max_size=10**8) as ws:
        _ws = ws
        _ws_url = ws_url

        # 启用 Network + Page
        await ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.enable", "params": {}}))
        await ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Page.enable", "params": {}}))

        # 启动 reader 后台任务
        reader_task = asyncio.create_task(_reader_loop())

        # 导航
        await ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Page.navigate", "params": {"url": url}}))

        # 等待加载完成
        try:
            await asyncio.wait_for(_nav_done.wait(), timeout=15)
            loaded = True
        except asyncio.TimeoutError:
            loaded = False

        await asyncio.sleep(0.3)  # 收尾事件
        reader_task.cancel()
        _exit_signal.set()

    return {
        "url": url,
        "ws_url": ws_url,
        "requests": list(_requests.values()),
        "responses": list(_responses.values()),
        "failures": _failures[:],
        "loaded": loaded,
    }


def single_mode(url: str, port: int, auto_start: bool):
    """单次模式入口。"""
    if auto_start:
        cdp_host = start_chrome(port)
    else:
        cdp_host = f"http://localhost:{port}"

    ws_url = get_ws_url(cdp_host)
    result = asyncio.run(_single_capture(url, ws_url))

    print(f"\n=== 捕获结果 ({result['url']}) ===")
    print(f"WebSocket: {result['ws_url']}")
    print(f"加载状态: {'成功' if result['loaded'] else '超时'}")
    print(f"请求数: {len(result['requests'])}")
    print(f"响应数: {len(result['responses'])}")
    print(f"失败数: {len(result['failures'])}")

    if result["failures"]:
        print("\n--- 失败的请求 ---")
        for f in result["failures"]:
            print(f"  {f.get('request', {}).get('url', '?')}: {f.get('errorText', '?')}")

    if result["requests"]:
        print("\n--- 请求列表（按时间） ---")
        for r in result["requests"]:
            req = r.get("request", {})
            ts = req.get("timestamp", 0)
            print(f"  [{ts:.2f}] {req.get('method', '?')} {req.get('url', '?')[:90]}")

    # 导出 JSON
    out = f"/tmp/cdp_capture_{int(time.time())}.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nJSON 已导出: {out}")


# ─────────────────────────────────────────────────────────
# 交互模式
# ─────────────────────────────────────────────────────────

def pump_queue(result_q: queue.Queue):
    """pump_thread 目标函数：从 result_q 读 JSON 写到 stdout。不阻塞 bg_loop。"""
    while True:
        try:
            item = result_q.get(timeout=0.5)
            if item is None:
                break
            print(json.dumps(item, ensure_ascii=False), flush=True)
        except queue.Empty:
            continue


def interactive_loop(default_port: int):
    """交互模式主循环。"""
    global _ws, _ws_url, _bg_loop, _interactive, _exit_signal
    _interactive = True

    # 启动 pump_thread（守护线程，只负责读 result_q 写 stdout）
    pump_thread = threading.Thread(target=pump_queue, args=(_result_queue,), daemon=True)
    pump_thread.start()

    print("Chrome CDP Interactive Mode — 等待命令...", flush=True)

    while True:
        try:
            line = input()
        except (EOFError, OSError):
            break
        if not line.strip():
            continue
        try:
            cmd_obj = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": "Invalid JSON"}), flush=True)
            continue

        cmd = cmd_obj.get("cmd", "")
        params = cmd_obj.get("params", {})
        result = {"ok": False, "error": f"Unknown command: {cmd}"}

        try:
            if cmd == "connect":
                # 先彻底断开旧连接
                if _bg_loop and not _bg_loop.is_closed():
                    _bg_loop.call_soon_threadsafe(_bg_loop.stop)
                    time.sleep(0.2)
                _ws = None
                _ws_ready.clear()

                port = params.get("port", default_port)
                cdp_host = start_chrome(port)
                ws_url = get_ws_url(cdp_host)
                _ws_url = ws_url
                _exit_signal = asyncio.Event()

                # 重置全局状态
                _nav_done.clear()
                with _lock:
                    _pending_futures.clear()
                    _requests.clear()
                    _responses.clear()
                    _failures.clear()
                    _events.clear()

                # 启动 bg_loop 线程
                _bg_loop = asyncio.new_event_loop()
                bg_thread = threading.Thread(target=_run_loop, args=(_bg_loop, ws_url), daemon=True, name="bg_thread")
                bg_thread.start()

                # 等待连接真正建立（由 _connect_and_read 内部的 _ws_ready.set() 触发）
                if not _ws_ready.wait(timeout=5.0):
                    result = {"ok": False, "error": "WebSocket connection timeout"}
                else:
                    # 主动开启 CDP 域，订阅页面和网络事件
                    async def enable_domains():
                        if _ws:
                            await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.enable", "params": {}}))
                            await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Page.enable", "params": {}}))
                    schedule_coro(enable_domains())
                    result = {"ok": True, "ws_url": ws_url, "note": "connected and domains enabled"}

            elif cmd == "disconnect":
                # 彻底清理旧 bg_loop，防止僵尸 reader
                if _bg_loop and not _bg_loop.is_closed():
                    _bg_loop.call_soon_threadsafe(_bg_loop.stop)
                    time.sleep(0.2)  # 等待 loop 真正停止
                    if not _bg_loop.is_closed():
                        try:
                            _bg_loop.run_until_complete(asyncio.sleep(0))
                        except Exception:
                            pass
                _ws = None
                _nav_done.clear()
                with _lock:
                    _pending_futures.clear()
                    _requests.clear()
                    _responses.clear()
                    _failures.clear()
                    _events.clear()
                result = {"ok": True}

            elif cmd == "navigate":
                if _bg_loop is None or _bg_loop.is_closed():
                    result = {"ok": False, "error": "bg_loop is dead, reconnect first"}
                else:
                    # 自愈：如果 _ws 瞬间为 None，极短时间重试
                    for _ in range(5):
                        if _ws is not None:
                            break
                        time.sleep(0.1)
                    if _ws is None:
                        result = {"ok": False, "error": "WebSocket not connected"}
                    else:
                        url = params.get("url", "")
                        _nav_done.clear()
                        async def do_nav():
                            if _ws is None:
                                raise RuntimeError("_ws is None during navigate")
                            await _ws.send(json.dumps({
                                "id": next(_cmd_id_counter),
                                "method": "Page.navigate",
                                "params": {"url": url}
                            }))
                        schedule_coro(do_nav())
                        result = {"ok": True, "note": "navigate 已提交"}

            elif cmd == "wait_navigate":
                timeout = params.get("timeout", 15)
                done = _nav_done.wait(timeout=timeout)
                result = {"ok": done, "note": "导航完成" if done else "导航超时"}

            elif cmd == "get_requests":
                with _lock:
                    reqs = [{"requestId": k, **v} for k, v in _requests.items()]
                result = {"ok": True, "requests": reqs, "count": len(reqs)}

            elif cmd == "get_responses":
                with _lock:
                    resps = [{"requestId": k, **v} for k, v in _responses.items()
                             if not str(k).endswith("_body")]
                result = {"ok": True, "responses": resps, "count": len(resps)}

            elif cmd == "get_failures":
                with _lock:
                    result = {"ok": True, "failures": list(_failures)}

            elif cmd == "get_response_body":
                rid = params.get("requestId", "")
                with _lock:
                    body = _responses.get(f"{rid}_body")
                if body:
                    result = {"ok": True, "body": body}
                else:
                    result = {"ok": False, "error": "body not found or not fetched"}

            elif cmd == "enable_network":
                if _bg_loop and _ws:
                    async def en():
                        await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.enable", "params": {}}))
                    schedule_coro(en())
                    result = {"ok": True}
                else:
                    result = {"ok": False, "error": "not connected"}

            elif cmd == "disable_network":
                if _bg_loop and _ws:
                    async def dis():
                        await _ws.send(json.dumps({"id": next(_cmd_id_counter), "method": "Network.disable", "params": {}}))
                    schedule_coro(dis())
                    result = {"ok": True}
                else:
                    result = {"ok": False, "error": "not connected"}

            elif cmd == "exit":
                if _bg_loop and not _bg_loop.is_closed():
                    _bg_loop.call_soon_threadsafe(_bg_loop.stop)
                result = {"ok": True}
                break

            else:
                result = {"ok": False, "error": f"Unknown command: {cmd}"}

        except Exception as e:
            import traceback
            result = {"ok": False, "error": str(e), "trace": traceback.format_exc()}

        print(json.dumps(result, ensure_ascii=False), flush=True)

    print("交互模式退出", flush=True)


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Chrome CDP 网络捕获工具")
    parser.add_argument("--url", help="目标 URL（单次模式）")
    parser.add_argument("--port", type=int, default=9222, help="CDP 调试端口")
    parser.add_argument("--auto-start", action="store_true", help="自动启动 Chrome")
    parser.add_argument("--interactive", action="store_true", help="交互模式")
    args = parser.parse_args()

    if args.interactive:
        interactive_loop(args.port)
    elif args.url:
        single_mode(args.url, args.port, args.auto_start)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
