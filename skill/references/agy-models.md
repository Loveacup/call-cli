# AGY 模型选择与版本兼容

在执行设备上使用已安装的原生 `agy acp`，不下载第三方 antigravity adapter，也不改默认 provider。

## 简称、默认与原生 Swarm

唯一需求正本见[AGY 合同](core-requirements.md#agy-native-swarm-target-and-acceptance-boundary)。新 AGY 未指定模型默认 `ocs`，不按任务自动换模型。小写简称 `oc/oa/4c/4a/4h` 分别映射 Orchestrator Carbon/Argon、Gemini 4 Carbon/Argon/Hydrogen；裸简称全部单代理 off，后缀 s 为 native Swarm（`ocs/oas/4cs/4as/4hs`）。后缀 t 是 Teamwork 意图，当前 unsupported，零业务 prompt 拒绝，不降级。简称与显式 agy_swarm 冲突拒绝，重复／混合后缀、组合或大小写变体不猜测。Alex 纯文字／感性工作偏好显式 `4a/4as`，未指定仍 `ocs`。完整 advertised ID/family 及显式路由保持兼容；旧任务不回填、其他 CLI 不变。worker 参数仍用广告 ID/family，默认 G4H。

native 从当次 ACP 目录解析：exact ID 同时是 model.config 选项时直接设置该精确值；OC/OA 已在 AGY 1.5.8 零 prompt 选中并读回，不拼 family/tier，其选中态无 thought_level，显式 effort 拒绝。其他 family/exact 继续使用广告 thought_level 唯一映射，歧义/未知/readback 错误在 prompt 前失败。worker family 按本次目录当前 effort；恢复的 orchestrator 元数据缺 effort 时，原连接临时选择已广告 worker config、load 其公开 effort 目录，再恢复协调者并精确读回，不猜 tier。保存 requested/resolved/readback_verified；worker 仍仅目录解析，不是独立 worker runtime 读回。元数据验证不证明 OCS 模型执行或 Swarm 行为；off 的 ACPX 路径不冒充已实测。


`agy_native.py` 在同一 `agy acp` 连接执行 new／设置／读回／首部 `/swarm` prompt。首轮 end_turn 后保持 running、原 owner 与占用，串行完整 load 观察 native board 自动唤醒后的结果，不发送定时 prompt、不读私有 DB。首回放按已发布工具 ID／消息逐次对齐，后续完整 load 共同前缀对账；失败或变更保 unknown。thought 丢弃，公开工具只留安全字段。

`native_snapshot` 是内部对账副本；Hermes／OMP 宿主 status/list/wait 投影排除它，仍公开 `native_result` 与安全权限决定。通用本地 CLI 记录可保留 snapshot 供授权对账，不把它作为宿主周期通知载荷。

正常结算按本 task/turn 的最终结果、对应 worker 原生 add/kill completed 和 session unload；ACP 缺 rawOutput 不单独导致 HOLD，不宣称所有后代进程死亡。新轮清除旧 result/cleanup/roster。fatal、断线、缺必要事件或不确定取消仍 unknown/HOLD，不自动重放。OMP/AGY unknown 经既有 runner attention 与宿主回流路径提示；通知产生/排队、模型消费与真人收到分层，真实宿主端到端未验。AGY 1.5.7 的 active-load 历史证据保留；另获授权的 AGY 1.5.8 v3 单 worker 样本已验证 end_turn 后一次公开 cancel、原 owner cleanup、completed kill+status、精确 unload/not-found、可信 cancelled、重复 close 相等及 natural exit 0。v3 使用候选线程 launcher/观测 wrapper，非部署/默认 launcher 或 active-tool 热取消验收，也不证明全部后代全停；旧 HOLD 不追溯结算。详见[当前验收索引](../../VERIFICATION.md#current-acceptance-index)。native 不接受硬执行 timeout，后续付费调用仍需明确授权。

取消不等于自动结束 worker。候选仅在原 owner 已 end_turn、完整历史对账、已知 roster 且无 pending/in_progress 工具的边界，封普通 dialogue 后发送一次绑定 cancel_id/turn/launch/epoch 的 cleanup-only prompt，撤销旧业务要求，仅 kill 精确已知 worker；不重复 /swarm，不 add/readd/替换，不新开业务。cleanup 不带普通 check_cancel；send_started 先持久化，超时、发送未知或拒绝不重试。最后完整 load 对账所有 completed kill 才 unload 并提交取消 receipt；缺证/新 worker/历史变化不提前 session unload，仍 unknown/HOLD。前台/工具活跃时不发送该 cleanup，不宣称无损热取消。既有 owner 退出策略不等于会话可自动恢复，unknown 不授予第二 owner。

cleanup 可附带确认为原生 swarm status 的只读观察：原始 call 的安全 ID、swarm 名称、精确 status action 与最终 completed/read-or-other 证据均校验；若带 worker 名须属于冻结 roster，不接受 model 参数。不是仅凭展示标题放行；未知/伪装工具、新 worker、新业务、失败 kill 仍 HOLD，status 不代替 kill/unload/receipt。

取消清理 unknown 的退出只关闭原 owner stdio 并有界观察自然退出，不发送 terminate/kill；未观察到退出时记录 PID、exit-unverified，worker fate 仍 unknown。此路径不保活、不创建接管 owner；原 owner 退出后的持续协调/恢复 unsupported，不能宣称 HOLD 是可恢复的活现场。正常完成/其他正常 stop 沿用原策略。原始 tool_call_update 与新建 tool_call 分开计 generation，合法同 ID 更新及 title 展示刷新不当作 readd；action/worker/model/kind 变化仍拒绝。

生产 prompt 仍强制 turn。仅同一 owner 新建精确 session、task 第 1 轮、提交前 load 为空且 prompt 已返回时，兼容缺 turn 的旧 envelope；仍要求 task 和本轮 add/kill 对账。保存 owner 推定的 turn_attribution 与未改动 original_envelope，不伪称模型回传 turn。恢复会话／第 2 轮起／无 owner 上下文均不适用，继续严格 turn 校验。

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

native callback 的 native_permission_decisions 仅保数值 RPC ID、root/foreign 归属、安全 tool ID/kind、option kinds、choice/reason、response_sent_at/send_error。approve-all 仅选 root 的 allow_once；缺该选项明确 allow_once_unavailable，foreign/child 返回 cancelled 并报 scope mismatch，不放行、不加 skip-permissions。approve-reads execute 仍拒绝。完整 worker 审批继承／真实 60s 等待根因未证，本片不改网络配置或编造 swarm 审批参数。


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
