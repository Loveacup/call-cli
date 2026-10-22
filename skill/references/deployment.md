# 发布与三端部署

- 先核对 Git 现态、远端归属和仓库可见性。新建个人仓库默认私有；只提交明确的源码／测试／文档，排除任务库、完整日志、原始会话证据与凭据。推送后用 GitHub API 或 ls-remote 核对远端分支 SHA，不能只看本地 origin 缓存。
- 仓库 `skill/` 是公开分发模板；本机运行技能可以有用户私有设备清单。发布时从仓库模板构建，禁止将本机运行技能整树回拷进仓库而重新引入设备、路径或个人授权；安装更新时同样保留用户的私有库存。
- 协调核心放在 POSIX 主机；其他端可仅部署 Node SSH 客户端，不为了 Windows 安装 fcntl 版核心。用提交 SHA 命名只读发布目录，再由 current 指向该版本，保留旧插件快照。覆盖前检查现有文件归属、哈希和活跃任务，不强制覆盖未知改动。
- 客户端及技能入口指向同一已发布 payload；逐文件读回哈希，并从三端真实入口执行短应答及 close/no-session。安装成功、--help 或技能文件存在均不等于业务可执行。
- OMP 不支持 get_commands 时，不能把该 RPC 的失败当成技能缺失。新会话经原生 read 读取 skill://call-cli，检查 tool_execution_end.isError=false 及实际返回的技能正文。Pi 可用 get_commands，Codex 可用 skills/list；区分原生目录发现与模型真实执行。
- Windows PowerShell 包装器先把完整参数数组转 JSON→UTF-8 Base64，再交给 Node 解码，避免原生参数传递丢失引号。大包通过 SCP 或 node - 的 stdin 传输，避免叠加 EncodedCommand 超出命令行上限。Git Bash 入口仅在该进程关闭路径转换，保留执行端 POSIX cwd。
- 不为刷新技能重启 gateway 或强改当前工具快照；现有对话与新会话可见工具分别说明。保留既有模型、认证与宿主设置。
- 既有 CLI 损坏与协作包部署分开报告：例如 pnpm shim 指向缺失 codex.js 时，不因安装了技能就标健康，也不擅自重装整套 CLI。部署回执保留设备、角色、SHA、入口、哈希、实测范围和未决项，不另建长期库存。
