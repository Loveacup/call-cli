# 给 AI：在线协作手册

本文用于已配置的发起端。先按[配置手册](configuration.md)确定入口、任务库、目标和授权范围。源码通用入口支持 `connect/start/list/status/events/wait/reply/dialogue/intervene/cancel/close`；`dialogue` 仅适用于有输入能力的 AGY native 原 owner。Hermes 候选另有受控 `reconcile`，实际调用以当前加载 schema 为准，不把源码动作当成现役已安装。

## 调用约定

以下 `ENTRY` 表示调用 `collab.py` 的命令，示例使用源码路径。把参数解析到实际环境，不执行占位值；所有后续调用复用同一 DB 和 `CLIENT_ID`。不同工具调用之间不要假设 shell 变量自动保留，应使用保存的精确参数。

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  connect --runtime codex

python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID start --device worker-laptop --agent omp \
  --cwd /absolute/target/workspace \
  --prompt '目标、只读或可写范围、产物和验收方法'
```

跨设备使用协调主机已有 SSH 别名。Windows 目标增加 `--remote-shell powershell`，cwd 使用该设备上的绝对盘符路径。发起端的路径不会自动映射到目标；不要把损坏的盘符或引号“修好”后悄悄继续。

- `--runtime` 取 `omp/codex/pi/hermes`：是发起方声明，不是认证。
- `--agent` 取 omp/codex/pi/agy。AGY 未指定模型用 G4C，G4C 默认 native swarm；G4A 文字／知识默认 off，可显式 native 开启，G4C 可显式 off。worker 默认 G4H，模型覆盖保留。路由随任务持久化，旧任务及其他 CLI 不变。
- `--write` 声明已授权编辑／测试与目录占用；OMP/Codex/Pi 沿用按 `write` 选审批策略。新 AGY 默认 `approve-all`，显式只读审批传 `--agy-approval approve-reads`，与 `write` 独立且随任务续轮保留；旧任务缺字段不扩权。审批不是文件沙箱。
- `--resource` 可显式标识跨设备同步工作区；不同别名、路径或 DB 不得用于绕过单写者约束。
- 常规协作省略执行 timeout，先由发起 agent 复核再决定介入或取消。Hermes/OMP 模型工具拒绝 timeout（包括旧缓存参数）；这不是 shell 权限边界。CLI `start --timeout SECONDS|none` 与 Python `start(timeout=...)` 保留非常规显式硬预算能力：旧值不自动清除，仍作用于 SDK timeoutMs、runner T+90 取消及 Hermes 存量轮 terminal T+180，不是提醒或精确总墙钟/费用上限。无预算轮等待 prompt 接受最多 120s，超出请求取消并保留 unknown/占用；连接与控制限时不变。
- native 不设硬执行 timeout，首 end_turn 后仍 running。正常业务按本轮结果、对应 worker add/kill completed 和 session unload 结算；可信取消按独立 owner receipt，不要求 native_result。缺 rawOutput 不单独保 HOLD；fatal/断线/缺证/取消不明仍 unknown，不自动重放。AGY 1.5.8 v3 候选单 worker 已实测 end_turn 后一次公开 cancel、cleanup kill+status、精确 unload/not-found、可信 cancelled、重复 close 相等及 natural exit 0。限线程 launcher/观测 wrapper 路径，不是部署、active-tool 热取消或所有后代全停证明；宿主接收/消费另验，后续付费调用须明确授权。见[当前验收索引](../VERIFICATION.md#current-acceptance-index)。
- 公共 runner 在原单循环每 600s 至多发一条协调者复核请求，即使持续活跃也会触发；静默与活跃检查点共享冷却，取消/结算后不再提醒。事件沿用 stage + quiet_notice，附 notice_kind（quiet/checkpoint）、elapsed、turn、seq；原生输出另含 task_id。最近公开状态供发起 agent 核查，不能凭活跃/静默判成败或卡死。提醒不自动 cancel/close/resume，不要求逐条发给用户；OMP followUp 入队、Hermes heartbeat 唤醒均不等于已消费，通用 CLI 必须持续 wait/events，owner 消失时不保证介入或终止。
- 匹配的权威终态界定本轮输入：先按接收顺序消费其前全部有限帧（含答案／问题），再有界等待本地传输退出（默认 2s），不以清理计时丢弃业务尾帧，也不无限等待 EOF。超界保守记 unknown、保留诊断与占用；只终止本轮直接创建的本地传输子进程，不杀进程组／后代／共享 queue owner，也不把本地清理当远端取消成功。
- `--skill MODE=PATH`、`--target-skill MODE=PATH` 或 `--skills-json ARRAY` 在 start 选择 required/optional/reference 技能；Hermes 用同一 `skills` 数组。选择、本地优先发现、固定版本、目标文件读取和逐轮 `CALLCLI_SKILL` 回执见[技能传递](../skill/references/skill-passing.md)。缺 required 回执仅标未验证，不降低已有效完成的业务状态。

## 观察、提问与回复

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID wait --task TASK_ID --after 0 --timeout 5
```

