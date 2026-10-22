# codex-ssh · Windows SSH 执行入口

## 用途与边界

解决 Windows OpenSSH 的 Session 0 服务窗口站与 Codex 0.163.0 沙箱桌面路径不匹配导致的 `0xC0000142`。只为本次调用在当前用户已登录的桌面上下文中新启 Codex CLI；不接管已有会话，不修改普通 codex、全局模型／登录／沙箱配置、系统 ACL 或防火墙，不建立常驻服务。

PC 必须有当前用户唯一可辨认的已登录桌面会话。未登录或会话歧义时预检拒绝；不会自动降级成无沙箱。此入口不是 Codex Desktop GUI 自动化，也不改变 ACPX agent `codex` 的默认后端路由。

## 直接从源码运行

```powershell
node .\codex-ssh.cjs --help
node .\codex-ssh.cjs --cwd "C:\absolute\workspace" --timeout-seconds 900 -- app-server --stdio
```

工作目录必须实际存在。支持非交互 `app-server --stdio` 以及 `exec -`（可附 `--json --ephemeral --skip-git-repo-check`）；exec 的任务正文从标准输入传入，结束时关闭 stdin。未实现交互 TUI 或任意参数透传。显式禁沙箱参数、任意 `-c` 配置覆盖等会被拒绝；app-server 客户端仍需遵守原生权限契约，本入口不构成新的 OS 沙箱或恶意客户端隔离边界。

源文件：`codex-ssh.cjs`。仓库提供可直接运行的源码，不包含预绑定某台协调主机的 `.ps1` 包装器。若用户需要命名入口，由配置 AI 发现实际源码路径后在获授权目录生成下列模板；已有入口先核对，不覆盖未知文件。更新可采用提交 SHA／文件哈希命名的版本目录并保留旧副本，不意味着本项目已有通用安装器。

```powershell
# codex-ssh.ps1：AI 将示例路径替换为该机器已确认的源码绝对路径
& node 'C:\approved\call-cli\codex-ssh.cjs' @args
exit $LASTEXITCODE
```

Windows PowerShell 5.1 的包装脚本若包含非 ASCII 路径，保存为 UTF-8 BOM，并从真实入口验证 argv、中文与退出码。包装器只转发本次调用，不注册常驻服务或改普通 Codex。

## 会话生命周期

1. 预检当前用户、桌面会话、实际 Codex 入口与 cwd。
2. 创建仅绑定 `127.0.0.1` 的临时监听，使用每次 256bit 随机 capability。任务动作只携带配置路径，不携带 capability 或任务正文。
3. 创建唯一、无触发器、最低权限 InteractiveToken 临时 Task；worker 认证后才接通 stdin/stdout/stderr，原始模型流不落日志。
4. 正常退出、EOF、断连、信号与超时均清理本次资源。断连时 worker 删除自身 Task 并停止自有进程树。不要以 Task 不存在单独证明全部进程已停止；验收同时绑定 PID 和创建时间。
5. 错误后的 `DO_NOT_REPLAY` 表示不要盲目重放；先对账自有 Task、精确进程和产物。清理失败不报告成功。

默认活动会话期限 900 秒，允许 1–3600 秒；桌面预检和有界控制／清理另占时间，不宣称严格端到端墙钟上限。临时 Task 的执行上限是兜底，不替代清理读回。

本机 loopback 不是本机用户隔离边界，能读当前用户运行配置的进程仍可能获得 capability；不要将该传输认证描述为 OS 级隔离。原 Codex 沙箱继续决定模型工具的文件与进程权限。

## 已验证与未验证

- Node 离线 28 项测试通过，包含真实本机 socket、字节／EOF、认证拒绝、超时、信号及清理；平台叶子使用假实现，不冒称 Windows 实测。
- Windows 中文与空格 PS1 路径，真实 Task Scheduler／临时 loopback，11,776 字节输入输出完全一致，中文 stderr 一致，非零退出码 7 保真。这里的 Codex 程序是测试假入口，无模型调用。
- 真实 Codex `app-server` 初始化及原生技能目录通过；EOF、主动 SSH 断连、超时三条路径均独立确认 Task 消失，捕获的 worker／子进程 PID+创建时间均不再存活。
- 实际跨 CLI 业务验收以本机 `evidence/` 最新回执为准，不把上述启动测试当完整业务通过。未实际登出／切换用户测试；未承诺桌面不可用时工作；未认证 GUI 行为或所有客户端／路由组合。

