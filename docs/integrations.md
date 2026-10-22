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

原生工具支持 `start/list/status/events/reply/cancel/close`。需要运行中 `intervene` 时选择通用 CLI，声明 `--runtime hermes` 也不产生原生来源绑定或通知能力。两条路径不能混称同一次验收。

## 回报用户

简短说明安装的是哪一层、所在设备／profile、实际入口、技能是否加载、真实任务结果和未解决项。安装文件、Doctor、一次模型执行、跨设备闭环分别报告。自动唤醒或 Telegram 多 topic 投递未独立验收时不顺带声称已通过。
