#!/usr/bin/env python3
"""DeepSeek 本地计量代理。

把客户端的 base_url 指到 http://127.0.0.1:18788/v1，其它照旧（key 还是你自己的）。
代理把请求原样转发到 api.deepseek.com，顺手把每次响应里的 usage（token 用量）
记进 usage_log.csv，并按官方价格算出这一次花了多少钱。

用法:
    python deepseek_meter.py                          # 启动代理（含余额监控）
    python deepseek_meter.py --port 9000              # 换端口
    python deepseek_meter.py --threshold 50 --interval 10   # 低于 50 元提醒，每 10 分钟查一次
    python deepseek_meter.py --balance                # 只查一次余额，看脚本是否正常
    python deepseek_meter.py --report                 # 看汇总
    python deepseek_meter.py --report --since 2026-11-01
    python deepseek_meter.py --report --sessions      # 按会话汇总
    python deepseek_meter.py --session S20261123      # 看单个会话的逐条明细
    python deepseek_meter.py --status                 # 当前任务累计消耗
    pythonw deepseek_meter.py --daemon-log meter.log  # 无窗口后台运行（开机自启用）
"""

import argparse
import csv
import datetime as dt
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(BASE_DIR, "usage_log.csv")
DEFAULT_UPSTREAM = "https://api.deepseek.com"
NOTIFY_PS1 = os.path.join(BASE_DIR, "notify.ps1")

# 单价（元 / 百万 tokens），取高峰价；空闲时段官方打 5 折。
PRICES = {
    "deepseek-flash": {"hit": 0.04, "miss": 2.0, "out": 8.0},
    "deepseek-v4-pro": {"hit": 0.30, "miss": 9.0, "out": 27.0},
}
# 旧模型名映射：DeepSeek 已下线这些名字，请求会被路由到新模型。
ALIASES = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}

FIELDS = [
    "ts", "model", "endpoint", "stream", "period",
    "prompt_tokens", "cache_hit", "cache_miss", "completion_tokens",
    "total_tokens", "cost_cny", "status",
    "session_id", "turn", "ctx_tokens", "session_cost", "compacted", "task",
]

# 会话切分：同一「首条消息指纹」且请求间隔不超过 30 分钟，算同一个会话。
SESSION_GAP_SEC = 30 * 60
# 单个会话累计花费到这个数就弹一次通知。
SESSION_ALERT_CNY = 5.0

_lock = threading.Lock()


# ---------- 计费 ----------
def is_peak(now: dt.datetime) -> bool:
    """高峰时段：北京时间周一至周五 9:00-12:00、14:00-18:00（不算法定节假日）。"""
    if now.weekday() >= 5:
        return False
    h = now.hour + now.minute / 60
    return (9 <= h < 12) or (14 <= h < 18)


def price_of(model: str) -> dict:
    model = (model or "").strip().lower()
    return PRICES.get(ALIASES.get(model, model), PRICES["deepseek-flash"])


def calc_cost(model: str, hit: int, miss: int, out: int, peak: bool) -> float:
    p = price_of(model)
    factor = 1.0 if peak else 0.5
    cost = (hit * p["hit"] + miss * p["miss"] + out * p["out"]) / 1_000_000
    return round(cost * factor, 6)


# ---------- usage 解析 ----------
def pick_usage(obj) -> dict | None:
    """从响应对象里取 usage，兼容 OpenAI / Responses / Anthropic 三种字段名。"""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    prompt = u.get("prompt_tokens", u.get("input_tokens"))
    comp = u.get("completion_tokens", u.get("output_tokens"))
    if prompt is None and comp is None:
        return None

    hit = u.get("prompt_cache_hit_tokens")
    miss = u.get("prompt_cache_miss_tokens")
    if hit is None:
        details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        hit = details.get("cached_tokens")
    if hit is None:
        hit = u.get("cache_read_input_tokens")
    return {
        "prompt": prompt or 0,
        "comp": comp or 0,
        "total": u.get("total_tokens") or (prompt or 0) + (comp or 0),
        "hit": hit,
        "miss": miss,
    }


def _migrate_log(log_path: str) -> None:
    """老版本 CSV 只有 12 列，就地补上新列（旧行留空），避免表头错位。"""
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    tmp = log_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDS})
    os.replace(tmp, log_path)


