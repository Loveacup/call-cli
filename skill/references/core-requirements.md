# 当前核心需求：持续双向协作

本文件是 call-cli 优化、升级、修复的唯一维护正本；个人安装保留语义一致的镜像，不另建需求版本。修改正文、适配或配套实现前先读本文件；用户改变需求时先更新这里，再同步摘要、引用、模板与验收。历史记录不是当前指令，能力目标不是已实现声明。

无论调用哪个 CLI，发起端都应持续掌握执行工作，将有价值的进展、问题、阻塞与最终结果送回真实的原会话；用户可随时在原会话补充或调整，由协调者传达到正确任务、确认实际生效并继续负责交付。用户不应切换终端搬运消息，或靠反复追问维持任务推进。这是本技能及配套实现优化、升级、修复的共同指向，不是某一 CLI 的特例，也不是当前所有链路已通过验收的声明。

- OMP、Codex、Pi 与 Hermes 可作为发起端；AGY 仅作为执行端，用户已取消其发起端需求，不再列为入口缺口或待验目标。发起、事件回流及控制分别适配各自宿主，绑定真实来源与精确任务，不把 Hermes 或任何 CLI 固化为必经中转。声明 runtime 名称不能替代宿主会话身份；不得把无原会话回流的 standalone 路径静默当作等价替代，入口不可达时说明缺口，不伪造来源或任务所有权。
- 区分事件产生、持久化、回注、协调者消费与用户实际收到；协调者转述有意义的阶段成果与待决策事项，不逐 token 汇报，也不只等最终结果。提问和介入结算后的必要续作由协调者承接。有长期责任方时，由该方直接创建并持有需跨轮的任务生命周期；短命代理需要发起时按实际宿主能力选择入口，事先明确持续协调与收尾路径，不假定父方自动继承所有权。暂不支持恢复不等于禁止该 CLI 发起。
- 用户可在原会话提出调整；发起 agent 也应根据执行期进展、偏差或阻塞，在已获授权范围内主动动态协调和调整，不依赖用户反复提醒。区分已提交、排队、被执行端接收及实际体现；按真实能力优先 steer，queue 与 cancel-and-resume 是不同语义，明确说明后备方式，不静默降级、不宣称无损。
- Hermes 作为 Alex 的副手，应理解真实需求并主动承担协调、纠偏、验收与收尾；已授权范围内的普通步骤自行推进，不逐项重复请批。重大取舍或新增生产副作用仍须按真实权限处理；审查发现本身不扩大生产操作授权。
- 按 CLI 原生能力、适配器、传输入口、协调层和宿主回流逐层核验；交互界面支持不证明所用 API 支持，OMP 亦须按同一标准验证。公共协作核心、发起端适配、执行端适配及工具链／skill 传递保持清晰职责与小接口，复用已有机制，仅隔离真实差异，不为各 CLI 组合新造调度体系。
- 新任务默认不设执行总截止；Hermes/OMP 模型工具不接收 timeout，保留 CLI/Python 显式硬 timeout 作为非常规兼容能力，不将其静默改为提醒。公共 runner 每 600s 至多请求一次发起方复核，持续活跃也不能无限推迟；静默与活跃检查点共享冷却，不自动取消、关闭或续跑。发起 agent 核查现状后选择继续等待、明确介入或取消，提醒不必逐条转给用户；入队不等于消费，CLI 依赖活跃 owner 轮询。已有硬预算在受支持续轮显式 clear_timeout 前仍生效；多问清除延迟到最后一答，中途介入替换答题序列时以介入自身 clear 选择为准，queued 拒绝清除。连接、启动与控制保持有界；unknown 保留资源占用并先对账，不自动重放。参数收口不是 shell 权限边界，时间限制不是费用保证。
- 一次逻辑协作启动时，检查 call-cli 本身及本轮实际参与的 CLI 协作 skill 的维护源与安装现态；按需核查本轮所选传递 skill 的来源、版本、目标端可用性与实际采用。区分可用更新与已部署版本、查询失败与已是最新；不扫描所有 skill、不在状态查询或同任务续轮重复检查，不因发现更新而擅自覆盖本地适配、打断任务或更改配置。
- 验收贯穿真实链路：执行中进度回原会话 → 用户在原会话补充 → 正确目标收到并按新要求继续 → 问题／失败／结果回到原会话 → 验收与收尾。模拟输入、离线测试、通用入口、宿主原生入口与真实用户投递分别记录，局部成功不代替全链通过。

