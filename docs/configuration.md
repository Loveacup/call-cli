# 给 AI：安装与配置 Call CLI

这是执行手册，读者是正在帮助用户配置 Call CLI 的 AI。**自行发现可查明的环境信息并执行获授权步骤，不把本文件转成让用户逐项填写的配置教程。** 本项目没有统一的 `call-cli.yaml`、安装向导或模块注册服务；下面的参数用于你组织任务，不是程序会读取的新配置格式。

终端命令和 CLI 内加载方法分工见[集成手册](integrations.md)；配置后使用[协作手册](usage.md)。

## 输入与权限

从用户请求和本机现态确定这些参数：

- **协调主机**：本机 POSIX，或已有 SSH 别名；记录源码入口的绝对路径。
- **发起端**：普通终端、OMP、Codex、Pi，或 Hermes 原生工具。
- **任务库**：一个明确的 DB 路径。后续调用始终复用同一位置。
- **执行目标**：`local` 或已有 SSH 别名；目标 agent 为 `omp`、`codex`、`pi` 之一；使用目标设备上的绝对 cwd。
- **接入范围**：仅源码入口、增加技能，或增加 Hermes 模块。按用户指定范围选择，不默认全部安装。
- **权限**：只读为起点；写任务需用户授权具体目标和路径。

已有安装就核验并复用。发现已有文件、链接或工作区改动时保留它们，不用强制覆盖解决冲突。新建协作会话，不接管用户打开的交互会话。服务重启、凭据操作、Git 发布及仓库可见性变更不包含在普通配置授权中。

## 1. 发现环境

在涉及的每台设备分别检查，不以协调端成功推断远端可用：

```sh
# POSIX：仅发现，不安装
uname -s
command -v python3 node npm acpx
python3 --version
node --version
npm --version
acpx --version
```

```powershell
# Windows：仅发现，不输出整个环境或配置
Get-Command node, npm, acpx -ErrorAction SilentlyContinue |
  Select-Object Name, Source
node --version
npm --version
acpx --version
```

完成标准：能区分协调主机、发起端和执行端；知道解释器／程序的实际入口、版本与 cwd。协调主机必须能导入 `fcntl`，Windows Python 不承担协调核心。当前 macOS 已有端到端证据，其他 POSIX 环境另行验证。

需要跨设备时读取当前用户已授权 SSH 别名的必要字段，并用有界、非交互连接核验目标。不要上传 SSH 配置或输出私钥／完整认证配置。非交互 PATH 与交互终端可能不同，应定位并显式使用实际入口。

## 2. 准备源码与执行依赖

在用户授权的位置取得源码。先检查目的目录，已存在则识别其来源和改动，不直接重新克隆或覆盖：

```sh
git clone https://github.com/Loveacup/call-cli.git /absolute/approved/call-cli
python3 /absolute/approved/call-cli/collab.py --help
```

记录公开仓库实际可见的 commit。公开版本采用独立的干净历史，沿用 v0.2.3 运行时基线；旧私有历史的提交标识不属于此仓库，不用于安装定位。安装更新版本时重新核对文档与兼容性，不把历史版本当永远最新。

执行设备需有全局 npm 安装、可被 `npm root -g` 找到且导出 `acpx/runtime` 的 ACPX。v0.2.3 实测 ACPX 0.20.0，其要求 Node.js >=22.13.0。只在需要安装且获授权时执行：

```sh
npm install -g acpx@0.20.0
acpx --version
npm root -g
```

不要对已存在的 fork、链接安装或更新版本盲目覆盖。源码和物理插件副本都必须把 `shared-prompt.cjs` 与 `runner.py` 放在同一目录；远端执行 facade 通过 SSH stdin 传入，无需另设常驻服务。

目标 CLI 需已安装，并通过自身原生流程完成登录。认证缺口由用户在原生登录流程处理，不索取密码／token，不复制另一设备凭据。技能安装不等于 CLI、适配器或认证已经就绪。

## 3. 连接执行目标

### SSH 是设备配置，不是 Call CLI 自建协议

优先使用现有别名。确实需要新增时，先确认用户授权具体设备与 SSH 配置范围，再按当前 SSH 配置合并；保留已有条目。以下仅是结构示例，不是可直接使用的地址：

```sshconfig
Host worker-laptop
    HostName worker.example
    User your-user
    IdentitiesOnly yes
```

使用 `ssh -G worker-laptop` 解析目标，不输出不相关条目；连通性检查可用 `ssh -o BatchMode=yes -o ConnectTimeout=8 worker-laptop ...`，检查目标上的程序入口和 cwd。不要以新建常驻服务、关闭主机密钥检查或扩大权限绕过连接问题。

### ACPX agent 适配在执行设备配置

Call CLI 使用 ACPX 的原生 agent 解析。OMP 可通过自定义 agent command 接入；先用当前安装的 `omp --help` 确认 ACP 入口。支持 `omp acp` 的版本，其配置片段为：

```json
{
  "agents": {
    "omp": {"command": "omp acp"}
  }
}
```

这只是合并片段。核对该版本 ACPX 的全局 `~/.acpx/config.json` 与 cwd 内 `.acpxrc.json` 规则，合并所需字段，不把示例整份覆盖到现有配置。Codex／Pi 使用实际安装的 ACP 适配，发现缺口先确认适配器入口，不把普通交互 CLI 或 Codex `app-server` 直接当作 ACP 程序。

