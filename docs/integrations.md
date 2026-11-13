# 给 AI：接入技能与 Hermes 模块

先完成[环境与入口发现](configuration.md)。**技能、客户端、Hermes 插件不是同一个安装对象**：技能指导 AI 如何协作，客户端负责 SSH 传输，插件提供 Hermes 宿主工具。只安装用户需要的部分。

## OMP、Codex、Pi：安装技能

### 终端方法

先定位当前 CLI 的实际技能目录，检查有没有同名安装。标准目录：

- OMP：`~/.omp/agent/skills/call-cli/`。
- Codex：`~/.agents/skills/call-cli/`。
- Pi：支持 `~/.agents/skills/call-cli/`，也支持其自身技能目录。

以已确认的 `REPO` 和 `SKILL_DEST` 为参数，复制整个 `skill/` 目录，保留 references/templates，不只复制 `SKILL.md`。以下是 POSIX 执行模板，变量应由你根据环境发现填写，不是要求用户手填：

```sh
# REPO=/absolute/approved/call-cli
# SKILL_DEST=/absolute/confirmed/skills/call-cli
if [ -e "$SKILL_DEST" ] || [ -L "$SKILL_DEST" ]; then
  printf '%s\n' 'Existing skill found: inspect and reuse; do not overwrite.'
else
  mkdir -p "$(dirname "$SKILL_DEST")"
  cp -R "$REPO/skill" "$SKILL_DEST"
fi
```

Windows 使用 PowerShell 的 `Test-Path`、`New-Item`、`Copy-Item` 完成相同操作；先识别现有目录和重解析点，不以 `-Force` 覆盖未知内容。链接安装也可以，但必须确认源目录会长期存在，且不会形成断链或同名重复技能。

仓库 `skill/` 已整理为不含维护者设备默认值的分发模板；用户自己的实际设备清单保留在本地，不提交到项目。安装或发布更新时不要把本机私有运行技能整树覆盖到仓库模板。

### CLI 内加载方法

安装文件后，再验证 AI 真正加载了该技能：

- **OMP**：让模型读取 `skill://call-cli`；不支持该 URI 的版本，读取发现到的 `SKILL.md` 绝对路径。启动新会话，或使用当前版本实际支持的资源重载方式。
- **Codex**：用 `$call-cli` 明确引用；检查原生技能发现是否包含 Call CLI。技能菜单和重载行为以当前版本为准。
- **Pi**：使用 `/skill:call-cli`；修改技能后可用当前版本的 `/reload` 再发现。Pi 官方支持 `.agents/skills/`。