实现与验收按来源、执行期回流、人工及主动调整、接收与生效、持续续办、最终清理逐项定位证据。分别标明源码实现、安装部署、当前入口加载、真实执行及用户投递；不能用其中一项代替另一项。离开当前协作后的离线自动唤醒、重启恢复与接管手动会话仍是独立范围，不因此削减在线协作目标。

### 控制动作与协调责任

- 自动观察仅由原 native owner 串行 `session/load`，不产生 prompt，也不定时 nudge。源码候选 `dialogue` 仅用于有真实 launch/turn/epoch 输入能力的活跃 AGY native owner，在完整 load 与下一请求之间串行 prompt；前台未返回仅排队，不是 active-tool 热 steer。旧 owner 无通道明确 unsupported，不把 `intervene` 静默改义。候选 schema 不代表运行 gateway 已加载。
- OMP 等既有 `intervene` 保持 cancel+resume：稳定 command_id 绑定原轮，确认取消结算后由原 controller 消费 status/events（通用入口 wait）推进一次 successor；unknown/HOLD 不续派。pending、RPC 返回、原生 dispatch、coordinator 报告采用、独立产物读回分别取证。
- `task.turn` 标识执行／续办轮；AGY 同运行轮显式输入以单调 `interaction_seq` 区分，旧 interaction envelope 不得结算新输入。roster 属于当前 native session，输入间保留；完成后 successor 才换 turn。dialogue 必须携带 expected_turn 与稳定 command_id，问题回答另绑 question_id，发送开始后不明保 unknown、不自动重放；不为旧进程补造能力。
- 机器唤醒须读取精确 task/turn/seq、问题及命令并作判断；重复 `[SILENT]` 不是消费或行动。仅已消费且无新增义务的机器回合可按宿主协议精确静默；真人、queued/steer/OOB 或来源不明必须实质回应。无合法输入／恢复通道时报告能力缺口并保留责任，不以显示修复冒称 worker 已推进。
- OMP 执行端仅 stage、无最终答案的轮次保持 unknown/HOLD；与 AGY 共用已持久 task/turn/seq 的 attention_required/notification_scope、可行动 next_action 和原 runner stdout 通知，不把阶段当完成。OMP 发起适配复用原轮询/followUp；无新终态事件时按 task snapshot 补提醒，同轮 snapshot/终态去重，新 turn 重新提醒。无自动重放或介入。生成、排队、真实宿主消费和用户收到仍分层验收；本合同不修复宿主混真人静默或 SJ/Jev producer。
- unknown snapshot fallback 只由该 task 当前 turn 的 unknown 事件替代；旧 turn 迟到事件不能挡住新 turn 提醒。提醒记账取实际发送的 unknown turn，不依赖同包 task snapshot 已是 unknown；无 turn 旧事件按当前 task turn 兼容，不能据此恢复未知的历史归属。非 unknown 事件顺序与游标消费不变。
- 当前 task track 在内存保留实际已送入 followUp 的 unknown turn 集合，不只记最高 turn：已提醒旧轮迟到不重复，从未提醒过的旧轮仍须首次送达。集合随原 close/detach 生命周期清理，不新增持久去重服务，不承诺宿主重启后的 exactly-once。


## AGY 原生模式与薄桥边界

单代理、Swarm、Teamwork、Dynamic 均保留。Call-CLI 只负责原来源收发、必要进度与问题、修改／取消转达、公开结果和自身会话收尾；AGY 原生负责模式内部角色、工作分配、审批策略、权限和执行编排。业务范围由发起者授权，不改写为父写／子只读或固定内部分工，不强制内部审批 mode，也不以子写权限证明作为接入或完成门槛。默认 OCS 不因任务内容静默改变；源码接入、离线验证、实装和用户闭环分别报告。