保存返回的 `after`，下一次接续它。观察状态、轮次与公开事件：

runner 通知携带 `task_id/turn/event_id/seq`，`seq` 与持久事件表游标相同；截尾或重新接入后用 `events(after=seq)` 补读，按任务、轮次与 event ID 去重，不把重复宿主回执当成新派工。重要 stage、answer、question 与终态仍通知；started/version/model/permissions 只入库。工具仅保留安全 ID/name/category/status，同一工具重复状态不再产出事件；每轮最多通知 12 条普通工具状态，失败不受此上限，全部变化仍可 events 查询。不是逐 token 通知或 exactly-once 宿主投递承诺。

终态通知携带 `question_ids` 和最近 1000 字公开回答 `answer_tail`；有 pending 介入时附带下一步动作。宿主截尾仍可能丢内容，应补读 events，不用占位回答代替读取。宿主/Telegram 的静默消费与真实路由须独立验收，不能据此关闭 issue #1。

`unknown` 轮的 `answer_tail` 与累计 `answer` 可能仍含工具前文字，仅用于历史／部分结果对账，不是完成证据；须先按终态判断，再验收具体成果。迟到的重复工具更新即使不再产生工具通知，也会重置最终回答窗口；其后没有新回答或待答问题时会保守结算为 `unknown`，不能用旧 `answer_tail` 覆盖该结算。

OMP 与 AGY 的 unknown 现复用原 runner 的主动 attention 通知：携精确 task/turn/seq、originating-session 范围及保留 HOLD/禁止自动重放的下一动作。仅 stage 输出仍是 unknown，不当作完成。OMP 发起扩展通过既有 wait/followUp 对同轮 snapshot/迟到终态去重，新 turn 可再提醒；已入队不代表模型消费或用户收到。收到取消后的 pending intervene 时，由原 controller 消费 Hermes status/events（通用入口 wait）推进一次 successor，继续验实际 target_turn 和修订结果；unknown 不续派。

问题协议使用 `CALLCLI_QUESTION Q1 问题正文`（依次 Q2、Q3）：`Q[1-9][0-9]*` 后接一个 ASCII 空格和非空正文。编号是同一 task/turn 内的逻辑问题身份，不是源 messageId，也不用于授权。相同编号与完整正文指纹（SHA-256 与字符长度）相同的复述只登记一次，保留首个 event token 供 reply；不同编号或无编号的同文问题仍各需一答。同编号正文不同保留独立可答 token，并附安全 `question_note`，不打断收流或覆盖正文。身份计算发生在公开文本截到 8000 字符之前；不持久化或通知超长原始正文。旧事件缺少完整身份依据时保守保留，不凭截断前缀合并。下一轮可重新使用 Q1；历史事件不回写，旧任务已登记的重复 token 不自动合并。

编号问题事件公开 `question_label`、`question_sha256`、`question_length`；同编号不同正文另带 `question_note`。指纹覆盖解码后完整问题文本（含编号、UTF-8 编码），长度为字符数，均在 8000 字符公开上限之前计算；因此两条可见截断文本相同的事件仍可能有不同指纹并分别待答。

解码行缓冲最多 64000 字符；超限仅保留公开前缀，附身份不完整的 `question_note`，不生成指纹、不按编号合并，保守逐条待答，后续行继续处理。