def log_row(log_path: str, row: dict) -> None:
    with _lock:
        new = not os.path.exists(log_path) or os.path.getsize(log_path) == 0
        if not new:
            with open(log_path, encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if header != FIELDS:
                _migrate_log(log_path)
        with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)


_sessions = {}  # 首条消息指纹 -> 当前会话状态


def fingerprint_of(obj) -> str:
    """取首条 user 消息的哈希，作为「是否同一个会话」的判据；取不到返回空串。"""
    msgs = obj.get("messages") if isinstance(obj, dict) else None
    if not isinstance(msgs, list):
        return ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            if isinstance(content, str) and content.strip():
                return hashlib.sha1(content.strip().encode("utf-8")).hexdigest()[:8]
    return ""


def update_session(fp: str, now: dt.datetime, ctx_tokens: int, cost: float, label: str = ""):
    """返回 (会话ID, 第几轮, 会话累计金额, 是否压缩, 是否需要提醒, 任务名)。"""
    with _lock:
        st = _sessions.get(fp)
        if st is None or (now - st["last"]).total_seconds() > SESSION_GAP_SEC:
            st = {
                "id": f"S{now:%Y%m%d-%H%M%S}-{fp[:6] or 'anon'}",
                "turn": 0,
                "prev": 0,
                "cost": 0.0,
                "alerted": False,
                "last": now,
                "label": label,
            }
            _sessions[fp] = st
        st["turn"] += 1
        # 输入 token 比上一轮缩水超过 10%，基本就是客户端把历史压缩/截断了
        compacted = 1 if st["prev"] and ctx_tokens < st["prev"] * 0.9 else 0
        st["prev"] = ctx_tokens
        st["cost"] = round(st["cost"] + cost, 6)
        st["last"] = now
        alert = st["cost"] >= SESSION_ALERT_CNY and not st["alerted"]
        if alert:
            st["alerted"] = True
        return st["id"], st["turn"], st["cost"], compacted, alert, st["label"]


def label_of(obj) -> str:
    """取首条 user 消息的前 30 个字，作为会话（任务）名，方便报告里认。"""
    msgs = obj.get("messages") if isinstance(obj, dict) else None
    if not isinstance(msgs, list):
        return ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(
                    str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
            if isinstance(content, str) and content.strip():
                return " ".join(content.split())[:30]
    return ""


def record(log_path: str, model: str, endpoint: str, stream: bool,
           usage: dict, status: int, fp: str = "", label: str = "") -> None:
    now = dt.datetime.now()
    peak = is_peak(now)
    hit = usage["hit"]
    miss = usage["miss"]
    if hit is None:
        hit = 0
    if miss is None:
        miss = max(usage["prompt"] - hit, 0)
    cost = calc_cost(model, hit, miss, usage["comp"], peak)
    sid, turn, sess_cost, compacted, alert, task = update_session(
        fp, now, usage["prompt"], cost, label)
    row = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model or "?",
        "endpoint": endpoint,
        "stream": int(bool(stream)),
        "period": "peak" if peak else "idle",
        "prompt_tokens": usage["prompt"],
        "cache_hit": hit,
        "cache_miss": miss,
        "completion_tokens": usage["comp"],
        "total_tokens": usage["total"],
        "cost_cny": cost,
        "status": status,
        "session_id": sid,
        "turn": turn,
        "ctx_tokens": usage["prompt"],
        "session_cost": sess_cost,
        "compacted": compacted,
        "task": task,
    }
    log_row(log_path, row)
    print(f"[{row['ts']}] {row['model']}  in={usage['prompt']} (hit {hit}) "
          f"out={usage['comp']}  ¥{cost:.4f}  {row['period']}", flush=True)
    if alert:
        notify("会话花费提醒",
               f"{sid} 已累计 {sess_cost:.2f} 元（{turn} 次请求）")


