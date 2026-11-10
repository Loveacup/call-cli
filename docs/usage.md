# 给 AI：在线协作手册

本文用于已配置的发起端。先按[配置手册](configuration.md)确定入口、任务库、目标和授权范围。通用入口支持 `connect/start/list/status/events/wait/reply/intervene/cancel/close`；Hermes 原生工具只使用其实际提供的动作。

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
- `--agent` 取 `omp/codex/pi/agy`：是真正的执行目标；AGY 另传当前发现的 `--model`。
- `--write` 声明已授权编辑／测试与目录占用；OMP/Codex/Pi 沿用按 `write` 选审批策略。新 AGY 默认 `approve-all`，显式只读审批传 `--agy-approval approve-reads`，与 `write` 独立且随任务续轮保留；旧任务缺字段不扩权。审批不是文件沙箱。
- `--resource` 可显式标识跨设备同步工作区；不同别名、路径或 DB 不得用于绕过单写者约束。
- 新任务省略 `--timeout`（或传 `none`）默认不设执行总截止；需要限时才显式传秒数，旧任务的已存 timeout 不变。显式值仍用于 ACPX 每步／prompt 截止和 runner 的 T+90 取消保护，不是精确总墙钟或费用上限。连接与控制限时保留；无预算轮等待 prompt 接受最多 120s，超出请求取消并记 unknown、保留占用。正常运行期间每段连续 600s 没有可见公开事件只发一条 stage 提醒，有公开活动后重新计时；提醒不取消，也不证明无进展或挂死。随时可显式 cancel。
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

问题协议使用 `CALLCLI_QUESTION Q1 问题正文`（依次 Q2、Q3）：`Q[1-9][0-9]*` 后接一个 ASCII 空格和非空正文。编号是同一 task/turn 内的逻辑问题身份，不是源 messageId，也不用于授权。相同编号与完整正文指纹（SHA-256 与字符长度）相同的复述只登记一次，保留首个 event token 供 reply；不同编号或无编号的同文问题仍各需一答。同编号正文不同保留独立可答 token，并附安全 `question_note`，不打断收流或覆盖正文。身份计算发生在公开文本截到 8000 字符之前；不持久化或通知超长原始正文。旧事件缺少完整身份依据时保守保留，不凭截断前缀合并。下一轮可重新使用 Q1；历史事件不回写，旧任务已登记的重复 token 不自动合并。

编号问题事件公开 `question_label`、`question_sha256`、`question_length`；同编号不同正文另带 `question_note`。指纹覆盖解码后完整问题文本（含编号、UTF-8 编码），长度为字符数，均在 8000 字符公开上限之前计算；因此两条可见截断文本相同的事件仍可能有不同指纹并分别待答。

解码行缓冲最多 64000 字符；超限仅保留公开前缀，附身份不完整的 `question_note`，不生成指纹、不按编号合并，保守逐条待答，后续行继续处理。

`Q1:`、`Q1.`、编号前多余空格或编号后多个空格不属于该身份语法，按无编号问题逐条登记，不进行宽松推断。原失败公开文本以 `Q1 ` 开头，已符合此规则；该形状的回放已通过，但真实任务仍需部署后的父方验收。

编号身份由持久事件保存，重新打开 Store 或同轮重复提交不重新登记已回答的问题；这不提供 runner 崩溃后自动恢复或 exactly-once 投递。普通同 token 重放、旧轮事件拒收及全部独立问题答完才续轮的规则不变。

- `running`：结合真实阶段、工具状态和产物判断进展；静默不等于卡死。
- `waiting_reply`：读取具体问题，再自行判断或询问用户。必须使用返回的准确 question ID。
- `completed/cancelled`：对应轮已结算，仍需检查真实结果或残留。
- `unknown`：保留任务与资源定位，先对账原会话和进程；不要重放。
- AGY 一轮公开输出的最后一个非空行若恰为 `⚠ Agent execution terminated due to error.`，即使协议返回 `end_turn` / exit 0，也记为 `unknown`，并在 `settlement_note` 与最终事件中写明“AGY 报告致命执行错误、未完成、结果不明、不自动重放”。该判定只拒绝完成，不代表已知根因；引用、前后缀或其后仍有内容的同类文字按普通内容处理。
- AGY 工具 failed 且最后一个工具事件后无最终回答、也无待答问题时，仅在传输 exit 0、未失败且 end_turn 的情况下，记 `unknown` 并附 `settlement_note`；stage 不算回答。传输失败保留直接的退出说明，不改称工具失败或权限拒绝。正常提问保留 `waiting_reply`，可用准确 question ID 回复；既有 fatal 最终行仍覆盖提问。工具失败后恢复并给出最终回答可 `completed`。协议结束、工具失败和业务结果分别核对。
- `busy: true, task: null`：数据库锁竞争，没有可信状态；保留游标，稍后再看。

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID reply --task TASK_ID --question-id QUESTION_ID \
  --prompt '针对该问题的真实回答'
```

多个问题逐项回答，全部记录后才续轮。发起 CLI 必须自行消费事件和作答；测试脚本替它代答不能证明原生发起端闭环。不要把监控写成一条长期占住控制权的 shell 循环，要允许用户在发起端继续输入要求。

## 执行中主动改向或传达用户要求

用户在当前发起 CLI 输入新要求后，由你读取当前精确任务状态，将修订传给目标，不让用户到另一终端搬运：

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID intervene --task TASK_ID \
  --command-id UNIQUE_STABLE_COMMAND_ID \
  --prompt '保留已有成果，只修改这里明确列出的方向与验收要求'
```

同一次提交的重试复用同一个 command ID。`pending/queued` 仅证明提交，不证明改向生效。实现语义是：请求介入 → 原轮取消 → 确认原轮结算 → 同目标会话续轮 → 读取修订结果。继续短 `wait` 驱动和观察状态转换，不宣称无损热修改。

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

`target_completed` 不是业务目标达成或调整生效证明；协调者仍需评估回答、问题与产物。问题回复可继续同 session 的后续 turn，但不改变该命令的原 `target_turn`，也不重新提交它。同 task 的相同 command ID / 相同 prompt 在任意轮重试均不重派；同 ID / 不同 prompt 拒绝。旧轮或重复结算不能覆盖后继状态；已关闭历史记录不补写、不重放。这里仍是 cancel+resume，不是 steer，也不新增 queue 模式。

显式 close 会撤回尚无目标轮的 pending 介入；若仍有目标轮的 queued/target_started 记录而任务已允许关闭，则记 target_unknown，不猜测其执行结果。关闭失败也不恢复续派授权。已结算命令保持原状态。

Hermes 原生工具使用 `{"action":"intervene","task_id":"TASK_ID","command_id":"UNIQUE_STABLE_COMMAND_ID","prompt":"修订要求"}`。后续调用 `status` 或 `events` 会在旧轮确认结算后启动同 session 新轮；取消回执本身不会启动新轮，`unknown` 不自动重放。来源仍由当前宿主捕获，不能从其他 session/topic 接管。后台进度回注依赖真实 Hermes terminal/process registry，通用事件可读不代表宿主已消费，更不代表 Telegram 已投递。

## 取消、验收、关闭

```sh
python3 /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 \
  --client CLIENT_ID cancel --task TASK_ID
```

取消受理不等于远端停止。确认实际轮次、退出与终止证据后再转移写权限；EOF、SSH 断线或超时不能代替停写证明。保留已有成果，未知状态不自动另派同一实现。

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