`Q1:`、`Q1.`、编号前多余空格或编号后多个空格不属于该身份语法，按无编号问题逐条登记，不进行宽松推断。原失败公开文本以 `Q1 ` 开头，已符合此规则；该形状的回放已通过，但真实任务仍需部署后的父方验收。

编号身份由持久事件保存，重新打开 Store 或同轮重复提交不重新登记已回答的问题；这不提供 runner 崩溃后自动恢复或 exactly-once 投递。普通同 token 重放、旧轮事件拒收及全部独立问题答完才续轮的规则不变。

native AGY 成功完整 replay 先按 baseline 前缀校验并生成 turn delta，公开事件与 completion 共用它；首次 replay 以有序文本游标允许跨工具块拆分／合并，新增重复同文保留，顺序／正文改写拒绝。后续 load 仍按位置校验，历史重分块可进入 unknown/HOLD；没有真实后续持久 history 重分块证据，不扩展重写算法。工具按 ID 合并有效状态。`native_observation` 保存本轮最后成功／语义变化墙钟时间、安全 pending_tools、question_ids 与 input_available；后者仅表示已注册原 owner 输入能力，unknown/退出后为 false，新 turn 清除旧观察，不证明 worker 活性。

完整问题依换行、新追加的原生工具／用户块或该文本所属前台 end_turn 发布；历史工具原位更新、完整 load、重复快照、异常退出和本地主动提交 dialogue 都不证明背景尾行结束。主动输入前的半行／协议前缀保留原 interaction，单独跟踪同 owner 的原 history 槽位与前缀，阻止 seal/unload。后续原槽位本次确有前缀增长时，换行或紧邻的原生 tool/user 后继块可证明边界；后继可已在前次成功 replay 中存在。整次 replay 校验成功后才按原 interaction 恰好一次发布。无增长时即使追加 tool/user 也不 flush 暂存片段；无边界仍阻塞。新输入同文问题独立保留，可逐 token dialogue 回答后正常结算。不支持任意历史重分块或猜测移位归属；歧义／改写保 unknown/HOLD，不重放、不伪称恢复。

`native_observation.question_blockers` 仅公开 interaction_seq、固定 reason、blocks_completion 和 available_actions，不含片段正文、长度或指纹。正常 incomplete_question 的动作是观察原 owner、显式 dialogue 或经复核取消；阻塞集合变化产生一次安全 stage/notice_kind=incomplete_question，不逐 load 刷屏。明确槽位归属歧义时公开 history_ambiguous；断线等其他失败使用中性 unresolved_question，真实原因保留在 settlement_note。两者动作均为 inspect_origin/reconcile_hold，旧输入能力失效。半行不是正式 question_id，不能当已答；取消不证明 worker 停止或 HOLD 释放。



- `running`：结合真实阶段、工具状态和产物判断进展；静默不等于卡死。
- `waiting_reply`：读取具体问题，再自行判断或询问用户。必须使用返回的准确 question ID。
- `completed/cancelled`：对应轮已结算，仍需检查真实结果或残留。
- `unknown`：保留任务与资源定位，先对账原会话和进程；不要重放。
- 以下 ACPX 完成／错误规则适用于 OMP/Codex/Pi 及 off／旧 AGY，不适用于 native swarm 的首轮 end_turn：正常 exit 0、本轮有效 end_turn 及最后工具后非空回答，或待答问题 waiting_reply。仅 stage／skill／空输出／工具状态保 unknown，不重放；普通回答不是独立业务验收。
- AGY 一轮公开输出的最后一个非空行若恰为 `⚠ Agent execution terminated due to error.`，即使协议返回 `end_turn` / exit 0，也记为 `unknown`，并在 `settlement_note` 与最终事件中写明“AGY 报告致命执行错误、未完成、结果不明、不自动重放”。该判定只拒绝完成，不代表已知根因；引用、前后缀或其后仍有内容的同类文字按普通内容处理。
- AGY 工具 failed 且最后一个工具事件后无最终回答、也无待答问题时，仅在传输 exit 0、未失败且 end_turn 的情况下，记 `unknown` 并附 `settlement_note`；stage 不算回答。传输失败保留直接的退出说明，不改称工具失败或权限拒绝。正常提问保留 `waiting_reply`，可用准确 question ID 回复；既有 fatal 最终行仍覆盖提问。工具失败后恢复并给出最终回答可 `completed`。协议结束、工具失败和业务结果分别核对。
- `busy: true, task: null`：数据库锁竞争，没有可信状态；保留游标，稍后再看。

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID reply --task TASK_ID --question-id QUESTION_ID \
  --prompt '针对该问题的真实回答'
