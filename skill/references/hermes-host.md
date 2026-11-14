# Hermes 原生来源与通知

仅 Hermes 发起或处理其后台通知时读取。`call_cli` 是可选已安装模块，复制 skill 不会启用它。源码／磁盘 schema 与当前会话工具快照分别核验，旧 schema 可能没有 AGY、skills 或 intervene。

## 派工及续办

先查当前工具 schema；支持时 start 保存真实 task_id、原生 process session_id、turn 与来源。有长期责任方时由该方直接创建跨轮任务；短命代理需要发起时先核实际宿主的事件消费、续办和收尾能力，明确谁持续负责，不能把父子关系当作所有权自动继承。模型不得填写 profile/session/platform/chat/topic；宿主真实上下文捕获身份，任务按来源隔离。新会话、历史文本或另一 topic 不授予旧任务控制权；恢复缺口不是禁止所有子代理或其他 CLI 发起。只有已授权编辑设 write:true，共享写面使用相同 resource。

已实现的原生 launch 经 `ctx.dispatch_tool('terminal', ...)` 调用 background=true、notify=true、heartbeat=60；它捕获原会话，不代表实际 Telegram 投递已验。心跳按有新增输出合并，可能只有尾部；用数值 seq 读取完整公开事件，不从心跳尾文重构全程。oneshot、会话结束及宿主生命周期对通知的影响要另查，不能宣称离线唤醒或重启恢复。

Hermes/OMP 模型工具均拒绝 timeout（含旧缓存字段），新任务的后台 terminal 省略执行总限时；CLI/Python 的原显式硬预算能力仍保留为非常规路径，不是权限边界。已有预算轮在 clear_timeout 生效前仍传 T+180，不能靠改 DB 撤回已派发进程计时器。reply/intervene 支持下一轮清除，queued 一律拒绝该清除；多问到最后一答才生效，中途介入替换答题序列时由介入自身 clear 选择决定。公共 runner 每 600s 至多发一条复核，静默与持续活跃共享冷却，无自动取消/关闭；发起 agent 核实后选择等待、介入或取消，不强制逐条告知用户。heartbeat/入队不等于已消费，真实回流单独验收；不增加 Hermes 专属调度。

既有 ACPX `reply` 使用原 question_id，多个问题全部记录后才续轮；`intervene` 必填稳定 command_id，保持取消旧轮、结算、续轮语义。源码候选 AGY native `dialogue` 必填 expected_turn/command_id，回答另传 question_id；仅向当前 controller 的活跃 launch-bound 原 owner 排队串行输入，不取消、不启动新 runner。旧 owner 无能力明确 unsupported，不第二 owner attach。Hermes status/events 会推进已授权 pending 介入，可能启动新轮，不是纯查询；unknown 不自动推进。dialogue 返回受理不证明采用，继续消费相同 task/turn/interaction 的公开效果及产物；运行 gateway schema 加载与真实投递另验。

OMP 执行端 stage-only 的 unknown 现与 AGY 一样带 task/turn 绑定的 attention_required、originating-session 范围、保留 HOLD/禁止自动重放的 next_action，runner stdout 与持久事件一致。仍沿既有 terminal notify/heartbeat 回原来源；唤醒后由当前 controller 补读 seq 并判断，不把通知当续派或消费证明。原 controller 的 status/events 推进 pending 仅一次，command_id/expected_turn 保留；真实 gateway 已加载、模型消费和 Telegram 投递未由离线 fixture 证明。SJ/Jev 与混入真人的宿主静默缺口仍独立。

旧 schema 缺少动作时，先核验受支持的已安装通用入口与已有原生桥。使用 standalone 时协调者须消费公开事件并向真实父会话转述，明确没有内层原生 chat/topic 绑定或自动回注；不能伪造来源。确需新 schema 时按宿主受支持方式加载新快照，而非强改工具集。`/new` 不是普遍唯一解，也不转移旧任务所有权。

## 轮转压缩后的同源任务访问

源码 P1：轮转压缩会提交新的 `parent_session_id`/父行 `end_reason='compression'` 后切换 SID；task 保存的 Origin 仍不变。源码版本支持时，原生入口只在当前受信 Hermes profile/home 与任务 Origin 的 profile 相符、五个非 SID 字段逐字匹配，且受信 `state.db` 元数据从当前 SID 向后到任务 SID 的每一跳均为已提交 compression 父边时，才恢复对该逻辑会话原任务的访问。此为同一 principal 续办，不是父子接管。

