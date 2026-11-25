# deepseek-meter

给大模型 API 加一个本地计量代理：**每次调用花了多少 token、多少钱，记在本地 CSV 里**，单个任务花费过多自动弹窗提醒，进程挂了看门狗自动拉起。

默认适配 DeepSeek，也能直接用在任何 OpenAI 兼容平台（Kimi、通义、智谱、硅基流动、OpenAI…），换 `--upstream` 和价格表即可。

平台官方一般只提供按天/按月、按 Key 的汇总用量，看不到逐条调用。而每次 API 响应体里其实都带着 `usage` 字段——这个工具就是把它截下来，按你配置的价格算成钱。

## 功能

- 透明代理：客户端把 `base_url` 指过来即可，请求原样转发，key 还是你自己的
- 跨平台：`--upstream` 指哪打哪，兼容 OpenAI / Anthropic / Gemini / Ollama 的 usage 字段
- 逐次记账：模型、输入/输出 token、缓存命中 token、金额、时段，写入 `usage_log.csv`
- 自动补 `stream_options.include_usage`，让流式请求也能拿到 token 数
- 余额监控：支持 DeepSeek / Moonshot(Kimi) / 硅基流动，低于阈值弹置顶通知
- 单任务提醒：花费每多一档（默认 5 元）或上下文每多一档（默认 50k tokens）弹一次置顶通知，内容含**当前余额**，并提示把工作写成任务卡、开新对话
- 汇总报告：按天 × 模型统计次数、token 与金额
- 会话统计：把同一段对话的多次请求归到一个会话，看单会话累计花费、轮数、缓存命中率和上下文压缩点
- 任务视角：会话用首条消息前 30 字命名，`--status` 直接看当前任务花了多少，超阈值提示换任务
- 看门狗：代理挂掉自动重启，自带 `/__meter/health` 健康检查
- 纯标准库实现，零第三方依赖

## 支持的平台

| 平台 | 计量（token / 金额） | 余额查询 | 说明 |
|---|---|---|---|
| DeepSeek | ✅ | ✅ `/user/balance` | 内置价、空闲半价 |
| Moonshot / Kimi | ✅ | ✅ `/v1/users/me/balance` | kimi-k2、moonshot-v1 |
| 硅基流动 | ✅ | ✅ `/v1/user/info` | 托管开源模型 |
| 通义千问（DashScope） | ✅ | ❌ 无公开接口 | 价格表已内置 qwen-* |
| 智谱 GLM | ✅ | ❌ 无公开接口 | 价格表已内置 glm-* |
| 火山方舟（豆包） | ✅ | ❌ 无公开接口 | 价格表已内置 doubao-* |
| OpenAI | ✅ | ❌ | 价格表按 USD 配置、自动折人民币 |
| Anthropic Claude | ✅ | ❌ | 流式 usage 分两次上报，已合并 |
| Google Gemini | ✅ | ❌ | 模型名在 URL 里，已自动识别 |
| 其它 OpenAI 兼容 | ✅ | ❌ | 加一行价格即可 |

“计量”对所有 HTTP API 都成立：代理只认响应里的 usage 字段；“余额查询”受各家是否开放接口限制，没开放的平台提醒里会写明“该平台没有公开的余额接口”。

## 工作原理

```
客户端 ──▶ 127.0.0.1:18788 ──▶ 上游 API（默认 api.deepseek.com）
                 │
                 └─ 解析响应里的 usage ──▶ usage_log.csv（含金额）
```

计费按 `prices.json` 里的价格表执行（单位：元 / 百万 tokens）。DeepSeek 的内置价：

| 模型 | 输入(缓存命中) | 输入(未命中) | 输出 |
|---|---|---|---|
| deepseek-flash | ¥0.04 | ¥2.00 | ¥8.00 |
| deepseek-v4-pro | ¥0.30 | ¥9.00 | ¥27.00 |

DeepSeek 空闲时段（非周一至周五 9:00–12:00、14:00–18:00）自动打 5 折，由价格表里的 `idle_factor: 0.5` 控制。其它平台不配 `idle_factor` 就不会打折。

### 价格表 prices.json

同目录的 [prices.json](prices.json) 会覆盖脚本内置价格，已预置 30+ 个主流模型（DeepSeek / Kimi / 通义 / 智谱 / 豆包 / OpenAI / Claude / Gemini），格式：

```json
{
  "usd_cny": 7.2,
  "default": {"hit": 0, "miss": 0, "out": 0},
  "models": {
    "kimi-k2": {"hit": 1.0, "miss": 4.0, "out": 16.0},
    "qwen-plus": {"hit": 0.16, "miss": 0.8, "out": 2.0},
    "deepseek-chat": "deepseek-flash",
    "gpt-4o": {"hit": 1.25, "miss": 2.5, "out": 10.0, "currency": "usd"}
  }
}
```

- 值是对象 → 价格；值是字符串 → 别名（转发给右边那个模型的价格）
- `currency: "usd"` 的条目按顶层 `usd_cny` 自动折成人民币，报表统一用元
- `idle_factor` 可选，表示非高峰时段的价格系数，不写等于 1
- 模型名支持前缀匹配，且优先匹配最长的（`gemini-2.5-flash-lite` 不会被 `gemini-2.5-flash` 抢走）
- 没配价格的模型按 `default` 估算（默认全 0，即只记 token 不算钱），控制台会提示一次
- 价格是 2026-11 的快照，各家调价后改这里即可，不用动代码

## 快速开始

需要 Python 3.10+，无需安装任何依赖。

```bash
python deepseek_meter.py
# 计量代理已启动: http://127.0.0.1:18788/v1  ->  https://api.deepseek.com
```

把客户端的 base_url 改成 `http://127.0.0.1:18788/v1`，其它（包括 API Key）保持不变。