```

既有 ACPX 路径逐项 reply 后续轮；native AGY 用 `dialogue --task TASK_ID --expected-turn TURN --command-id STABLE_ID --question-id QUESTION_ID --prompt ANSWER`，同运行轮逐问串行送入原 owner。question_id 只在返回 end_turn 后从待答列表移除；RPC 返回只证明输入返回，不证明业务采用。相同 task/turn/interaction 编号和完整正文的问题重述幂等，下一 interaction 可重用编号，旧待答问题不会自动丢弃。协调者持续消费并独立读回结果，不以测试代答冒充真实宿主消费。

## 执行中主动改向或传达用户要求

用户在当前发起 CLI 输入新要求后，由你读取当前精确任务状态，将修订传给目标，不让用户到另一终端搬运：

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID intervene --task TASK_ID \
  --command-id UNIQUE_STABLE_COMMAND_ID \
  --prompt '保留已有成果，只修改这里明确列出的方向与验收要求'
```

同一次提交的重试复用同一个 command ID。`pending/queued` 仅证明提交，不证明改向生效。实现语义是：请求介入 → 原轮取消 → 确认原轮结算 → 同目标会话续轮 → 读取修订结果。继续短 `wait` 驱动和观察状态转换，不宣称无损热修改。

AGY native 自动观察只 load，禁止定时 nudge；显式串行输入用新 `dialogue`，不走本段 cancel+resume。必填 observed expected_turn 和稳定 command_id，可选 question_id；只支持当前 controller、running、launch/epoch 匹配且 accepting 的原 owner。load/prompt outstanding 时仅排队，请求返回后串行发送，task.turn 和 session roster 不变。commands 的 accepted→claimed→send_started→returned 不是业务采用证明；发送不明 unknown 不重放，未发送 not_sent 不自动补发。每输入 interaction_seq 递增，当前 final 必须携带匹配 interaction_seq，旧结果不能结算；待答/待发送命令阻止 seal/unload。新候选 schema 不等运行 gateway 已加载，旧 G4C owner 无能力明确 unsupported，不第二 owner 接管。

回答准入从持久原 question event 提取 token、label、原 interaction、公开正文及完整身份指纹，随 CALLCLI_DIALOGUE 结构头送入原 owner，避免同答案／倒序答问混淆；仅返回后移除精确目标 token，其余待答保留。正文沿用公开有界投影，不冒称截断长问题的全文。后续 prompt live 文本在自身 end_turn 发布完整无换行问题，与下一成功 load 按序对齐避免重复。原生 post_message completed 只证明 dispatch 已观察；公共 ACK 是 coordinator-reported，实际采用须独立产物读回。fixture 校验头并据实际身份生成结果，合成 ACK 不是真实 worker 收件证明。claimed/send_started 后不明不重领不重放。



清除误设/继承预算：reply/intervene 支持布尔 `clear_timeout`，CLI 对应 `--clear-timeout`；省略保留预算。reply 需等原轮结算，多个问题的清除选择暂存到最后一答推进新轮；中途 intervene 替换答题序列会丢弃该暂存选择，由介入自身的 clear 决定。running/cancelling 介入只记录下一轮清除，确认原轮结算后原子应用；撤回不改旧预算。所有 queued 清除保守拒绝（后台进程可能已捕获旧预算），可等待结算或显式取消后再介入；普通 queued 介入不变。unknown 仍拒绝续轮并保留锁。CLI/OMP reply 必须有真实未答 question_id，completed 无问题用 intervene；Hermes 保留无问题的 completed reply 能力。

例如 `collab.py --client CLIENT_ID intervene --task TASK_ID --prompt '修订要求' --command-id STABLE_ID --clear-timeout`；这是有意取消结算后同 session 续轮，不是当前轮热修改。cancel 不等于 close，也不删除已有文件；半写入/外部副作用需要对账，不保证回滚。

