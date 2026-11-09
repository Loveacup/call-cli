<div align="center">

<img src="docs/assets/call-cli-banner.png" alt="Call CLI — 跨 CLI 与跨设备协作，连接 OMP、Codex、Pi 与 AGY（可选接入 Hermes）" width="100%">

**给你的 CLI 一个搭档，而不只是另一个子进程。**

<a href="README.md">English</a> · <a href="README.zh-CN.md"><strong>简体中文</strong></a>

<a href="https://github.com/openclaw/acpx"><img src="https://img.shields.io/badge/transport-ACPX%200.22%2B-49c5b6?style=flat-square&amp;labelColor=161b22" alt="基于 ACPX Shared Runtime"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8cbaff?style=flat-square&amp;labelColor=161b22" alt="MIT 许可证"></a>

[快速开始](#快速开始) · [选择接入方式](#选择接入方式) · [核心能力](#核心能力) · [架构与协作周期](#工作原理) · [证据分层与边界](#证据与边界) · [文档导览](#文档)

</div>

---

Call CLI 通过轻量协调层连接 **OMP、Codex、Pi 和 AGY**。新建协作会话、跟进实时进展、按编号回答问题、按任务固定传递技能，并在执行期间调整方向——**既能在本机运行，也能通过你现有的 SSH 跨设备协作。**

**明确边界（不做什么）：** 不强制依赖 Hermes，不新增常驻后台服务或守护进程，不接管你已经手动打开的终端、TUI 或桌面 GUI 会话。

## 为什么需要 Call CLI

你在一个 CLI 里规划，让同机或 SSH 远端机器上的另一个 agent 实现，再并行交给第三个 agent 审查。阶段进展、编号问答、固定技能包和中途改向都通过发起端 CLI 往返，不必在多个终端窗口之间手工复制粘贴。

| | 能力 | 带来的变化 |
| :-- | :-- | :-- |
| 💬 | **持续多轮问答与执行期改向** | 用 `reply` 按问题编号（`CALLCLI_QUESTION`）作答，或用 `intervene` 在同一目标会话内取消旧轮、确认结算后按新要求续轮。 |
| 📡 | **可续读的事件流（`seq`）** | 基于持久游标的有界等待（`wait` / `events --after SEQ`）返回公开阶段（`CALLCLI_STAGE`）、去重后的安全工具状态、待答 `question_ids` 与结果尾部 `answer_tail`，与轮次状态同一事务原子提交。 |
| 🤝 | **多 CLI 并行协作与写隔离** | 将 OMP、Codex、Pi、AGY 作为并列执行目标并行调度（各自独立 origin/client/task/session），由单写者目录占用锁（`--write`、`--resource`）防止并发写冲突。 |
| 🤖 | **原生 AGY 支持与持久工具放行** | 通过 `models.cjs` 发现精确 ACP 模型，新 AGY 任务默认持久化 `approve-all`（可显式 `approve-reads`），未恢复的工具失败或致命错误标记如实结算为 `unknown`。 |
| 📦 | **按任务固定传递技能** | 任意发起 CLI 或 Hermes 均可传递 `required`、`optional`、`reference` 技能（支持 `origin` 与 `target`），跨轮固定 SHA-256 快照并记录每轮 `CALLCLI_SKILL` 自报回执。 |
| 🔌 | **模块化后端扩展接口** | 公共任务/轮次/锁协调（`bridge.py`、`runner.py`）与 ACPX/AGY 策略解耦，通过 `backends.py` 注册扩展；新增后端不强制要求 ACP 或独立守护进程。 |

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
2. 按需最小准备：只安装我当前工作流需要的入口、技能或可选 Hermes 模块，不新建常驻服务。
   开始任务前说明每任务一次的 npm 版本检查；软件包升级须在设备空闲时经单独授权执行。
3. 分层安全验证：先运行不调用外部模型的本地检查（connect + list）。获得授权后再执行
   真实只读协作验证，关闭自有测试会话（确认 no-session），并分别报告源码检查、宿主兼容
   与真实端到端结果及剩余阻塞。
```

链接的 [AI 安装与配置手册（`docs/configuration.md`，简体中文）](docs/configuration.md)是**写给 AI 代理的执行说明**，不是让你逐项手填的配置问卷。中英文 README 覆盖完全一致的命令、参数与验证边界。

<details open>
<summary><strong>方式二：在 POSIX 终端直接运行（连续可执行路径）</strong></summary>

#### 前置条件

- **协调主机：** 支持 `fcntl` 的 POSIX Python 3.9+（仅用标准库，无需 `pip install`）。端到端协调已在 macOS 实测；Windows 支持作为发起客户端或执行设备，不作为 Python 协调主机。
- **执行设备（阶段 B 真实协作）：** Node.js（>=22.13.0）、npm、导出公共 `acpx/runtime` 的 ACPX，以及已安装且通过原生流程登录的目标 CLI（`omp`、`codex`、`pi` 或 `agy`）。
- **ACPX 版本兼容：** 本机 AGY（`1.5.3`）、OMP 与技能传递续轮已在 **ACPX 0.22.0** 实测（跨设备 Windows SSH OMP 在 **0.21.1** 实测，Shared Runtime 基线 **0.20.0**），不硬编码锁死单一版本。

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

在**同一个 shell** 中继续（复用已赋值的 `CALL_CLI_HOME`、`CALL_CLI_DB` 和 `CLIENT_ID`），发起一次只读协作：

```sh
# 检查执行设备上的 ACPX 版本
acpx --version

# 启动只读 OMP 任务检查当前仓库（不传 --write，不授予写权限）
TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  --client "$CLIENT_ID" start --device local --agent omp --cwd "$CALL_CLI_HOME" \
  --timeout 120 --prompt 'Read collab.py and summarize the CLI subcommands in 3 bullet points. Do not modify files.')"