不要仅凭目录存在判定成功。确认模型能够读取技能和其引用，再走实际入口 `connect/list`；需要真实执行时按[配置手册的验收步骤](configuration.md#6-验证并完成交付)完成。

用户可在 CLI 内直接要求：

> 读取 Call CLI 的 `docs/configuration.md` 和 `docs/integrations.md`，在当前 CLI 实际使用的目录加载它的技能。保留已有同名安装，按现有协调入口配置，验证技能被模型读取并完成最小只读协作；不要新建常驻服务或复制认证。

## Hermes：安装可选 `call_cli` 模块

只有用户需要 Hermes 宿主来源绑定与原生后台通知时才做此分支。普通跨 CLI 协作不需要它；它也不能代替其他 CLI 消费事件。

Hermes 宿主静默兼容问题另有[独立可选源码补丁](../compat/hermes-silence/README.md)，只支持文档指定的精确公开 commit；不属于插件或 Call CLI 核心，不会自动应用、修改配置或重启服务。先核对整文件版本与验证边界，版本不匹配即停止。

### 终端方法

先检查当前 profile、已有插件和当前版本帮助。下面是已核对的原生命令形式；不要在另一个 profile 上代替用户安装：

```sh
hermes plugins install Loveacup/call-cli --no-enable
hermes config set plugins.entries.call-cli.settings.enabled true
hermes plugins enable call-cli
hermes plugins doctor call-cli
```

如需固定源码，`install` 可加 `--ref` 和明确的 40 字符 commit。已有插件时先识别其来源和本地改动，按原生更新机制或原有部署协议处理，不直接重装。

这里有两层开关：`plugins enable` 控制宿主加载；`settings.enabled` 是本插件自身的业务开关。配置通过 Hermes 原生 CLI 修改，不要求用户手工编辑配置文件。当前 SDK 的插件相对配置读取位置是 `plugins.entries.<plugin_id>.settings`；旧版本路径或 CLI 选项不同就查对应帮助，不猜字段。

### Hermes 内调用

新工具需要新会话加载。已打开的会话不要强改工具集，也不要为让验证通过擅自重启 gateway。配置好后，在能够看到 `call_cli` 的真实宿主会话中验证：

```json
{
  "action": "start",
  "device": "local",
  "agent": "omp",
  "cwd": "/absolute/approved/workspace",
  "prompt": "只读检查指定文件并报告结果，不修改文件。",
  "write": false
}
```

上例无执行预算；Hermes 模型工具拒绝 timeout，OMP 模型工具同样收口，旧缓存字段会明确报错。非常规显式硬预算仍可通过原 CLI/Python start 接口表达，shell 不受此 schema 约束；存量显式值在受支持续轮清除前继续生效。保留真实返回的 task.id 和 process ID，后续动作只用该 ID。宿主捕获 profile/session/platform/chat/topic，模型不能填造来源。

原生工具支持 `start/list/status/events/reply/intervene/cancel/close`。运行期介入使用精确 `task_id`、修订 `prompt` 和必填稳定 `command_id`；重试复用相同 ID。`pending/cancelling` 不是生效证明：先等旧轮结算，再调用 `status` 或 `events` 推进同 session 续轮；重复查询不重复启动，`unknown` 不续派。`cancel` 撤回待执行介入。通用 CLI 声明 `--runtime hermes` 不产生原生来源绑定或通知能力，两条路径不能混称同一次验收。

**Hermes 的 `status` / `events` 不是纯读操作**：它们会推进此前已由 `intervene` 授权、仍为 pending 的介入；旧轮确认结算后，查询可能启动同 session 的下一轮。没有已授权 pending 介入时不会自行派工；`unknown` 保留不确定结果，不自动重试。终端 dispatch 的迟到回执按 turn 与 launch identity 条件写回，不能覆盖新轮的状态或 process ID。

命令生命周期由共享 Store/runner 按精确 target_turn 更新，两个入口直接读取同一记录：runner 领取为 target_started，正常结算为 target_completed，取消／不确定分别为 target_cancelled／target_unknown。它们不是模型理解或业务生效承诺，waiting_reply 也可属于正常结算；参见[完整状态合同](usage.md#执行中主动改向或传达用户要求)。多轮回复仍沿用原 command ID，不因查询、迟到通知或问题续轮重新派工；已有关闭历史不回填。

升级前没有 `needs_session` / `acpx_created` / `session_creation_attempted` 的旧 Hermes 任务，仅凭 `cancelled`、空 process ID 或字段缺失不能推断会话尚未创建。已有 native record ID 则复用；没有 ID 时，只有第一轮取消终态及该轮 runner 持久事件 `Cancelled before provider launch` 同时存在，才允许续轮首次创建。证据不足会在记录介入或取消旧轮之前明确拒绝，提示先核对旧任务再使用新的 `start`，不猜测重建。取消发生在 runner 启动前、没有此事件的旧记录也遵循拒绝策略。

### Operator-only fixed-target release candidate

This source candidate adds dashboard routes under `/api/plugins/call-cli`: `POST /release/prepare`, `POST /release`, `POST /release/reconcile`, and `GET /release/receipt`. They use the existing Hermes sensitive HTTP authentication gate, including its request-local authenticated verdict over private plugin-host IPC. These fixed-policy dashboard routes accept no caller-selected target, owner transfer or business prompt. The separate native Hermes model tool exposes task-scoped `reconcile` under the controller/metadata admission described below. Hosted audit identifies the authenticated host gate, not a named user absent from that transport.

Activation requires a separately approved, operator-owned mode-0600 local file at `<default-hermes-root>/plugin-data/call-cli/release-policy.json`. Never commit it. It contains exactly `task`, `owner` (the full original Origin object), `turn`, `record`, `acp`, `cwd`, `agent`, `session`, and `submission`; the submission is audit association, not authentication. Only an existing native database at the same `plugin-data/call-cli/tasks.sqlite3` is accepted. Non-default profile requests and caller-selected target/approval fields are refused. No real policy is included or installed by this candidate.

An authenticated prepare returns the pinned metadata and a 120-second one-use confirmation. The same authenticated host surface accepts only `{"nonce":"<prepare result>","operation":"release-only"}`. Admission rechecks the whole snapshot plus a durable task revision, records owner termination metadata, revokes unconsumed commands and commits an unknown reservation and permanent SQL fence before calling the backend. Original owner and business settlement remain unchanged. Old Store connections and runner SQL cannot mutate the fenced task or insert events; this is persistence containment, not an OS execution sandbox. Replays only read the durable receipt, including after a crash or uncertain close; they never automatically retry close.

The adapter is pinned to ACPX 0.22.0's public Shared Runtime exact-record close. Its void return is joined with exact closed-record readbacks and `findSession(...) === undefined` in the pinned name/agent/cwd scope. Receipts explicitly identify this provenance; they are not CLI `session_closed` or `status_snapshot` stdout. Supported close settles ACPX-managed execution and its turn lock. It does not prove arbitrary detached historical descendants or revoke an OS user's separate native resume/recreate permissions. Explicit resume and mapping replacement are not authorized by these routes. Missing/ambiguous proof, timeout or storage failure keeps the reservation unknown. Deployment must separately verify the trusted profile policy, actual loaded payload and absence of a bypassing historical execution path. Do not edit a production DB or activate this surface merely to repeat synthetic verification.


### Ended-owner controller recovery (source candidate)

Native Hermes access is action-scoped. Same-profile/route discovery uses read-only committed owner metadata; inspection never advances a pending intervention. Explicit settled continuation uses observed `expected_turn` and stable `command_id` to install a single `controller` and increment `control_epoch` without changing the original `Origin`. That admitted controller retains status/events/advance/cancel authority after the former owner reopens; former origins are read-only. New tasks emit fixed `--turn`, `--launch-id`, and `--epoch` runner arguments, checked atomically before claim. Legacy argv is accepted only for an unclaimed first turn; it cannot claim a successor. Tasks created by older source versions retain legacy no-question reply compatibility; newly created tasks require the observed turn and command ID.

Native `reconcile` and ended-controller `close` are host-origin, local exact-record operations protected by the existing P2 fence. They require an observed turn and controller authority, reject active work, and establish unknown/HOLD before external action; active writer reservation conflicts fail admission. Reconcile is a physically separate read-only ACPX adapter path (`getStatus` + scoped `findSession`, zero `runtime.close`) and settles only exact record/backend/request ID plus `no-session`, under a reservation snapshot recheck. `close` commits an attempt first and reconciles instead of calling `close` when the task is already unknown. Failures retain stage/reason_code and the fence; duplicates do not reclose. Neither record closure nor DB fence proves arbitrary detached descendants stopped. The fixed-target dashboard policy/release surface is unchanged. This source candidate has synthetic coverage only; the new native actions are not deployed/loaded or live-production verified.

In this slice, new ended-controller recovery is Hermes-plugin-only; standalone CLI and OMP keep their existing action sets. Native reconcile refuses healthy completed/cancelled/waiting_reply tasks before probing or fencing; an explicit recovery close also refuses pending questions. Native receipts label `binding_source=record-resolved` because backend-session identity is read from the exact record, while the dashboard policy pins it independently. That provenance difference and `arbitraryDetachedDescendantsProven=false` remain visible rather than implying all workspace writers stopped.

All public `close` actions now share `release.close_task`, including same-controller Hermes, standalone CLI and OMP. Existing close request shapes remain valid; supplied expected_turn is checked, while an omitted same-controller close observes the current turn without authorizing business continuation. Local exact-record closure always commits the existing durable fence before close and requires structured exact-record probe/readback. Unknown tasks only probe; repeat close never reissues an uncertain close. Cancelled, positively never-created sessions can be fenced closed without backend calls. Missing-record, remote or otherwise unsupported exact-proof backends conservatively remain unknown/HOLD with `exact_record_unavailable`; the prior unbound status/text close is not preserved as success. Non-ACP execution remains supported, but its status-only close cannot release a writer without a supported exact proof adapter.

Native no-question reply is an explicit data-version compatibility boundary: new tasks store `continuation_version=2` and require a caller-observed `expected_turn` plus stable `command_id`; read status first and retain the same intent fields on retry. Existing tasks without the field or at version 1 keep legacy same-controller requests, without claiming the new stale-intent protection. Question-ID replies remain compatible. Cross-controller continuation always uses observed-turn/command admission. No database rewrite/migration is required or performed; never substitute a fresh server-side latest turn for a stale caller intent.

Task and settlement receipts include a recovery card with exact task/turn/controller/epoch, event cursor, question/command identities, write reservation and device/session/record, but no private DB path or command prompt text. The creating surface is persisted and reused by runner events. Notification return is marked configured, not delivery-verified, for native Hermes; exit interception and successor handoff remain uncovered. The OMP adapter redacts session and agent IDs while preserving its opaque host binding.

CLI/OMP `close_receipt` is a diagnostic projection containing only `state`, `phase`, `stage`, `reason_code`, and `external_close_attempted` when present. OMP applies the same whitelist to nested close receipts in the entire response. Full owner/target/proof/attempt audit remains stored internally; output filtering never changes the task Origin or audit record. Recovery-card `capabilities.exact_record_reconcile` describes an unknown task with a local ACPX record, regardless of its historical creating surface (including legacy tasks without one). It is not authorization or proof that the backend is reachable or will verify settlement. Advice uses the current entry point: native Hermes uses `reconcile`, CLI/OMP use `close` in probe-only mode. Unknown entry points, including stored runner events, give neutral exact-record probe guidance without promising an action verb. Missing record or remote device instead directs the caller to retain HOLD and obtain authorized identity/device resolution.
Shared Runtime does not read the CLI's configured agent aliases. For this release path the adapter explicitly supplies the existing documented `omp` -> `omp acp` mapping via public `createAgentRegistry({overrides: ...})`; it does not read or forward ACPX configuration/authentication. The policy retains its Call CLI agent name. Pre-close name/cwd/record/ACP checks remain mandatory under that registry, and the same registry is used for the post-close lookup. An OMP session created with a different custom command is not silently accepted. The default unconfigured SDK registry resolving `omp` literally is not equivalent to a CLI invocation that applies `agents.omp.command`.

Before the first prepare/release, deploy the corrected adapter through the separately approved deployment path and verify its physical payload hash and corrected scoped preflight. An installed adapter without this registry mapping fails only after release admission has committed the permanent fence/unknown reservation. Prepare creates a nonce; release admission consumes it before backend control. Do not submit release against the old adapter or attempt a new nonce as an uncertain-effect retry.

Initialization is explicit in authenticated prepare only: its first successful transaction installs the necessary release tables/triggers, enrolls the exact policy target at revision zero, and binds the snapshot/nonce under the same `BEGIN IMMEDIATE` writer lock. Failed validation rolls back schema/enrollment; concurrent prepares never reset an enrolled revision. Revision triggers update existing enrolled rows only; unrelated task create/update/delete does not add or increment auxiliary rows. Target enrollment survives deletion so delete/recreate and update-based A-to-B-to-A invalidate old confirmations. Constructor binding opens no database and performs no DDL/chmod. Receipt GET opens `mode=ro` with `query_only=ON`, takes no writer lock, and reports `{"state":"not_prepared"}` when schema is absent. No production migration is performed by source verification. Deployment must carry `dashboard/` and runtime dependencies into the physical plugin payload and verify policy `agent` registry resolution against the exact record before closing; a scope mismatch refuses rather than treating an unrelated empty lookup as no-session.

First-deployment prerequisite: the native tasks DB must contain no `release_*` schema objects or `task_revisions` from an earlier candidate build. This candidate is not a migration: when `release_fences` exists, initialization reuses it and does not replace existing trigger bodies. An earlier global-counter schema would therefore retain its old behavior. If previous-candidate objects are found, stop before activation and obtain a separately reviewed migration; do not drop production triggers or assume this source converges them. Repeated prepare on a database initialized by this exact scoped candidate remains supported.

## OMP 原生 extension（实验性、可选）

在新建自有 RPC 实例中单次加载：`omp --mode rpc --no-ui --no-extensions -e /absolute/call-cli/omp-extension.mjs`；不全局启用，不热附着、重启或 reload 现有宿主。`-e` 使用固定 commit 目录的绝对路径，按下方[运行时依赖闭包](#运行时依赖闭包)保留相邻文件；只复制 `.mjs` 不可运行。要求实测 OMP 18.18.1 extension API、Python 3，以及业务执行时现有 ACPX/目标 CLI。可用 `CALL_CLI_PYTHON` 指定解释器，`CALL_CLI_DB` 指定既有共享任务库；默认沿用通用入口的共享 DB，不另建库绕过 writer 互斥。零模型夹具才使用独立 scratch DB，不把它当生产协作隔离方式。

模型工具 `call_cli_omp` 接受 start/list/status/events/reply/intervene/cancel/close，不接受 origin、capability 或 DB。可信 extension 从实际 `ctx.sessionManager.getSessionId()` 和 `ctx.agent.id` 注入绑定，通过 stdin 调用 `omp_bridge.py`。Python 接缝复用 `Collaboration` 和 Store，保存 capability 哈希与宿主绑定；随机原值仅保留宿主内存，不写入模型文本、参数 schema、日志或 `appendEntry`。这是可信本地 extension 提供的绑定，不是远程认证服务；同用户任意代码不在隔离边界内。standalone 的私有 client handle 语义不变。

这是活跃任务轮询，不是 stdout 直接推送，也不是 daemon：每个活跃 binding 用默认 1 秒受管 timer，批次内每个活跃任务各启动一次 one-shot Python/SQLite 读取；批次结束后再计时，无重叠读取。成本随活跃任务数和活跃时间增长，尚未实测吞吐或 CPU，不能据此宣称低开销。无活跃任务时不设 timer；waiting_reply、终态或单任务读取异常暂停对应跟踪，close 移除对应跟踪；显式 status/reply/intervene 等可重新激活。短暂 SQLite busy 保留游标及跟踪，在下一批再读，不生成错误唤醒；其他异常仅暂停该任务，不拖停整个 binding，不自动重派或取消。

非阻塞保护仅用于内部 poll（wait）：初始化与归属读取之前设置 busy_timeout=0，锁竞争返回 busy，不虚构任务状态。bind/start/reply/intervene/close 等非 poll 动作仍保留 Store 的 SQLite 15 秒连接超时及显式失败语义，不返回假 busy-success；这不是所有工具动作都非阻塞的承诺。

每个短批次把 stage 合并为最新一条，连续相同 stage 文本不再唤醒；有变化的阶段仍可主动唤醒纠偏。问题、失败、完成在下一次成功读取时及时以官方 `sendMessage` follow-up 投递，工具状态等无业务变化不唤醒；不是 steer。回答片段合并到结算通知，游标依据实际读到并获宿主队列接纳的批次推进，不依据 stdout 尾号；队列接纳不等于模型已经理解或用户已经看到。同 session 的首次并发 start 共用一次 bind，不串行所有任务；reply/intervene 等重新激活跟踪后，旧读取结果按请求 revision 丢弃并由新读取补齐，不得覆盖新轮状态。intervene 仍是 core 的 cancel+resume，不是 OMP steer。

复核请求沿用 stage + quiet_notice:true，附 notice_kind（quiet/checkpoint）与 elapsed，并保留 turn/seq；这类事件绕过普通 stage 合并和同文去重。公共 runner 复用同一循环/600s 常量，静默与长期活跃共享冷却，每 600s 至多一条，活动不会推迟检查点；取消/结算后停止。发起方据真实状态选择等待、明确介入或取消，不自动终止/续跑，也不要求逐条转给用户。followUp 入队不证明已消费，CLI 依赖活跃 owner wait；退出后无介入/终止保证。reply/intervene 的 clear_timeout 只作用于受支持的下一轮，queued 保守拒绝，详见 usage 的清除契约。新宿主任务无硬预算，旧值不清除仍保留硬截止；启动/控制边界不变。

session switch/branch/tree/shutdown 清理本宿主 timer、丢弃迟到投递，不自动取消、关闭或改写业务状态。runner 复用现有 detached/DEVNULL 启动，one-shot bridge stdout 断开不影响 runner 输出或 Store 持久化。退出后任务可能自行完成，也可能保留 pending/unknown；不承诺离线回注、自动续轮或恢复，操作者需按精确任务对账。主动 cancel 是另一个明确动作，不能用宿主断连代替取消结算。

本轮仅实跑 owned RPC 实例；补充的零模型生命周期检查由真实 extension 调用 close，等待 handler 完成回执并读回原生 status_snapshot/no-session，不把 prompt ACK 当关闭完成。自有合成会话未提交任何业务 prompt，不能替代真实模型执行、原会话消费或完整联合验收；旧两次 AGY 运行由外部 bridge 收尾的事实不变。普通 TUI 未验，ACP client 可 defer agent-initiated turns，不能将 RPC 成功外推为 ACP idle 唤醒或 Telegram 投递。源码使用需在获准的新宿主实例验证，不热附着现有用户会话，不切个人安装。

## 后端扩展接口

`bridge.py` 只管理任务归属、状态机、轮次、问题、介入、游标和 writer 占用；`runner.py` 认领一轮、持久化公开事件并通过宿主原有 stdout 通知。`backends.py` 是唯一后端注册表，两个入口共用 `prepare_start`，CLI choices 与 Hermes schema 从注册表生成。

一个适配器提供三个方法：`prepare_start(model, effort, approval)` 返回持久选择字段；`run(store, task, emit)` 执行已认领轮次，发出安全的 `stage/answer/question/tool` 事件并返回 `TurnResult(state, exit_code, stop_reason, note)`；`control(task, action, timeout)` 提供精确会话控制与关闭后的 `status_snapshot/no-session` 读回。执行中检查共享任务的 `cancelling`，只有执行真正结算才能返回 cancelled；未知结果返回 unknown，不重放。适配器不修改归属、轮次或 writer 占用；现有设备锁可以复用，不新增进程服务或状态库。新增后端只需实现并注册它，随 runner 安装，不必修改其他后端；不要求使用 ACP。

公共 start 接受 AGY 专用 agy_approval、agy_swarm、agy_worker_model；G4C 默认 native，G4A 默认 off，显式模式覆盖。native 唯一 ACP owner 不经 ACPX control/recovery；close 按当前 task/turn 结果、worker add/kill completed 与 unload 证据释放，不要求 ACP 未提供的 rawOutput，也不证明任意后代死亡。异常保 unknown/HOLD。

结算状态与该轮唯一 `<turn>:settled` 事件在同一事务提交，事件带持久 seq、问题 ID、回答摘要和适用的介入续作提示。runner 仅打印事务接受的结算事件；即使其后轮次已推进或进入关闭，也不会丢弃这个已合法结算的通知。被拒绝的旧 runner 不广播终态，普通旧轮事件过滤保持不变；游标补读可恢复已入库事件，不承诺进程崩溃后的 stdout 重放。

ACPX 路径及其他 backend 不变；agy.py 按持久路由选择 native owner。native 复用 Store/runner/安装锁/skill/event；首 end_turn 仍 running，完整 load 观察后台。逐轮清除旧结果与清理证据，当前轮独立对账。AGY unknown 原子事件带 attention_required/next_action，runner stdout 与 OMP 托管轮询 follow-up/Hermes notify 主动返回；真实宿主接收/消费未验，不新增监控平台。详见 [AGY 合同](../skill/references/agy-models.md)。

公共 start 可选 `skills` 数组，两个入口通过 Store 创建固定快照；ACPX 后端每轮在 prompt 前经现有目标命令暂存并加入技能路径/模式提示。其他后端可复用 `skills.stage(store, task, emit)` 与 `skills.guidance(task)`，不使用时不得声称支持读取。目标 `CALLCLI_SKILL` 行规范化为独立 skill 事件，Store 按轮次记录实际自报；required 缺报只标未验证，不改业务结算。契约、限制和选择策略见[技能传递](../skill/references/skill-passing.md)。

## 运行时依赖闭包

安装按入口生成文件与 SHA-256 manifest，不用旧总数推算。当前 import 与相邻资源读取关系如下；这些文件须来自同一固定 commit，源码更新不自动更新已安装副本或内存 schema。

- 公共 core／runner 闭包：`bridge.py`、`native_continuity.py`（Hermes native wrapper 使用；只读当前 profile `state.db` metadata）、`runner.py`、`backends.py`、`acpx_backend.py`、`agy.py`、`agy_native.py`、`events.py`、`transport.py`、`updates.py`、`skills.py`、`skill-stage.cjs`、`shared-prompt.cjs`。后两个资源分别由 `skills.py`、`acpx_backend.py` 相邻读取。`collab.py` 仍直用基础 `Store`，不启用 native metadata continuity。
- 通用 POSIX 入口：公共闭包加 `collab.py`。
- Hermes 插件入口：公共闭包加 `__init__.py`、`plugin.yaml`；manifest 声明 `call_cli`，不声明或全局启用 OMP extension。已有插件若提供 `collab.py`、`models.cjs`、`remote-client.cjs` 等附带入口，按其实际依赖保留，不替换整个物理目录或删除未列文件。
- 实验性 OMP 宿主入口：公共闭包加 `collab.py`、`omp_bridge.py`、`omp-extension.mjs`；`.mjs` 通过相邻路径启动 Python bridge，后者导入 `collab.py`，再复用现有 runner。它不是 Hermes 插件注册项。
- 可选模型发现：`models.cjs` 相邻需要 `shared-prompt.cjs`；实际调用还需设备本机 Node/npm/ACPX。SSH 客户端按所选 `remote-client.cjs`／`remote-client.ps1`／`codex-ssh.cjs` 单独打包，不改变协调核心依赖。
- 公共协作技能：完整 `skill/SKILL.md`、`skill/references/`、`skill/templates/`，包含 OMP 适配、单次 load 和维护检查指导；个人副本另合并私有库存／Hermes 独有内容并验链接，不回拷进公开仓。检查目标仍是 call-cli 与本轮实际参与 CLI 的协作 skill。

本仓库没有自动安装脚本；示例命令不是自动切换授权。发布时从干净提交生成真实闭包清单与逐文件哈希，再验证 bridge 导入、宿主工具注册与零模型 list；原生 close 生命周期、业务执行与原会话实际投递分别验收。切入口和回退遵循[部署流程](../skill/references/deployment.md)，先核活跃任务，不混用运行中的模块。

## 回报用户

简短说明安装的是哪一层、所在设备／profile、实际入口、技能是否加载、真实任务结果和未解决项。安装文件、Doctor、一次模型执行、跨设备闭环分别报告。自动唤醒或 Telegram 多 topic 投递未独立验收时不顺带声称已通过。