`commands[command_id].status` 跟踪该命令的精确 `target_turn`，`intervention.status` 同步最近一条命令：

| 状态 | 框架能证明的事实 |
| --- | --- |
| `pending` | 修订已保存，等待旧轮确认结算，尚无目标轮 |
| `queued` | 修订已成为目标轮输入，runner 尚未领取 |
| `target_started` | runner 已原子领取目标轮；不证明 provider 已接收或模型已理解 |
| `target_completed` | 目标轮以 exit 0 / `end_turn` 正常结算；包括仍需回答问题的 `waiting_reply` |
| `target_cancelled` | 目标轮确认取消；不表示修订完成 |
| `target_unknown` | 目标轮失败、结果不确定或派工回执缺失；不自动重放 |
| `superseded` | 领取前被同一 queued turn 的另一条修订替换，不归功于后来执行 |
| `withdrawn` | 待执行介入被显式 cancel 或 close 撤回 |

`target_completed` 不是业务目标达成或调整生效证明；协调者仍需评估回答、问题与产物。问题回复可继续同 session 的后续 turn，但不改变该命令的原 target_turn，也不重新提交它。同 task 的相同 command ID、prompt 与 clear_timeout 重试均不重派；同 ID 不同参数拒绝（旧记录缺 clear 按 false）。旧轮或重复结算不能覆盖后继状态；已关闭历史不补写、不重放。这里仍是 cancel+resume，不是 steer，也不新增 queue 模式。

显式 close 会撤回尚无目标轮的 pending 介入；若仍有目标轮的 queued/target_started 记录而任务已允许关闭，则记 target_unknown，不猜测其执行结果。关闭失败也不恢复续派授权。已结算命令保持原状态。

Hermes 原生工具使用 `{"action":"intervene","task_id":"TASK_ID","command_id":"UNIQUE_STABLE_COMMAND_ID","prompt":"修订要求"}`。后续调用 `status` 或 `events` 会在旧轮确认结算后启动同 session 新轮；取消回执本身不会启动新轮，`unknown` 不自动重放。来源仍由当前宿主捕获，不能从其他 session/topic 接管。后台进度回注依赖真实 Hermes terminal/process registry，通用事件可读不代表宿主已消费，更不代表 Telegram 已投递。