新 AGY 任务未指定模型默认 `ocs`。`oc/oa/4c/4a/4h` 为现有模型简称，裸简称走单代理；后缀 `s` 走原生 Swarm，`t` 走原生 Teamwork，均通过实际 advertised slash command 发起，不降级成普通 prompt。显式 `agy_mode=single/swarm/teamwork/dynamic` 与简称意图必须一致。完整 advertised model ID 与显式 effort 保留既有选择／读回；所有入口仍共用 prepare_start，不回填旧记录。worker 型号仅在用户显式指定时作为原生任务偏好转达，不再默认钉住逐成员型号或作为完成门槛。

当前片在保留 Swarm 薄桥及所有既有成果的基础上接通 Teamwork 最小纵向路径：按 advertised teamwork 命令启动，同 owner 完整 load、异步访谈、原来源答复／修改、公开最终结果和 mode=teamwork receipt／正常 close。invoke_subagent completed 仅证明委派返回，不是整个业务完成；优先沿用 CALLCLI_STAGE/QUESTION 和已知公开原生工具阶段，不建立逐角色账本、全后代终态证明或固定人工确认门槛。公开问题／审批事件有则沿现有机制传递；无事件明确不可观测，不推测内部审批状态或强塞回调。保留现有公开回调的会话身份校验，不修改 AGY 权限、配置或内部调度。启动问答与完整业务支持分别取证，不以换前缀冒称全链通过。取消仅沿原有单次 native cancel，未知如实、不重放／强杀，不定义模式永久必须 HOLD。

简称解析采用有限保留语法，不猜任意前缀：已定义小写简称照常解析；大小写变体、重复／混合 `s/t`、多个简称直接拼接或经空格／`,`／`_`／`:`／`/`／`+`／`-` 组合，以及简称后误附 `-low/-medium/-high` 均拒绝。该保留语法内的字符串不兼作不透明完整 ID；其他非空 literal ID（如 `octopus-exact`、`oc-special`）原样交给现有 advertised catalog 校验，不按 `oc/oa/4a` 前缀拒绝，也不保证未广告 ID 可执行。未指定的自然语言拼法不作无限猜测；模式仍须用已定义简称或显式参数表达。

### Dynamic 源码接入候选

显式 `agy_mode=dynamic` 选择同一 native ACP owner 与 `/run-workflow`，没有新增简称；`single/swarm` 为共享模式字段的既有两种语义。省略模式保持既有默认及旧记录行为，`agy_swarm` 路由冲突拒绝。Dynamic 模型使用完整 advertised ID/family，子 agent 的 inherit/flash_lite/flash/pro 属于待审批 Script 的原生命名空间，不复用 `agy_worker_model`，不把父配置说成子模型已证。缺 `run-workflow` 广告在业务 prompt 前拒绝，无普通 prompt/Swarm fallback。

原controller通过 `permission` 对精确 permission_id、expected_turn、稳定command_id、Script SHA-256 作 allow_once/reject_once；脚本最大128000 UTF-8字节，原样提供给原来源审批，不执行自造AST自动授权。若同时有args.Script必须与rawInput.Script一致；只接纳不歧义的两个单次选项。普通非workflow工具仍按已有审批策略；异源/子session回调不获父批准。许可记录、send_started与returned分别取证；approve-all不能替代脚本精确审批。发送不明、取消或owner退出保留未发送/unknown，不自动重放。

原始workflow事件在normalize之前核对args.Script与rawInput.Script、继承同ID的workflow身份核验稀疏update；原始重复tool_call不等价合法tool_call_update，单次回放/执行响应中重复创建保HOLD，不以snapshot合并证明single-use。pending/decided/send_started/unknown许可阻止seal，封口后不再接纳或发送新批准；卸载期间新增许可拒绝、新执行/正文使旧快照失效并保unknown/HOLD。通信RPC deadline不计精确审批中的原来源决策等待，发送/接收与实际通信仍有界，不引入任务总截止。

