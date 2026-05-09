#!/usr/bin/env python3
"""
Chrome 浏览器进程管理
负责以调试模式启动/关闭 Chrome
"""

import subprocess
import time
import sys
import os
import json
import asyncio
import requests

CHROME_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

def find_chrome():
    """查找 Chrome 可执行文件路径。"""
    if sys.platform == "darwin" and os.path.exists(CHROME_MACOS):
        return CHROME_MACOS
    for name in ["google-chrome", "chromium", "chromium-browser", "chrome"]:
        result = subprocess.run(["which", name], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    return None

def start_chrome(
    port: int = 9222,
    user_data_dir: str = None,
    headless: bool = False,
    timeout: float = 15
) -> tuple:
    """
    启动 Chrome 调试模式，轮询等待就绪后返回 (proc, ws_url)。
    """
    chrome_path = find_chrome()
    if not chrome_path:
        raise RuntimeError("未找到 Chrome/Chromium，请先安装")

    if user_data_dir is None:
        user_data_dir = f"/tmp/chrome-cdp-profile-{port}"

    os.makedirs(user_data_dir, exist_ok=True)

    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        cmd.append("--headless=new")

    print(f"[cdp_browser] 启动 Chrome: {' '.join(cmd[:3])} ...", file=sys.stderr)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 轮询直到端口可用或超时
    ws_url = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            resp = requests.get(f"http://localhost:{port}/json/version", timeout=1)
            ws_url = resp.json().get("webSocketDebuggerUrl")
            if ws_url:
                print(f"[cdp_browser] Chrome 已就绪，WebSocket: {ws_url}", file=sys.stderr)
                return proc, ws_url
        except requests.exceptions.ConnectionError:
            # 端口还没开，继续等
            pass
        except Exception:
            # 其他错（证书、JSON解析等），也继续等
            pass

    proc.terminate()
    proc.wait(timeout=5)
    raise RuntimeError(f"Chrome 启动超时（{timeout}s），请检查端口 {port} 是否被占用")

def close_chrome(port: int = 9222):
    """
    关闭指定端口的 Chrome。
    先尝试通过 CDP Browser.close 优雅关闭，失败后强杀。
    """
    try:
        resp = requests.get(f"http://localhost:{port}/json/version", timeout=3)
        ws_url = resp.json().get("webSocketDebuggerUrl")
    except Exception:
        # 端口不可用，尝试直接杀进程
        subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True
        ).stdout.strip().split("\n")
        return

    async def _close():
        import websockets
        try:
            async with websockets.connect(ws_url, timeout=5) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            pass

    try:
        asyncio.run(_close())
    except Exception:
        pass

    # 清理残留进程
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"],
        capture_output=True, text=True
    )
    for pid in result.stdout.strip().split("\n"):
        if pid:
            try:
                os.kill(int(pid), 15)
            except (ProcessLookupError, PermissionError):
                pass

    print("[cdp_browser] Chrome 已关闭", file=sys.stderr)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Chrome 调试模式启动/关闭")
    parser.add_argument("--start", action="store_true", help="启动 Chrome")
    parser.add_argument("--stop", action="store_true", help="关闭 Chrome")
    parser.add_argument("--port", type=int, default=9222, help="调试端口")
    parser.add_argument("--user-data-dir", help="用户数据目录")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=float, default=15, help="启动超时(s)")
    args = parser.parse_args()

    if args.start:
        proc, ws_url = start_chrome(args.port, args.user_data_dir, args.headless, args.timeout)
        print(ws_url)

    elif args.stop:
        close_chrome(args.port)
        print("done")

    else:
        parser.print_help()