printf '%s\n' "$TASK_JSON"

TASK_ID="$(printf '%s' "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

# 有界短等待（每次最多 5 秒）；后续调用将 --after 更新为返回的游标
python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  wait --task "$TASK_ID" --after 0 --timeout 5
```

**阶段 B 可观察的成功判据与后续操作：**
- **任务创建：** `start` 返回包含 16 位十六进制 `id`（`TASK_ID`）、`"session": "call-<id>"`、`"state": "queued"`（或 `"running"`）及 `"turn": 1` 的 JSON。
- **进度与结算：** 用返回的整数游标 `after` 重复短调用 `wait --task "$TASK_ID" --after "$AFTER" --timeout 5`。本轮结束时，`task.state` 变为 `"completed"`（若发出 `CALLCLI_QUESTION` 则为 `"waiting_reply"`），`task.exit_code` 为 `0`，`task.stop_reason` 为 `"end_turn"`，`events` 中出现带 `answer_tail` 的 `"token": "1:settled"` 事件。
- **回答问题或执行期改向（按需）：**
  - 若 `task.state` 为 `"waiting_reply"`，按 `task.question_id` 的精确编号作答：
    `python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" reply --task "$TASK_ID" --question-id "$QUESTION_ID" --prompt '你的回答'`
  - 若要在运行中或结算后改向，传入幂等安全的 `--command-id`：
    `python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" intervene --task "$TASK_ID" --command-id rev-1 --prompt '修订后的范围'`
    随后继续短 `wait`，由协调端取消旧轮、确认结算并在同一会话启动第 2 轮。
- **调用 AGY 或传递任务技能：** 调用 AGY 前须先在执行设备运行发现脚本获取当前广告的模型 ID（不同设备/账号返回的候选模型可能不同，不能预设任何设备都有固定模型 ID）。下方示例省略了默认 `--effort`；若显式传入 `--effort`，其值必须从 `models.cjs` 返回的 `thought_level.options` 中核验：
  ```sh
  # 1. 发现本执行设备当前广告的精确 ACP 模型 ID 与 thought_level.options
  node "$CALL_CLI_HOME/models.cjs" "$CALL_CLI_HOME"

  # 2. 将 MODEL_ID 设为本次 availableModelIds 返回的精确值，独立发起 AGY 任务、短等待结算并关闭（确认 no-session）
  MODEL_ID="gemini-4-argon"  # 仅作示例；请按当前执行设备本次发现结果选择
  AGY_TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    start --device local --agent agy --model "$MODEL_ID" \
    --cwd "$CALL_CLI_HOME" --skill reference="$CALL_CLI_HOME/skill" \
    --prompt 'Read the passed skill as reference and list the supported actions.')"
  printf '%s\n' "$AGY_TASK_JSON"

  AGY_TASK_ID="$(printf '%s' "$AGY_TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

  # 有界短等待（按返回游标更新 --after，直至 AGY_TASK_ID 结算）
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    wait --task "$AGY_TASK_ID" --after 0 --timeout 5

  # 确认 AGY_TASK_ID 已结算（completed 或 cancelled）后关闭会话
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    close --task "$AGY_TASK_ID"
  ```
