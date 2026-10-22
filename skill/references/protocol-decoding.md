# ACP 解码与实任务验收

- 区分活动 `session/prompt` 结果与 agent→client 的工具回调结果。公开 ACP 流可能包含 `fs/read_text_file` 的 JSON-RPC error，随后 agent 正常恢复、提问并以 `end_turn` 结束；不能仅因任意 message.error 就把整轮判 unknown。
- 先核对生产者实际输出，再改解码器：双向 JSON-RPC 可以复用同一个数值 ID；方向存在时按方向、ID 类型和值关联反向响应，不能把 ID 当全局唯一键。若 CLI formatter 已丢弃方向，不在接收端猜测或人为补方向。优先检查正式公开的共享运行时是否提供权威 turn result；没有可靠来源时继续保守 unknown，不因后续 exit0/end_turn 放宽错误过滤。排查只投影方法、ID、错误码、首次失败位置和 stopReason，不复制原始提示、工具正文或推理。
- 产物验收、独立审核与协调端协议结算分别裁决。真实文件及哈希、跨平台测试和审核报告可以支持产物 PASS，但未解决的协议 unknown 仍须记为链路 PARTIAL；精确关闭后的 closed 只证明清理，不把旧轮改写成 completed。
- 保留旧裸 RPC 解码路径的边界：只豁免已相关且不与活动 prompt ID 碰撞的只读回调；响应后消费回调 ID，新方法复用 ID 时清除旧关联。未分类错误、无 prompt 关联、写回调、已消费 ID 和无方向的双向碰撞仍 fail closed，不凭零退出码豁免真实 prompt 错误。
- 使用 `acpx/runtime` 时选择 `createSharedAcpRuntime()`，并核对已安装实现确实复用 CLI queue-owner、排队与同会话恢复；不要把进程内 `createAcpRuntime()` 当作等价替换，也不要依赖带 hash 的内部模块冒充稳定公开 SDK。保留原有权限策略，从精确创建/读回记录获取 `acpxRecordId`，勿混用任务 ID、RPC ID 或 provider session ID。
- CLI 改接 SDK 时先逐项对照原生连接选项，不假设 SDK 自动加载 CLI 配置。认证在执行设备本地解析并传入 `authCredentials/authPolicy`；核对已安装 CLI 的全局／项目优先级、默认值、空值与非法配置处理，保留凭据字面量，不经控制端任务包、事件或异常正文传送凭据。用合成凭据覆盖配置合并和实际 owner 启动，不能只测试注入 factory。
- 区分首轮版本检查历史与当前轮执行版本；等待回答期间可能发生允许的兼容更新。每轮在既有共享安装锁内读回当前安装，再核对实际加载的 SDK 包，不拿首轮快照锁死续轮，也不为消除误拒绝而取消包版本一致性校验。覆盖“提问 → 安装版本变化 → 原会话回答续轮”的回归；这不要求实际升级，更不在续轮中新增 registry 查询或升级。
- 将 CLI prompt 改接公开 SDK 时核对执行设备上的配置语义：共享运行时不自动加载 CLI 的认证配置，须按已安装版本的全局/项目优先级在执行端本地解析并传递 authCredentials/authPolicy；保留凭据字面量，不把凭据放入控制端 payload、任务库、事件或异常。合成认证 endpoint 应隔离 HOME，只使用明确的假凭据，不能继承真实用户 auth 配置来测试。
- 同会话可能等待回答跨越兼容更新。每轮在既有共享安装锁内核验当前安装并与实际加载包匹配；首轮 version_check 是历史，不可永久锁死续轮。当前执行版本单独记录，不为了刷新它重复查询 registry 或升级；测试模拟版本变化与真实安装升级须分开报告。
- 将事件展示与最终结算分开：消费 `turn.events` 时仅投影公开助手文本及必要工具状态，最终等待同一次 `turn.result`。将终结记录绑定协调端生成的 task/turn requestId，拒绝外来、重复、缺失、矛盾和失败回执；助手正文中长得像终结 JSON 的文本不能成为回执。仍共同检查进程退出、有效 stopReason 和实际产物，不把 accepted/queued 当完成。
- 区分共享客户端断连与取消：`closeStream()`、`shutdown()` 或提交者进程退出不证明已接受工作停止。取消必须经原生控制并观察真实 turn 结算；输出观察/转发失败保留 unknown，不因本地客户端结束就释放写占用或重放业务。
- 用真实失败流的脱敏形状建立 RED，再验证真实调用组合。对同号回调问题，可用明确标注的合成 ACP endpoint 经实际 Node bootstrap、Shared Runtime/queue-owner 触发同号请求和真实读错误，同时覆盖真正 prompt error、运行中取消及每次后端 prompt 数量；不要 mock 掉正在验证的加载器、SDK 或排队层。合成端点使用隔离 HOME 和自有临时项目配置，防止原生 CLI 自动把真实全局认证注入测试进程；只记录认证是否成功，不记录值。再用新建真实模型会话验证文件内容、跨设备编码、同会话问答与关闭读回。真实模型跑通但未触发原反例，只是正向 smoke；合成 endpoint、原生传输和真实模型证据分别报告。不要把状态改写、人工添加方向或 mock 结果当修复。
- `Internal error` 不能证明文件缺失。未核实目标端当时的文件状态或权限时，只写“读取回调失败”，不把推测固化为根因；当前文件存在也不能反证过去一定存在。
- 安装与版本、登录与原生协议、受限 shell 和实际代码任务分层报告。Windows SSH 下 `0xC0000142` 不因包安装成功而消失；另一 CLI 的成功不算原 CLI 修好。不要关闭沙箱、改 ACL 或新增 daemon 来凑通过，超出原修复范围的变化先取得针对性授权。
