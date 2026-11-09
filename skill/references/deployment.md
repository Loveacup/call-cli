# 发布与三端部署

- 先核对 Git 现态、远端归属和仓库可见性。新建个人仓库默认私有；只提交明确的源码／测试／文档，排除任务库、完整日志、原始会话证据与凭据。推送后用 GitHub API 或 ls-remote 核对远端分支 SHA，不能只看本地 origin 缓存。
- 仓库 `skill/` 是公开分发模板；本机运行技能可以有用户私有设备清单。发布时从仓库模板构建，禁止将本机运行技能整树回拷进仓库而重新引入设备、路径或个人授权；安装更新时同样保留用户的私有库存。
- 协调核心放在 POSIX 主机；其他端可仅部署现有 SSH 客户端，不为 Windows 安装 fcntl 核心。已提交发布以真实 commit SHA 定位；获准的未提交候选以内容哈希定位并标明候选。运行时依赖与技能分别列清单，保留旧快照；切入口前核活跃任务，不能在运行中混用模块。
- 源 skill、个人安装与消费者实际加载分别验收。以最新逐文件哈希生成真实 manifest，不沿用旧 revision；read-only release 内的 skill 也不能就地改写。需要 skill-only 同步时先提出目标、个人适配保留、manifest／链接影响与回退方案，未获准不重指 current、软链或其他 profile。内容优化完成不等于全部安装同步。
- OMP 不支持 get_commands 时，不能把该 RPC 的失败当成技能缺失。新会话经原生 read 读取 skill://call-cli，检查 tool_execution_end.isError=false 及实际返回的技能正文。Pi 可用 get_commands，Codex 可用 skills/list；区分原生目录发现与模型真实执行。
- Windows PowerShell 包装器先把完整参数数组转 JSON→UTF-8 Base64，再交给 Node 解码，避免原生参数传递丢失引号。大包通过 SCP 或 node - 的 stdin 传输，避免叠加 EncodedCommand 超出命令行上限。Git Bash 入口仅在该进程关闭路径转换，保留执行端 POSIX cwd。
- 不为刷新技能重启 gateway 或强改当前工具快照；现有对话与新会话可见工具分别说明。保留既有模型、认证与宿主设置。
- 既有 CLI 损坏与协作包部署分开报告：例如 pnpm shim 指向缺失 codex.js 时，不因安装了技能就标健康，也不擅自重装整套 CLI。部署回执保留设备、角色、SHA、入口、哈希、实测范围和未决项，不另建长期库存。

## 最小 skill-only 同步候选

不更换运行时：在获准的独立技能位置准备完整 root/references/templates，合并所需个人适配并保留私有设备库存；核链接、文件清单和语义，然后只切消费者的 skill 绑定并用其原生加载器读回。若现有 skill 与 runtime 共用 release/current，优先提出解耦该绑定的精确候选，不默许移动运行时 current。新目标、绑定或登记变更仍属独立安装／暴露授权；本文件只给方案，不执行部署。