仅对已识别workflow的原始args/rawInput：字段缺省、空对象或同Script对象可保留既有身份；显式null、字符串、数组、数字、布尔及非字符串Script无法证明同一性，保守unknown/HOLD。现有真实ACP样本未证明null是“无变化”信号；此局部证据合同不是全局ACP类型判断，不推断恶意或断言不同脚本实际执行。普通工具不套用此workflow guard。

命令广告可晚于load response；缺广告时在原读取loop中最多观察1s异步通知，见精确run-workflow即返回，无新RPC/业务prompt；到界仍未广告才拒绝。最终工具终态且前台已end_turn、无未决问题时，replay无换行父答案以该边界finish，先确认可用输出再seal/unload；半行问题仍HOLD。其他父工具failed不一律否定workflow completed：失败之后有新的父最终答案才按恢复性失败处理，completed仅代表该父结果合同，不代表所有工具成功。末尾失败后无新答案保留上下文/HOLD；workflow自身failed仍非业务成功。

Dynamic可信mode receipt提交时同步native_phase：正常completed为settled，失败unknown为unknown；不把dynamic_pending留在已结算任务上。该显示修正仅作用于新receipt提交，不改写历史任务或放宽实际完成／close证明。

公开进展仅为父工具安全ID/name/status、父公开文本、问题与许可；当前ACP样本无直接Logs/Output/phase/子agent事件，native_observation明确unavailable，父回答不升级为子结果独立证明。首end_turn后完整load持续对账；正常完成须run_workflow completed、对应已批准脚本hash、父工具无活动项、父公开最终答案及精确卸载/负向读回；业务验收仍由发起者独立完成。失败工具可在相同安全终态卸载并留可信mode receipt，但任务仍unknown，不报业务成功。close只消费原owner的精确mode receipt，不借Swarm roster/add/kill。

显式dialogue是同turn排队输入，前台返回后串行发送，不承诺运行中脚本/child热修改；问题回答绑定原question_id与interaction。半行问题归属不明、历史变更、fatal、断连、缺审批/脚本hash/工具证据、取消传播未证均unknown/HOLD，不自动续派。取消不产生Swarm cleanup prompt，不把session/cancel、stdio退出或卸载当所有后代已停；无持续恢复owner保证。Dynamic真实call-cli宿主/用户闭环和取消级联仍待单独授权验收；Teamwork最小源码路径见前述薄桥边界，不以Dynamic样本冒称支持。

Teamwork修正真实样本已证明brief采用revised，但原生材料报告implementer/reviewer交互许可60s超时，父ACP未收到子许可，终态non-victory且预期结果缺失；不是父方明确拒绝，也非子agent逐个终止证明。保留本轮整体目标，当前不假称完整支持，不自动重跑真实样本或绕审批。


AGY 原生模式复用同一 native ACP owner、Store/runner、身份和写面互斥，不创建第二 controller 或调度器。end_turn 是前台返回；随后完整 load 对账公开历史，工具仍活动或问题待答就继续持有 owner。当前响应有最终回答且公开工具已终态时可正常回收结果，不推断未公开内部工作已结束。仅发布完整成功回放的增量，丢弃 thought，历史歧义如实 unknown，不静默重写。

Incomplete public question fragments are not formal questions. Preserve their generating interaction across explicit inputs; expose only sanitized blocker reason, interaction and available actions through existing observation/events. A tracked original history slot may resume by prefix growth and a provable boundary, after the full replay validates, publishing once under its original interaction. Replays must not duplicate it or merge identical text from another interaction. Ambiguous ownership/history stays actionable unknown/HOLD, never silent permanent waiting or guessed recovery; no arbitrary history-rewrite algorithm.


Swarm／Teamwork 复用原 owner 的公开前台响应、完整 load、工具状态、问题与最终回答判断当前请求是否结算，业务产物由发起者验收；不要求 CALLCLI_RESULT、逐成员 add/kill/model 或全员终态才能回收结果。首 end_turn 仅在无公开活动工具、有最终回答且无待答问题时可结算当前请求，不等同证明整个原生项目或全部后代停止。正常 close 记录本任务会话卸载与可观察状态；缺内部细节如实标 unknown，不把它自动升级成业务失败或伪造停写证明。取消仅转达原生控制、不重放业务或自动生成逐员清理 prompt；原生未给停止确认时明确报告，不宣称全后代停止。旧任务已有 receipt-only/HOLD 记录不追溯改写。

