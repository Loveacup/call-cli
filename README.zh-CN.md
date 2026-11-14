<div align="center">

<img src="docs/assets/call-cli-banner.png" alt="Call CLI — 跨 CLI 与跨设备协作，连接 OMP、Codex、Pi 与 AGY（可选接入 Hermes）" width="100%">

**给你的 CLI 一个搭档，而不只是另一个子进程。**

<a href="README.md">English</a> · <a href="README.zh-CN.md"><strong>简体中文</strong></a>

<a href="https://github.com/openclaw/acpx"><img src="https://img.shields.io/badge/transport-ACPX%200.22%2B-49c5b6?style=flat-square&amp;labelColor=161b22" alt="基于 ACPX Shared Runtime"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8cbaff?style=flat-square&amp;labelColor=161b22" alt="MIT 许可证"></a>

[快速开始](#快速开始) · [选择接入方式](#选择接入方式) · [核心能力](#核心能力) · [架构与协作周期](#工作原理) · [证据分层与边界](#证据与边界) · [文档导览](#文档)

</div>

---

Call CLI 通过轻量 POSIX 协调层连接 **OMP、Codex、Pi 和 AGY**。新建协作会话、在发起端持续接收阶段进展与事件回流、按编号回答问题、按任务固定传递技能，并通过显式的**取消并同会话续轮（`intervene`，`cancel+resume`）**在执行期调整方向——**既能在本机运行，也能通过你现有的 SSH 跨设备协作。**

**明确边界（不做什么）：** 不强制依赖 Hermes，不新增常驻后台服务或守护进程，不把取消续轮（`cancel+resume`）包装成执行期无损热插（`steer`），也不接管你已经手动打开的终端、TUI 或桌面 GUI 会话。

## 为什么需要 Call CLI

你在一个 CLI 里规划，让同机或 SSH 远端机器上的另一个 agent 实现，再并行交给第三个 agent 审查。阶段进展、编号问答、固定技能包和中途改向都通过发起端 CLI 持续往返，不必在多个终端窗口之间手工复制粘贴。

| | 能力 | 带来的变化 |
| :-- | :-- | :-- |
| 🔄 | **持续跨 CLI 协作与回流** | 在发起端持续跟进可续读的阶段进展、待答问题与最终结果，用 600 秒协调者复核检查点代替僵硬的模型执行超时。 |
| 🧭 | **单次控制与 `cancel+resume` 改向** | 用 `reply` 按编号回答问题，或用 `intervene` 在同一目标会话内取消当前轮、确认结算后按新要求续轮（绝不冒充执行期热插 `steer`）。 |
| 🤖 | **原生 AGY 与显式模型模式** | 新 AGY 默认 **ocs**（Orchestrator Carbon＋native Swarm）。裸简称 **oc/oa/4c/4a/4h** 全部单代理 `off`；后缀 **s** 为 Swarm、**t** 为按广告原生命令启动的 Teamwork 源码候选；真实 Teamwork 业务验收未完成。 |
| 🤝 | **多 CLI 并行协作与写隔离** | 让 OMP、Codex、Pi、AGY 在各自隔离的会话中并行工作，由单写者工作区锁防止并发写冲突。 |
| 📦 | **按任务固定传递技能** | 为任务附加协调端或执行端技能（`required`、`optional`、`reference`），跨轮复用不可变快照并记录每轮采用回执。 |
| 🔌 | **多入口接入与可选宿主桥** | 支持从任意 POSIX 终端、SSH 客户端或 CLI 技能发起协调，并提供单次加载的实验性 OMP RPC 扩展、可选 Hermes 插件及独立未应用的 Hermes 静默兼容补丁。 |

<a id="快速开始"></a>
<a id="quick-start"></a>
## 快速开始

### 方式一：让 AI 帮你完成配置（推荐）

将下面这段提示词直接粘贴给你正在使用的 OMP、Codex、Pi 或 Hermes 会话：

```text
请读取 https://github.com/Loveacup/call-cli 中的 README.zh-CN.md 和
docs/configuration.md，并查看 collab.py 与 docs/integrations.md，为当前 CLI 配置 Call CLI。

1. 先只读发现环境：在询问缺失参数前，先检查 python3、node、npm、acpx、现有
   Call CLI 检出目录、SSH 别名、技能目录及目标 CLI 登录状态。保留已有文件、配置与凭据。
2. 按需最小准备：只安装我当前工作流需要的入口、技能或可选 Hermes 模块，不新建常驻服务，
   也不自动应用上游兼容补丁。开始任务前说明每任务一次的 npm 版本检查；软件包升级须在
   设备空闲时经单独授权执行。
3. 分层安全验证：先运行不调用外部模型的本地检查（connect + list）。获得授权后再执行
   真实只读协作验证，关闭自有测试会话（确认 released / no-session），并分别报告源码检查、
   宿主兼容与真实端到端结果及剩余阻塞。
```

链接的 [AI 安装与配置手册（`docs/configuration.md`，简体中文）](docs/configuration.md)是**写给 AI 代理的执行说明**，不是让你逐项手填的配置问卷。中英文 README 覆盖完全一致的命令、参数与验证边界。

<details open>
<summary><strong>方式二：在 POSIX 终端直接运行（连续可执行路径）</strong></summary>

#### 前置条件

- **协调主机：** 支持 `fcntl` 的 POSIX Python 3.9+（仅用标准库，无需 `pip install`）。端到端协调已在 macOS 实测；Windows 支持作为发起客户端或执行设备，不作为 Python 协调主机。
- **执行设备（阶段 B 真实协作）：** Node.js（>=22.13.0）、npm、导出公共 `acpx/runtime` 的 ACPX，以及已安装且通过原生流程登录的目标 CLI（`omp`、`codex`、`pi` 或 `agy`）。
- **ACPX 与 AGY 版本兼容：** 本机 AGY（`1.5.3` / `1.5.7`）、OMP 与技能传递续轮已在 **ACPX 0.22.0** 实测（跨设备 Windows SSH OMP 在 **0.21.1** 实测，Shared Runtime 基线 **0.20.0**），不硬编码锁死单一版本。

#### 阶段 A：本地协调核心自检（无需目标模型或外部凭据）

克隆到新目录（或 `cd` 进入已有检出目录），在同一个 POSIX shell 中连续执行以下本地自检（不调用目标模型，仅对本地临时文件进行写入）：

```sh
# 若尚无本地检出，先克隆到新目录：
git clone https://github.com/Loveacup/call-cli.git
cd call-cli

# 1. 只读检查 CLI 入口帮助
python3 collab.py --help
python3 collab.py start --help

# 2. 用 mktemp（遵循 TMPDIR）创建隔离的 SQLite 临时任务库与本地客户端句柄
CALL_CLI_HOME="$PWD"
CALL_CLI_DB="$(mktemp "${TMPDIR:-/tmp}/call-cli-quickstart.XXXXXX")"

CLIENT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" connect --runtime omp)"
printf '%s\n' "$CLIENT_JSON"

CLIENT_ID="$(printf '%s' "$CLIENT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"

# 3. 用刚获得的句柄验证任务列表查询
python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" list
```

**阶段 A 可观察的成功判据：**
- `connect` 退出码为 `0`，输出包含 UUID `client_id` 与 `"declared_runtime": "omp"` 的 JSON。
- `list` 退出码为 `0` 并输出 `[]`，证明本地 SQLite 任务库初始化（权限 `0600`）与客户端句柄校验已正常工作，且未调用任何目标模型。

#### 阶段 B：真实只读协作（需要 ACPX 与已登录的目标 CLI）

在**同一个 shell** 中继续（复用已赋值的 `CALL_CLI_HOME`、`CALL_CLI_DB` 和 `CLIENT_ID`），发起一次只读协作。常规协作不设执行总截止：由协调端复核进展（每 600 秒至多发出一条复核提醒），再决定继续等待、介入或取消。旧 CLI `--timeout SECONDS` 仅保留非常规显式硬预算能力（不适用于 `--agy-swarm native`）；下方的 `wait` 只是短轮询时间片。

```sh
# 检查执行设备上的 ACPX 版本
acpx --version

# 启动只读 OMP 任务检查当前仓库（不传 --write，不授予写权限）
TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  --client "$CLIENT_ID" start --device local --agent omp --cwd "$CALL_CLI_HOME" \
  --prompt 'Read collab.py and summarize the CLI subcommands in 3 bullet points. Do not modify files.')"
printf '%s\n' "$TASK_JSON"

TASK_ID="$(printf '%s' "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

# 有界短等待（每次最多 5 秒）：保存 WAIT_JSON 并从返回输出提取 after 游标赋给 AFTER
WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  wait --task "$TASK_ID" --after 0 --timeout 5)"
printf '%s\n' "$WAIT_JSON"

AFTER="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"
```

**阶段 B 可观察的成功判据与后续操作：**
- **任务创建：** `start` 返回包含 16 位十六进制 `id`（`TASK_ID`）、`"session": "call-<id>"`、`"state": "queued"`（或 `"running"`）、`"turn": 1` 及脱敏 `recovery` 恢复卡的 JSON。
- **进度与结算：** 后续每次轮询传入 `--after "$AFTER"`，并从返回的 `"after"` 整数游标更新 `WAIT_JSON` 与 `AFTER`（`WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" wait --task "$TASK_ID" --after "$AFTER" --timeout 5)"` 及 `AFTER="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"`）。本轮结束时，`task.state` 变为 `"completed"`（若发出 `CALLCLI_QUESTION` 则为 `"waiting_reply"`），`task.exit_code` 为 `0`，`task.stop_reason` 为 `"end_turn"`，`events` 中出现带 `answer_tail` 的 `"token": "1:settled"` 事件。
- **回答问题或执行期改向（`reply` / `intervene`）：**
  - 若 `WAIT_JSON` 中 `task.state` 为 `"waiting_reply"`，先从 `task.question_id` 提取 `QUESTION_ID`，再按该精确编号作答：
    ```sh
    QUESTION_ID="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["question_id"])')"
    python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
      reply --task "$TASK_ID" --question-id "$QUESTION_ID" --prompt '你的回答'
    ```
  - 若要在运行中或结算后改向（`cancel+resume` 取消并续轮，而非 `steer`），传入幂等安全的 `--command-id`（可选附带 `--expected-turn` 或 `--clear-timeout`）：
    ```sh
    python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
      intervene --task "$TASK_ID" --command-id rev-1 --prompt '修订后的范围'
    ```
    随后继续短 `wait`（同步更新 `WAIT_JSON` 与 `AFTER`），由协调端取消旧轮、确认权威结算并在同一原生会话启动第 2 轮。
- **调用 AGY（模型简称与只读审批）或传递技能：** 新 AGY 默认 **ocs**，不按任务内容自动换模型。小写 **oc/oa/4c/4a/4h** 裸简称全部单代理 `off`，后缀 **s** 为原生 Swarm，**t** 为按广告原生命令启动的 Teamwork 源码候选。显式模式／路由必须与简称一致；完整广告 ID 保留原路由／覆盖兼容，详见[模型合同](skill/references/agy-models.md)。新 AGY 任务即使在 `write` 为 `false` 时也默认持久化 `approve-all`；严格只读工具审批请显式传 `--agy-approval approve-reads`（审批模式与 `write: false` 都不是操作系统或文件系统沙箱；`--write` 独立声明编辑授权并占用工作区）：
  ```sh
  # 1. 发现本执行设备当前广告的精确 ACP 模型 ID 与 thought_level.options
  node "$CALL_CLI_HOME/models.cjs" "$CALL_CLI_HOME"

  # 2. 将 MODEL_ID 设为本次 availableModelIds 返回的精确值，独立发起只读 AGY 任务、短等待结算并关闭
  MODEL_ID="gemini-4-argon"  # 仅作示例；请按当前执行设备本次发现结果选择
  AGY_TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    start --device local --agent agy --agy-swarm off --agy-approval approve-reads --model "$MODEL_ID" \
    --cwd "$CALL_CLI_HOME" --skill reference="$CALL_CLI_HOME/skill" \
    --prompt 'Read the passed skill as reference and list the supported actions. Do not modify files.')"
  printf '%s\n' "$AGY_TASK_JSON"

  AGY_TASK_ID="$(printf '%s' "$AGY_TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

  # 有界短等待：保存 AGY_WAIT_JSON 并提取 AGY_AFTER（后续轮询传 --after "$AGY_AFTER"，直至 AGY_TASK_ID 结算）
  AGY_WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    wait --task "$AGY_TASK_ID" --after 0 --timeout 5)"
  printf '%s\n' "$AGY_WAIT_JSON"

  AGY_AFTER="$(printf '%s' "$AGY_WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"

  # 确认 AGY_TASK_ID 已结算（completed 或 cancelled）后关闭会话
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    close --task "$AGY_TASK_ID"
  ```
- **关闭会话与清理核验：** 确认 OMP 任务（`$TASK_ID`）已结算（`completed` 或 `cancelled`）后关闭该会话：
  ```sh
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" close --task "$TASK_ID"
  ```
  `close` 先写入持久 SQLite 释放围栏（`release_fences`），只有在核验本地精确 closed 记录与同作用域 `no-session`（或 `--agy-swarm native` 的单 owner worker/session 清理证据）并清理任务技能快照后，才返回 `"state": "closed"` 及 `close_receipt.state == "released"`。对 `unknown` 任务，`close` 仅执行只读探测（probe-only），绝不盲目重发外部关闭。切勿仅因关闭了单个任务就直接 `rm -f` 删除共享的 `$CALL_CLI_DB`——清理前应先通过 `list` 核验库内所有任务（如 `$TASK_ID` 与 `$AGY_TASK_ID`）均已处于 `closed`，避免遗留活动会话。

</details>

> [!NOTE]
> **版本检查不是自动升级：** 每个逻辑新任务只查询一次 `acpx --version` 与 `npm view acpx dist-tags.latest` 并持久化到 `version_check`。正常派工绝不自动安装包；单独授权的维护升级（`updates.py`）必须在设备无活动任务锁且验证官方仓库来源后执行。

<a id="选择接入方式"></a>
<a id="choose-your-entry"></a>
## 选择接入方式

| 你的工作环境 | 接入入口 | 单次控制、回流方式与当前验证边界 |
| :-- | :-- | :-- |
| **POSIX 终端或脚本** | `python3 collab.py` | 源码动作：`connect`、`start`、`list`、`status`、`events`、`wait`、`reply`、`dialogue`、`intervene`、`cancel`、`close`。`dialogue` 仅限有能力的 AGY native 原 owner，不是通用 steer。调用方短 wait/游标 events；runtime 声明与本地 capability 不等原生宿主身份，已装命令另验。 |
| **OMP、Codex 或 Pi** | `skill/` 技能包 + 已配置的协调入口 | 指导发起端 AI 完成环境发现、在线协调、`seq` 增量消费、问答、`cancel+resume` 介入与技能传递；技能本身不安装系统依赖，也不代替目标 CLI 原生登录。 |
| **新建自有 OMP RPC 宿主（实验性）** | `omp --mode rpc --no-ui --no-extensions -e /absolute/call-cli/omp-extension.mjs` | 仅新自有 RPC 单次加载，不全局启用/热附着。ctx 绑定身份，既有 1 秒 wait 轮询支持 pending 续派及 OMP/AGY unknown 提醒；task-local 已提醒 turn 集合保留首次迟到事件，非持久 exactly-once。followUp 排队不是 steer、模型消费或 Telegram 证明。退出只停投递不取消业务；TUI、ACP defer、重启恢复未验。 |
| **其他机器（跨 SSH）** | `remote-client.cjs` | 单文件 Node SSH 客户端，将单次 JSON 命令转发至 POSIX 协调主机的 `collab.py`。 |
| **Windows 受限 Codex 环境** | `remote-client.ps1` / `codex-ssh.cjs` | `remote-client.ps1` 规避已观察到的 Windows Codex 沙箱内 Node `spawn(ssh) EPERM`（当前仅核验 macOS 协调端）；`codex-ssh.cjs` 通过一次性 InteractiveToken 任务在已登录桌面上下文启动新 Codex，规避 Session 0 `0xC0000142`。 |
| **Hermes 宿主（可选插件）** | `call_cli` 插件（`__init__.py`、`plugin.yaml`） | 捕获原生 `HERMES_SESSION_*` 来源身份，经宿主 terminal 工具后台启动 runner（`notify=true, heartbeat=60`），在调用 `status`/`events` 时推进已授权的 `intervene`，并在源码层支持 P1 轮转压缩同源续办（`native_continuity.py`）、已结束 owner 分动作恢复（`reconcile`/`close`）及固定目标释放路由（`dashboard/plugin_api.py`）。加载新工具 schema 需按宿主支持方式刷新工具表或新开 Hermes 会话；宿主当前会话实时消费与 Telegram 端到端投递（Issue #1）仍需单独宿主验收。 |
| **Hermes 源码检出（可选静默兼容补丁）** | [`compat/hermes-silence/`](compat/hermes-silence/README.md) | **独立于 Call CLI 核心与插件，绝不会自动应用。** 严格绑定上游 `NousResearch/hermes-agent` 提交 `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`，由只读 `compat/hermes-silence/check.py` 校验整文件 SHA-256。仅在空闲干净源码检出上手动校验/应用，升级上游前须手动反向回滚。 |

**终端安装与 CLI 内加载属于同一条配置路径的先后步骤。** 各入口运行时依赖闭包与加载方式详见[集成手册（`docs/integrations.md`，简体中文）](docs/integrations.md)。

发起端为 OMP、Codex、Pi 与 Hermes。AGY 仅作执行端；AGY 发起能力不再属于实现或验收缺口。

<a id="核心能力"></a>
<a id="capabilities"></a>
## 核心能力

### 1. 持续跨 CLI 协作、单次控制与事件回流

- **持久 `seq` 游标与原子结算（`bridge.py`、`runner.py`、`events.py`）：** runner 输出通知与 `events`/`wait` 共享 SQLite 持久 `rowid`（`seq`）游标。同一工具的重复相同状态自动去重（stdout 普通工具通知每轮最多 12 条，失败状态不受限，全部记录均可经 `events` 查询）。每轮结束时在同一个 SQLite 事务内原子提交任务状态与唯一 `<turn>:settled` 事件（含 `question_ids`、最长 1000 字 `answer_tail` 及待执行介入的 `next_action`），避免 `close` 或 `intervene` 并发竞争丢失结算通知。
- **编号问题 `CALLCLI_QUESTION` 身份与去重：** 目标端通过 `CALLCLI_QUESTION Q1 <正文>`（`Q[1-9][0-9]*` 加单空格与非空正文）发起提问。同一 task/turn 内，若问题编号与完整正文指纹（在 8000 字符公开截断前计算的 UTF-8 SHA-256 与字符长度，行缓冲上限 64000 字符）完全一致，复述只会登记一次并保留首个 `question_id` 供 `reply` 作答；不同编号（`Q1`、`Q2`）或同编号但正文已修订的问题则保留各自独立的可答 token（附安全 `question_note`），不中断后续输出流。
- **600 秒协调者复核检查点，而非自动掐断：** Hermes（`call_cli`）与 OMP（`call_cli_omp`）模型工具均拒绝 `timeout` 执行总限时（含旧缓存字段）。公共 runner 在主循环中每 **600 秒**至多发出一条复核请求（`quiet_notice: true`，`notice_kind: "quiet"|"checkpoint"`），静默与持续活跃共享同一冷却计时，因此持续工具活动不会无限推迟复核检查点。复核提醒绝不自动取消、关闭或续跑任务；由发起端 agent 核查真实进展后自主选择继续等待、明确介入或取消。旧 CLI `--timeout SECONDS|none` 与 Python `start(timeout=...)` 仍为非 Swarm ACPX 任务保留非常规显式硬预算，直至在受支持的续轮中通过 `--clear-timeout`（`reply`/`intervene` 的 `clear_timeout`，`queued` 状态保守拒绝）显式清除。
- **绑定轮次的续办、恢复卡与精确证明 `close`（`recovery.py`、`release.py`）：**
  - `reply` 与 `intervene` 支持传入 `--expected-turn`（`expected_turn`）与幂等 `--command-id`（`command_id`），防止过期意图推进未观察到的新轮次。
  - 任务查询与结算输出均附带不含凭据或私有库路径的 `recovery` 恢复卡（记录 `task_id`、`turn`、`control_epoch`、`controller`、`seq`、`questions`、`commands`、`reservation`、`capabilities.exact_record_reconcile`，以及如实标明 `coverage: "uncovered"`、`pre_exit_hook: false`、`exit_interception: "uncovered"`、`successor_takeover: "uncovered"` 的 `responsibility` 字段）。
  - 全部公开 `close` 入口（`collab.py`、OMP、Hermes）共用 `release.close_task` 与持久 SQLite 释放围栏（`release_fences`）。本地 ACPX 任务在调用外部关闭前先提交围栏与尝试记录，并要求核验精确 closed 记录读回与同作用域 `no-session`（`findSession(...) === undefined`）。对 `unknown` 任务，`close`（CLI/OMP）与 `reconcile`（Hermes）一律走**只读探测模式（`probeTarget`，零次外部 `close` 调用）**，绝不盲目重复发送关闭请求。CLI/OMP 返回白名单过滤后的 `close_receipt`（`state`、`phase`、`stage`、`reason_code`、`external_close_attempted`）；缺记录或远端设备的任务保守保留 `unknown`（`exact_record_unavailable`）与写占用锁。
- **轮转压缩同源续办与已结束 owner 恢复（Hermes 原生源码能力）：** `native_continuity.py` 在同一可信 Hermes profile 与原路由下，通过只读校验已提交的 `state.db`（schema 32）`compression` 父链，允许轮转压缩后的新 SID 继续访问原任务而不改写 `Origin`。当原 owner 会话已有明确提交的结束原因（`agent_close`、`cli_close`、`tui_close`）时，同 profile/route 的当前会话可只读查看 `list`/`status`/`events`（不推进 pending 介入），或凭 `expected_turn` + `command_id` 显式接续已结算任务（递增 `control_epoch`）。另有面向操作者的固定目标释放路由（`/api/plugins/call-cli/release/*`），要求宿主敏感 HTTP 认证与本地私有 `0600` 的 `release-policy.json`。

Hermes 另提供显式 `release_only`：仅 unknown 本地 exact-record 任务的原始且当前 controller，要求已结算非 native runner 与 observed expected_turn。先围栏后至多一次 close，再严格 probe；已有 fence 不重复 close。receipt 保留 business_state=unknown，资源释放不是业务完成；CLI/OMP 动作不扩展。

### 2. 原生 AGY 支持、按模型默认 Swarm 与持久工具放行（`agy.py`、`agy_native.py`、`models.cjs`）

- **显式模型模式（默认 ocs）：**
  - `oc/oa` 映射 Orchestrator Carbon/Argon，`4c/4a/4h` 映射 Gemini 4 Carbon/Argon/Hydrogen。裸简称全部 off；后缀 s 选 Swarm、t 选 Teamwork。显式 `agy_mode=single/swarm/teamwork/dynamic` 须匹配简称意图；缺所选命令广告不降级 Swarm 或普通 prompt。
  - 新任务经单一公共入口解析后持久化型号／路由。旧任务、其他 CLI 不迁移；完整广告 ID/family 保持既有行为。显式 worker 型号仅作为偏好转给 AGY，无默认逐员型号或完成门槛。纯文字／感性工作可显式选择 `4a/4as`；未指定仍 `ocs`。
  - 原生 OC/OA 零 prompt 配置／读回已验，不是 OCS 模型／Swarm 或 ACPX off 业务验收。不猜 effort tier、不静默降级。详见 [AGY 合同（`skill/references/agy-models.md`）](skill/references/agy-models.md)。
- **单 owner 原生 Swarm 薄桥：** Call-CLI 发起原生 `/swarm`，转达授权任务、公开进度／问题、同 owner dialogue 和取消；AGY 自行组织角色与内部工作。end_turn 后完整 load 对账当前 interaction 的最终回答及公开工具／问题状态，正常响应结算与精确会话卸载不要求 CALLCLI_RESULT 或逐成员 add/kill/model 证明。业务产物仍由发起者核验，会话卸载不证明全部后代停止。见[当前 Swarm 合同](skill/references/agy-models.md)。
- **Teamwork 源码候选：** 复用同 owner/load/dialogue/问题和精确 unload，receipt 为 mode=teamwork。委派 RPC completed 及首轮确认不当最终结果；合成路由、异步访谈、同源修改与正常 close 已验，不代表真实宿主业务验收。AGY 原生负责内部角色、分工、审批及权限；Call-CLI 不要求父写、子只读或子写权限证明。公开问题／审批事件有则沿现有机制传递，无内部事件明确不可观测，不猜测或强塞回调。见[模式合同](skill/references/agy-models.md)。
- **取消不生成清理业务：** 仅转达一次原生 cancel，不发逐员 cleanup prompt、不重放业务。缺原生内部停止确认时保留 unknown/HOLD 并说明限制；取消未证只关 stdio、不 terminate/kill，正常成功退出沿用既有 fallback。旧可信取消 receipt 与 AGY 1.5.8 v3 样本保留为历史，不代表本候选取消行为或已部署验收。
- **默认 `approve-all` 审批与工作区写占用分离：** 新建 AGY 任务默认持久化 `agy_approval: "approve-all"`（即使 `write` 为 `false`），放行任务授权范围内的 shell 与原生工具，同时提示词优先引导使用原生 read/search 工具。若需严格只读审批，显式传入 `--agy-approval approve-reads`（Hermes：`agy_approval: "approve-reads"`），此时 shell/写请求会被非交互拒绝。**工具审批策略与 `write: false` 都不是操作系统或文件系统沙箱；** 获授权编辑文件仍须另传 `--write` 以声明写权限并占用工作区。`reply`/`intervene` 续轮保留该策略；缺省该字段的旧任务仍按原 `write` 映射，OMP/Codex/Pi 的默认策略（缺省 `approve-reads`，`--write` 选 `approve-all`）保持不变。
- **安全认证诊断（off ／旧 ACPX 路径）：** 失败轮可识别精确 `error.detailCode: "AUTH_REQUIRED"`，或在精确匹配 host request、turn_started、唯一主 prompt RPC/response 及 failed turn_result 且满足 `error.code === -32000` 与 `data.authRequired === true` 时，通过公开 `runtime.watchSession()` 做有界被动回放（1 秒／1000 条当前请求事件／10000 条总事件）。回调 ID 碰撞、重复、缺 journal 或超时均保持原因未知；仅向 `settlement_note` 投影固定类别和来源标签，不匹配错误正文、不扫描 AGY 日志、不改凭据或自动重放。

### 3. 按任务固定传递技能（`skills.py`、`skill-stage.cjs`）

- **发起端通用的公共契约：** 无论从 `collab.py`、OMP 还是 Hermes 发起，均可通过同一任务契约传递技能：可重复的 `--skill MODE=PATH`（`location: "origin"`，协调端技能目录）、`--target-skill MODE=PATH`（`location: "target"`，执行端已有技能目录，含 Windows 绝对路径），或 `--skills-json ARRAY`（Hermes / OMP / Python `Collaboration.start` 的 `skills` 数组，可带 `source` 来源元数据）。
- **本地优先选择与公开仓库线索：** 发起端先检查本地技能并实际阅读 `SKILL.md`、相对 `references/`/`scripts/` 及依赖要求。本地不足再检索公开技能：仓库 star 数与最近实质内容更新（区分真正的工作流/代码更新与仅改 license、格式或权限位的提交）作为筛选线索而非唯一质量保证；选定公开技能后在 scratch 固定具体 commit SHA，不运行全局安装。
- **三种模式（`required` / `optional` / `reference`）、SHA-256 快照与逐轮回执：**
  - `required`：目标必须实际读取 `SKILL.md` 及所需资源并按适用方法执行；不适用或缺少依赖须如实报告 `not_applicable` / `unavailable`。若 `required` 技能暂存失败，不会提交业务 prompt。
  - `optional` / `reference`：目标按任务价值读取采用（未采用报 `not_adopted`）或作为非约束性参考查阅。
  - `origin` 技能在任务创建时生成每文件 SHA-256 清单与私有快照（`0700`/`0600`，自动跳过点文件、符号链接、`node_modules`、`__pycache__`、`.pem`/`.key`、`id_*`、`credentials*` 与 `auth.json`，上限为**单文件 4 MiB、单技能包 16 MiB、1000 个文件**），每轮执行前经本机/SSH stdin 暂存到 `<os.tmpdir()>/call-cli-skills/<task>/<skill>/<sha>/`。`reply`/`intervene` 续轮复用固定快照；经验收释放的 `close` 会删除协调端快照并清理执行端临时目录。每轮记录 `CALLCLI_SKILL <id> <read|adopted|not_adopted|not_applicable|unavailable> <原因或证据>`（缺少 `required` 回执仅标 `skill_verification: "unverified"`，不会把已完成的有效业务结果降级为 `unknown`）。
- **读取边界：** ACPX 0.22.0 的 `fs/read_text_file` 回调仅允许读取 `cwd` 子树内路径，而 agent 原生文件读取工具（已在 macOS/Windows OMP 与 macOS AGY 实测）可直接读取上述临时目录，无需污染项目 `cwd`。仅依赖受限回调的目标如实报告 `unavailable`。详见[技能选择与传递指南（`skill/references/skill-passing.md`）](skill/references/skill-passing.md)。

### 4. 多 CLI 并行协作、完成判据与可选 Hermes 静默兼容补丁

- **多 CLI 并行执行与单写者锁：** 同一设备或跨 SSH 可同时并行运行多个目标 CLI（如 AGY 与 OMP 并发工作），按 origin/client/task/session 严格隔离。`--write` 与可选跨设备 `--resource` 提供单写者目录占用保护；只读任务（包括 `write: false` 下默认 `approve-all` 的 AGY 任务）可与另一任务的合法写者安全并存。仅负责派工和消费事件的中继必须保持 `write: false`，只有实际编辑者才声明写占用。
- **进度不等于完成：** 四个 ACPX 执行目标（OMP、Codex、Pi 及 off／旧 AGY）共同要求 exit `0`、有效且属于本轮的 `end_turn`，以及最后一次工具事件后的非空解码回答（或有待答问题处于 `waiting_reply`）。仅有 `CALLCLI_STAGE`、`CALLCLI_SKILL`、空输出或工具状态时一律记 `unknown`，保留写占用且绝不自动重放。迟到的重复工具更新即使在 stdout 去重，仍会重置最终回答窗口。
- **模块化后端扩展接口（`backends.py`、`acpx_backend.py`）：** `backends.py` 注册实现 `prepare_start(model, effort, approval, ...)`、`run(store, task, emit)` 与 `control(task, action, timeout)` 的后端适配器。非 ACP 后端可直接注册在同一表中，无需新增守护进程或独立状态库。详见[后端扩展接口（`docs/integrations.md#后端扩展接口`）](docs/integrations.md#后端扩展接口)。
- **可选 Hermes 静默兼容补丁（[`compat/hermes-silence/README.md`](compat/hermes-silence/README.md)）：**
  - **独立于核心、绝不自动应用：** `compat/hermes-silence/hermes-2bd378ed-silence.patch` 是针对上游 Hermes 源码的可选、未应用补丁，不属于 Call CLI 核心运行时或 `call_cli` 插件。Call CLI 没有任何代码会导入、安装或自动应用它，也绝不会自动修改 Hermes 配置或重启任何服务。
  - **精确基线与只读校验门禁：** 严格限定于 `NousResearch/hermes-agent` 公开提交 [`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`](https://github.com/NousResearch/hermes-agent/commit/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19)（而非此后的 `main` 如 `33cbf1e813d4ff35c7709b21c809f55eb3af9288`）。`compat/hermes-silence/manifest.json` 固定了全部 10 个受影响 gateway 文件及 3 个测试文件在补丁前后的整文件 SHA-256，由只读标准库脚本 `python3 compat/hermes-silence/check.py "$HERMES" --state before|after` 校验。
  - **兼容行为与必要维护：** 在固定源码归档的隔离测试中（经 `compat/hermes-silence/run_tests.py` 运行 5 文件网关回归共 `138` 项通过），该补丁使运行中回合在消费需要回复的真人介入后移除机器/bot 开场静默豁免并保留可见静默告警、扣留纯静默标记构成的已完成中间 commentary（同时保留分段/TTS 刷新边界与后续正文）、剥离非空正文前未引用的首行静默控制标记而非吞掉后续正文，并防止 commentary tick 提前刷出缓冲中的裸静默标记。维护要求：仅在工作区完全干净且处于精确基线提交的空闲源码检出上手动执行 `check.py --state before` 与 `git apply`；升级上游 Hermes 前必须先校验 `--state after` 并手动 `git apply --reverse` 回滚；待上游发布并独立验证等效修复后即退役该临时补丁。

<a id="工作原理"></a>
<a id="how-it-works"></a>
## 架构与协作生命周期

<div align="center">

<img src="docs/assets/call-cli-architecture.png" alt="Call CLI 概念架构图：发起端通过 Call CLI POSIX 协调中心，以扇出（fan-out）方式并行调度本机或 SSH 远端的多个独立目标 CLI（OMP、Codex、Pi、AGY）" width="100%">

</div>

[打开高清架构图](docs/assets/call-cli-architecture.png)——手机端建议打开原图后缩放查看模块文字。

*概念架构图（非实时运行截图）：由单一发起入口持有 Call CLI 任务（`bridge.py` / `runner.py`），再向本机或 SSH 远端的多个**并列、隔离的目标 CLI 会话**（`OMP`、`Codex`、`Pi`、`AGY`）并行扇出派工。图中各目标 CLI 为并列目标，连线不表示目标间串联依赖（不禁止用户授权的分层协调），且 Call CLI 绝不接管用户手动打开的终端。*

### 单任务多轮问答与介入时序

```text
发起 CLI                        Call CLI（Store + Runner）            目标 CLI（经 ACPX / 原生 ACP）
   │                                       │                                        │
   ├──── 1. start（含可选技能包）─────────►├──── 设备锁 + 会话创建 + 技能暂存 ─────►│
   │◄─── 2. wait / events (--after seq) ───┤◄─── CALLCLI_STAGE / 工具 / QUESTION ───┤
   │     （或 600s 协调者复核检查点）      │     （或 AGY native session/load）     │
   ├──── 3a. reply (--question-id) ───────►├──── 同一原生会话启动 turn+1 ──────────►│
   ├──── 3b. intervene (--command-id) ────►├──── 请求取消当前轮 → 确认取消结算 ────►│
   │     （下次 wait/status 推进续轮）────►├──── 同会话启动修订轮 + 复用固定技能 ──►│
   │◄─── 4. <turn>:settled + close ────────┼◄─── turn.result + 围栏关闭证明 ────────┤
```

**控制语义区分（`steer` vs. `queue` vs. `cancel+resume`）：** ACPX 0.22.0 公共 `startTurn` 明确拒绝执行期热插 `steer`，而进程内排队（`queue`）会等待当前轮结束，无法在运行中改向。因此 Call CLI 的 `intervene` 采用明确的**取消并同会话续轮（cancel-and-resume）**契约：保存幂等 `command_id`（状态依次跟踪 `pending` → `queued` → `target_started` → `target_completed` / `target_cancelled` / `target_unknown` / `superseded` / `withdrawn`） → 请求取消当前运行轮 → 确认取消权威结算 → 在下一次 `wait`（通用 CLI ／ OMP 适配）或 `status`/`events`（Hermes）时于**同一原生会话**启动修订轮。`pending`/`queued` 仅表示已登记，不代表已生效；`unknown` 结果保留现场供对账，绝不自动重放。

<a id="证据与边界"></a>
<a id="evidence-and-boundaries"></a>
## 证据分层与验证边界

Call CLI 严格区分**源码已实现、离线/合成测试已通过、本机已安装运行核验、跨 CLI 真实端到端执行与宿主用户收到**（详见 [`skill/references/core-requirements.md`](skill/references/core-requirements.md) 与 [`VERIFICATION.md`](VERIFICATION.md)）：

| 验证层级 | 已核验范围 | 明确边界／未宣称范围 |
| :-- | :-- | :-- |
| **源码与离线测试套件** | 最后完整基线：Python `357` 项（`345` pass、`12` skip），六文件 Node `81` pass。之后 S6/core fixture 与最新 OMP adapter `31/31` 为独立增量，不合并冒称新全量；见[当前索引](VERIFICATION.md#current-acceptance-index)。 | 基线早于后续改动，不绑定最新树；skip 非 pass。合成 transport/SQLite/宿主 fixture 不证明部署或用户投递。 |
| **原生 ACPX 与 AGY 协议探针（`RUN_NATIVE_ACPX_TESTS=1`）** | Opt-in 的 `test_native_shared`、`test_native_skills` 与 `test_native_release` 在本机已安装的 ACPX 0.22.0 配合隔离 `HOME` 与合成 ACP 端点全部通过；AGY 1.5.7 实测零 prompt 模型选择读回与单 worker `session/load` 回放对账（`test_agy_native_owner.py`）。 | 真实 ACPX / ACP 传输配合合成端点或有界探针，不等于所有真实大模型多轮/取消场景已全面通过。 |
| **本机与跨设备 SSH 真实模型协作** | 历史 macOS/Windows SSH CLI/模型检查保留于 VERIFICATION。另有 AGY 1.5.8 v3 新候选任务：G4C low＋一个 G4H medium，end_turn 后 cleanup、completed kill＋status、精确 unload、可信 cancelled、重复 close 和 natural exit 0 已验。 | 仅该 CLI/模型/入口样本。线程 launcher/观测 wrapper 不认证已部署/默认 launcher、active-tool 热取消、所有脱离后代全停、持久历史删除或 GUI；旧 HOLD 未对账或追溯结算。 |
| **宿主原会话消费与 Telegram 投递** | 可选 Hermes 插件（`__init__.py`）、P1 轮转压缩同源续办（`native_continuity.py`）、已结束 owner 恢复（`release.py`）、实验性 OMP RPC 扩展（`omp-extension.mjs`）以及可选未应用的 Hermes 静默兼容补丁（`compat/hermes-silence/`，5 文件隔离网关套件 `138` 项通过）均已完成源码与合成验证。 | **严禁外推为所有宿主真实消费或 Telegram E2E 全部通过：** 加载新工具 schema 需宿主支持的工具表刷新或新开会话；原生工具注册、本机部署或合成收集器测试**不等于**所有 CLI 宿主原会话真实消费、运行中 gateway 已加载补丁、Telegram 空通知问题（**Issue #1**）已关闭、上游 AGY 错误输出（**Issue #2**）已解决，或离开当前协作后的离线自动唤醒已打通。 |

Jev exact-silence producer 修正仅为独立未应用的私有候选，独审核对 9 隔离测试、54 AST 合同格与 13 包文件 hash；此仓库不分发本地插件源码、私密路径或候选安装器。None 仅保本 hook 原文，不阻止后续 hook 改写，不修混真人/OOB admission；运行时另验。S7 旧任务恢复及 S8 部署/加载/真实闭环/外部记录同步均未完成。

```sh
# 运行标准离线验证套件（无需模型凭据或联网）
python3 -B -m unittest discover -v
node --test test_skill_stage.cjs test_shared_prompt.cjs test_models.cjs test_codex_ssh.cjs test_omp_extension.mjs test_release_backend.cjs

# 可选：真实本机 ACPX queue-owner + 合成 ACP 端点集成测试（无真实大模型调用）
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest test_native_skills test_native_shared test_native_release -v
```

<details>
<summary><strong>授权前请阅读：范围与安全边界</strong></summary>

- **只新建协作会话：** Call CLI 绝不接入、控制或向用户已经手动打开的终端、TUI 或桌面 GUI 注入消息。
- **审批策略与释放围栏不是系统沙箱：** OMP/Codex/Pi 默认 `approve-reads`，传 `--write` 时使用 `approve-all`；新 AGY 任务默认持久化 `agy_approval: "approve-all"`（可显式传 `--agy-approval approve-reads`）。审批策略、`write: false` 以及 SQLite `release_fences` **都不是操作系统或文件系统沙箱，也不证明任意脱离后代进程均已停止**；SSH 与目标 CLI 仍以当前系统账号权限运行。
- **结果不明先对账：** `queued` / `pending` 仅代表已提交，不代表已生效。遇到 `unknown` 或客户端返回 `do_not_replay: true`，保留写占用 HOLD 并使用精确记录只读探测（CLI/OMP 的 probe-only `close`，或 Hermes 的 `reconcile`）先对账，再决定是否重试。
- **技能传递边界：** 技能快照会自动跳过点文件、符号链接、`node_modules`、`__pycache__` 与常见私钥文件名并限制体积/文件数，但它**不是恶意代码沙箱或自动隐私审计**；只传递本任务需要且允许目标模型读取的目录。
- **单协调主机状态管理：** 请将 SQLite 任务库、`client_id` 句柄、提示词、`release-policy.json` 与认证保存在用户私有目录。由单个 POSIX 协调主机持有状态与锁，不承诺跨协调主机分布式锁、进程崩溃后的 stdout 通知重放或离开当前协作后的自动唤醒。

</details>

<a id="文档"></a>
<a id="documentation"></a>
## 文档导览

### 给实际操作 Call CLI 的 AI

| 手册 | 内容概要 | 语言 |
| :-- | :-- | :-- |
| [安装与配置（`docs/configuration.md`）](docs/configuration.md) | 环境发现、SSH 别名、ACPX agent 配置、认证、权限、版本检查与交付验收。 | 简体中文 |
| [CLI／模块接入（`docs/integrations.md`）](docs/integrations.md) | OMP/Codex/Pi 技能安装、单次加载的实验性 OMP RPC 适配、可选 Hermes 插件、已结束 owner 恢复与释放围栏、各入口运行时依赖闭包与后端扩展接口（`backends.py`）。 | 简体中文 |
| [在线协作（`docs/usage.md`）](docs/usage.md) | `seq` 游标续读、编号问题 `CALLCLI_QUESTION` 身份、`reply`、`intervene` 取消续轮、600 秒复核检查点、`--clear-timeout` 与 `close`。 | 简体中文 |
| [当前核心需求（`skill/references/core-requirements.md`）](skill/references/core-requirements.md) | 持续双向协作、AGY native Swarm、已结束 owner 恢复与分层证据核验的唯一维护正本。 | 简体中文 |
| [任务技能传递（`skill/references/skill-passing.md`）](skill/references/skill-passing.md) | 本地优先选择、公开仓库线索、`required`/`optional`/`reference` 模式、暂存限制与 `CALLCLI_SKILL` 回执。 | 简体中文 |
| [AGY 模型、Swarm 与审批（`skill/references/agy-models.md`）](skill/references/agy-models.md) | 默认 ocs、裸/s/t 简称、冲突拒绝、精确 ID 兼容、单 owner 结算与验证边界。 | 简体中文 |
| [Hermes 宿主与连续性（`skill/references/hermes-host.md`）](skill/references/hermes-host.md) | 原生来源捕获、P1 轮转压缩同源续办、已结束 owner 恢复/对账与 `[SILENT]` 机器通知轮指导。 | 简体中文 |
| [维护源与安装检查（`skill/references/maintenance.md`）](skill/references/maintenance.md) | 每次逻辑启动的维护源/安装检查与空闲设备 ACPX 升级边界。 | 简体中文 |
| [Windows Codex SSH（`CODEX_SSH.md`）](CODEX_SSH.md) | 桌面上下文 `codex-ssh.cjs` 启动入口与原生 PowerShell 客户端（`remote-client.ps1`）。 | 简体中文 |
| [可选 Hermes 静默兼容补丁（`compat/hermes-silence/README.md`）](compat/hermes-silence/README.md) | 独立未应用补丁（严格固定 `NousResearch/hermes-agent` `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`）、只读 `check.py` 门禁、手动应用/回滚与隔离测试脚本。 | English |

### 给使用者和贡献者

[验证记录（`VERIFICATION.md`，English）](VERIFICATION.md) · [路线图（`ROADMAP.md`，简体中文）](ROADMAP.md) · [安全说明（`SECURITY.md`，English）](SECURITY.md) · [贡献指南（`CONTRIBUTING.md`，English）](CONTRIBUTING.md) · [许可证（`LICENSE`）](LICENSE)

*注：`docs/assets/render_banner.py` 仅在重新生成仓库配图时使用 Pillow；Pillow 不属于 Call CLI 的运行时或测试依赖。*

## 致谢

基于 [ACPX](https://github.com/openclaw/acpx) 与 OMP、Codex、Pi、AGY、Hermes 的原生能力构建。Call CLI 是独立项目，不隶属于上述上游项目。

<div align="center">

**工具各有长处，协作保持同向。**

[MIT 许可证](LICENSE) · [English](README.md)

</div>
