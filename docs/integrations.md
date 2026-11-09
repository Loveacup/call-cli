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
  "write": false,
  "timeout": 120
}
```

保留真实返回的 `task.id` 和 process ID，后续动作只用该 ID。宿主捕获 profile/session/platform/chat/topic，模型不能填造这些来源字段。

原生工具支持 `start/list/status/events/reply/intervene/cancel/close`。运行期介入使用精确 `task_id`、修订 `prompt` 和必填稳定 `command_id`；重试复用相同 ID。`pending/cancelling` 不是生效证明：先等旧轮结算，再调用 `status` 或 `events` 推进同 session 续轮；重复查询不重复启动，`unknown` 不续派。`cancel` 撤回待执行介入。通用 CLI 声明 `--runtime hermes` 不产生原生来源绑定或通知能力，两条路径不能混称同一次验收。

**Hermes 的 `status` / `events` 不是纯读操作**：它们会推进此前已由 `intervene` 授权、仍为 pending 的介入；旧轮确认结算后，查询可能启动同 session 的下一轮。没有已授权 pending 介入时不会自行派工；`unknown` 保留不确定结果，不自动重试。终端 dispatch 的迟到回执按 turn 与 launch identity 条件写回，不能覆盖新轮的状态或 process ID。

升级前没有 `needs_session` / `acpx_created` / `session_creation_attempted` 的旧 Hermes 任务，仅凭 `cancelled`、空 process ID 或字段缺失不能推断会话尚未创建。已有 native record ID 则复用；没有 ID 时，只有第一轮取消终态及该轮 runner 持久事件 `Cancelled before provider launch` 同时存在，才允许续轮首次创建。证据不足会在记录介入或取消旧轮之前明确拒绝，提示先核对旧任务再使用新的 `start`，不猜测重建。取消发生在 runner 启动前、没有此事件的旧记录也遵循拒绝策略。

## 后端扩展接口

`bridge.py` 只管理任务归属、状态机、轮次、问题、介入、游标和 writer 占用；`runner.py` 认领一轮、持久化公开事件并通过宿主原有 stdout 通知。`backends.py` 是唯一后端注册表，两个入口共用 `prepare_start`，CLI choices 与 Hermes schema 从注册表生成。

一个适配器提供三个方法：`prepare_start(model, effort, approval)` 返回持久选择字段；`run(store, task, emit)` 执行已认领轮次，发出安全的 `stage/answer/question/tool` 事件并返回 `TurnResult(state, exit_code, stop_reason, note)`；`control(task, action, timeout)` 提供精确会话控制与关闭后的 `status_snapshot/no-session` 读回。执行中检查共享任务的 `cancelling`，只有执行真正结算才能返回 cancelled；未知结果返回 unknown，不重放。适配器不修改归属、轮次或 writer 占用；现有设备锁可以复用，不新增进程服务或状态库。新增后端只需实现并注册它，随 runner 安装，不必修改其他后端；不要求使用 ACP。

当前扩展边界：公共 start 仍显式接受 AGY 专用 `agy_approval`，并以第三个 `approval` 参数交给适配器；不存在通用后端私有参数字典。close 仍沿用 ACPX 风格约定：CLI 要求 JSON `action=status_snapshot`、`status=no-session`；Hermes 端检查 stdout 的 `no-session` / `not running` 字符串。非 ACP 适配器必须归一化到此控制返回格式。这不阻碍已实现的 fake 后端；未来出现实际专属参数或关闭格式需求时再扩展公共契约，不代表任意新后端零入口修改。

结算状态与该轮唯一 `<turn>:settled` 事件在同一事务提交，事件带持久 seq、问题 ID、回答摘要和适用的介入续作提示。runner 仅打印事务接受的结算事件；即使其后轮次已推进或进入关闭，也不会丢弃这个已合法结算的通知。被拒绝的旧 runner 不广播终态，普通旧轮事件过滤保持不变；游标补读可恢复已入库事件，不承诺进程崩溃后的 stdout 重放。

当前 `acpx_backend.py` 负责 ACPX 命令、会话创建、模型读回、共享安装锁与 Shared Runtime 传输；`transport.py` 保留通用本机/SSH 命令和协作协议；`events.py` 规范化 ACP 公开事件。OMP/Codex/Pi 使用该适配器原默认策略，AGY 在 `agy.py` 局部覆盖 raw command、显式 model/effort、默认 approve-all、提示与结果判断。`test_backends.py` 用非 ACP fake 后端通过同一公开协调入口完成问答、运行期介入、续轮、隔离、writer 冲突和关闭；这不是模型或宿主通知验收。

公共 start 可选 `skills` 数组，两个入口通过 Store 创建固定快照；ACPX 后端每轮在 prompt 前经现有目标命令暂存并加入技能路径/模式提示。其他后端可复用 `skills.stage(store, task, emit)` 与 `skills.guidance(task)`，不使用时不得声称支持读取。目标 `CALLCLI_SKILL` 行规范化为独立 skill 事件，Store 按轮次记录实际自报；required 缺报只标未验证，不改业务结算。契约、限制和选择策略见[技能传递](../skill/references/skill-passing.md)。

运行时安装必须一起包含 `__init__.py`、`collab.py`、`bridge.py`、`runner.py`、`backends.py`、`acpx_backend.py`、`agy.py`、`events.py`、`transport.py`、`updates.py`、`skills.py`、`skill-stage.cjs`、`shared-prompt.cjs`、`models.cjs`。源码变更不会自动更新已安装副本或已加载 schema。

## 回报用户

简短说明安装的是哪一层、所在设备／profile、实际入口、技能是否加载、真实任务结果和未解决项。安装文件、Doctor、一次模型执行、跨设备闭环分别报告。自动唤醒或 Telegram 多 topic 投递未独立验收时不顺带声称已通过。