现有取消、身份隔离、写面互斥和未知效果不重放原则保持。旧版 Swarm 逐成员清理证明只解释历史 receipt，不作为新薄桥模式的必经工作流；新模式只陈述实际观察到的前台取消、会话卸载和未知内部工作状态，不人工补证、盲 attach 或强放旧 HOLD。


Native reconcile consumes an existing trusted owner receipt only: no second owner, post-unload load, repeated close or ACPX substitution. Model-provided or task-JSON cleanup fields alone are not trusted receipt provenance. Fresh exclusively created, empty pre-submission history with proven no prompt and exact unload may release an abnormal preprompt failure while preserving its original diagnosis; reused/creation-uncertain sessions require evidence and otherwise HOLD. Permission diagnostics expose only safe callback identity/scope/kind/options/decision/send outcome; missing allow_once is explicit, child scope is not authorized, approve-all never silently becomes skip-permissions.


新 Swarm 不再要求模型生成 turn/worker envelope；结果归属由原 owner 的 task/turn/launch/epoch、完整回放与精确 dialogue 用户消息边界确定。旧 envelope 兼容/逐成员证明仅为历史记录，不用于新响应结算，也不授权追溯释放旧任务。

AGY abnormal exit/fatal output and OMP unknown settlement must produce durable, task/turn-bound actionable notification through the existing runner/native return paths. OMP polling/follow-up and Hermes notification are not proof of real host consumption or user delivery. The AGY 1.5.8 v3 candidate sample verified one G4C low coordinator and one G4H medium worker with approve-reads: after end_turn, one public cancel triggered original-owner cleanup, completed kill plus status, exact unload/not-found, a trusted cancelled receipt, equal repeated public close and natural exit 0. This is a direct candidate control-path observation using a threaded launcher/observation wrapper, not default-launcher/deployed-host acceptance, active-tool hot cancellation or arbitrary descendant termination. Earlier unknown/HOLD samples remain unchanged; no reconciliation or retroactive settlement was performed. Further paid samples, production DB/policy changes, old-task controls, upstream edits, installs/reloads/deployment and external writes require their own explicit scope. Keep prior failures and source-bound test counts historical; see the [current verification index](../../VERIFICATION.md#current-acceptance-index).

## Ended-owner native access

Prefer preventing orphaned tasks; when they occur, make diagnosis and recovery available through the existing native interface. A current session in the same trusted profile and original route may discover and inspect tasks whose original owner is currently ended according to committed read-only host metadata. Explicit close reasons are distinct from compression, reset, disconnect, missing PID or mere task completion. No model-provided identity or parent/child relation grants access.

Inspection must not advance pending interventions or launch business work. Explicit settled continuation binds the observed turn and stable command ID; admission atomically records a single current controller and monotonically increasing control_epoch while preserving Origin/history. The admitted controller retains control after the former owner reopens; former owners may inspect but cannot control a live successor. Runner dispatch binds exact turn/launch_id/epoch and never reads a newer successor from delayed argv. Missing controller fields on old tasks mean original Origin/epoch 1; legacy unbound runner compatibility is limited to an unclaimed first turn, never a successor. New-controller active cancellation remains available; unknown/HOLD is never replayed.

Native local exact-record close/reconcile reuses the P2 durable fence. Recovery requires explicit observed turn and current-controller or trusted ended-controller admission, a settled/unknown snapshot, writer exclusion and exact backend binding; stale former-controller inspection is not recovery authority. Backend reconcile performs no close, and only exact closed record plus scoped no-session may settle HOLD transactionally. Existing uncertain close attempts are never repeated; receipt preserves attempts and sanitized stage/reason_code. DB fence does not prove arbitrary detached processes stopped. Start/query/event/settlement receipts provide a recovery card and honestly mark missing verified handoff/pre-exit coverage; short-lived agents remain supported. Source validation is not deployment or live-host acceptance.

This slice exposes new recovery/reconcile only through the Hermes native plugin (plus the existing authenticated fixed-target dashboard). Standalone CLI and OMP retain their original control actions, not native ended-controller recovery. A healthy task cannot be frozen by reconcile: native reconcile requires unknown/HOLD or an already released receipt, and recovery close refuses pending questions. Native binding_source is record-resolved, unlike the dashboard's policy-pinned backend session; no release receipt implies a workspace free of arbitrary writers. Public recovery cards omit private DB paths and command prompts, preserve the actual initiating surface and distinguish configured notification routing from unverified delivery/exit/handoff coverage.

All public close entrypoints (Hermes, standalone CLI and OMP) share the same proof boundary regardless of controller identity. Local exact-record close commits a durable fence/attempt before external close and requires exact structured probe evidence to release. Unknown is probe-only on every entrypoint; arbitrary JSON status or substring text is never sufficient. An unchanged cancelled task with explicit acpx_created=false and session_creation_attempted=false and no record can be fenced closed without backend calls. Missing-record/remote/unproven backend close preserves unknown/HOLD and reports exact_record_unavailable without external control. Existing same-controller close request shapes remain usable; explicit expected_turn, when supplied, is checked. Ended-controller recovery admission still requires observed turn; it is not made available across CLI/OMP controller identities.

CLI/OMP close output projects only state, phase, stage, reason_code and external_close_attempted; nested close receipts use the same whitelist. Complete owner/target/proof/attempt history remains internal and is not rewritten by projection. Recovery cards mark exact_record_reconcile for unknown tasks with a local ACPX record, independently of the historical creating surface. Advice uses the current caller entry: Hermes points to reconcile, CLI/OMP to probe-only close; unknown entries use neutral exact-record probe guidance without promising an action verb. Capability is not authorization or proof of backend availability/settlement. Missing-record/remote tasks retain HOLD and request authorized identity/device resolution.

Hermes 原生 `release_only` 是普通 unknown close 的显式资源释放例外：仅原始且仍为当前的 controller、显式整数 expected_turn、unknown/HOLD、已结算非 native runner、local exact record 可准入。复用完整快照复核、持久 fence/attempt、至多一次外部 close 与严格 closed-record/request/no-session 读回；已有 fence 只 probe，已释放重复返回原 receipt。不转移 owner、不派业务、不将资源关闭改称业务成功；新 receipt 保留 business_state=unknown，任意 detached descendants 仍未证。宿主可显式配置 local_toolchain_bin 为已核验 Node/npm/ACPX 的绝对 bin 目录，local start/续轮与 close/reconcile/release_only 共用该目录的子进程环境，配置缺程序拒绝而不回退 bundled 链；未配置及远端默认不变。继续用既有 npm root -g，不内置本机安装路径或新增自动搜索层级。

## P2 fixed-target release-only candidate

One fixed legacy cross-owner task may be finalized by an operator admitted through the existing Hermes sensitive HTTP gate. Exact identifiers belong only in a local untracked policy file, never the public source. This does not transfer the original owner, permit business continuation, or authenticate a submission string. Bind task, owner, turn, backend record, ACP session, profile home and an expiring one-use confirmation to a host-admitted locked snapshot. Preserve business results and completed command history. A durable fence rejects late events, launches, metadata changes and reopen attempts, including writes from previously loaded code. Revoke only unconsumed commands. Release requires a supported exact-record execution-side close, strict matching close receipt, closed-record readback and associated strict no-session result; missing or uncertain evidence retains an unknown reservation without automatic close replay. Offline source/integration verification is not production authorization, deployment or release.

Revision tracking is restricted to exact targets enrolled by prepare; unrelated task create/update/delete must not add auxiliary records or counter writes. Constructor binding and receipt GET must not initialize schema or take a write lock. Receipt uses a read-only database connection and explicitly reports not prepared before initialization. Prepare alone initializes necessary release schema, and target enrollment plus snapshot/nonce binding share one writer transaction so concurrent prepare and update/delete-recreate ABA cannot create an untracked interval.
