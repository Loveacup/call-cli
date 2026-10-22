<div align="center">

<img src="docs/assets/call-cli-banner.png" alt="Call CLI — 不同 CLI，同一段协作" width="100%">

**给你的 CLI 一个搭档，而不只是另一个子进程。**

<a href="README.md">English</a> · <a href="README.zh-CN.md"><strong>简体中文</strong></a>

<a href="VERIFICATION.md"><img src="https://img.shields.io/badge/version-0.2.3-8b7cf8?style=flat-square&amp;labelColor=161b22" alt="版本 0.2.3"></a>
<a href="https://github.com/openclaw/acpx"><img src="https://img.shields.io/badge/transport-ACPX-49c5b6?style=flat-square&amp;labelColor=161b22" alt="基于 ACPX"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8cbaff?style=flat-square&amp;labelColor=161b22" alt="MIT 许可证"></a>

[快速开始](#快速开始) · [选择接入方式](#选择接入方式) · [工作原理](#工作原理) · [文档](#文档)

</div>

---

Call CLI 通过轻量协调层连接 **OMP、Codex 和 Pi**。新建协作会话、观察执行进展、回答对方的问题，并在执行期间调整方向——**既能在本机，也能通过你现有的 SSH 跨设备工作。**

**不强制依赖 Hermes，不新增常驻服务，不接管你已经打开的终端会话。**

## 为什么需要 Call CLI

你在一个 CLI 里规划，让另一台机器上的 agent 实现，再交给第三个 agent 审核。问题和修订通过发起端往返，不必在终端之间手工搬运消息。

| | 能力 | 带来的变化 |
| :-- | :-- | :-- |
| 💬 | **真正的多轮对话** | 按问题编号回复，继续同一个目标会话。 |
| 📡 | **可以据此行动的进展** | 带游标的短等待返回公开阶段、工具状态、问题与结果。 |
| 🧭 | **方向仍由你控制** | 发起 CLI 可以主动纠偏，也能转达你的新要求。 |
| 🌐 | **本机或跨设备** | 一个 POSIX 协调主机，经现有 SSH 连接执行设备。 |
| 🔒 | **如实表达执行状态** | 依靠 Shared Runtime 的权威结果结算，结果不明不悄悄重放。 |

## 快速开始

### 让 AI 帮你配置

将这段交给你正在使用的 OMP、Codex、Pi 或 Hermes：

```text
请读取 https://github.com/Loveacup/call-cli 中的 README 和
docs/configuration.md，为当前 CLI 配置 Call CLI。

先发现现有安装、SSH 别名、登录与工作目录，只安装我需要的入口、技能或模块。
保留已有文件与凭据，不新建常驻服务。开始任务前，说明首轮 npm 检查和
可能的全局 ACPX patch 更新，并确认在授权范围内。

用真实只读协作验证安装，关闭自有测试会话，报告可用范围和剩余阻塞。
```

配置手册是 **AI 的执行说明**，不是让你填写一份新配置文件。详细 AI 手册目前以**简体中文**维护；英文与中文 README 保持相同的功能范围。

<details>
<summary><strong>更习惯终端？直接使用源码入口。</strong></summary>

#### 环境要求

- **协调主机：** 能使用 `fcntl` 的 POSIX Python。核心路径已在 macOS 验证；Windows 可作为客户端／执行设备，不作为本项目的 Python 协调主机。
- **执行设备：** Node.js、npm、ACPX，以及已配置适配并完成认证的目标 CLI。
- **已验证基线：** ACPX **0.20.0**，要求 Node.js **22.13.0+**。适配配置与平台限制见 [AI 配置手册](docs/configuration.md)。

```sh
git clone https://github.com/Loveacup/call-cli.git
cd call-cli
python3 collab.py --help
acpx --version
```

当前按源码分发，没有已发布的 Call CLI npm／pip 包或通用一键安装器。

#### 启动一个只读协作

在源码检出目录、同一个 POSIX shell 中执行。示例不授予写权限。

```sh
CALL_CLI_HOME="$PWD"
CALL_CLI_DB="$HOME/.local/state/call-cli/tasks.sqlite3"

CLIENT_ID="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  connect --runtime omp | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"

TASK_ID="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  --client "$CLIENT_ID" start --device local --agent omp --cwd "$PWD" \
  --timeout 120 --prompt '只读查看当前项目，概括主要入口；不修改文件。' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  wait --task "$TASK_ID" --after 0 --timeout 5
```

保存真实 client/task ID 和返回的 `after` 游标。继续短等待；收到问题后按准确编号 `reply`，需要改向时用稳定的 command ID 调用 `intervene`。对应轮真正结算后检查结果，再 `close`；不能把关闭正在执行的任务当作取消。

client ID 是本地协作句柄；`--runtime omp` 只是声明，不是已认证的原生 CLI 身份。完整命令与收尾方法见[在线协作手册](docs/usage.md)。

</details>

> **首次派工前注意：** v0.2.x 会检查 npm 稳定版，并可能在来源、版本和空闲检查满足时全局安装兼容的 ACPX patch。目前没有禁用开关。严格冻结环境需先评估，并确认相关副作用在授权范围内。[查看细节](docs/configuration.md#acpx-版本检查与更新)

## 选择接入方式

| 你在哪里工作 | 使用方式 | 能力范围 |
| :-- | :-- | :-- |
| 普通终端／脚本 | `python3 collab.py` | 全部通用协调动作。 |
| OMP、Codex、Pi | `skill/` 技能包 + 已配置的入口 | 由 AI 执行协作；技能本身不安装依赖，也不完成认证。 |
| 其他机器 | `remote-client.cjs` | 通过一次性 SSH 调用协调主机。 |
| Windows 受限 Codex 环境 | `remote-client.ps1` | 对已观察到的 Node spawn SSH `EPERM` 使用原生 PowerShell/.NET 替代；目前仅核验 macOS 协调端。 |
| Hermes | 可选 `call_cli` 插件 | 原生来源绑定与后台通知；**不提供**通用 `intervene` 动作。 |

**终端安装与 CLI 内加载是同一条配置路径的不同步骤。** [集成手册](docs/integrations.md)说明应该安装哪一层、AI 如何加载它，以及怎样验证真实入口而不叠加冲突安装。

## 工作原理

```text
发起 CLI                          Call CLI                        目标 CLI
   │                                │                               │
   ├────────── 新建任务 ────────────►│──── 本机 / SSH + ACPX ────────►│
   │◄───────── 进展 / 问题 ──────────┤◄──────── 公开事件 ──────────────┤
   ├────────── 回答 / 介入 ─────────►│──────── 同会话续轮 ────────────►│
   │◄───────── 结果及收尾状态 ────────┤◄──────── 权威轮次结果 ──────────┤
```

协调端持有小型 SQLite 任务库和有界 worker。ACPX 提供原生会话与 queue-owner 生命周期，通过公开的 `acpx/runtime` API 获取权威 `turn.result`。公开事件用于进度与问答，不用于猜测最终结算。

执行中改向明确采用：**提出介入 → 取消旧轮 → 确认结算 → 同会话续轮 → 核验修订结果**。这不是对任意工具都能无损热修改或暂停／恢复的承诺。

## 文档

### 给实际操作 Call CLI 的 AI

| 手册 | 内容 | 语言 |
| :-- | :-- | :-- |
| [安装与配置](docs/configuration.md) | 环境发现、SSH、适配、认证、权限与交付验证。 | 简体中文 |
| [CLI／模块接入](docs/integrations.md) | 终端安装、技能加载与可选 Hermes 插件。 | 简体中文 |
| [在线协作](docs/usage.md) | 进度、问答、介入、取消、验收与关闭。 | 简体中文 |
| [Windows Codex SSH](CODEX_SSH.md) | 桌面上下文执行与原生 PowerShell 客户端模板。 | 简体中文 |

### 给使用者和贡献者

[验证说明](VERIFICATION.md) · [路线图](ROADMAP.md) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

<details>
<summary><strong>授权前请阅读：范围与安全边界</strong></summary>

- **只新建协作会话。** 不接管已经手动打开的终端、TUI 或桌面 GUI。
- `queued`／`pending` 仅表示提交，不表示生效。`unknown`／`DO_NOT_REPLAY` 先对账，再决定是否重试。
- `--write` 使用 ACPX `approve-all`，是审批策略，**不是文件系统沙箱**；SSH 与目标 CLI 仍使用相应系统账号的权限。
- 任务库、句柄、正文、回答与认证保存在私有位置。“公开事件”不等于内容已脱敏。
- 由一个协调主机持有状态与锁，不承诺跨协调主机分布式互斥、重启恢复或离开当前协作后的自动唤醒。
- 源码测试、已安装入口和全部平台／CLI 覆盖是不同结论，详见[验证说明](VERIFICATION.md)。

</details>

## 开发与验证

Python 核心使用标准库。**v0.2.3 基线**为 Python discovery 59 项（58 通过、1 项原生集成默认跳过）、Node facade 11 项，以及另行运行的四类原生集成场景。原生 endpoint 是明确标注的合成 fixture，不冒称真实模型。

```sh
python3 -B -m unittest discover -v
node --test test_shared_prompt.cjs
node --test test_codex_ssh.cjs
# 可选：创建并关闭本机自有 ACPX 会话，合成 endpoint，无模型调用
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest -v test_native_shared
```

真实模型与跨设备验证需要执行授权和现有登录，私有运行记录不随源码分发。`docs/assets/` 中的可选首图渲染器使用 Pillow，**不属于运行时依赖**。

## 致谢

基于 [ACPX](https://github.com/openclaw/acpx) 与各目标 CLI 的原生能力构建，Call CLI 不隶属于这些项目。


<div align="center">

**工具各有长处，协作保持同向。**

[MIT 许可证](LICENSE) · [English](README.md)

</div>