- **关闭会话与清理核验：** 确认 OMP 任务（`$TASK_ID`）已结算（`completed` 或 `cancelled`）后关闭该会话：
  ```sh
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" close --task "$TASK_ID"
  ```
  `close` 只有在 ACPX 读回确认 `status_snapshot` / `no-session` 并清理任务技能快照后，才会返回 `"state": "closed"`。切勿仅因关闭了单个 OMP 任务就直接 `rm -f` 删除共享的 `$CALL_CLI_DB`——清理前应先通过 `list` 核验库内所有相关任务（如 `$TASK_ID` 与 `$AGY_TASK_ID`）均已处于 `closed`，避免遗留未关闭的 AGY 或 OMP 会话。

</details>

> [!NOTE]
> **版本检查不是自动升级：** 每个逻辑新任务只查询一次 `acpx --version` 与 `npm view acpx dist-tags.latest` 并持久化到 `version_check`。正常派工绝不自动安装包；单独授权的维护升级（`updates.py`）必须在设备无活动任务锁且验证官方仓库来源后执行。

<a id="选择接入方式"></a>
<a id="choose-your-entry"></a>
## 选择接入方式

| 你的工作环境 | 接入入口 | 职责范围与当前验证边界 |
| :-- | :-- | :-- |
| **POSIX 终端或脚本** | `python3 collab.py` | 完整通用协调 CLI（`connect`、`start`、`list`、`status`、`events`、`wait`、`reply`、`intervene`、`cancel`、`close`）。`connect --runtime {omp,codex,pi,hermes}` 仅记录调用方自报声明（返回本地 `client_id` 句柄），不是已认证的原生宿主身份。 |
| **OMP、Codex 或 Pi** | `skill/` 技能包 + 已配置的协调入口 | 指导发起端 AI 完成环境发现、在线协调与技能传递；技能本身不安装系统依赖，也不代替目标 CLI 原生登录。 |
| **其他机器（跨 SSH）** | `remote-client.cjs` | 单文件 Node SSH 客户端，将 JSON 命令转发至 POSIX 协调主机的 `collab.py`。 |
| **Windows 受限 Codex 环境** | `remote-client.ps1` / `codex-ssh.cjs` | `remote-client.ps1` 规避已观察到的 Windows Codex 沙箱内 Node `spawn(ssh) EPERM`（当前仅核验 macOS 协调端）；`codex-ssh.cjs` 通过一次性 InteractiveToken 任务在已登录桌面上下文启动新 Codex，规避 Session 0 `0xC0000142`。 |
| **Hermes 宿主** | 可选 `call_cli` 插件（`__init__.py`、`plugin.yaml`） | 捕获原生 `HERMES_SESSION_*` 来源身份，经宿主 terminal 工具后台运行 runner，并在调用 `status`/`events` 时推进已授权的 `intervene`。加载更新后的工具 schema 需按宿主支持方式刷新工具表或新开 Hermes 会话（原会话工具快照可能仍为旧版）；宿主当前会话实时消费与 Telegram 投递（Issue #1）仍需单独宿主验收。 |

**终端安装与 CLI 内加载属于同一条配置路径的先后步骤。** 必须一起部署的 14 个运行时文件清单与各 CLI 内加载验证方式详见[集成手册（`docs/integrations.md`，简体中文）](docs/integrations.md)。

<a id="核心能力"></a>
<a id="capabilities"></a>
## 核心能力

### 1. 原生 AGY 支持与持久工具放行（`agy.py`、`models.cjs`）