# ---------- 余额监控 ----------
def load_api_key(cli_key: str | None) -> str:
    """按优先级找 key：命令行 > 环境变量 > 工具目录 key.txt > 项目 .env。"""
    if cli_key:
        return cli_key.strip()
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()
    candidates = [
        os.path.join(BASE_DIR, "key.txt"),
        os.path.join(BASE_DIR, ".env"),
        os.path.join(BASE_DIR, "..", "..", ".env"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip("'\"")
                    if value and not value.startswith("sk-在这里"):
                        return value
    return ""


def fetch_balance(key: str, upstream: str = DEFAULT_UPSTREAM) -> dict:
    up = urlsplit(upstream)
    use_tls = up.scheme == "https"
    cls = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
    conn = cls(up.hostname, up.port or (443 if use_tls else 80), timeout=15)
    try:
        conn.request("GET", up.path.rstrip("/") + "/user/balance",
                     headers={"Authorization": f"Bearer {key}",
                              "Accept": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data[:120].decode('utf-8', 'replace')}")
        obj = json.loads(data)
    finally:
        conn.close()

    infos = obj.get("balance_infos") or []
    cny = next((i for i in infos if i.get("currency") == "CNY"), infos[0] if infos else {})
    return {
        "available": obj.get("is_available"),
        "total": float(cny.get("total_balance") or 0),
        "granted": float(cny.get("granted_balance") or 0),
        "topped": float(cny.get("topped_up_balance") or 0),
    }


def notify(title: str, text: str) -> None:
    """右下角系统通知；脚本缺失就退化成控制台输出。"""
    if not os.path.exists(NOTIFY_PS1):
        print(f"[提醒] {title}：{text}", flush=True)
        return
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", NOTIFY_PS1,
             "-Title", title, "-Text", text],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        print(f"[提醒] {title}：{text}（通知没弹出来: {exc}）", flush=True)


def watch_balance(key: str, upstream: str, threshold: float, interval_min: float) -> None:
    """后台线程：定期查余额，低于阈值弹通知（同一次低余额最多 6 小时提醒一次）。"""
    last_alert = 0.0
    while True:
        try:
            b = fetch_balance(key, upstream)
            print(f"[余额] {b['total']:.2f} 元（赠金 {b['granted']:.2f} / 充值 {b['topped']:.2f}）",
                  flush=True)
            if b["total"] < threshold and time.time() - last_alert > 6 * 3600:
                notify("DeepSeek 余额不足",
                       f"当前 {b['total']:.2f} 元，低于 {threshold:.0f} 元，记得充值")
                last_alert = time.time()
        except Exception as exc:  # noqa: BLE001
            print(f"[余额] 查询失败：{exc}", flush=True)
        if interval_min <= 0:
            return
        time.sleep(interval_min * 60)


# ---------- 请求处理 ----------
def prepare_body(body: bytes, inject: bool):
    """解析请求体，必要时给流式请求补上 include_usage，保证能拿到 token 数。"""
    model, stream, fp, label = "", False, "", ""
    if not body:
        return body, model, stream, fp, label
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, model, False, fp, label
    if not isinstance(obj, dict):
        return body, model, False, fp, label

    model = obj.get("model") or ""
    stream = bool(obj.get("stream"))
    fp = fingerprint_of(obj)
    label = label_of(obj)
    if stream and inject and "stream_options" not in obj:
        obj["stream_options"] = {"include_usage": True}
        return json.dumps(obj, ensure_ascii=False).encode("utf-8"), model, stream, fp, label
    return body, model, stream, fp, label


class MeterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DeepSeekMeter/1.0"

    # 这些由主流程设置
    log_path = DEFAULT_LOG
    upstream = urlsplit(DEFAULT_UPSTREAM)
    inject_usage = True

    def log_message(self, fmt, *args):
        pass  # 静音默认访问日志

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def _proxy(self):
        if self.path.startswith("/__meter/health"):
            data = json.dumps({"ok": True,
                               "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}).encode()
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        body, model_hint, is_stream_req, fp, label = prepare_body(body, self.inject_usage)

        headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "accept-encoding",
                             "connection", "transfer-encoding"):
                continue
            headers[k] = v
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))

        up = self.upstream
        use_tls = up.scheme == "https"
        conn_cls = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
        conn = conn_cls(up.hostname, up.port or (443 if use_tls else 80), timeout=600)
        path = up.path.rstrip("/") + self.path if up.path else self.path

        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            self.send_error(502, f"upstream error: {exc}")
            print(f"[!] 连不上上游: {exc}", flush=True)
            return

        ctype = resp.getheader("Content-Type", "") or ""
        is_sse = "event-stream" in ctype.lower()

        self.close_connection = True
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "content-length",
                             "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        raw = bytearray()
        scan = b""
        last_usage = None
        resp_model = ""

        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                print("[!] 客户端提前断开，继续读完响应以便记账", flush=True)

            if is_sse:
                scan += chunk
                *lines, scan = scan.split(b"\n")
                for line in lines:
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]" or b'"usage"' not in payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    u = pick_usage(obj)
                    if u:
                        last_usage = u
                    if not resp_model and isinstance(obj.get("model"), str):
                        resp_model = obj["model"]
            elif len(raw) < 16_000_000:
                raw += chunk

        conn.close()

        usage = last_usage
        if not is_sse and raw:
            try:
                obj = json.loads(raw)
                usage = pick_usage(obj) or usage
                if isinstance(obj, dict) and isinstance(obj.get("model"), str):
                    resp_model = obj["model"]
            except json.JSONDecodeError:
                pass

        model = resp_model or model_hint or "?"
        if usage and resp.status < 400:
            record(self.log_path, model, self.command + " " + self.path,
                   is_stream_req or is_sse, usage, resp.status, fp, label)
        elif resp.status >= 400:
            print(f"[!] {resp.status} {self.path}，未记账", flush=True)
        elif is_stream_req:
            print("[!] 流式响应没带 usage（客户端可能覆盖了 stream_options），本次未记账",
                  flush=True)


class MeterServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许两个进程绑同一端口，关掉它避免跑出重复实例
    allow_reuse_address = False


# ---------- 汇总报告 ----------
def report(log_path: str, since: str | None) -> int:
    if not os.path.exists(log_path):
        print(f"还没有记录文件：{log_path}")
        return 1
    agg = {}
    total = {"n": 0, "cost": 0.0, "in": 0, "out": 0}
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if since and row["ts"][:10] < since:
                continue
            key = (row["ts"][:10], row["model"])
            a = agg.setdefault(key, {"n": 0, "in": 0, "hit": 0, "out": 0, "cost": 0.0})
            a["n"] += 1
            a["in"] += int(row["prompt_tokens"])
            a["hit"] += int(row["cache_hit"])
            a["out"] += int(row["completion_tokens"])
            a["cost"] += float(row["cost_cny"])
            total["n"] += 1
            total["in"] += int(row["prompt_tokens"])
            total["out"] += int(row["completion_tokens"])
            total["cost"] += float(row["cost_cny"])

    if not agg:
        print("该范围内没有记录")
        return 0

    print(f"{'日期':<12}{'模型':<20}{'次数':>6}{'输入tok':>12}{'命中tok':>11}"
          f"{'输出tok':>12}{'金额(元)':>12}")
    print("-" * 85)
    for (day, model), a in sorted(agg.items()):
        print(f"{day:<12}{model:<20}{a['n']:>6}{a['in']:>12,}{a['hit']:>11,}"
              f"{a['out']:>12,}{a['cost']:>12.4f}")
    print("-" * 85)
    print(f"{'合计':<32}{total['n']:>6}{total['in']:>12,}{'':>11}"
          f"{total['out']:>12,}{total['cost']:>12.4f}")
    return 0


def report_sessions(log_path: str, since: str | None) -> int:
    """按会话汇总：每个会话一共花了多少、聊了多少轮、缓存命中率、压缩过几次。"""
    if not os.path.exists(log_path):
        print(f"还没有记录文件：{log_path}")
        return 1
    agg = {}
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if since and row["ts"][:10] < since:
                continue
            sid = row.get("session_id") or "(旧记录)"
            a = agg.setdefault(sid, {"start": row["ts"], "end": row["ts"], "n": 0, "task": "",
                                     "in": 0, "hit": 0, "out": 0, "cost": 0.0, "comp": 0})
            if not a["task"] and row.get("task"):
                a["task"] = row["task"]
            a["start"] = min(a["start"], row["ts"])
            a["end"] = max(a["end"], row["ts"])
            a["n"] += 1
            a["in"] += int(row["prompt_tokens"] or 0)
            a["hit"] += int(row.get("cache_hit") or 0)
            a["out"] += int(row.get("completion_tokens") or 0)
            a["cost"] += float(row.get("cost_cny") or 0)
            a["comp"] += int(row.get("compacted") or 0)
    if not agg:
        print("该范围内没有记录")
        return 0

    print(f"{'会话ID':<26}{'任务':<20}{'开始':<20}{'时长':>6}{'次数':>5}{'输入tok':>12}"
          f"{'命中率':>8}{'输出tok':>10}{'金额(元)':>11}{'压缩':>5}")
    print("-" * 123)
    grand = 0.0
    for sid, a in sorted(agg.items(), key=lambda kv: kv[1]["start"]):
        fmt = "%Y-%m-%d %H:%M:%S"
        mins = (dt.datetime.strptime(a["end"], fmt)
                - dt.datetime.strptime(a["start"], fmt)).total_seconds() / 60
        rate = a["hit"] / a["in"] * 100 if a["in"] else 0
        task = (a["task"] or "")[:18]
        print(f"{sid:<26}{task:<20}{a['start']:<20}{mins:>5.0f}分{a['n']:>5}{a['in']:>12,}"
              f"{rate:>7.0f}%{a['out']:>10,}{a['cost']:>11.4f}{a['comp']:>5}")
        grand += a["cost"]
    print("-" * 123)
    print(f"{'合计 ' + str(len(agg)) + ' 个会话':<88}{grand:>11.4f}")
    return 0


