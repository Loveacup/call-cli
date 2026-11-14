# AGY 模型选择与版本兼容

在执行设备上使用已安装的原生 `agy acp`，不下载第三方 antigravity adapter，也不改默认 provider。

## 简称、默认与原生薄桥

唯一需求正本见[AGY 合同](core-requirements.md#agy-原生模式与薄桥边界)。新 AGY 默认 `ocs`；裸 `oc/oa/4c/4a/4h` 为单代理，后缀 `s` 为原生 Swarm、`t` 为原生 Teamwork；或显式 `agy_mode=single/swarm/teamwork/dynamic`（须匹配简称）。用户显式模型和模式优先，不按任务静默改默认。缺所选命令广告不发业务 prompt，不降级 Swarm／普通 prompt。旧任务不回填，其他 CLI 不变。

协调者模型继续按当次 advertised exact/config/effort 设置并读回。Swarm 不再默认选择或验证逐成员 G4H 型号；`agy_worker_model` 仅在显式指定时作为任务偏好转给 AGY，自身不成为完成条件。内部角色、拆分、审批策略、权限和调度由 AGY 原生管理；Call-CLI 不把授权业务范围改写为父写／子只读，不设置 Teamwork 内部审批 mode，也不以子写权限证明作为接入或完成门槛。


`agy_native.py` 在同一 `agy acp` owner 执行 new／模型设置／读回，确认所选 `swarm`／`teamwork` 广告后发送对应 slash command 与用户任务。公开进度、问题和回答沿现有事件回流；`dialogue` 在前台返回后串行发送，保留 task/turn/command/question/interaction 归属，不承诺热 steer。native 问题用带 question_id 的 dialogue 答复，普通 reply 仍是已结算轮续办。完整 load 与已有公开流对账，不读私有 DB、不周期发模型 status prompt。

`native_snapshot` 是内部对账副本；Hermes／OMP 宿主 status/list/wait 投影排除它，仍公开 `native_result` 与安全权限决定。通用本地 CLI 记录可保留 snapshot 供授权对账，不把它作为宿主周期通知载荷。

正常请求结算依据 end_turn、完整公开历史、无活动工具、无待答问题以及最终回答；不要求 CALLCLI_RESULT、逐成员 add/kill/model 或固定汇报模板。发起者仍须核业务产物，completed 只表示当前公开响应合同，不证明整个原生项目或不可见后台工作结束。正常会话卸载仍经原 owner close 与精确负向读回，可信 receipt 记录自身会话收尾，`all_descendants_proven_stopped=false`。缺内部角色细节不单独 HOLD；fatal、断连、空结果、卸载失败或未决公开工具／问题仍如实保留 unknown／运行中。

工具 RPC 的 completed 不代表原生后台工作完成。已观察的 swarm status 若明确报告 working/running/pending/in_progress，只投影未完布尔并继续原 owner 观察／dialogue；最近一次明确状态可替代历史状态，稀疏更新没有 rawOutput 不自行清除未完。当前完整回答仅为 `Run /swarm board`（可带句末句点）也是导航进展，不是最终结果。普通最终答案中提及该命令不拦截；未知原生状态不虚构完成或逐员证明。此窄兼容源于独立合成反例，其他上游输出格式仍需真实样本确认。

Teamwork 最小源码路径用现有 CALLCLI_STAGE/QUESTION 提示：委派或继续工作报 stage，需原来源回答报 question，实际结果再给公开最终答案。已知 `invoke_subagent/send_message/ask_question` 是交接阶段工具，completed 只表示 RPC 返回；该前台响应及初次 replay 的交接确认不作为最终结果，继续持有同 owner，等待后续公开输出／问题。没有逐角色账本、固定人工批准门槛或虚构 phase。后续最终回答仍沿完整 load、无活动工具／问题和精确 unload 判据，receipt 准确 mode=teamwork，close 不要求 Swarm 专属证明。

观察局限：未核实 Teamwork 可机器依赖的结构化业务终态字段。后续未标 stage 的普通文本可能是进展而非结果；只有委派确认、工具终态或缺公开回流时不声称完整业务完成。离线 fixture 证明路由／异步访谈／同源修改／结果与正常 close，不证明真实 provider 暴露相同序列或子团队已停。既有真实样本报告子写审批 60s 超时，但父 ACP 未收到对应公开许可，原因不可观测；这不构成子写禁令或父写要求。公开问题／审批事件有则沿现有机制传递，无事件明确不可观测，不强塞回调或为此修改 AGY 权限、配置及调度。

取消仅转发一次原生 session/cancel，不自动生成逐员 cleanup prompt，不重放业务；当前 ACP 未提供可独立依赖的内部停止确认，因此结算为 unknown/HOLD 并明确内部停写未知，不把前台 cancelled、stdio 退出或 PID 消失当作全后代已停。现存旧任务可信 receipt 仍可按旧合同消费，不追溯补证或释放旧 HOLD。

本次仅源码与离线 fixture 验证；真实 Hermes／OMP 必须更新完整运行时副本并由宿主重新加载后才获得新行为。未部署、未发新模型请求，历史模型／取消样本保持历史口径，不作为当前真实宿主消费或用户实收证明。

以下为显式 off 与旧任务的 ACPX 模型／诊断路径。

## 发起端 Core CLI 负责选择

1. 先读当前 `agy models`，再在执行设备的 Call CLI 检出目录运行 `node models.cjs /absolute/workdir`。后者通过 ACPX 公共 `inspectAgentModels` 读取本次 ACP 模型元数据，无业务 prompt、文件或终端能力；可能留下原生 metadata session 历史，探测连接会关闭。
2. 用户显式模型优先，从当次目录选择 exact/config 值。省略模型沿本文件 OCS 默认，不由协调者静默改成其他“便宜”模型；缺 OC、未知 effort 或控制失败明确拒绝，不换 provider。
3. 可选 JEV 只比较本次实际候选及脱敏任务摘要，不接收凭据、私有任务正文；结果必须属于候选集。不在集合内就拒绝该建议，由发起端在原集合选择。JEV 不是必需依赖，不自动安装，不替代用户选择。
4. 完整 ID/family 沿既有路由；省略 model 而显式 off 保留 OC/off 兼容。简称则已编码路由，如 `oc`＋native、`ocs`＋off 均拒绝，不能悄悄覆盖。原生零 prompt OC/OA 选择成功不等于 ACPX off 业务兼容已验。G4C/off 建议使用当次广告 exact ID；family 与 tier-ID 差异的旧兼容限制不因简称支持消失。旧任务不重解析。

## ACPX 0.22 / AGY 元数据差异

`agy models` 或 legacy `session.models` 可能显示带 effort 后缀的 ID；`configOptions` 可能使用独立 `model` family 与 `thought_level`。**使用 ACPX 当前认可的候选，不从字符串猜测或剥离后缀。** `models.cjs` 使用 ACPX 的元数据优先级；不要把发现的候选固化为代码 allowlist。

需要指定 effort 时，核对本次 ACP `config_options` 的 `thought_level.options`（自有命名会话的 `acpx --agent 'agy acp' --cwd "$work" --format json sessions show "$session"` 可读回）。只有实际广告的值才能传 `effort`；未核实时省略，不从模型名称推断。

```sh
# 替换为刚发现并由发起端选定的 ID；不是固定默认型号。
python3 collab.py --db "$DB" --client "$CLIENT" start \
  --agent agy --model "$MODEL_ID" --cwd "$WORK" \
  --agy-approval approve-reads --prompt 'Read-only inspection; do not modify files.'
# 若本次 metadata 已核实，可加 --effort "$THOUGHT_LEVEL"
```

Hermes `call_cli` 同样接受 `agent: "agy"`、`model`、可选 `effort`。模型/effort 存在任务 JSON，reply/intervene 沿用，不对旧任务强加新字段。创建原生会话使用 `--agent 'agy acp'`；每轮在 prompt 前设置精确模型/effort并读回，事件 `model` 记录该核验。状态、取消和关闭仍指向同一原生命名会话。

ACPX Shared Runtime 的 `setModel`/`setConfigOption` 需要已经运行的 owner，不能用来启动空闲会话。runner 使用原生 CLI `set` 后 `sessions show` 读回，再由 Shared Runtime 提交 prompt；控制失败不提交。不要用离线 mock 的成功推断真实 owner 已存在。

## 持久审批与结果诊断

`--agy-approval approve-all|approve-reads`（Hermes `agy_approval`）仅适用于 AGY。新任务省略时持久化 `approve-all`，本次任务须已有相应工具授权；显式只读审批选 `approve-reads`，shell／写入请求非交互 deny。`write` 独立表示编辑授权和工作区占用，获授权编辑仍须设 true。审批不是 OS／文件沙箱，不能靠 `write: false` 限制真实文件访问。reply/intervene 保留策略，旧 JSON 缺字段继续按原 `write` 映射，OMP/Codex/Pi 的默认值不变。原生 read/search 优先提示不禁用已授权 shell。

native callback 的 native_permission_decisions 仅保数值 RPC ID、root/foreign 归属、安全 tool ID/kind、option kinds、choice/reason、response_sent_at/send_error。既有会话身份校验不变：approve-all 仅选匹配本任务 session 的 allow_once；缺该选项明确 allow_once_unavailable，非匹配 session 返回 cancelled 并报 scope mismatch。这是公开回调归属校验，不是 Teamwork 内部权限配置；不猜测或授权未暴露的子 session。approve-reads execute 沿用既有拒绝语义，不新增 Teamwork 强制策略。完整 worker 审批继承／真实 60s 等待根因未证，不据此要求子权限证明或追加权限实验。


AGY 专属策略在 `agy.py`；任务生命周期、SQLite、runner、Shared Runtime 与 OMP/Codex/Pi 共用。不要另建协调栈。`end_turn`／exit 0 只证明协议结束：工具 failed 且最后工具事件后无最终回答、也无待答问题，或既有 fatal 精确最终行，结算 `unknown`，不自动续轮／重放。stage 不算回答；正常提问保留 `waiting_reply` 可回复，fatal 最终行仍覆盖提问。失败后恢复并给最终回答可完成。

公开事件仅保留安全 identifier 形式的工具 title、类别和状态，舍弃 rawInput/rawOutput/content、摘要、路径和思维链。当前 ACPX 0.22 公共 SDK 没有权限决策事件，`permission_diagnostics.permission_decisions` 为 `unobservable`；工具 failed 的原因不能猜成权限拒绝。AGY wire 的 `name` 当前也未进入 SDK 事件，title 可是完整 shell 正文，此时只显示 ID／类别／状态，不伪造名称。若已授权原生诊断，仅提取精确自有记录的安全 name/kind/status 与权限 optionId，不导出完整日志。

失败轮的 `error.detailCode` 精确等于 `AUTH_REQUIRED` 时投影固定 `auth_required`。若 queue 仅返回 `QUEUE_RUNTIME_PROMPT_FAILED` 等无具体类别的失败，可通过现有公开 `runtime.watchSession()` 最多被动回放 1 秒、每请求 1000 事件、总计 10000 事件；精确 host requestId、turn_started、唯一主 prompt RPC ID 和 response、failed turn_result 均成立，且 `error.code === -32000 && data.authRequired === true` 才分类。回调 ID 碰撞、重复、缺记录、接口不支持或超时都保持原因未知；不读错误正文，不回流原消息或任意 data。

安装的 ACPX 0.22.0 与隔离合成端点已验证该公开路径可达，保留部分成果和 unknown、每轮仅一次 prompt、close/no-session；不是实际模型认证故障证明。`settlement_note` 区分 turn.result 与 session_watch 来源，提示保留成果、由操作者检查认证后再明确授权续办。该类别不证明 executor 401，不自动刷新、重登或重放。只有 fatal 文本而无结构化证据仍为原因未知；本轮不采用 AGY 原始日志或进程绑定探针。

AGY 历史查询始终使用精确 cwd 与 raw agent command，不能用内置 `agy` alias：

```sh
acpx --cwd "$WORK" --format json --agent 'agy acp' sessions history --limit 10 "$SESSION"
```


## 版本与维护

通用版本、来源／安装检查及升级边界统一见[维护检查](maintenance.md)，不因 AGY 特例扩大到其他 CLI。每轮在既有共享安装锁内核对当前 CLI／SDK 身份；首轮 latest 查询是历史，续轮不重新联网或安装。