- **实时 ACP 模型与 effort 发现：** 在执行设备运行 `node models.cjs /absolute/workdir`，通过 ACPX 公共 `inspectAgentModels` 读取当前广告的模型候选。不同执行设备/账号返回的模型候选可能不同，须按该设备本次发现结果从 `availableModelIds` 中选择精确 ID 传入 `--agent agy --model ID`（Hermes：`agent: "agy", model: "ID"`）；可选 `--effort VALUE` 须从 `models.cjs` 返回的 `thought_level.options` 中核验（已在 **AGY 1.5.3 / ACPX 0.22.0** 实测 `gemini-4-argon` / `medium`、`claude-opus-5-5` / `medium` 与 `gemini-3.8-flash` / `low`）。绝不从显示名猜测或剥离后缀。每轮提交 prompt 前由原生 CLI `set` 设置并经 `sessions show` 精确读回核验，失败直接停止，不静默回退模型或切换 provider。
- **默认 `approve-all` 审批与工作区写占用分离：** 新建 AGY 任务默认持久化 `agy_approval: "approve-all"`（即使 `write` 为 `false`），放行任务授权范围内的 shell 与原生工具，同时提示词优先引导使用原生 read/search 工具。若需严格只读审批，显式传入 `--agy-approval approve-reads`（Hermes：`agy_approval: "approve-reads"`），此时 shell/写请求会被非交互拒绝。**工具审批策略与 `write: false` 都不是操作系统或文件系统沙箱；** 获授权编辑文件仍须另传 `--write` 以声明写权限并占用工作区。`reply`/`intervene` 续轮保留该策略；缺省该字段的旧任务仍按原 `write` 映射，OMP/Codex/Pi 的默认策略（缺省 `approve-reads`，`--write` 选 `approve-all`）保持不变。
- **如实结算业务结果：** 协议层 `end_turn` / exit `0` 不直接等于业务成功。对 AGY 任务，若任一工具调用状态为 `failed` 且最后一次工具事件后既无最终回答也无待答问题——或公开输出最后一个非空行恰为致命错误标记 `⚠ Agent execution terminated due to error.`——该轮如实结算为 `unknown` 并记录 `settlement_note`（不自动重放）。待答问题仍保留 `waiting_reply` 可正常回复（致命错误最终行优先覆盖）；工具失败后若恢复并输出最终回答则可正常完成。公开事件仅保留安全工具标识符、类别与状态；因当前公共 Shared Runtime 事件不暴露权限决策，`permission_diagnostics.permission_decisions` 统一记为 `unobservable`，不把工具失败臆测为权限拒绝。详见 [AGY 模型与审批工作流（`skill/references/agy-models.md`）](skill/references/agy-models.md)。
- **安全认证诊断：** 失败轮可识别精确 `error.detailCode: "AUTH_REQUIRED"`，或通过公开 `runtime.watchSession()` 做有界被动回放（1 秒／1000 条当前请求事件／10000 条总事件）。后者要求精确 host request、turn_started、唯一主 prompt RPC／response 及 failed turn_result，且结构字段 `error.code === -32000`、`data.authRequired === true` 同时成立。回调 ID 碰撞、重复、缺 journal 或超时均保持原因未知。只回流固定类别和来源标签；不匹配错误正文、不回流原消息／任意 data／adapter stderr，不扫描 AGY 日志，不改凭据或重放。
  已在安装的 ACPX 0.22.0 配合隔离合成 ACP 端点实测认证分类、普通错误不分类、正常结果不变、单次 prompt 与 close/no-session。通用 queue detailCode 本身不是认证证据；fatal-only executor HTTP 401 与认证生命周期仍未解决，也不证明 Hermes/Telegram 投递通过。

### 2. 按任务固定传递技能（`skills.py`、`skill-stage.cjs`）

- **发起端通用的公共契约：** 无论从 `collab.py` 还是 Hermes 发起，均可通过同一任务契约传递技能：可重复的 `--skill MODE=PATH`（`location: "origin"`，协调端技能目录）、`--target-skill MODE=PATH`（`location: "target"`，执行端已有技能目录，含 Windows 绝对路径），或 `--skills-json ARRAY`（Hermes / Python `Collaboration.start` 的 `skills` 数组，可带 `source` 来源元数据）。
- **本地优先选择与公开仓库线索：** 发起端先检查本地技能并实际阅读 `SKILL.md`、相对 `references/`/`scripts/` 及依赖要求。本地不足再检索公开技能：仓库 star 数与最近实质内容更新（区分真正的工作流/代码更新与仅改 license、格式或权限位的提交）作为筛选线索而非唯一质量保证。选定公开技能后在 scratch 固定具体 commit SHA，不运行全局安装。
- **三种传递模式（`required` / `optional` / `reference`）：**
  - `required`：目标必须实际读取 `SKILL.md` 及所需资源并按适用方法执行；不适用或缺少依赖须如实报告 `not_applicable` / `unavailable`。若 `required` 技能暂存失败，不会提交业务 prompt。
  - `optional`：目标按任务价值读取和采用，未采用时说明原因（`not_adopted`）。
  - `reference`：作为非约束性参考材料按需查阅。