## Windows Codex 发起协作：原生 PowerShell 客户端

当前 Windows Codex `workspace-write` 沙箱内，已安装的两种 Node 运行时均观察到 `child_process.spawn(ssh)` 返回 `EPERM`；不据此断言确切的底层原因，也不宣称该 Node 兼容问题已修复。同一受限上下文中 PowerShell/.NET 启动系统 OpenSSH 已真实成功，因此增加独立薄客户端 `remote-client.ps1`，不更改原有 Node 客户端、普通 `call-cli.ps1` 或 Codex 沙箱。

在 PC Codex 的 PowerShell 工具中调用：

```powershell
& .\remote-client.ps1 --host coordinator --entry /absolute/call-cli/collab.py --db /absolute/private/tasks.sqlite3 -- connect --runtime codex
# 后续仍使用原有 --client、start、wait、reply、intervene、close JSON API。
```

由发起端现有 SSH 别名连接协调主机的 `collab.py`，复用同一任务库与完整业务协议；示例 `coordinator` 是需按现态解析的占位别名，不是维护者机器地址。客户端不代答、不自动重放业务，不另建服务、状态库或执行控制面；只将 Windows 参数编码后交给远端 POSIX shell，并传回完整 JSON。当前解码使用 macOS `/usr/bin/base64 -D`，**仅核验 macOS 协调端，不承诺 Linux 可用**。

若要生成 `call-cli-native.ps1`，由配置 AI 将主机别名、协调端路径和本机源码路径替换为实际发现值，再在获授权位置写入模板：

```powershell
$forward = @('--host', 'coordinator',
  '--entry', '/absolute/call-cli/collab.py',
  '--db', '/absolute/private/tasks.sqlite3', '--') + @($args)
& 'C:\approved\call-cli\remote-client.ps1' @forward
exit $LASTEXITCODE
```

将字面量 `'--'` 和所有转发参数放进同一个数组再 splat，不写裸 `-- @args`。先从生成的入口做 connect/list，再启动任务，不能以直接调用叶子脚本代替包装器验收。

客户端等待有界，完整 JSON 确认优先于其后的 SSH 退出不确定性；无完整回复或清理不明返回 `do_not_replay: true`。结束本次 SSH 子进程不等于取消远端协作任务，业务取消与关闭仍使用原有 API。运行副本存于 `.local/share/codex-ssh/native-clients/<sha256>/`，安装时拒绝覆盖未知内容。

当前证据：Windows 六项假 SSH 用例通过（完整回复提前返回、错误、部分回复超时且只启动一次、参数引号、空白前缀后完整 JSON、仅空白直到超时）；父方另行精确断言中文回复和 POSIX argv 保真。第一独审发现纯空白可被 PowerShell 误当解析成功，已先用两个新增用例复现真实 RED，再加最小非空白守卫得到同测试哈希的六项 GREEN。修复版已安装、哈希读回一致，真实 connect 和既有 closed 任务 status 查询均通过。

真实业务 `30f76ce63b5145a1`：PC Codex 经此客户端派发 MacBook OMP，读取真实 question_id 并自行回复，同会话续至 turn 2，生成 `csv_audit.py`、`test_csv_audit.py`、`README.md` 并关闭。父方复跑六项测试和八组 golden cases 全部通过；Windows 收尾读回无 `CodexSsh-*` Task、desktop worker 或 Codex stdio app-server。此次未在运行前捕获完整 PID 树，不能把收尾快照宣传成逐 PID 的前后跟踪（此前独立生命周期测试已做精确捕获）。源码与 PC 运行副本哈希一致。证据目录：`evidence/real-task-91cc1038c0/`。完整业务运行使用空白守卫修复前客户端 `49605ef5…`；修复版 `88f576fa…` 的验证为六项 Windows 回归与真实 connect/status，并未重跑整套模型代码任务。定点独立复核已 PASS（`evidence/native-client-review-final.json`），源码、RED/GREEN 与测试哈希已核对；其范围仅上一 finding，不外推全面 Windows 审计。

