#!/usr/bin/env python3
"""DeepSeek 计量代理看门狗。

每 N 分钟探一次本地端口，进程掉了就重新拉起。
开机自启的快捷方式指向本脚本，由它负责把 deepseek_meter.py 拉起来。

用法:
    pythonw watchdog.py                 # 后台守护（自启用这个）
    python watchdog.py --interval 0.2   # 测试用：12 秒探一次
"""

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
METER = os.path.join(BASE_DIR, "deepseek_meter.py")
METER_LOG = os.path.join(BASE_DIR, "meter.log")
WATCH_LOG = os.path.join(BASE_DIR, "watchdog.log")


def log(msg: str) -> None:
    with open(WATCH_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def alive(port: int, timeout: float = 2.0) -> bool:
    """走代理自己的健康接口判断，避免把别的程序占着端口当成“活着”。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/__meter/health")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status == 200 and b'"ok"' in body and b"true" in body
    except Exception:  # noqa: BLE001
        return False


def pythonw() -> str:
    """始终用 pythonw 拉子进程，避免弹黑窗口。"""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def start_meter(port: int) -> None:
    args = [pythonw(), METER, "--daemon-log", METER_LOG]
    if port != 18788:
        args += ["--port", str(port)]
    subprocess.Popen(args, cwd=BASE_DIR,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log(f"已发起启动代理（端口 {port}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek 计量代理看门狗")
    ap.add_argument("--port", type=int, default=18788)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="巡检间隔，单位分钟（默认 2）")
    args = ap.parse_args()
    interval = max(args.interval, 0.05)

    log(f"看门狗启动：端口 {args.port}，每 {interval:g} 分钟巡检")
    while True:
        if not alive(args.port):
            time.sleep(3)  # 先等 3 秒复查，排除偶发卡顿
            if alive(args.port):
                log("出现短暂无响应，已自行恢复")
            else:
                log(f"端口 {args.port} 无响应，重新拉起")
                start_meter(args.port)
                time.sleep(15)
                log("拉起结果：" + ("正常" if alive(args.port) else "仍无响应，下一轮再试"))
        time.sleep(interval * 60)


if __name__ == "__main__":
    sys.exit(main())
