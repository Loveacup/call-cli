---
name: call-cli
description: "Coordinate cross-CLI and cross-device agent tasks: dispatch, monitor progress, answer questions, revise work and accept results. Use for ongoing collaboration with OMP, Codex, Pi or AGY, not ordinary local commands."
version: 1.3.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [acpx, orchestration, omp, codex, pi, agy]
    related_skills: [hermes-agent]
---

# Call CLI · 调 CLI

新建跨 CLI／跨设备协作会话，发起端负责在线协调、实质汇报与验收；Hermes 可以参与，但不是必经中转。复用 ACPX、现有 SSH、公共任务桥与宿主原生能力，不另建调度平台或常驻服务。普通本机命令、纯宿主内部子代理及已有平台负责的编排不必走本技能；接管手动会话另需明确范围。

## 核心方向

目标是从真实 origin 到执行期进度／问题／失败回原会话，用户及发起 agent 都能动态调整正确任务，核验接收与生效，持续续办直至验收收尾。发起端为 OMP、Codex、Pi 或 Hermes；AGY 仅为执行端，不再验收其发起能力。OMP 同样逐层验证，不预设达标。

[当前核心需求](references/core-requirements.md)是唯一维护正本，个人副本仅保留语义镜像。**修改本技能或配套实现前先读；需求变化先更新正本，再同步正文、引用、模板和验收。** 源码支持、安装部署、当前 schema、真实执行和用户收到分别判定，不把目标写成已完成。

## 启动：先确认来源、能力与范围

