# Windows Codex：SSH 的窗口站与沙箱启动

- Codex 在 SSH 下执行 PowerShell 即返回 `0xC0000142` 时，先分开包启动、原生模型、受限 shell 和桌面 GUI，不能用 CLI 启动成功证明 shell 可用，也不能把 SSH 故障说成桌面应用故障。
- 只读查询当前 Session ID、GetProcessWindowStation 的名字及 OpenWindowStationW("WinSta0") 权限。Windows OpenSSH 进程可能位于 Session 0 的 Service-* 窗口站；不要据此直接改 ACL。
- 对照所安装 release 的源码。官方 rust-v0.163.0（commit 83086188ae0614bd882818ff1aa23f3194962012）desktop.rs 中 LaunchDesktop 把启动路径构成 `WinSta0\\<private desktop>`，与服务窗口站上下文不匹配。区分源码线索与因果验证。
- 用自有临时桌面做同一 PowerShell `exit 0` 的 A/B：仅变 STARTUPINFO.lpDesktop 中的窗口站；错误 WinSta0 路径可复现 `0xC0000142`，当前服务窗口站路径退出 0。cmd.exe 可能两边都成功，因为不能保证加载同样的 GUI/.NET 初始化依赖；不能拿它的阴性结果排除桌面问题。关闭所有自有句柄，不改已有对象 ACL。
- 经用户针对性批准后，可用现有 Task Scheduler 的 InteractiveToken、最低权限、无触发器、唯一临时任务，在当前已登录桌面会话中新启动一个 Codex CLI 作对照。真实使用原 workspace-write 沙箱执行写入与独立读取，再由外部读回文件。结束后只停止自己的任务实例、删除精确任务名，并再次查询含 hidden 的任务集合确认不存在。任务注册／Run 回执不算代码执行成功。
- 一次性桌面验证成功不等于直接 SSH 入口修好，也不等于真实 Codex Desktop GUI 验收；只证明该启动上下文可用。要提供可复用 SSH 入口，需要另行明确接口、stdin/stdout、取消／断连清理和桌面未登录时的失败语义。不得关闭沙箱、扩大现有桌面 ACL、复制登录凭据或私自建立常驻桥。
- 通过 SSH 传送较长 PowerShell/C# 时避免再嵌一层 powershell -Command 的引号解析，也避免双层 EncodedCommand 超过 Windows 命令行限制。普通 SSH 父进程可用 node - 的 stdin 传递代码，再在该进程中 spawn PowerShell EncodedCommand；这不证明 Codex 受限工具内的 Node spawn 可用。只保留诊断字段与公开结果，不保存原始模型 JSONL、思考内容或完整环境。

## 已落地入口与受限发起端

下文命名 `.ps1` 是可选的本地包装器名称，不代表复制技能后已经安装；先发现用户实际入口，没有时从仓库源码运行或按 `CODEX_SSH.md` 的模板生成。

- PC 独立入口为 `$HOME\.local\bin\codex-ssh.ps1`；每次新建桌面上下文 CLI，要求用户保持登录，不改普通 codex、不改沙箱/ACL，不接管旧会话。实现与边界见当前项目检出目录的 `CODEX_SSH.md` 或 [仓库文档](https://github.com/Loveacup/call-cli/blob/main/CODEX_SSH.md)；它不自动改变 ACPX codex 目标后端。
- 同沙箱内 Node `child_process.spawn(ssh)` 的 `EPERM` 必须与 SSH 程序可运行分开判断。两种已安装 Node 的失败不构成确切底层根因；用相同沙箱的 PowerShell/.NET ProcessStartInfo 做对照，不靠更换 Node、禁沙箱或扩大权限碰运气。
- PC Codex 作为发起端时，可从一开始选已安装 `$HOME\.local\bin\call-cli-native.ps1`，它通过原生 PowerShell/.NET 调用现有 SSH 和同一 collab.py JSON API；普通 Node 客户端保持不变，不宣称其兼容问题已修复。当前原生客户端远端解码用 macOS `/usr/bin/base64 -D`，不外推到 Linux。
- PowerShell 脚本包装另一 PS1 时，把所有参数和字面量 `'--'` 放入字符串数组再 splat。不要写裸 `-- @args`：PowerShell 可消费终止标记并改变后续数组传递，导致下游缺少协议分隔符。先验证真实已安装包装入口，不只测直接数组调用的叶子。
- 原生客户端只做 SSH/JSON，不能代答或隐式重放。完整 JSON 可确认本次动作回复；超时、断线、无完整回复或清理不明返回 do_not_replay。结束本地 SSH 不代表取消远端任务；未知业务效果先按精确 task/session 对账。
- 用 PowerShell ConvertFrom-Json 判断完整回复前先排除纯空白：它可能返回空结果却不抛异常，不能把“没有异常”当作 JSON 确认。回归同时覆盖空白前缀后实际 JSON，以及仅空白直到超时；后者必须 unknown/do_not_replay，且不重发业务。该缺陷已在 Windows 用同一测试哈希真实 RED→六项 GREEN，定点独审 PASS；修复后真实 connect/status 通过，旧版完整模型任务保留为旧版证据。随后 v0.2.2 已部署后的新鲜 PC Codex→MacBook OMP 代码任务也已验收：`e93bc69daa454b8f`，同会话两轮后 closed，父方六项测试及八组 golden cases 通过；证据在 `hermes-call-cli/evidence/real-task-a61ced237d/`。仓库现名 `Loveacup/call-cli`，本地旧目录名不变。
- 真实 PC Codex→MacBook OMP 代码任务已由父方复跑六项测试、八组 golden cases，并验证同会话问答续轮及 closed。源测试与证据位于 `hermes-call-cli/evidence/real-task-91cc1038c0/`；这是该链路的一次实测，不泛化为 GUI、未登录、所有客户端或自动异步唤醒通过。
