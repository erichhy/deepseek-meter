# deepseek-meter

给 DeepSeek API 加一个本地计量代理：**每次调用花了多少 token、多少钱，记在本地 CSV 里**，余额低于阈值自动弹窗提醒，进程挂了看门狗自动拉起。

DeepSeek 官网只提供按月、按 Key 的汇总用量，看不到逐条调用。而每次 API 响应体里其实都带着 `usage` 字段——这个工具就是把它截下来，按官方价格算成钱。

## 功能

- 透明代理：客户端把 `base_url` 指过来即可，请求原样转发，key 还是你自己的
- 逐次记账：模型、输入/输出 token、缓存命中 token、金额、时段，写入 `usage_log.csv`
- 自动补 `stream_options.include_usage`，让流式请求也能拿到 token 数
- 余额监控：定期查 `/user/balance`，低于阈值弹 Windows 通知
- 汇总报告：按天 × 模型统计次数、token 与金额
- 会话统计：把同一段对话的多次请求归到一个会话，看单会话累计花费、轮数、缓存命中率和上下文压缩点
- 任务视角：会话用首条消息前 30 字命名，`--status` 直接看当前任务花了多少，超阈值提示换任务
- 看门狗：代理挂掉自动重启，自带 `/__meter/health` 健康检查
- 纯标准库实现，零第三方依赖

## 工作原理

```
客户端 ──▶ 127.0.0.1:18788 ──▶ api.deepseek.com
                 │
                 └─ 解析响应里的 usage ──▶ usage_log.csv（含金额）
```

计费按官方定价表执行，空闲时段自动打 5 折：

| 模型 | 输入(缓存命中) | 输入(未命中) | 输出 |
|---|---|---|---|
| deepseek-flash | ¥0.04 | ¥2.00 | ¥8.00 |
| deepseek-v4-pro | ¥0.30 | ¥9.00 | ¥27.00 |

单位为元 / 百万 tokens，取高峰价。高峰时段为北京时间周一至周五 9:00–12:00、14:00–18:00。价格变动时改脚本里的 `PRICES` 即可。

## 快速开始

需要 Python 3.10+，无需安装任何依赖。

```bash
python deepseek_meter.py
# 计量代理已启动: http://127.0.0.1:18788/v1  ->  https://api.deepseek.com
```

把客户端的 base_url 改成 `http://127.0.0.1:18788/v1`，其它（包括 API Key）保持不变。

查看花费与余额：

```bash
python deepseek_meter.py --report             # 按天/模型汇总
python deepseek_meter.py --report --since 2026-11-01
python deepseek_meter.py --report --sessions  # 按会话汇总
python deepseek_meter.py --session S20261123  # 看单个会话的逐条明细（可只写前缀）
python deepseek_meter.py --status             # 当前任务累计消耗（判断何时换任务）
python deepseek_meter.py --balance            # 只查一次余额
```

Windows 下也可直接双击 `start_meter.bat` 启动。

## 参数

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口，默认 `127.0.0.1:18788` |
| `--upstream` | 上游地址，默认 `https://api.deepseek.com` |
| `--log` | 记账 CSV 路径，默认 `usage_log.csv` |
| `--no-inject-usage` | 不自动给流式请求补 `include_usage`，兼容个别客户端 |
| `--key` | 余额监控用的 API Key（默认读环境变量 / `key.txt` / `.env`） |
| `--threshold` | 余额低于多少元弹通知，默认 `20` |
| `--interval` | 余额查询间隔（分钟），默认 `30`，`0` 表示只在启动时查一次 |
| `--daemon-log` | 把输出追加到文件，配合 `pythonw` 做无窗口后台运行 |
| `--report` / `--since` | 打印汇总后退出，可用 `--since` 限定起始日期 |
| `--sessions` | 配合 `--report`，按会话汇总 |
| `--session` | 查看单个会话的逐条明细，会话 ID 可只写前缀 |
| `--status` | 看当前（默认 30 分钟内活跃）任务的轮数、token、累计金额 |
| `--active` | `--status` 判定「当前任务」的活跃窗口（分钟，默认 30） |
| `--session-alert` | 单会话累计多少元提醒，默认 5 |

API Key 查找顺序：`--key` 参数 → 环境变量 `DEEPSEEK_API_KEY` → 同目录 `key.txt` → 上级目录 `.env`。没有 key 时只关闭余额监控，代理本身照常工作。

## 会话怎么切分

- 同一「首条用户消息」的哈希值，且两次请求间隔不超过 **30 分钟**，算同一个会话；换话题或半小时没动静就开新会话
- 任务名取首条用户消息的前 30 个字，写进 CSV 的 `task` 列，报告里能直接看出是哪个任务在烧钱
- 会话 ID 形如 `S20261123-211045-a1b2c3`（开始时间 + 指纹前缀），只存在内存里，代理重启后会重新切分
- 某次请求的输入 token 比上一轮缩水 10% 以上，标记 `compacted=1`——基本就是客户端压缩或截断了历史
- 单个会话累计花费到 **5 元**弹一次通知（每个会话只弹一次）

CSV 在原有 12 列后面追加了 `session_id`、`turn`（会话内第几轮）、`ctx_tokens`（本次上下文大小）、`session_cost`（会话累计金额）、`compacted`、`task`（任务名）。旧版 CSV 会在下次写入时原地补列，老记录留空。

## 后台常驻（Windows）

1. **看门狗**：`watchdog.py` 每 2 分钟请求一次 `/__meter/health`，代理无响应就重新拉起，日志在 `watchdog.log`
2. **登录自启**：在启动文件夹放一个快捷方式，目标指向 `pythonw.exe`、参数为 `watchdog.py`

```powershell
$dir = "路径\deepseek-meter"
$pyw = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
$lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\DeepSeekMeter.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + $dir + '\watchdog.py"'
$s.WorkingDirectory = $dir
$s.Save()
```

看门狗巡检间隔可用 `watchdog.py --interval 分钟数` 调整。

## 文件

| 文件 | 作用 |
|---|---|
| `deepseek_meter.py` | 代理主体：转发、记账、计费、余额监控、汇总报告 |
| `watchdog.py` | 看门狗：健康检查 + 自动重启 |
| `notify.ps1` | 右下角系统通知（托盘气泡） |
| `start_meter.bat` | Windows 前台启动脚本 |

## 已知限制

- 无法判断法定节假日，节假日会按高峰价计算，结果偏高
- 只统计经过代理的调用；同一把 key 在别处直连的用量不在账上
- 金额为按定价表估算，与官方账单可能有微小差异，仅供预算参考
- 健康检查接口 `/__meter/health` 仅本机应答，不会转发到上游

## License

[MIT](LICENSE)
