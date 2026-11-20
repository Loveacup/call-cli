# 通用在线协作流程

用于新建协作会话；不接管已手动打开的终端。先从现态设备表与项目 README 定位入口、工作目录和依赖，再核对项目的验收记录。不要把某个协调主机的路径、版本或安装状态固化为其他设备的前提。

## 1. 选择发起端与执行方式

先确认真实发起 CLI 如何调用入口、接收结果和继续行动，再选择目标 CLI／设备。同一入口应供不同发起端复用，不为每对 CLI 单独建桥。通用任务逻辑不依赖 Hermes 环境变量；原生通知适配与通用在线协调分开。

逻辑启动先按[维护检查](maintenance.md)检查本轮维护源与安装现态；角色目标、实际入口支持和已验覆盖分开。通用 connect 接受 omp/codex/pi/hermes 标签；AGY 仅作执行端，不作为发起端，也不列为待补入口或验收缺口。

需要用户执行期介入时，保持发起端在线协调，并使用短工具调用让出控制权；不要把监控写成一个直到最终结果才退出的长 shell 循环。离开当前协作后的自动唤醒是另一种能力，按用户确认的范围处理，不为此削减执行期监控、主动协调或人工介入。

## 2. 建立独立客户端与新任务

已核验的通用实现可用以下调用形式；所有命令应使用同一个明确 DB 和返回的句柄，不依赖工具调用之间保存 shell 变量。

```bash
python3 /absolute/collab.py --db /absolute/tasks.sqlite3 connect --runtime codex
python3 /absolute/collab.py --db /absolute/tasks.sqlite3 --client CLIENT_ID start \
  --device EXISTING_SSH_ALIAS --agent omp --cwd /absolute/target/workspace \
  --prompt '目标、范围、产物和验证方法'
```

按实际发起端选择 runtime 标签；支持的标签从当前帮助读取。client_id 是私有协作句柄，声明的 runtime 不是认证过的原生 CLI 身份。原生身份验证另从真实运行时取证，不由模型编造 session ID 或 Telegram 来源。有长期责任方时由该方创建并保存跨轮任务句柄；短命发起方须先明确宿主支持的持续协调与收尾路径，不能靠新 connect 或父子关系自动接管。目标为 Windows 时设置 powershell；只有已获写入授权才启用 write。保留 client、task、目标 session 与 turn，不用最近会话猜目标。

### Python 库形式（同源脚本派发）

在已装 call-cli 源码的宿主上直接从 Python 派任务：

```python
import sys; sys.path.insert(0, '/absolute/call-cli')
from collab import Collaboration   # 类名是 Collaboration，没有 Collab 别名

c = Collaboration()
t = c.start(client, prompt='目标、范围、产物和验证方法', cwd='/absolute/workspace',
            agent='agy', model='gemini-4-argon', write=True)
# t['id'] 即 task id
```

- `client` 是 **clients 表里的字符串句柄**（`sqlite3 tasks.sqlite3 "SELECT id FROM clients WHERE declared_runtime='...'"` 查），传 dict 会在参数绑定时崩；句柄不存在报 `PermissionError: Unknown client handle`。
- OMP 新启动在 POSIX 与 Windows（经 `acpx/runtime` 结构化 `agentArgv`）均默认启用 `--auto-approve --allow-home` 原生跳过 `~/` 目录限制；但实证 ACPX Shared Runtime（`0.21.1`/`0.22.0`）的 `fs/read_text_file` / `fs/write_text_file` 回调本身由 `FileSystemHandlers.resolvePathWithinRoot` 硬性限定在 `cwd` 子树内且不支持覆写 `fs` 回调（`ACP_INVALID_RUNTIME_OPTION`），此项属于待 PM/用户决策（Option A 维持现有共享架构依赖 CLI 原生工具读写跨目录，或 Option B 后续专项改上游/直接 ACP）的公开上游缺口，而非已接受例外；跨目录写入建议在授权范围内或写 cwd 内 `./out/` 验收后搬移。
- 跨轮 reply/cancel/close 必须复用派发时的 owner 句柄（task `origin.session_id`），换句柄会被锁拒。

常规任务省略执行 timeout，先复核再决定介入/取消。Hermes/OMP 模型工具拒绝该参数；旧 CLI start --timeout SECONDS|none 与 Python timeout= 仅保留非常规显式硬预算兼容，旧值未清除仍会到期，不是软提醒。wait --timeout 5 只是轮询。clear_timeout/--clear-timeout 仅通过受支持 reply/intervene 对下一轮生效，queued 一律拒绝清除，unknown 保留占用；多问与介入替换规则见调用手册。