若 metadata DB 缺失、损坏、锁忙、schema 版本／表结构未知、home/profile 不明、链断裂／反向／超深／成环，或遇到 reset、branch、delegate、canonical compression mutation、tool/未知 source，拒绝续办；不要按 `session_key` 放宽，也不要把 task Origin 改写为新 SID。默认 profile 的宿主 child 行可能 `profile_name=NULL`，仅当当前已验证的 state.db home 正是该 Store 所属 profile 时可按 Store scope 解释；未知／外部 DB 的 NULL 一律拒绝。此 metadata 查询只投影会话关系字段和 fork 标记，不读消息、摘要、配置或凭据；不是对 Hermes schema 32 以外版本的承诺。

本能力只适用于 native `call_cli`，standalone `collab.py --client` 仍要求原 handle，且无效 lineage 不得让 `list` 泄漏历史任务。它不授予其他 profile／route 的权限，不恢复 child→parent 的 P2，也不证明 executor/runner 已停止写入。新任务仍由长期责任方持有生命周期；源码和合成验证不表示当前安装或 live host 已启用。

当前兼容范围还要求链内 source 一致、无继承 fork 标记且总链到 root 不超过 32 边；source 一致是插件保守约束，不是 Hermes publisher 保证。CLI 未提供独立 session_key 时该字段随 SID 变化，严格五字段匹配会拒绝 fallback；不能静默归一化放宽权限。metadata Store scope 使用宿主无参 root，task DB 仍使用原有 plugin-data root；profile resolver 不可用只禁用 fallback，不影响 exact-origin。

## 已结束 owner 的旧任务

源码 native `call_cli` 按动作开放：同可信 profile、platform/chat/thread 和原 route 的当前会话，可 list/status/events 查看旧 owner 已结束的任务。沿用 schema 32 的只读 metadata 校验；只接受有限正 ended_at 与 `agent_close`、`cli_close`、`tui_close`，当前调用 session 必须仍开放且 source 受支持。compression/reset、丢连接、idle、无 PID、任务 completed 都不是此证明。CLI 两端均以 SID 回退为 key 且无 chat/thread 时可跨 SID；其他 route key 仍须逐字相同。不是按父子关系继承。

非当前 controller 的 status/events 不推进 pending 介入。跨 controller 的 settled reply/intervene 必须带 observed `expected_turn` 与稳定 `command_id`，同事务绑定 `controller/control_epoch`；旧任务缺字段按 Origin/epoch 1 解释。新 controller 可管理与取消已准入轮，原 owner reopen 不夺权；旧 owner 只能查看，不能对活跃新轮 reply/intervene/cancel。当前 controller 已变更后的无问题 reply 也必须带 turn/command 防迟到续轮，重复同 command 不重派。原始 controller 的旧 API 与问题回答保持兼容；可选 expected_turn 在原有入口同样严格校验。

native 新增 `reconcile`；跨 owner `close` 与 `reconcile` 要显式 `expected_turn`，仅本地且精确 ACPX record 可恢复。先复用 P2 持久 fence 固定 snapshot、保留 unknown 占用，再只读查原 record 的 backend session/last attempted request 和同作用域 no-session；证据充分才事务结算 closed。unknown 的 close 只转对账，不重发外部 close；失败保存 attempt 与脱敏 stage/reason_code。active 任务先正常结算，不拿 owner 结束当停写证明。远程恢复仍不支持；不宣称任意 detached descendants 已停止。

AGY native 是独立 receipt-only 分支，不使用上述 ACPX record：当前 controller 的 unknown 可 reconcile(expected_turn) 消费已持久可信 cancellation/fresh-no-prompt cleanup；缺证返回 native_workers_unproven/HOLD，无新 owner/load/close。不会向已卸载的旧任务补证；现有 worker 权限／HOLD 生产任务未因此恢复。源码 receipt 区分取消与业务成功，公开回执不承诺任意 descendants 停止。

新 Swarm 薄桥取消不再生成逐成员 cleanup prompt 或这类 verified-cancellation receipt；当前未获内部停止确认时明确 unknown/HOLD。上段 receipt-only 仍用于消费历史可信记录，不为旧任务补证或自动释放。


native/standalone/OMP 回执提供 recovery card 与 uncovered 提示；没有已核实的 pre-exit hook 或交接就明确 false，不拦禁短命发起。runner argv 固定 turn/launch-id/epoch；仅未 claim 的首轮支持旧缺参 argv，后续旧 argv 拒绝而不执行最新轮。源码合成验证不代表安装或宿主已加载。