- **SHA-256 快照、临时目录暂存与逐轮回执：** `origin` 技能在任务创建时生成每文件 SHA-256 清单与私有快照（`0700`/`0600`，自动跳过点文件、符号链接、`node_modules`、`__pycache__`、`.pem`/`.key`、`id_*`、`credentials*` 与 `auth.json`，上限为**单文件 4 MiB、单技能包 16 MiB、1000 个文件**）。每轮执行前通过现有本机/SSH stdin 通道暂存到执行端临时目录 `<os.tmpdir()>/call-cli-skills/<task>/<skill>/<sha>/`。`reply`/`intervene` 续轮复用同一固定快照；`close` 成功后删除协调端快照并清理执行端临时目录。目标每轮结束前输出 `CALLCLI_SKILL <id> <read|adopted|not_adopted|not_applicable|unavailable> <原因或证据>`；缺少 `required` 回执仅标记 `skill_verification: "unverified"`（列入 `skill_report_missing`），不会把已完成的有效业务结果降级为 `unknown`。
- **读取边界：** ACPX 0.22.0 的 `fs/read_text_file` 回调仅允许读取 `cwd` 子树内路径，而 agent 原生文件读取工具（已在 macOS/Windows OMP 与 macOS AGY 实测）可直接读取上述临时目录，无需污染项目 `cwd`。仅依赖受限回调的目标如实报告 `unavailable`。详见[技能选择与传递指南（`skill/references/skill-passing.md`）](skill/references/skill-passing.md)。

### 3. 多 CLI 并行协作、可续读事件与模块化后端

- **多 CLI 并行执行与单写者锁：** 同一设备或跨 SSH 可同时并行运行多个目标 CLI（如 AGY 与 OMP 并发工作），按 origin/client/task/session 严格隔离。`--write` 与可选跨设备 `--resource` 提供单写者目录占用保护；只读任务（包括 `write: false` 下默认 `approve-all` 的 AGY 任务）可与另一任务的合法写者安全并存。
  仅派工和消费事件的中继须保持 `write:false`，即使在同 cwd 发起内层 writer；只有实际编辑者占写锁。scratch cwd 可选作职责隔离，不是绕过锁的条件；共享任务库与锁语义不变。