## 取消、验收、关闭

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID cancel --task TASK_ID
```

取消受理不等于远端停止。确认实际轮次、退出与终止证据后再转移写权限；EOF、SSH 断线或超时不能代替停写证明。保留已有成果，未知状态不自动另派同一实现。

AGY native 候选把取消清理与业务成功分开：封普通输入后，由原 owner 在 end_turn、可信 roster、无活动工具的串行边界必要时发送一次绑定 cancel_id 的 cleanup-only prompt，撤销旧业务，仅 kill 精确已知 worker，并允许确认过的原生 swarm status 观察。status 的原始工具 ID/name/action、原始参数契约与最终 completed/kind/worker 证据均严格校验，不代替 kill。完整 history 对账每 worker completed kill 后才精确 unload、提交匹配 task/session/turn/launch/epoch/cancel_id 的可信 receipt，允许 cancelled；不要求 CALLCLI_RESULT、不伪造 native_result。缺证、failed、新 worker/re-add、未知/业务工具或发送不明仍 HOLD，不提前 session unload。dialogue 取消只确认该输入返回，不当已答；accepted 未发记 not_sent。正常 seal 后晚到 cancel/intervene 拒绝。

取消在 load outstanding 期间提交时，该 load 返回不能扩充取消 roster；清理仍以请求开始前最后可信完整 history（首次则为已发布 live 工具集合）为准。首次取消 replay 使用严格有序 live/replay 对齐，允许合法 user 回显和分块差异，工具身份／结构缺失或改写拒绝；后续完整快照继续前缀校验。已知 worker 的状态升级允许，新 add 不准入。原始 replay 中 completed kill 后再次 add 同 worker，即使复用 toolCallId，也因 generation 歧义保 HOLD，不能由 snapshot 合并掩盖。cancelled 不能借 native_result/普通成功 cleanup JSON 走 completed 门禁。


native close 只消费已有 cleanup，不再发原生 close。Hermes reconcile(expected_turn) 可对 unknown 读回可信取消／fresh-no-prompt receipt；CLI/OMP 用 close 同一证据门禁。task JSON 手填字段、coordinator 文本、ACPX no-session 都不能代替 owner receipt；缺证保 HOLD，不能卸载后第二 owner attach/load 或重放 close。fresh 独占创建、空 preprompt baseline、未提交 prompt 且 exact unload 的模型设置失败可安全 close，原 unknown/诊断保留至 close；reused/创建不明不适用。

完成或可信取消已卸载后，同逻辑 task 后续 turn 使用 fresh native session 并记 native_predecessor_session_id，不冒充同 native session resume。native_owner_exit 按实际记录 natural/terminate-fallback/kill-fallback 及 returncode；成功路径默认 stop 的 fallback 能力仍存在，v3 实际为 natural 0。取消清理未证路径仅关 stdio、有界观察，不发送 terminate/kill；退出未证记 PID、stdio-closed-exit-unverified，worker fate 仍 unknown，原 owner 退出后持续协调/恢复 unsupported，不称活现场可恢复。receipt 不防有权直接修改私有 DB 的操作者伪造，也不证明任意后代全停。

关闭诊断区分 operation/stage/substep；pre-fence 诊断 operation 保留 backend 的 resolve，用户 close/reconcile 意图另记 requested_action。resolve 失败持久 close_diagnostic 且 external_close_attempted=false，保原 completed、不造 fence；修复依赖后显式 close 可重新 resolve。post-fence close 一次，后续只 probe；first_failure 与 latest_probe 分开，secondary shutdown 单列。仅公开安全 name/code、returncode/signal/timeout、stderr 有无/长度，不回流 message/stack/raw stderr。候选通过不代表当前服务已加载。


完成后读回产物、检查范围并执行必要测试；只读任务则核对返回值与真实输入。执行方自报“完成”不是验收。随后关闭并核对结果：

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID close --task TASK_ID
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID status --task TASK_ID
```

`close` 会关闭自有 ACPX 会话并检查 `no-session`。关闭不明保留原定位，不删除任务库或手改状态制造成功。仅回收本任务拥有的会话、观察器和临时资源，不停止同名但归属不明的程序。

## 常见阻塞：按阶段处理

| 观察 | 应采取的动作 |
| --- | --- |
| connect 无完整回复／`do_not_replay` | 保存已知 client/task，先查同一调用的效果，不重发业务 |
| SSH 中找不到 acpx/node | 发现该设备非交互 PATH 和实际入口，不复制另一机器的内部安装路径 |
| agent 已安装但认证失败 | `auth_required` 仅表示 runtime 报告需要认证、认证来源未知，不证明 executor HTTP 401。保留成果、先核对精确任务；由操作者检查设备认证后再明确授权续办，不传 token、不自动重登或重放。 |
| 工具 failed／疑似权限拒绝 | 先核对持久审批：新 AGY 默认 `approve-all`，可显式 `approve-reads`；OMP/Codex/Pi 默认 `approve-reads`，`write: true` 才选 `approve-all`；旧 AGY 缺字段沿用原 `write` 映射。公开 SDK 不暴露权限决策，不能从 failed 推断 deny；必要时仅查精确自有原生记录。不要把权限拒绝当模型能力失败，也不要自动重放。 |
| Windows Codex `0xC0000142` | 核对 SSH 服务窗口站问题与独立桌面入口，不关闭沙箱或扩大 ACL |
| Codex 沙箱内 Node spawn SSH `EPERM` | 使用已配置的 PowerShell 原生客户端，不自动重复已启动业务 |
| 资源耗尽／多次控制命令超时 | 暂停新派工，按设备和精确进程身份排查，不盲重试或广泛 kill |

## 回报完成

说明整体目标是否完成、真实结果、所在设备／版本／入口、测试范围、是否已收尾及剩余阻塞。已部署与仅改源码分开，局部 GREEN 与完整交付分开。保留必要的私有验收证据，公开仓库只保存脱敏摘要，不上传原始思维流、会话、认证或用户任务数据。