新增 ended-controller recovery/reconcile 仅 Hermes native plugin 提供，standalone/OMP 未增加该权限。健康任务 reconcile 不加 fence；unknown/HOLD 或已 released 回执才可对账，pending questions 拒绝 recovery close。native receipt 标 `binding_source=record-resolved`，不冒充 dashboard 的 policy-pinned backend session。恢复卡不含私有 DB 路径或 command prompt；creating surface 由宿主记录，notification routing configured 不代表用户已收到，退出拦截和接方交接仍 uncovered。

全部公开 close（Hermes、CLI、OMP）复用同一 proof/fence，不再按 same-controller 选择文本 no-session 分支。本地 exact record 先持久 fence/attempt，unknown 一律只 probe；错 record/backend session/request、非结构化文本、缺 closed/no-session 证据均保 HOLD。未创建会话只在 cancelled 且明确 acpx_created=false、session_creation_attempted=false、无 record 的不变快照上无后端调用结算；缺 record/remote/无受支持精确证明的后端返回 unknown 与 exact_record_unavailable，不盲目重关。

显式 `release_only` 是普通 unknown close 的受限例外，仅原始且仍为当前 controller 可用，必填 observed expected_turn；只处理 unknown/HOLD、已结算非 native runner 与 local exact record。至多一次外部 close 前先持久 fence/attempt，之后严格 probe，已有 fence 不重复 close。资源 closed 不改业务成功与否，新 receipt 保留 business_state=unknown；不借 ended-owner 权限扩大准入，不新增 CLI/OMP 动作。源码归入不等于当前原会话已加载，实际以 schema 为准。

无问题 reply 迁移：本版本新 native task 持久 continuation_version=2，必须由调用方先读 status.turn，再显式提交同一 expected_turn 和稳定 command_id；重试复用原字段，不能临时读取最新 turn 当旧意图。旧持久任务缺字段/version1 仍兼容旧同 controller 请求，但不声称具备同等 stale-intent 防护。question_id 绑定的问题回复保持兼容；跨 controller 接续始终要求 turn/command。无需手改数据库或迁移旧记录。

## 通知轮缺少身份

若通知轮调用 status/events/reply/cancel/close 报 `No host session identity`，用 start 返回的精确 process session_id，经当前 `process_manage` schema 的 poll/log/wait 观察状态与输出。它是观察兜底，不授予任务所有权，也不证明 reply／介入续办已完成。需要继续控制时核验既有来源匹配的支持路径；无此路径就报告精确缺口，不把 process ID 伪装成身份或用另一来源重派。

已结算任务的迟到回执只需核对终态；没有改变交付、失败、阻塞或下一动作的通知静默消费，不输出空调用、占位回复或自行认定用户看到了空消息。对空回复投诉，区分用户观察、模型输出、宿主重试／后备和平台投递，日志未证实不宣称同一根因。

仅在 Hermes 机器通知轮成功消费事件、且确无新增交付、待答、失败或下一动作时，用整个回复恰为 `[SILENT]` 表达静默；不要用空正文代替。当前宿主的空结果／`(empty)` 属于失败诊断路径，不等于静默。真正的人类请求轮应给实质回答，不用裸静默标记；其他发起宿主须按其已核验契约处理，不能照搬此 token。本段是宿主协议指导，不证明模型实际采用、消息未投递或 issue #1 根因已修复。
机器唤醒须补读精确 task/turn/seq、未答问题及命令并作有依据的等待／控制／升级决定，反复输出 `[SILENT]` 不算行动。源码候选已为具备真实 launch/turn/epoch 输入能力的新 AGY native owner 提供 `dialogue`，同运行轮以 `interaction_seq` 区分输入并保留 session roster；隔离测试通过不等于现役 schema 已加载或宿主已消费。无能力的旧 owner 仍明确 unsupported，不能补造通道或第二 owner 接管。
当前轮只要收到任何真实真人文本（包括排队、steer 或 out-of-band），就必须实质回复，不能仅回 `[SILENT]`；拿不准来源时给出可见回复。合成 S5 已显示原生 busy 机器回合并入真人后仍可能允许静默，这是 Hermes 尚未修复的宿主缺口，不能声称本指导已解决。

Jev exact `[SILENT]` producer 的两行修复仅在独立私有候选中通过隔离/AST 合同验收，未应用到现役插件，也不包含在本仓库安装包。它只让本 hook 保持原文，不阻止后续 hook 改写，不修复真人/OOB admission；部署和真实加载验收须另获维护授权。

## 错误与清理

一个业务任务只启动一次。失败先按 task/process/session/cwd/raw agent command 对账，关闭前读必要公开事件；查不到正文就报告未知，不用历史故障代替本次原因。close 读回当前实现要求的 no-session。通知完整、身份可用、提问／修订续办及真实用户收到分别验收，合成 host dispatch 不算平台投递。