- **持久 `seq` 游标与原子结算（`bridge.py`、`runner.py`、`events.py`）：** runner 输出通知与 `events`/`wait` 共享 SQLite 持久 `rowid`（`seq`）游标。同一工具的重复相同状态自动去重（stdout 普通工具通知每轮最多 12 条，失败状态不受限，全部记录均可经 `events` 查询）。每轮结束时在同一个 SQLite 事务内原子提交任务状态与唯一 `<turn>:settled` 事件（含 `question_ids`、最长 1000 字 `answer_tail` 及待执行介入的 `next_action`），避免 `close` 或 `intervene` 并发竞争丢失结算通知。
- **模块化后端扩展接口（`backends.py`、`acpx_backend.py`）：** 任务归属、轮次状态机、介入与写锁由 `bridge.py` 和 `runner.py` 统一管理。`backends.py` 注册实现 `prepare_start(model, effort, approval)`、`run(store, task, emit)` 与 `control(task, action, timeout)` 的后端适配器。非 ACP 后端可直接注册在同一表中，无需新增守护进程或独立状态库（已由 `test_backends.py` 中的非 ACP fake 完整生命周期契约验证）。详见[后端扩展接口（`docs/integrations.md#后端扩展接口`）](docs/integrations.md#后端扩展接口)。

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
发起 CLI                        Call CLI（Store + Runner）            目标 CLI（经 ACPX / 后端适配器）
   │                                       │                                        │
   ├──── 1. start（含可选技能包）─────────►├──── 设备锁 + 会话创建 + 技能暂存 ─────►│
   │◄─── 2. wait / events (--after seq) ───┤◄─── CALLCLI_STAGE / 工具 / QUESTION ───┤
   ├──── 3a. reply (--question-id) ───────►├──── 同一原生会话启动 turn+1 ──────────►│
   ├──── 3b. intervene (--command-id) ────►├──── 请求取消当前轮 → 确认取消结算 ────►│
   │     （下次 wait/status 推进续轮）────►├──── 同会话启动修订轮 + 复用固定技能 ──►│
   │◄─── 4. <turn>:settled + close ────────┼◄─── turn.result + close (no-session) ──┤
```

**控制语义区分（`steer` vs. `queue` vs. `cancel+resume`）：** ACPX 0.22.0 公共 `startTurn` 明确拒绝执行期热插 `steer`，而进程内排队（`queue`）会等待当前轮结束，无法即时改向。因此 Call CLI 的 `intervene` 采用明确的**取消并同会话续轮（cancel-and-resume）**契约：保存幂等 `command_id` → 请求取消当前运行轮 → 确认取消权威结算 → 在下一次 `wait`（通用 CLI）或 `status`/`events`（Hermes）时于**同一原生会话**启动修订轮。`pending`/`queued` 仅表示已登记，不代表已生效；`unknown` 结果保留现场供对账，绝不自动重放。

<a id="证据与边界"></a>
<a id="evidence-and-boundaries"></a>
## 证据分层与验证边界

Call CLI 严格区分**源码已实现、离线/合成测试已通过、本机已安装运行核验、跨 CLI 真实端到端执行与宿主用户收到**（详见 [`skill/references/core-requirements.md`](skill/references/core-requirements.md) 与 [`VERIFICATION.md`](VERIFICATION.md)）：

| 验证层级 | 已核验范围 | 明确边界／未宣称范围 |
| :-- | :-- | :-- |
| **源码与离线测试套件** | deadline 返工后的发布候选：Python `114` 项（`109` 通过、`5` 个 opt-in 跳过），四个 Node 套件共 `51` 项通过，详见 `VERIFICATION.md`。 | 假传输与合成端点不证明真实模型行为或宿主通知路由。 |
| **原生 ACPX 传输集成（`RUN_NATIVE_ACPX_TESTS=1`）** | deadline 返工后，`test_native_shared` 与 `test_native_skills` 的全部三个 opt-in 方法在安装的 ACPX 0.22.0 上通过。 | 真实 ACPX 传输配合合成端点，不是真实 LLM 模型；默认套件跳过原生/模型探针。 |
| **本机与跨设备 SSH 真实模型协作** | 已在 macOS（**ACPX 0.22.0**、**AGY 1.5.3** 的 `gemini-4-argon` / `medium`、`claude-opus-5-5` / `medium`、`gemini-3.8-flash` / `low` 及本机 **OMP**）实测：模型/effort 读回、AGY + OMP 并发执行、运行中 `intervene` 取消续轮、本地/公开 PDF 技能传递；跨设备 Windows SSH **OMP**（`ACPX 0.21.1`）与 **Pi**/**Codex** 对应测试场景已验。 | 每次实测仅证明所跑通的具体 CLI、模型、入口与设备组合；不外推所有未测 CLI/设备组合、任意后台子进程强杀或桌面 GUI 自动化。 |
| **宿主原会话消费与 Telegram 投递** | 可选 Hermes 插件（`__init__.py`）已在源码与单测中实现来源捕获、后台 runner 启动、技能 schema 与 `intervene` 状态推进。 | **未宣称全链已验收：** 加载新工具 schema 需按宿主支持方式刷新工具表或新开 Hermes 会话（原会话快照可能仍旧，不宣称当前对话内已成功重载）；宿主当前会话实时消费、Telegram 空通知问题（**Issue #1**）、上游 AGY 错误输出（**Issue #2**）以及离开协作后的离线自动唤醒仍保持开放或属独立范围。 |

```sh
# 运行标准离线验证套件（无需模型凭据或联网）
python3 -B -m unittest discover -v
node --test test_skill_stage.cjs test_shared_prompt.cjs test_models.cjs test_codex_ssh.cjs

# 可选：真实本机 ACPX queue-owner + 合成 ACP 端点集成测试（无真实大模型调用）
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest test_native_skills test_native_shared -v
```

<details>
<summary><strong>授权前请阅读：范围与安全边界</strong></summary>

- **只新建协作会话：** Call CLI 绝不接入、控制或向用户已经手动打开的终端、TUI 或桌面 GUI 注入消息。
- **审批策略不是沙箱：** OMP/Codex/Pi 默认 `approve-reads`，传 `--write` 时使用 `approve-all`；新 AGY 任务默认持久化 `agy_approval: "approve-all"`（可显式传 `--agy-approval approve-reads`）。审批策略和 `write: false` **都不是操作系统或文件系统沙箱**；SSH 与目标 CLI 仍以当前系统账号权限运行。
- **结果不明先对账：** `queued` / `pending` 仅代表已提交，不代表已生效。遇到 `unknown` 或客户端返回 `do_not_replay: true`，先与原会话和工作区对账，再决定是否重试。
- **技能传递边界：** 技能快照会自动跳过点文件、符号链接、`node_modules`、`__pycache__` 与常见私钥文件名并限制体积/文件数，但它**不是恶意代码沙箱或自动隐私审计**；只传递本任务需要且允许目标模型读取的目录。
- **单协调主机状态管理：** 请将 SQLite 任务库、`client_id` 句柄、提示词与认证保存在用户私有目录。由单个 POSIX 协调主机持有状态与锁，不承诺跨协调主机分布式锁、进程崩溃后的 stdout 通知重放或离开当前协作后的自动唤醒。

</details>

<a id="文档"></a>
<a id="documentation"></a>
## 文档导览

### 给实际操作 Call CLI 的 AI

| 手册 | 内容概要 | 语言 |
| :-- | :-- | :-- |
| [安装与配置（`docs/configuration.md`）](docs/configuration.md) | 环境发现、SSH 别名、ACPX agent 配置、认证、权限、版本检查与交付验收。 | 简体中文 |
| [CLI／模块接入（`docs/integrations.md`）](docs/integrations.md) | OMP/Codex/Pi 技能安装、CLI 内加载、可选 Hermes 插件、14 文件运行时清单与后端扩展接口（`backends.py`）。 | 简体中文 |
| [在线协作（`docs/usage.md`）](docs/usage.md) | `seq` 游标续读、多问题 `reply`、`intervene` 取消续轮、取消、验收与 `close`。 | 简体中文 |
| [当前核心需求（`skill/references/core-requirements.md`）](skill/references/core-requirements.md) | 持续双向协作与分层证据核验的唯一维护正本。 | 简体中文 |
| [任务技能传递（`skill/references/skill-passing.md`）](skill/references/skill-passing.md) | 本地优先选择、公开仓库线索、`required`/`optional`/`reference` 模式、暂存限制与 `CALLCLI_SKILL` 回执。 | 简体中文 |
| [AGY 模型与审批（`skill/references/agy-models.md`）](skill/references/agy-models.md) | `models.cjs` 模型发现、显式 `model`/`effort`、持久 `approve-all` 与 `unknown` 结果诊断。 | 简体中文 |
| [维护源与安装检查（`skill/references/maintenance.md`）](skill/references/maintenance.md) | 每次逻辑启动的维护源/安装检查与空闲设备 ACPX 升级边界。 | 简体中文 |
| [Windows Codex SSH（`CODEX_SSH.md`）](CODEX_SSH.md) | 桌面上下文 `codex-ssh.cjs` 启动入口与原生 PowerShell 客户端（`remote-client.ps1`）。 | 简体中文 |

### 给使用者和贡献者

[验证记录（`VERIFICATION.md`，English）](VERIFICATION.md) · [路线图（`ROADMAP.md`，简体中文）](ROADMAP.md) · [安全说明（`SECURITY.md`，English）](SECURITY.md) · [贡献指南（`CONTRIBUTING.md`，English）](CONTRIBUTING.md) · [许可证（`LICENSE`）](LICENSE)

*注：`docs/assets/render_banner.py` 仅在重新生成仓库配图时使用 Pillow；Pillow 不属于 Call CLI 的运行时或测试依赖。*

## 致谢

基于 [ACPX](https://github.com/openclaw/acpx) 与 OMP、Codex、Pi、AGY、Hermes 的原生能力构建。Call CLI 是独立项目，不隶属于上述上游项目。

<div align="center">

**工具各有长处，协作保持同向。**

[MIT 许可证](LICENSE) · [English](README.md)

</div>