中继只派工和消费事件时保持 `write:false`，即使内层任务会修改同一 cwd；只有实际编辑者传 `--write`。中继误声明写入会在内层任务创建前正确触发冲突，不是锁泄漏。使用同一个任务库对账，不新增父子交接或绕过锁；另选 scratch cwd 只做职责隔离。

### 写隔离与仓外同级 Worktree（Hermes 管理，未部署源码候选）

- 协调者按任务在普通目录（不强行 Git 化，仅授权任务目录确需隔离时按需 `init-repo`）／单仓直接推进（保护用户既有改动）／**仓外同级** worktree 之间自然判断（不每次询问用户开关，不在纯代码另造策略状态机）。仅需并行或写隔离时，由 Hermes 通过纯标准库薄辅助 `git_worktree.py`（无 daemon、不写 SQLite，**本版本源码**，详见[调用手册](../../docs/usage.md#hermes-透明-gitworktree-薄辅助-git_worktreepy未部署源码候选)）准备**仓外同级** worktree，保留相对子目录偏移并通过既有 `cwd` 派发（避免仓内 `.agents/worktrees/*` 与仓根触发 `bridge.py` 父子锁冲突）。worktree 只隔离检出文件，不是权限沙箱，也不继承主仓未提交脏文件。
- 派工与用户通告写明 **“Git/worktree 由 Hermes 管理”**、`worktree_root`/有效 `cwd`、`branch`、`device`、检出 `HEAD` 基线提交及收敛目标（保持正常成功链路简洁）。建树或复用校验失败时 fail-closed 停工报错，严禁静默回退写入原工作区；返工或续作在确认前序停写后优先串行复用同一检出。

## 3. 运行在线协调循环

1. 调用 `wait --task TASK_ID --after CURSOR --timeout 5`，接续返回游标。
2. 结合 `task.state`、`turn`、公开阶段、工具状态和产物判断进展；**向用户汇报严格由事件与实质进展驱动**（产物、已验证结论、阻塞、待答问题、风险、带明确建议的重大取舍），绝不按固定时间间隔刷屏。授权内常规可逆工程选择自主处理，重大取舍或挤占主线变化带明确建议升级用户（**本版本源码** Slice A 通过 `events.classify_event_delivery` 在保持 SQLite `Store.events` 原始全量落库的同时静默实时 stdout/OMP 常规诊断与流式中间行）。静默不等于失败。
   收到 `stage + quiet_notice` 的复核请求时，必须读取精确 `task/turn/seq`、`review_facts.progress_state`（**本版本源码**区分 `substantive_progress` / `tool_churn_only` / `quiet`，显式识破工具很忙但零实质进展）与真实产物，再明确作出“继续等待 / 自主可逆纠偏或介入 / 带建议升级用户”决定；静默/持续活跃共享每 600s 一次冷却。复核供协调者内部判断，不必逐条转给用户；绝不自动取消/关闭/续跑，Hermes 机器轮仅在确认已消费且无新增义务后才精确回复 `[SILENT]`，事件可读不代表已消费。CLI 依赖活跃 owner 持续 wait，owner 消失不保证及时介入或终止。
3. 收到问题后保存完整身份及准确 token；既有 ACPX 路径使用 `reply --task TASK_ID --question-id TOKEN --prompt ANSWER`（或显式多题 `--question-ids ID1 ID2 ...`）。native AGY 新 owner 有输入能力时用 `dialogue`，必填 expected_turn、稳定 command_id，回答另传互斥的 `question_id`、`question_ids` 或显式片段取代 `blocker_interaction_seq: int`（**安装后以实际宿主 schema 和新 runner 为准**）；原 owner 串行接收，不改变 task.turn。`question_ids: list[str]`（`collab.py` `--question-ids ID1 ID2 ...`、Hermes `call_cli` 与 OMP `call_cli_omp` 的 `question_ids`）与标量 `question_id: str` 严格互斥（`question_id=None` 与单值 `question_id` 兼容；拒绝同时传入、空数组、空串、非字符串、重复 ID、CSV 字符串与联合强转，禁止 trim/改写任何字节），按原字节精确匹配当前轮未答 `questions`，仅在 `command_id` 幂等比对时用 `sorted(question_ids)`；`blocker_interaction_seq: int`（`>= 0`、`bool` 拒绝、`0` 按 `is not None` 处理）仅限 `dialogue`（`reply` 及 `withdraw` 拒绝），与 `question_id`/`question_ids` 三路互斥，未传入时单题/多题/未绑头不变；`reply` 共享 `question_id`/`question_ids` 校验与幂等逻辑并保留既有合并正文（单题 `answers[0][1]`，多题 `'\n'.join(k + ': ' + v ...)`）。缺能力报告 unsupported 并保留占用，不走取消续轮冒充回答。
4. 发起端依据进展主动纠偏，或消费用户在其自身原生输入通道送入的新要求后，按真实能力选择动作。既有 `intervene --task TASK_ID --command-id UNIQUE_ID --prompt REVISION` 保持 cancel+resume，不自动转换为 AGY dialogue；`list`/`status`/`events`/`recovery_status` 均为纯只读（`?mode=ro`），旧轮取消结算后可由原 controller 以同一 `--command-id UNIQUE_ID` 显式重试 `intervene`（或继续调用 `wait`）推进一次 successor。跨路线精确任务状态排查可用 `python3 collab.py --db /absolute/tasks.sqlite3 --client CLIENT_ID recovery_status --task TASK_ID`，仅返回 `resource_recovery` 与闭集 `diagnostic`（不提供跨群批量 `list` 或业务自由文本）。
5. 继续短 wait，区分请求收到、排队与对应轮次已执行。pending／queued 是提交阶段，不是生效证明；重试提交沿用同一个 command_id，不另造一次业务执行。

有界等待必须包含 CLI 初始化、数据库连接与锁竞争，不只限制循环内 sleep。遇锁可明确返回 busy；不可读时返回空状态，不伪造 queued。零等待并发调用可能全部 busy，验收应要求随后一次非竞争有界调用可推进且不重复启动，而不是假定每次探针都取得写锁。

问题身份使用 `CALLCLI_QUESTION Q1 正文`：同一 task/turn 的相同编号、完全相同正文只需一答，仍用首个返回 token 回复；独立同文问题分配不同编号。无编号问题逐条登记；跨轮可重用编号。严格语法、冲突结算与恢复边界见[问题协议](../../docs/usage.md#观察提问与回复)；源码候选需独立验收与部署，不能推定安装副本已采用。

## 4. 明确介入与取消语义

支持热输入时验证真实生效；不支持时明确采用取消旧轮、确认停止、原协作会话续作，不宣传为无损热修改或通用暂停。

| 方式 | 语义 | 当前证据与判定 |
| --- | --- | --- |
| steer | 当前执行轮内接收并采用修订 | 必须核目标原生能力、适配器与传输；界面支持不证明 API 支持 |
| queue | 后续输入等当前轮结束再执行 | 受理／排队不是即时改向，需看后续 turn 与结果 |
| cancel+resume | 终止旧轮，确认结算后同 session 续轮 | 当前 call-cli intervene 使用此方式；可能中断工具，不是无损暂停 |

ACPX 0.22.0 Shared Runtime 的公开 startTurn 明确拒绝 steer；同版 in-process 的 steer 等待活动轮结束，不改变该轮，不能改用它冒充热修改。AGY 1.5.3 一次 initialize 未宣告 steer，只是所探测协议面的证据，不足以否定所有原生入口。OMP 也按实际使用栈逐层核验，不据品牌豁免。以后上游能力改变时重查，不把版本事实永久化。

当前命令记录的 pending/queued 只证明保存与排队；target_started 只证明 runner 已领取精确 target_turn，target_completed 只证明该轮 exit 0 / end_turn 正常结算（可仍有问题待答），不证明模型理解或业务生效。target_cancelled / target_unknown 不表示成功，后者不自动重放；superseded / withdrawn 分别表示领取前被替换／主动撤回。修订相关回答／产物及独立读回才支持已生效；完整状态合同见[调用手册](../../docs/usage.md#执行中主动改向或传达用户要求)。`list`/`status`/`events`/`recovery_status` 均为纯只读；旧轮结算后由原 controller 以同一 `command_id` 显式重试 `intervene`（通用入口另可用 `wait`）推进已授权介入；不能只收到取消回执就停止消费。问题回复与新 turn 完成监控由原协调者继续承接，同一 controller command_id 不因问题多轮而重派。

AGY native 自动观察仅 load，不发 nudge。源码候选公开 `dialogue`，按实际 launch/turn/epoch 能力准入，前台未返回只排队、完整 load 后串行 prompt，不取消。`task.turn` 不变、interaction_seq 单调递增、session roster 保留，旧输入结果不能结算新输入。accepted/claimed/send_started/returned 分别是受理／领取／可能发送／前台返回，不等于 worker 已采用；unknown 不重放，not_sent 不自动补发。**本版本源码；安装后需宿主重载 schema 与新 runner 后生效**：不引入事后零发送补结算子协议，多题 `dialogue` 只发单次 `session/prompt`，仅在 `returned` 时原子记录 `answers[qid]=prompt` 并移除绑定问题（`accepted`/`claimed`/`send_started`/`withdrawn`/`superseded`/`not_sent`/`cancelled`/`unknown` 均不清题；`accepted` 阶段 `withdraw` 或 `target_command_id` 替换释放全部绑定 ID）；普通未绑 `dialogue`（无论同交互或跨交互）与绑题 `dialogue`（`question_id`/`question_ids`）均绝不取代或清除 `incomplete_question`。只有显式传入匹配活跃 `incomplete_question` 的 `blocker_interaction_seq: int`（`>= 0`、`bool` 拒绝、`0` 按 `is not None` 处理；与 `question_id`/`question_ids`/`withdraw` 互斥；未传入时单题/多题/未绑头不变；已结算同 `command_id` 重试先于活跃 blocker 校验保持幂等），且同时满足 `returned`、原槽位文本未变、精确 `dialogue_body` 位于 `history[deferred['index']+1:]`（可跨前序 `question_ids`/`tool_call`）、无活动工具、无未决正式 `questions` 与最终公开回答时，才仅核销该目标 `interaction_seq` 槽位（其余片段继续阻塞，前缀增长仍优先恢复正式题）；而 Dynamic 分支（**本版本源码**）的 `dialogue` 答题绑定原 `question_id`（或显式 `question_ids`，在 `CALLCLI_DIALOGUE` 转发 `question_ids` 与完整 `questions` 原题元数据，保持单题与普通未绑头逐字节一致；仅 `returned` 时清除所绑问题并写入 `answers`，其余或新到正式问题仍阻塞）与 `interaction`，未完半行问题片段直接对账并保留 `unknown`/HOLD、不暴露 `incomplete_question` blocker，且遇 `blocker_interaction_seq` 时显式拒绝（发送前失败并保留 HOLD），绝不静默丢弃字段并结算。缺能力的旧 owner 明确拒绝；候选不代表现役安装已加载。


- 将取消受理与执行终止分开判断：prompt 的成功退出、无协议错误及有效 stopReason 才构成该实现的结算证据。SSH EOF、exit255、缺终止协议不能转成 cancelled 后自动启动下一轮，因为远端旧轮可能仍活着。
- 以事务和 turn/state 条件保护取消、续轮与关闭；只记录取消期间的问题，不让问题提前开放下一轮。旧轮迟到事件不能覆盖新轮。
- OMP/AGY unknown 提醒必须按精确 task/turn/seq 读回原事件、settlement_note 和 recovery card，再决定合法对账或升级；stage-only 不是成功，不因提醒自动重放。候选复用原 runner 通知，OMP 发起扩展对同轮 snapshot/迟到终态只提醒一次。Hermes/OMP/CLI 由当前 controller 以同一 `command_id` 显式重试 `intervene`（通用入口另可用 `wait`）才推进已结算的 pending intervene（`status`/`events` 保持纯只读不推进）；确认 target_turn 实际启动、结果体现修订，再收尾，unknown/HOLD 不续派。通知生成或 followUp 排队不等于真人收到或模型已经行动。
- 分别记录会话创建是否尝试和是否成功；只有明确未尝试创建的路径才能跳过远端关闭，不能把“未记录成功”解释成“未发生”。
- 对需要跨短命 shell 调用继续运行的有界 worker，使用独立进程会话并保留超时与精确清理责任，防止工具回收进程组时让任务永远停在 queued；不要因此创建常驻 daemon 或免除生命周期 owner。

## 5. 跨设备调用与权限

可复用单次 SSH JSON 客户端。Windows Codex 受限发起端优先采用已安装 `call-cli-native.ps1`（原生 PowerShell/.NET 传输，同一 JSON API）；不要先触发 Node `spawn EPERM` 再盲目重放。独立桌面启动入口 `codex-ssh.ps1` 要求用户已登录，不改变普通 Codex 或 ACPX 默认后端；详见 [Windows Codex SSH](windows-codex-ssh.md)。通用 Node 客户端仍保留：

```bash
node remote-client.cjs --host EXISTING_SSH_ALIAS \
  --entry /absolute/collab.py --db /absolute/tasks.sqlite3 -- ACTION ARGS
```

先验证一条无业务执行的 connect/list 路径，再启动真实任务。客户端只负责传输；收到了完整 CLI JSON 后可以关闭自有 SSH 传输，但不能据此取消、重放或推断远端 worker 死亡。结果不明时先按已持有的 client/task 对账。

- 从执行端现态解析 SSH 别名和可执行入口；非交互 SSH 显式提供已验证的 PATH，不以交互 shell 成功推断远端同样可用。
- 对 workspace-write 等沙箱，单次授予任务库与共用安装锁的必要目录；不要为测试关闭整个沙箱或修改全局权限。保留跨客户端共用的安装锁，不用每个测试私设锁目录绕过互斥。
- 在 Git Bash 调原生 Node／SSH 时，按需使用单次 `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'`，防止执行端 POSIX 路径被转换成本机盘符。入口按执行端格式校验，拒绝损坏的值，不猜测修复。
- Windows 长脚本通过 SCP／文件传输并读回哈希，避免多层 EncodedCommand 膨胀超过启动命令长度。不要把传输或引用错误归因于模型能力。

## 6. 验收、归档与收尾

为每个待支持发起端走真实调用链：新建跨 CLI 会话 → 结束前消费阶段并汇报 → 问答续轮 → 执行中主动改向 → 原生输入送入后由发起 CLI 介入 → 修订结果读回 → 关闭。模拟传输先验证控制逻辑，真实 CLI 再验证正常入口实际提供了所需能力。

- 用工具边界刷新没有尾换行的完整公开文本，防止阶段直到任务末尾才出现。介入探针必须证明旧轮在介入时仍执行、随后确实取消；最终文件变了但旧轮早已完成，只能证明完成后续轮，不能判为运行中介入通过。
- 将用户修订送入发起端的原生 RPC／turn-steer 或真实终端输入路径，由发起 CLI 产生介入调用。测试驱动输入标为模拟用户输入，不冒称用户真实键入，也不外推为终端画面测试或 idle 自动唤醒。
- 独立读真实文件／结果，明确谁完成了读回。执行方自验失败与验收端成功读回分别记录；后者可以支持产物结论，但不能改称前者已独立验证。
- 执行方发出“完成”文本后仍可能继续工具调用并修复新反例；只在对应轮实际结算后冻结文件哈希与复验，不能以第一条完成文案提前关闭或验收。为演示运行中介入预留的 sleep/无写观察窗口必须显式标为测试设计；该窗口成功不证明任意工具即时终止、半写文件恢复或全部后台子进程清理。
- close 后确认当前实现要求的 no-session 读回。传输或关闭不明保留 unknown 与资源占用，不靠修改状态制造成功；确认 provider 启动前失败的自有探针可在精确状态读回后收尾。
- 将必要公开事件、结果、源码绑定和清理回执保存在任务获准的私有证据位置，报告准确定位和保留边界；长期验收索引指向既有项目记录。scratch 可能清理，重要证据需按权限安排持久保留，不将私密原始日志搬进公开仓库。
- 分别声明源码测试、已安装副本、原生宿主回注、各发起角色与设备覆盖；单条角色链路成功不代表所有设备／安装组合通过。迟到回执按 SKILL.md 的对账与静默消费规则处理。
- 使用 Hermes 透明 worktree 隔离时（**本版本源码**）：交付区分四态——**分支提交（`branch_commits`）／未提交改动（`uncommitted_files`）／已集成（`integrated`，工作区干净且相对实际整合目标 `target_ref` 已合入）／已验证（`verified`，绝不从 commit 推定）**；授权范围内由 Hermes 完成完整收束而非甩给用户：先 `harvest` 收齐已提交及未提交／未跟踪交付（过滤未跟踪缓存噪声）→ 检查目标暂存区及重叠改动保护用户预置变更（**严禁自动 `stash`/`reset`/`clean`**）→ 串行 `git merge --no-ff`（**遇真冲突保留冲突现场与 worktree 并带建议升级，绝不先机械 `git merge --abort`**）→ 目标目录独立复验 → **整合且复验通过后默认执行非强删清理**（`git worktree remove` + `git branch -d`，**严禁 `--force`/`-D`**），否则保留并明确给出 `retain_reasons`（`retained_with_reason`）；本地集成与主仓合入/`current` 部署及对外 `push` 仍分开授权。