def status(log_path: str, active_min: float = 30.0) -> int:
    """看最近一个/几个任务的累计消耗，用来判断该不该换任务。"""
    if not os.path.exists(log_path):
        print("还没有记录文件：没有请求走过代理")
        return 1
    agg = {}
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = row.get("session_id") or ""
            if not sid:
                continue
            a = agg.setdefault(sid, {"last": row["ts"], "task": "", "turn": 0,
                                     "in": 0, "out": 0, "cost": 0.0})
            a["last"] = row["ts"]
            if not a["task"] and row.get("task"):
                a["task"] = row["task"]
            a["turn"] = int(row.get("turn") or a["turn"])
            a["in"] += int(row["prompt_tokens"] or 0)
            a["out"] += int(row.get("completion_tokens") or 0)
            a["cost"] += float(row.get("cost_cny") or 0)
    if not agg:
        print("还没有会话记录")
        return 0

    now = dt.datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    alive = []
    for sid, a in agg.items():
        gap = (now - dt.datetime.strptime(a["last"], fmt)).total_seconds() / 60
        a["gap"] = gap
        if gap <= active_min:
            alive.append((sid, a))
    shown = alive or [max(agg.items(), key=lambda kv: kv[1]["last"])]

    print(f"{'任务':<22}{'会话ID':<26}{'轮数':>5}{'输入tok':>12}{'输出tok':>10}"
          f"{'累计(元)':>10}{'最后活跃':>12}")
    print("-" * 100)
    for sid, a in sorted(shown, key=lambda kv: kv[1]["last"]):
        task = (a["task"] or "(无任务名)")[:20]
        print(f"{task:<22}{sid:<26}{a['turn']:>5}{a['in']:>12,}{a['out']:>10,}"
              f"{a['cost']:>10.4f}{a['gap']:>10.0f}分")
    print("-" * 100)
    if not alive:
        print(f"最近 {active_min:.0f} 分钟内没有活动会话，上面是最后一个")
    for sid, a in shown:
        if a["cost"] >= SESSION_ALERT_CNY:
            print(f"⚠ {a['task'] or sid} 已花 {a['cost']:.2f} 元，"
                  f"超过 {SESSION_ALERT_CNY:.0f} 元，可以考虑换任务")
    return 0