新增设备别名不等于新增 CLI 后端；当前目标参数只接受 OMP、Codex、Pi。Windows Codex 在 SSH 服务上下文可能失败，其独立桌面入口见 [CODEX_SSH.md](../CODEX_SSH.md)，没有自动切换路由承诺。

## 4. 选择入口与模块

只需终端／脚本时，直接使用源码入口，无需修改 shell 配置、安装技能或启用 Hermes 模块。

CLI 内让 AI 调用入口时，按[集成手册](integrations.md)安装技能并验证实际加载。Hermes 原生来源绑定／后台通知确有需要时，再安装其可选插件；需要 `intervene` 时应选择通用 CLI，因为原生 `call_cli` 工具没有该动作。

远端发起端可使用 Node 客户端：

```sh
node /absolute/client/remote-client.cjs \
  --host coordinator --entry /absolute/coordinator/collab.py \
  --db /absolute/private/tasks.sqlite3 -- connect --runtime omp
```

Windows 受限 Codex 发起端优先使用原生 PowerShell/.NET 客户端，而不是先触发已知 Node spawn SSH `EPERM` 后重试业务：

```powershell
& 'C:\absolute\client\remote-client.ps1' `
  --host coordinator --entry /absolute/coordinator/collab.py `
  --db /absolute/private/tasks.sqlite3 -- connect --runtime codex
```

PowerShell 客户端当前使用 macOS `/usr/bin/base64 -D`，仅核验 macOS 协调端。`--host` 是发起设备到协调主机的别名；后续 `start --device` 是协调主机到执行设备的别名，两者不要混淆。

## 5. 配置权限与数据位置

- CLI 用 `--db` 指定明确任务库；缺省为用户主目录下 `.hermes/plugin-data/call-cli/tasks.sqlite3`，这只是路径约定，并不要求安装 Hermes。
- DB、client handle、任务正文、回答、SSH 信息和运行记录放在用户私有位置，不放进公开仓库。
- 新建任务前说明首轮的 npm 查询及可能的全局 ACPX patch 更新，确认这类副作用在用户授权内。不要从项目维护者的个人环境推导当前用户授权。
- 同一协作复用同一 DB 与 client handle。Hermes 插件的 DB 由宿主代码定位，没有独立的 DB 配置键；通用入口要复用其任务库时，先核对实际路径。
- 源码不会替你把文件同步到执行设备；cwd 和产物都属于目标设备。
- 默认只读；`--write` 使用 `approve-all`，不是路径沙箱。把获授权路径与完成标准写进任务，并保留目标 CLI 原有沙箱。
- 不因创建另一 DB 就认定旧 writer 已停；先按原任务对账，不启动竞争写者。

### 认证

执行端的 `~/.acpx/config.json` 与 cwd `.acpxrc.json` 在该设备本地解析，项目值优先。Shared Runtime 只在执行端接收所需 `authCredentials`／`authPolicy`。v0.2.3 支持 `authPolicy` 为 `skip` 或 `fail`；缺省 `skip` 不表示目标 CLI 无需自身登录。

仅在真实需要时配置 ACPX 的 `auth` method 值；它是字符串字面量，不是本项目实现的环境变量插值模板。不要把用户凭据写入示例、任务正文、配置回报或 Git。先核验项目级配置的可信来源：它可影响实际执行命令与认证行为。

### ACPX 版本检查与更新

首轮会查询执行端安装版本与 npm 稳定版；来源可识别为官方 npm 安装、无占用且版本主次号相同时，可能执行兼容 patch 更新。其他版本变化记录待核对，不升级 OMP／Codex／Pi。开始升级后验证失败会阻止业务启动。

这是现有代码行为，不是后台更新服务。当前没有 `--no-update` 或统一配置开关。用户要求严格冻结环境时，应说明此缺口，不承诺只靠填写配置即可禁用。续轮在共享安装锁内读当前安装版本，首轮 `version_check` 仅保留为历史。

## 6. 验证并完成交付

先做不执行模型业务的检查，再做获授权的最小只读真实任务：

1. 运行实际入口 `--help`；确认 facade、核心文件和配置路径正确。
2. `connect` 后保存真实 `client_id`，同入口执行 `list`，确认使用同一 DB。
3. 如安装技能，实际加载该技能；如安装插件，使用真实宿主工具验证，不用普通 CLI 冒充原生来源绑定。
4. 新建一个隔离 cwd 的只读任务，确认公开输出、退出码、终止原因及真实结果；多轮需求验证提问和同会话续轮。
5. 按任务实际状态取消／关闭并读回；结果不明保留 `unknown`，不自动重放。
6. 报告：安装位置与 commit、设备及入口、已验证能力、未验证／阻塞、会话收尾。不要仅凭版本输出或安装成功声称全部可用。

## 用户可交给 AI 的入口提示词

> 请读取 Call CLI 仓库的 `docs/configuration.md`，先发现现有环境，再为当前 CLI 配置跨 CLI 协作。复用现有 SSH 和目标 CLI 登录；只安装我需要的入口／技能／模块，不覆盖已有文件，不修改无关配置。将终端调用与 CLI 内加载都验证到真实结果，关闭自有测试会话，并告诉我具体可用范围与剩余缺口。新增权限或登录确实需要我操作时再提出明确请求。