### 用在其他平台

代理本身不认识平台，只要上游是 HTTP API 就能计量。给另一个平台再开一个实例（换端口、换日志）：

```bash
python deepseek_meter.py --port 18789 --upstream https://api.moonshot.cn/v1 \
    --log usage_kimi.csv --key sk-你的MoonshotKey
```

把对应客户端的 base_url 指到 `http://127.0.0.1:18789/v1`。价格在 `prices.json` 的 `models` 里加一行即可；余额能查的只有 DeepSeek / Moonshot / 硅基流动，其余平台加 `--no-balance`。

后台常驻时同理，看门狗也支持透传这些参数：

```bash
pythonw watchdog.py --port 18789 --upstream https://api.moonshot.cn/v1 --log usage_kimi.csv
```

注意：能计量的只有走 HTTP API 的调用。直接用官方客户端、且拿不到 token 数的场景（例如某些 IDE 自带额度）无法统计。

### 查看花费与余额

```bash
python deepseek_meter.py --report             # 按天/模型汇总
python deepseek_meter.py --report --since 2026-11-01
python deepseek_meter.py --report --sessions  # 按会话汇总
python deepseek_meter.py --session S20261123  # 看单个会话的逐条明细（可只写前缀）
python deepseek_meter.py --status             # 当前任务累计消耗（判断何时换任务）
python deepseek_meter.py --balance            # 只查一次余额（DeepSeek / Moonshot / 硅基流动）
```

Windows 下也可直接双击 `start_meter.bat` 启动。

## 参数

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口，默认 `127.0.0.1:18788` |
| `--upstream` | 上游地址，默认 `https://api.deepseek.com`，可换成任意 OpenAI 兼容平台 |
| `--prices` | 价格表 JSON，默认同目录 `prices.json` |
| `--log` | 记账 CSV 路径，默认 `usage_log.csv` |
| `--no-inject-usage` | 不自动给流式请求补 `include_usage`，兼容个别客户端 |
| `--key` | 余额监控用的 API Key（默认读环境变量 `METER_API_KEY` / `DEEPSEEK_API_KEY` / `key.txt` / `.env`） |
| `--no-balance` | 不启动余额监控（上游没有公开余额接口时） |
| `--threshold` | 余额低于多少元弹通知，默认 `20` |
| `--interval` | 余额查询间隔（分钟），默认 `30`，`0` 表示只在启动时查一次 |
| `--daemon-log` | 把输出追加到文件，配合 `pythonw` 做无窗口后台运行 |
| `--report` / `--since` | 打印汇总后退出，可用 `--since` 限定起始日期 |
| `--sessions` | 配合 `--report`，按会话汇总 |
| `--session` | 查看单个会话的逐条明细，会话 ID 可只写前缀 |
| `--status` | 看当前（默认 30 分钟内活跃）任务的轮数、token、累计金额 |
| `--active` | `--status` 判定「当前任务」的活跃窗口（分钟，默认 30） |
| `--session-alert` | 单任务每多花多少元弹一次提醒，默认 5；`0` 表示关闭 |
| `--ctx-alert` | 单任务上下文每多多少 token 提醒拆任务，默认 50000；`0` 表示关闭 |

API Key 查找顺序：`--key` 参数 → 环境变量 `METER_API_KEY` → `DEEPSEEK_API_KEY` → 同目录 `key.txt` → 上级目录 `.env`。key 只用于查 DeepSeek 余额；上游不是 DeepSeek 时不需要 key，代理本身照常工作。

## 会话怎么切分

- 同一「首条用户消息」的哈希值，且两次请求间隔不超过 **30 分钟**，算同一个会话；换话题或半小时没动静就开新会话
- 任务名取首条用户消息的前 30 个字，写进 CSV 的 `task` 列，报告里能直接看出是哪个任务在烧钱
- 会话 ID 形如 `S20261123-211045-a1b2c3`（开始时间 + 指纹前缀），只存在内存里，代理重启后会重新切分
- 某次请求的输入 token 比上一轮缩水 10% 以上，标记 `compacted=1`——基本就是客户端压缩或截断了历史
- 单个任务累计花费每跨过一档（默认 5 元）弹一次通知，比如 5、10、15 元各提醒一次
- 单个任务的上下文每跨过一档（默认 50k tokens）也会提醒：长对话里每一轮都要重发全部历史，涨到 50k、100k 后继续聊，重复计费的占比会越来越大——这时候就该把剩余目标写成任务卡、开新对话了。`--status` 会显示“重复计费倍数”= 累计输入 / 当前上下文
- 通知里带当前余额，只提示，不会自动切换
- 通知是自绘的置顶窗口（右下角，20 秒自动消失，点一下也能关），不用系统气泡——气泡会被 Windows「专注助手/勿扰」静默吞掉

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
| `deepseek_meter.py` | 代理主体：转发、记账、计费、提醒、余额监控、汇总报告 |
| `prices.json` | 价格表：模型单价、别名、兜底价 |
| `watchdog.py` | 看门狗：健康检查 + 自动重启（可透传上游/端口/价格表参数） |
| `notify.ps1` | 右下角置顶提醒窗（WinForms 自绘，不受专注助手影响） |
| `start_meter.bat` | Windows 前台启动脚本 |

## 已知限制

- 无法判断法定节假日，DeepSeek 节假日会按高峰价计算，结果偏高
- 只统计经过代理的调用；同一把 key 在别处直连的用量不在账上
- 金额为按价格表估算，与官方账单可能有微小差异，仅供预算参考
- 余额查询只有 DeepSeek / Moonshot / 硅基流动 有公开接口，其它平台请到各自控制台确认
- 健康检查接口 `/__meter/health` 仅本机应答，不会转发到上游

## License

[MIT](LICENSE)
