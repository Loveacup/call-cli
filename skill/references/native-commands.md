# ACPX 原生操作备忘

先查执行端 `acpx --version` 和对应子命令 `--help`。命令最初在 ACPX 0.19.6 验证；当前各端版本和升级后复验范围统一读 [设备／CLI 表](devices.md)，不固定安装版本。

## 入口与权限

POSIX 用 `command -v acpx omp codex pi`，Windows 用 `Get-Command`。自定义 agent 可能通过配置中的 command/argv 定位；只读取必要字段，不输出认证配置。OMP 常用 `omp acp`。未知 agent 名不代表内置支持。

- 无工具问答：`--deny-all --no-fs --no-terminal`。
- 读文件：`--approve-reads --non-interactive-permissions deny`。
- 已授权的本地编辑／测试：`--approve-all`，任务包明确路径与交付。
- 只读模式拒绝 shell 工具时，不解释为模型能力失败；Hermes 可运行必要验证提供结果，或在原授权范围内选择合适权限。

## 命令速查

```bash
acpx --cwd /absolute/project --format json omp sessions list --local
acpx --cwd /absolute/project --format json omp sessions show session-name
acpx --cwd /absolute/project --format json omp sessions read --tail 10 session-name
acpx --cwd /absolute/project --format json omp sessions watch --name session-name --cursor cursor-value
acpx --cwd /absolute/project --format json --deny-all --timeout 120 omp exec "仅回答简短问题"
```

`--timeout` 不保证覆盖全部启动、退出或 SSH 阶段；外层等待结束后仍需查执行端。`--ttl` 控制 queue owner 空闲寿命，不是任务总时长；多轮任务不要照搬 smoke 的极短 TTL。

## Hermes 后台观察

Hermes 正常派工通过 `call_cli` 复用原生 `terminal(background=true, notify=true, heartbeat=60)`；独立短测才直接运行 prompt，并保存 process ID。通过 `process_manage` 的当前 schema 读输出，不照抄旧文档中的 `process(action=...)`。完成通知只负责唤醒，Hermes 继续验收。不要把该宿主的通知行为推定给其他 CLI。

优先看当前 prompt 流，其次原生 status/watch，再核对文件和验证结果。watch 本身可能是长进程，收尾只回收本任务创建的 watcher。无变化不定时刷屏。

## JSON 事件

- 直接 JSON-RPC 流：`event.params.update.sessionUpdate`。
- watch 的 `type=message` 将 JSON-RPC 包在 `event.message`；另有 `turn_started`、`turn_result` 和 cursor。
- `agent_message_chunk.content.text` 是片段；完整答案按序拼接，不能逐片匹配完整标记。
- `tool_call` / `tool_call_update` 提供工具身份与 pending、in_progress、completed 等状态。错误也要保留，不只统计数量。
- `usage_update` 是用量，不等于进度；结束仍检查结果和真实退出码。

需要留存时仅抽取任务身份、事件类型、工具状态、必要公开回答及最终结果；不把完整协议流长期存盘，排除 thought/reasoning、认证和原始初始化载荷。不另搭日志服务。

## 实现协作入口时的生命周期约束

- 把取消的状态读取、判定和更新放入同一事务；runner 若已认领，必须进入取消中而不是提前释放写目录，否则第二个 writer 可与旧轮重叠。
- 取消期间只记录问题，不转为可续轮状态；由 runner 确认退出后结算。所有结束写回检查认领的 turn 和允许的 state，防止迟到的旧轮覆盖新轮。
- 在事务内认领 closing 后再调用远端关闭，closing 继续保留写占用；回复不得与关闭并发续轮。关闭结果不确定时保留占用，不先写 closed。
- 让每一轮执行（包括问题回复和关闭）与 ACPX 升级共用安装级锁；执行持共享锁、升级持排他锁。把锁放在同一协调主机各 Store／profile 可共用的位置，不能只锁版本检查函数，因为回复可能在 busy 检查后启动。
- 在同一协调主机规范化 SSH user/hostname/port 作为设备键；仍用显式 resource 对齐不同地址、同步目录或未规范化的远端路径，不宣称跨协调主机已有分布式互斥。
- 单独记录原生会话是否成功创建；首次执行尚未创建就取消、后续修改任务时，不能仅因 turn 增加而跳过创建。对未知状态先对账，不用该标记推断远端已经停止。
- 解析 npm 稳定版查询时接受单个 JSON 字符串或单元素字符串数组，并验证其为一项稳定版本；其他形状保留为查询异常，不误报目标 CLI 不可用。

## SSH 跨设备

SSH 在目标设备启动 ACPX；ACPX 本身不是跨机器会话服务。

1. 读取当前 SSH 配置中的目标，使用非交互有界连接。
2. 远端发现 ACPX／CLI 和 cwd。Mac SSH 的 PATH 常需补实际 Homebrew／用户安装目录。
3. 短任务直接传字符串；长任务在授权远端目录写文件，再用 `prompt --file`。本机绝对路径不代表远端文件。
4. SSH stdout 可持续带回事件；status/watch/cancel/close 始终在同一目标执行。
5. 产物留在执行端；精确读回或下载后验收。

Windows 用 PowerShell `-NoProfile -EncodedCommand`：脚本以 UTF-16LE 编码再 base64，输出显式 UTF-8。读 JSON 用 `Get-Content -Raw -Encoding UTF8`，解析错误不复述完整配置。先 `Get-Command acpx`，不写死 npm 包内部 cli.js，也不把 POSIX 引号照搬 cmd。

## 遇到问题

- 入口缺失：核对安装／PATH，不默认安装已退役 CLI。
- 认证失败：报告该设备和调用路径，不复制其他设备凭据。
- 只有握手：核对本轮 prompt、可见输出和工具事件。
- 静默但 alive：看权限等待、外部服务和产物变化，不按固定秒数判死。
- 发送错误／断线：先核对同一会话是否收到，避免重复运行。
- 结果不够：反馈具体缺口，同任务可续接；独立审查新开会话。