def session_detail(log_path: str, sid_prefix: str) -> int:
    """打印某个会话的逐条明细，会话ID支持只写前缀。"""
    if not os.path.exists(log_path):
        print(f"还没有记录文件：{log_path}")
        return 1
    rows = []
    with open(log_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sid = row.get("session_id") or ""
            if sid and sid.startswith(sid_prefix):
                rows.append(row)
    if not rows:
        print(f"没找到会话：{sid_prefix}（可以用 --report --sessions 看有哪些）")
        return 1

    sids = sorted({r["session_id"] for r in rows})
    if len(sids) > 1:
        print(f"前缀 {sid_prefix} 匹配到 {len(sids)} 个会话，请多写几位：")
        for s in sids[:10]:
            print("  " + s)
        return 1

    sid = rows[0]["session_id"]
    print(f"会话 {sid}（{len(rows)} 次请求）")
    print(f"{'时间':<20}{'轮':>3} {'模型':<20}{'上下文tok':>11}{'命中tok':>10}"
          f"{'输出tok':>9}{'本轮(元)':>10}{'累计(元)':>10}{'压缩':>5}")
    print("-" * 99)
    for row in rows:
        print(f"{row['ts']:<20}{int(row.get('turn') or 0):>3} {row['model']:<20}"
              f"{int(row['prompt_tokens'] or 0):>11,}{int(row.get('cache_hit') or 0):>10,}"
              f"{int(row.get('completion_tokens') or 0):>9,}"
              f"{float(row.get('cost_cny') or 0):>10.4f}"
              f"{float(row.get('session_cost') or 0):>10.4f}"
              f"{'是' if int(row.get('compacted') or 0) else '':>5}")
    print("-" * 99)
    ctx = max(int(r["prompt_tokens"] or 0) for r in rows)
    print(f"会话累计 {float(rows[-1].get('session_cost') or 0):.4f} 元，"
          f"最大上下文 {ctx:,} tok")
    return 0


def main() -> int:
    global SESSION_ALERT_CNY
    ap = argparse.ArgumentParser(description="DeepSeek 本地计量代理")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址")
    ap.add_argument("--port", type=int, default=18788, help="监听端口（默认 18788）")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="上游地址")
    ap.add_argument("--log", default=DEFAULT_LOG, help="CSV 记录文件")
    ap.add_argument("--no-inject-usage", action="store_true",
                    help="不自动给流式请求补 include_usage")
    ap.add_argument("--report", action="store_true", help="打印汇总后退出")
    ap.add_argument("--sessions", action="store_true", help="配合 --report，按会话汇总")
    ap.add_argument("--session", help="查看单个会话的逐条明细（可只写前缀）")
    ap.add_argument("--status", action="store_true", help="看当前任务的累计消耗（判断何时换任务）")
    ap.add_argument("--active", type=float, default=30.0,
                    help="--status 里多久没活动就不算当前任务（分钟，默认 30）")
    ap.add_argument("--session-alert", type=float, default=5.0,
                    help="单会话累计多少元提醒，默认 5")
    ap.add_argument("--since", help="汇总起始日期，如 2026-11-01")
    ap.add_argument("--key", help="DeepSeek API Key（默认读环境变量/key.txt/项目 .env）")
    ap.add_argument("--balance", action="store_true", help="只查一次余额然后退出")
    ap.add_argument("--threshold", type=float, default=20.0,
                    help="余额低于多少元弹通知（默认 20）")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="每隔多少分钟查一次余额，0 表示只在启动时查一次（默认 30）")
    ap.add_argument("--daemon-log", help="把输出追加到这个文件（pythonw 无窗口时用）")
    args = ap.parse_args()

    if args.daemon_log:
        # pythonw 没有控制台，先接管 stdout/stderr，后面所有 print 才有地方去
        log_file = open(args.daemon_log, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log_file
        print(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====", flush=True)

    if args.session:
        return session_detail(args.log, args.session)

    SESSION_ALERT_CNY = args.session_alert

    if args.status:
        return status(args.log, args.active)

    if args.report:
        if args.sessions:
            return report_sessions(args.log, args.since)
        return report(args.log, args.since)

    key = load_api_key(args.key)

    if args.balance:
        if not key:
            print("没找到 API Key：可用 --key 传入，或设置环境变量 DEEPSEEK_API_KEY")
            return 1
        b = fetch_balance(key, args.upstream)
        print(f"可用: {'是' if b['available'] else '否'}")
        print(f"总余额 {b['total']:.2f} 元（赠金 {b['granted']:.2f} / 充值 {b['topped']:.2f}）")
        return 0

    MeterHandler.log_path = args.log
    MeterHandler.upstream = urlsplit(args.upstream)
    MeterHandler.inject_usage = not args.no_inject_usage

    try:
        srv = MeterServer((args.host, args.port), MeterHandler)
    except OSError as exc:
        print(f"[x] 端口 {args.port} 绑不上（可能已被占用）：{exc}")
        return 5
    srv.daemon_threads = True
    print(f"计量代理已启动: http://{args.host}:{args.port}/v1  ->  {args.upstream}")
    print(f"记录文件: {args.log}")
    if key:
        threading.Thread(target=watch_balance,
                         args=(key, args.upstream, args.threshold, args.interval),
                         daemon=True).start()
        every = f"每 {args.interval:.0f} 分钟查一次" if args.interval > 0 else "仅启动时查一次"
        print(f"余额监控已开启：低于 {args.threshold:.0f} 元弹通知，{every}")
    else:
        print("余额监控未开启：没找到 API Key（可设置环境变量 DEEPSEEK_API_KEY）")
    print("把客户端的 base_url 指到上面的地址即可，Ctrl+C 停止。", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
