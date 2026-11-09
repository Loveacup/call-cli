# 选择与传递任务技能

派工需要特定方法、脚本或参考材料时，发起端先选择并阅读技能，再通过公共 `start.skills` 传递。Hermes 与其他 CLI 使用同一契约；call-cli 不负责搜索、全局安装或同步技能。

## 选择

1. 先按任务找本地合适技能。Hermes 用现有 `skills_list` / `skill_view`，其他 CLI 用其原生技能发现；读取 SKILL.md、所需引用和脚本、依赖声明，判断目标设备能否满足依赖。任务相关性优先，不为派工强选不适用技能。
2. 本地不足才搜索公开技能。Hermes 可用现有 `hermes skills search QUERY --json` / inspect；其他 CLI 用已有网络搜索。比较同类候选时看仓库级 star、作者和最近实质内容更新；license、机器人、批量格式或权限位变动不冒充方法更新。没有 star/更新数据就明确缺失，不生成评分系统。
3. 读取候选内容与依赖后固定公开仓库 commit，在 scratch 下载该技能和所需相对资源。记录 URL、revision、选择理由、仓库 star 和实质更新时间到 `source`。不要用全局 `install` 替代临时固定版本，也不上传私有任务、材料或凭据去搜索。
4. 传递前检查整个所选目录。当前传递目录内全部非排除文件，保持相对路径；只准备本任务需要且允许目标模型读取的资源。若引用在目录外，先把合法资源整理到 scratch 的自包含目录并修正引用。自动过滤不是隐私审计：普通文件也可能有私有信息。

## 公共接口

Hermes 新 schema 或 Python `Collaboration.start`：

```json
{"skills":[{"path":"/absolute/selected-skill","mode":"required","location":"origin","source":{"repo":"owner/repo","revision":"full-commit-sha","selection_reason":"task relevance and dependency review"}}]}
```

通用 CLI 的 `start` 支持可重复 `--skill required=/absolute/skill`、`--skill optional=/absolute/skill`、`--skill reference=/absolute/skill`；执行端已有资源使用 `--target-skill MODE=PATH`，包括 Windows 绝对路径。`--skills-json` 接收同一数组，可带 `source`。`path` 也可直接指向 SKILL.md。不传 skills 的旧调用保持原行为。新源码不等于已安装入口或旧 Hermes 会话 schema 已刷新。

- `required`：先实际读取并遵循适用方法；不适用则说明 `not_applicable`，依赖缺失则报告 `unavailable`，不得伪称采用。
- `optional`：按任务价值决定读取/采用，未采用说明原因。
- `reference`：按需读取作参考，不把其指令视为约束。

## 固定、读取、回报

origin 技能在创建任务时保存每文件 SHA-256 清单与任务私有快照；target 技能在第一次目标暂存时固定。每轮通过现有本机/SSH stdin 通道在执行端用户临时目录 `call-cli-skills/<task>/<skill>/<sha>/` 暂存，不修改工作目录或全局配置。origin 副本每轮从固定快照恢复；target 快照后续验证原字节，缺失或被改不重新读取变化后的源。reply/intervene 沿用同一选择、SHA 与会话；新任务独立目录，即使技能名相同。

目标按提示中的绝对 `skill_file` 读取 SKILL.md 和相对资源。优先 agent 自有 read/search 工具；已授权时脚本经现有解释器调用。ACPX 0.22.0 的 `fs/read_text_file` 回调仅允许 cwd 子树，不能读取此临时目录；agent 原生工具不是该回调，已实测 OMP 在 macOS/Windows 可读。仅有受限回调的目标应如实报告不可读，不扩大 cwd、写入用户项目或偷偷变更权限。依赖安装、全局配置与发布仍需原任务授权。

每轮结束前，每个技能单独一行：

```text
CALLCLI_SKILL skill-1 adopted Read SKILL.md and references/example.md; used its method
CALLCLI_SKILL skill-2 not_applicable Task does not involve this format
CALLCLI_SKILL skill-3 read Consulted reference; no instructions adopted
```

ID 使用 start 返回的 `skills[].id`。状态为 `read/adopted/not_adopted/not_applicable/unavailable`，附具体原因或证据。回执是目标自报；发起端仍核对 read 工具、内容依赖结果或产物，不把暂存成功当采用证明。任务状态保留逐轮 `skill_reports`；required 缺本轮回执列入 `skill_report_missing`，`skill_verification=unverified`，不把可用业务结果自动改为 unknown。`reported` 仅表示回执齐全，不代表独立验证采用。

required 暂存失败不提交业务 prompt；optional/reference 暂存失败标 unavailable 继续业务。成功 close 后删除本任务协调端快照并尽力清理执行端暂存；远端清理失败留 `skill_cleanup` 事件，不能宣称清理完成。未关闭任务保留资源供续轮/核验。

自动跳过点文件/目录、node_modules、__pycache__、符号链接、常见密钥/凭据文件；限制为每文件 4 MiB、每技能 16 MiB、1000 文件。超限显式报错，不截断内容。完整 skipped 清单可在任务中检查。这里不是技能沙箱或恶意代码判定服务。
