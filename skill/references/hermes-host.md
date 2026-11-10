# Hermes 原生来源与通知

仅 Hermes 发起或处理其后台通知时读取。`call_cli` 是可选已安装模块，复制 skill 不会启用它。源码／磁盘 schema 与当前会话工具快照分别核验，旧 schema 可能没有 AGY、skills 或 intervene。

## 派工及续办

先查当前工具 schema；支持时 start 保存真实 task_id、原生 process session_id、turn 与来源。模型不得填写 profile/session/platform/chat/topic；宿主真实上下文捕获身份，任务按来源隔离。新会话或另一 topic 不能冒认旧任务。只有已授权编辑设 write:true，共享写面使用相同 resource。

已实现的原生 launch 经 `ctx.dispatch_tool('terminal', ...)` 调用 background=true、notify=true、heartbeat=60；它捕获原会话，不代表实际 Telegram 投递已验。心跳按有新增输出合并，可能只有尾部；用数值 seq 读取完整公开事件，不从心跳尾文重构全程。oneshot、会话结束及宿主生命周期对通知的影响要另查，不能宣称离线唤醒或重启恢复。

新任务未设 timeout 时，call-cli 的后台 terminal 调用省略该键；已核对的 Hermes 后台 spawn 路径没有执行总限时参数，不能用 0／Infinity 或巨大数冒充无限。显式旧任务仍传 T+180。该结论是源码契约，不证明宿主退出、其他版本或真实通知投递；能力不符时报告缺口，不改宿主或配置。启动／控制边界与静默提醒在公共 runner 中处理，不依赖 Hermes 专属调度。

`reply` 使用原 question_id；多个问题全部记录后才续轮。`intervene` 必填稳定 command_id，采用取消旧轮、结算、同 session 续轮。Hermes status/events 会推进已授权的 pending 介入，可能启动新轮，不是纯查询；只读排查不要误触。unknown 不自动推进。续轮核对新 process/turn；旧 runner 完成不能代替新轮结算。

旧 schema 缺少动作时，先核验受支持的已安装通用入口与已有原生桥。使用 standalone 时协调者须消费公开事件并向真实父会话转述，明确没有内层原生 chat/topic 绑定或自动回注；不能伪造来源。确需新 schema 时按宿主受支持方式加载新快照，而非强改工具集。`/new` 不是普遍唯一解，也不转移旧任务所有权。

## 通知轮缺少身份

若通知轮调用 status/events/reply/cancel/close 报 `No host session identity`，用 start 返回的精确 process session_id，经当前 `process_manage` schema 的 poll/log/wait 观察状态与输出。它是观察兜底，不授予任务所有权，也不证明 reply／介入续办已完成。需要继续控制时核验既有来源匹配的支持路径；无此路径就报告精确缺口，不把 process ID 伪装成身份或用另一来源重派。

已结算任务的迟到回执只需核对终态；没有改变交付、失败、阻塞或下一动作的通知静默消费，不输出空调用、占位回复或自行认定用户看到了空消息。对空回复投诉，区分用户观察、模型输出、宿主重试／后备和平台投递，日志未证实不宣称同一根因。

仅在 Hermes 机器通知轮成功消费事件、且确无新增交付、待答、失败或下一动作时，用整个回复恰为 `[SILENT]` 表达静默；不要用空正文代替。当前宿主的空结果／`(empty)` 属于失败诊断路径，不等于静默。真正的人类请求轮应给实质回答，不用裸静默标记；其他发起宿主须按其已核验契约处理，不能照搬此 token。本段是宿主协议指导，不证明模型实际采用、消息未投递或 issue #1 根因已修复。
当前轮只要收到任何真实真人文本（包括排队、steer 或 out-of-band），就必须实质回复，不能仅回 `[SILENT]`；拿不准来源时给出可见回复。合成 S5 已显示原生 busy 机器回合并入真人后仍可能允许静默，这是 Hermes 尚未修复的宿主缺口，不能声称本指导已解决。

## 错误与清理

一个业务任务只启动一次。失败先按 task/process/session/cwd/raw agent command 对账，关闭前读必要公开事件；查不到正文就报告未知，不用历史故障代替本次原因。close 读回当前实现要求的 no-session。通知完整、身份可用、提问／修订续办及真实用户收到分别验收，合成 host dispatch 不算平台投递。