1. **一次逻辑启动检查**：[维护源与安装检查](references/maintenance.md)覆盖 call-cli、本轮参与 CLI 的协作 skill；任务所选 skill 另查来源／版本、目标可读性与采用。记录 installed／available／unknown，查询失败不称最新；同任务 status／续轮复用结果，不重复全检。检查不是升级授权。
2. **在哪里、做什么**：按[设备发现约定](references/devices.md)复用私有清单或本任务现有别名，确定目标绝对 cwd、输入、写入范围、产物和验证。跨设备文件不自动同步；复杂任务用[任务模板](templates/task.md)。
3. **真实返回路径与责任方**：先核验发起宿主如何消费事件、收原会话输入与继续行动，再选入口与执行端；跨轮任务及短命发起方按[生命周期责任](references/coordination.md#单一-owner-与有效上下文)明确持续协调与收尾。命名 runtime 或 client handle 不是认证来源；保存设备、DB、client/task/session/turn、原生 process 与 cursor，不能猜“最近任务”。

| 入口 | 来源与回流 | 使用条件 |
| --- | --- | --- |
| Hermes `call_cli` | 宿主捕获真实来源；原生 terminal 通知桥 | 只用当前实际加载的 agent／动作；先读[宿主分支](references/hermes-host.md) |
| OMP `call_cli_omp`（实验性） | 原生 ctx 注入身份；受管 1 秒 wait0 轮询后 followUp，不是推送或 steer | 仅在新建自有 RPC 实例以 `omp --mode rpc --no-ui --no-extensions -e /absolute/call-cli/omp-extension.mjs` 单次加载；先读[依赖与加载边界](references/deployment.md#omp-实验性宿主适配) |
| 通用 `collab.py`／已安装包装器 | 私有 client handle；发起端自己消费短 wait 并向原会话转述 | 按[在线流程](references/online-collab.md)核对当前 help；不宣称 chat/thread 绑定或自动回注 |
| 原生 ACPX | 命名会话、流与控制能力依适配器 | [独立短测或诊断](references/native-commands.md)，不绕过已登记任务再执行一遍 |

旧 schema 缺动作时，先核验已有受支持入口及宿主桥；明确通用路径损失和协调者中继责任。需要新工具快照时才考虑宿主支持的重载／新会话，`/new` 不是普遍唯一解，也不会让新来源取得旧任务所有权。AGY 发起端已取消，不为此补 runtime 标签或替代入口。

OMP 实验适配复用现有 core／writer 互斥：同 session 首次并发请求共用 bind、各 task 独立跟踪；身份不由模型填写。退出或切 session 只停宿主投递，不取消业务。已验零模型 RPC close，不代表普通 TUI、ACP defer、恢复或真实最终业务 E2E 通过；维护检查仍覆盖 call-cli 与本轮实际参与 CLI 的协作 skill，不因新增入口改变检查目标。

## 在线协调：消费、判断、接续

- 用数值 `seq` 增量消费公开事件，有界 `wait` 给原宿主输入让出控制权；心跳仅是唤醒，不是完整记录。区分事件产生、持久化、回注、协调者消费与用户实际收到。实质阶段、待决策与失败及时转述，不逐 token 或重复心跳刷屏。
- 发起 agent 根据偏差／阻塞在授权内主动协调，不只转发用户。问题保留精确 `question_id`，能自行决定就回答并观察续轮；真正需用户决定才回原会话询问。多个待答问题逐条结算，不能提问后失去后续责任。
- 公共 runner 每 600s 至多发一次复核请求，长期活跃与静默共享冷却；发起 agent 读精确 task/turn/seq 和真实现状后选择等待、明确介入或取消，不将提醒自动升级为终止，也不必逐条转给用户。Hermes/OMP 不接收硬 timeout，旧 CLI/Python 显式硬预算仅非常规兼容且未清除仍生效；受支持 reply/intervene 可 clear_timeout，queued 拒绝清除，unknown 不绕锁。具体多问与介入替换契约见调用手册；入队不证明已消费，通用 CLI 依赖活跃 owner 轮询。
- 用户修订按精确 task 与稳定 `command_id` 传递；重复提交沿用同 ID。**steer、queue、cancel+resume 是不同语义**：先查所用栈的真实能力，再明确采用方式。当前 Shared Runtime 拒绝 steer；`intervene` 是取消、结算、同 session 续轮，不是无损热修改。详见[控制语义及生效判据](references/online-collab.md)。
- 区分命令已保存、待续轮、执行端已接收与结果实际体现；`pending/queued` 不等于生效。绑定目标 turn 的事件、修订相关结果与产物验证，不凭自报或状态名判定有效。旧 runner 完成不等于新轮完成；续轮重新跟踪实际 process/turn。
- 只汇报有用变化；静默不等于失败，alive 不等于有进展。`unknown`、断线或工具失败先按原句柄对账，不换模型、换通道或新派任务盲目重放。公开 SDK 不可观测的权限决定如实写未知。

## 权限与任务材料

授权决定业务范围；审批不是沙箱，write 表示编辑授权与目录占用。新 AGY 默认 `ocs`，裸简称 off、后缀 s 原生 Swarm、t 原生 Teamwork 源码候选；显式模式须匹配，缺广告不降级。旧任务、完整广告 ID 与其他 CLI 保持兼容。内部角色、分工、审批策略及权限由 AGY 原生负责，桥不设置父写／子只读或子权限证明门槛，不改 AGY 配置和调度。公开问题／审批事件沿现有机制传递，无事件明确不可观测；native 问题用绑定原 question_id 的 dialogue，普通 reply 仍为已结算轮续办。正常公开结果与精确卸载不要求逐成员 add/kill、不宣称后代全停；Teamwork 首次委派返回不是完成，观察边界见[模式合同](references/agy-models.md)。fatal/异常 unknown 主动回流，不自动重放；源码、安装与用户实收分别验。

方法材料先本地选择，不足再按相关性、维护来源与实质更新找公开候选；[任务技能传递](references/skill-passing.md)提供 required／optional／reference 快照与逐轮读取／采用核验。暂存、读取、自报采用、独立验收是不同证据；传递 skill 不扩大业务权限，也不默认全局安装。

按[生命周期责任](references/coordination.md#单一-owner-与有效上下文)保留单一 owner 和同写面唯一 writer。复用公共核心、发起端适配、执行端适配及工具链／skill 传递的小接口，仅隔离真实差异。已有平台编排的工作交回其 owner；不建立竞争调度。Git 发布、部署、系统安装、凭据操作、其他 profile、服务重启及删除无关文件须有针对性授权；已授权同目标发布的普通返工按原权限继续，新增目标或重要权限变化再确认。普通派工或维护检查不包含这些权限，详见[发布边界](references/deployment.md)。

Hermes native task 在旋转式 compaction 后仅可沿同一已验证 profile 的 committed compression metadata，由 SID 新段续办原有 task；不能据此接管其他 session／subagent。细节与 fail-closed 边界见[Hermes P1 连续性](references/hermes-host.md#轮转压缩后的同源任务访问)。

只负责派工、短 wait 和转述的中继使用 `write:false`；在同一 cwd 发起内层 writer 时，中继不占用写锁。只有实际修改该写面的执行任务声明 `write:true`。scratch cwd 可用于职责隔离，但不是同 cwd 中继的必要条件；不要通过另一个 DB 或放宽锁绕过占用。

已结束 owner 的旧任务可沿[分动作恢复](references/hermes-host.md#已结束-owner-的旧任务)处理：只读查看不续跑；显式接续绑定 expected_turn/command_id 与当前 controller，关闭/对账须 exact-record fence。保留回执 recovery card；coverage 为 uncovered 或 pre_exit_hook=false 时，不宣称已交接或退出前会自动拦截。

## 验收与收尾

发起端读真实回答与产物、检查范围并运行必要验证，再报告整体完成／部署状态。`completed`、exit 0 和执行者自报都不代替验收；协议 unknown 与产物通过分别记录。取消回执不等于停写，确认结算后才续作；未知结果保持资源占用并对账。结束后关闭自有会话，读回当前实现要求的 no-session，清理自有 watcher／临时材料；不能为清理成功改写失败历史。

[验证范围](references/verification.md)区分指导场景、合成协议、真实模型、真实发起端消费及原会话投递；局部成功不外推所有 CLI／设备。把授权内剩余工作持续办完，不把一个切片通过说成整体完成；真正缺授权或入口能力时列精确缺口与候选，不扩大实现。

阶段复盘优先更新项目已有记录和 issue，先定位、读已有正文并去重；对外写入需授权且脱敏、写后读回，不自动关闭未验收工作。复用既有证据，反例推动具体规则修订或撤回，不不断追加通用禁令。详见[反馈与协作维护](references/coordination.md)。

## 按分支读取

- [维护源与安装检查](references/maintenance.md)：每次逻辑启动的检查、更新边界与兼容性。
- [在线流程](references/online-collab.md)：通用命令、问答、介入、跨平台传输与链路验收。
- [Hermes 宿主](references/hermes-host.md)：真实来源、旧 schema、通知轮身份与原生过程观察。
- [原生命令](references/native-commands.md)：权限、SSH／编码、watch、取消与故障定位。
- [AGY 模型与审批](references/agy-models.md)：候选／effort、结果不确定与安全诊断。
- [协议结算](references/protocol-decoding.md)：Shared Runtime、RPC 反例与权威结果。
- [Windows Codex SSH](references/windows-codex-ssh.md)：服务／桌面会话、独立入口及受限客户端。
- [副本与发布](references/deployment.md)：源／个人差异、依赖清单、加载验证及独立部署授权。

项目接口从当前安装与公开 [Loveacup/call-cli 文档](https://github.com/Loveacup/call-cli)核对；这是可移植指导，不携带维护者的设备库存、历史授权或安装认证。
