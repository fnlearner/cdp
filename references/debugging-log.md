# CDP 调试日志：交互模式 navigate 后 get_requests 返回空

## 问题描述

单次模式（`--url`）正常，交互模式（`--interactive`）下 `navigate` 后 `get_requests` 返回空列表。

## 根因链条（9 层）

每层独立可复现，修复顺序按发现顺序排列。

### L1: `run_until_complete` 导致 Reader 提前退出

- **现象**：`run_loop` 里的 `loop.run_until_complete(reader_coro())` 在 enable 命令（id=1,2）收到响应后立即返回
- **证据**：DEBUG 输出 `reader_coro started` 后没有后续消息；`_exit_signal.wait()` 让 `run_until_complete` 直接返回
- **修复**：`run_until_complete` → `asyncio.ensure_future(reader_coro())` + `loop.run_forever()`

### L2: 跨线程 await `concurrent.futures.Future`

- **现象**：`await _pending_futures[cmd_id]` 报错 `"'Future' object can't be awaited"`
- **根因**：`cdp_send` 用 `concurrent.futures.Future`，不能直接 `await`
- **修复**：`asyncio.wrap_future()` 转换

### L3: `asyncio.set_event_loop` 跨线程失效

- **现象**：子线程里 `"There is no current event loop in thread 'Thread-2 (run_loop)'"`
- **根因**：Python 3.10+ 每个线程必须自己调用 `set_event_loop`，主线程设置后无法传给子线程
- **修复**：将 `asyncio.set_event_loop(bg_loop)` 移入 `run_loop()` 函数内部

### L4: pump_thread 高频抢 GIL 饿死 bg_loop

- **现象**：`do_nav` 的 DEBUG 从未出现，但 bg_loop 线程存在且 `run_forever()` 确认在跑
- **证据**：加 `time.sleep(0.05)` 后 `do_nav` 开始出现
- **修复**：`queue.Empty` 分支加 `time.sleep(0.05)` 让出 GIL

### L5: 全局变量 `= {}` 在子线程创建新对象

- **现象**：命令响应永远收不到，但 loop 确实在跑
- **根因**：`run_loop` 里 `global _pending_futures; _pending_futures = {}` 创建新 dict，主线程 `cdp_send` 还在操作旧 dict
- **修复**：改用 `_pending_futures.clear()`

### L6: 函数内部赋值遮蔽外层全局变量（Queue 分裂）— **最关键**

- **现象**：所有 DEBUG 都正常，pump_thread 也在跑，但 `get_requests` 永远为空
- **根因**：connect handler 第 435 行 `result_q = queue.Queue()` 创建新的局部 Queue，遮蔽外层 `_result_queue` 全局变量。pump_thread 绑定外层 queue（空），bg_thread 写新 queue（数据在里面），两者操作不同对象。
- **证据**：多线程模拟验证 `id(outer_queue) != id(new_queue)`
- **修复**：删除 `result_q = queue.Queue()` 这一行

### L7: `ws.close()` 用的是局部变量

- **修复**：统一用全局 `_ws`

### L8: CLI `--port` 参数未传递到 connect handler

- **修复**：`interactive_loop(default_port)` + `params.get("port", default_port)`

### L9: `wait_navigate` 无同步机制

- **修复**：引入 `threading.Event _nav_done`，navigate 清除、Page.loadEventFired 设置、wait_navigate 等待

## 验证方法

```bash
# 启动 Chrome
~/.hermes/skills/software-development/chrome-cdp/venv/bin/python \
  ~/.hermes/skills/software-development/chrome-cdp/cdp_browser.py --port 9223

# 测试交互模式
printf '%s\n' \
  '{"cmd":"connect","params":{"port":9223}}' \
  '{"cmd":"navigate","params":{"url":"https://httpbin.org/get"}}' \
  '{"cmd":"wait_navigate","params":{"timeout":10}}' \
  '{"cmd":"get_requests","params":{}}' \
  | ~/.hermes/skills/software-development/chrome-cdp/venv/bin/python \
      ~/.hermes/skills/software-development/chrome-cdp/cdp.py --interactive
```

## 最小化复现验证

`run_coroutine_threadsafe + run_forever` 组合在干净环境下正常工作：

```python
import asyncio, threading, time, queue

bg_loop = asyncio.new_event_loop()
q = queue.Queue()

def pump():
    while True:
        x = q.get(timeout=0.5)
        print(f"GOT: {x}")

async def task():
    await asyncio.sleep(0.1)
    asyncio.run_coroutine_threadsafe(q.put, "from-bg", bg_loop)
    return 42

t = threading.Thread(target=lambda: pump(), daemon=True)
t.start()

asyncio.set_event_loop(bg_loop)
asyncio.ensure_future(task())
bg_loop.run_forever()
# 输出: GOT: from-bg ✓
```

## 关键教训

- Python 线程 + asyncio 的组合里，GIL 争用是真实存在的——pump 线程每 300ms 的 `queue.get` 醒来就能打断 bg_loop 的协程调度
- 全局变量在子线程里重新赋值（`=`）会创建新对象，主线程持有旧引用——这是最隐蔽的引用分裂 bug
- Queue 对象同样适用：函数内部 `x = queue.Queue()` 会遮蔽外层同名词，导致生产者和消费者绑定不同对象
