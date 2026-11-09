# AGY 模型选择与版本兼容

在执行设备上使用已安装的原生 `agy acp`，不下载第三方 antigravity adapter，也不改默认 provider。

## 发起端 Core CLI 负责选择

1. 先读当前 `agy models`，再在执行设备的 Call CLI 检出目录运行 `node models.cjs /absolute/workdir`。后者通过 ACPX 公共 `inspectAgentModels` 读取本次 ACP 模型元数据，无业务 prompt、文件或终端能力；可能留下原生 metadata session 历史，探测连接会关闭。
2. 从返回的 `availableModelIds` 选择一个精确 ID。用户明确选择优先；未指定时由发起端根据任务复杂度、延迟、成本选择，并说明理由。简单探测优先轻量模型，复杂实现/审查按需求选择能力更强的当前候选，不永久绑定历史型号。
3. 可选 JEV 只比较本次实际候选及脱敏任务摘要，不接收凭据、私有任务正文；结果必须属于候选集。不在集合内就拒绝该建议，由发起端在原集合选择。JEV 不是必需依赖，不自动安装，不替代用户选择。
4. 新 AGY 任务必须传 `model`；普通 agent 仍可省略。模型不可用或控制失败时停下报告，不替换模型、不切 provider、不重复业务任务。

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

AGY 专属策略在 `agy.py`；任务生命周期、SQLite、runner、Shared Runtime 与 OMP/Codex/Pi 共用。不要另建协调栈。`end_turn`／exit 0 只证明协议结束：工具 failed 且最后工具事件后无最终回答、也无待答问题，或既有 fatal 精确最终行，结算 `unknown`，不自动续轮／重放。stage 不算回答；正常提问保留 `waiting_reply` 可回复，fatal 最终行仍覆盖提问。失败后恢复并给最终回答可完成。

公开事件仅保留安全 identifier 形式的工具 title、类别和状态，舍弃 rawInput/rawOutput/content、摘要、路径和思维链。当前 ACPX 0.22 公共 SDK 没有权限决策事件，`permission_diagnostics.permission_decisions` 为 `unobservable`；工具 failed 的原因不能猜成权限拒绝。AGY wire 的 `name` 当前也未进入 SDK 事件，title 可是完整 shell 正文，此时只显示 ID／类别／状态，不伪造名称。若已授权原生诊断，仅提取精确自有记录的安全 name/kind/status 与权限 optionId，不导出完整日志。

AGY 历史查询始终使用精确 cwd 与 raw agent command，不能用内置 `agy` alias：

```sh
acpx --cwd "$WORK" --format json --agent 'agy acp' sessions history --limit 10 "$SESSION"
```


## 版本检查不等于升级

每个逻辑新任务查询 installed/latest 一次，结果持久保存；尚未启动就改为续轮的新任务也必须检查。reply/status/wait/close 不重复 npm 查询。每轮执行持有共享安装锁，并重新读取本地安装身份以保证 CLI 与 SDK 来自同版；这是防竞态核验，不是新一轮 latest 查询或硬编码版本锁。

正常派工只检查，绝不自行 npm install。独立维护只有明确授权、来源已验证且设备无活动任务时才能升级；活动会话、来源不明、主次版本变化需要人工/发起端评估，不为了升级停止别人任务。内部 `ensure_current(..., allow_upgrade=True)` 仅保留受锁和忙碌检测保护的同主次版本 patch 维护路径，不是工具参数，也不是自动授权。升级后读回版本并真实短测；当前版本缺少所需公共 API 时明确阻塞，不改走未知 provider。
