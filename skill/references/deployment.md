# 发布与三端部署

- 先核对 Git 现态、远端归属和仓库可见性。新建个人仓库默认私有；只提交明确的源码／测试／文档，排除任务库、完整日志、原始会话证据与凭据。发布／推送须有针对性授权；已授权同仓库／分支或文档／记录目标的普通返工沿原权限继续，新目标、可见性、强制覆盖或部署等重要边界变化仍需相应授权。推送后用 GitHub API 或 ls-remote 核对远端分支 SHA，外部记录写后读回正文，不能只看本地缓存。
- 仓库 `skill/` 是公开分发模板；本机运行技能可以有用户私有设备清单。发布时从仓库模板构建，禁止将本机运行技能整树回拷进仓库而重新引入设备、路径或个人授权；安装更新时同样保留用户的私有库存。
- 协调核心放在 POSIX 主机；其他端可仅部署现有 SSH 客户端，不为 Windows 安装 fcntl 核心。已提交发布以真实 commit SHA 定位；获准的未提交候选以内容哈希定位并标明候选。运行时依赖与技能分别列清单，保留旧快照；切入口前核活跃任务，不能在运行中混用模块。
- 源 skill、个人安装与消费者实际加载分别验收。以最新逐文件哈希生成真实 manifest，不沿用旧 revision；read-only release 内的 skill 也不能就地改写。需要 skill-only 同步时先提出目标、个人适配保留、manifest／链接影响与回退方案，未获准不重指 current、软链或其他 profile。内容优化完成不等于全部安装同步。
- OMP 不支持 get_commands 时，不能把该 RPC 的失败当成技能缺失。新会话经原生 read 读取 skill://call-cli，检查 tool_execution_end.isError=false 及实际返回的技能正文。Pi 可用 get_commands，Codex 可用 skills/list；区分原生目录发现与模型真实执行。
- Windows PowerShell 包装器先把完整参数数组转 JSON→UTF-8 Base64，再交给 Node 解码，避免原生参数传递丢失引号。大包通过 SCP 或 node - 的 stdin 传输，避免叠加 EncodedCommand 超出命令行上限。Git Bash 入口仅在该进程关闭路径转换，保留执行端 POSIX cwd。
- 不为刷新技能重启 gateway 或强改当前工具快照；现有对话与新会话可见工具分别说明。保留既有模型、认证与宿主设置。
- 既有 CLI 损坏与协作包部署分开报告：例如 pnpm shim 指向缺失 codex.js 时，不因安装了技能就标健康，也不擅自重装整套 CLI。部署回执保留设备、角色、SHA、入口、哈希、实测范围和未决项，不另建长期库存。

Hermes 多工具链宿主的 native 安装适配必须随代码交付保留 `plugins.entries.call-cli.settings.local_toolchain_bin`：由部署者将同目录 node/npm/acpx 的已核验绝对 bin 路径传入现有 plugin settings。local start/续轮和 native close/reconcile/release_only 共用，不能仅保留旧 release.py 本机硬编码，也不能只要求用户改全局 PATH。配置传递模板见[集成手册](../../docs/integrations.md)。空值和其他设备原默认不变，远端不覆盖；独立 dashboard release 无 ctx，不借此宣称同配置覆盖。部署前后均做 bundled-first、零 runtime/模型的 SDK-only 加载并比较 Node/npm root/version；真实任务控制另需授权。当前源码修复不代执行生产设置或安装。

切换先后：新代码/schema 准备就绪 → 写入并读回 local_toolchain_bin → 宿主支持的插件激活/加载 → 实际 ctx 设置与 bundled-first SDK-only 验证。生效前保留当前有效链，不先激活空配置候选；若宿主不接受预先写入新键，停止处理部署门禁，不自动重启或换来源。新会话不是旧任务所有权迁移，宿主实际刷新语义仍需部署时核验。

## 最小 skill-only 同步候选

不更换运行时：在获准的独立技能位置准备完整 root/references/templates，合并所需个人适配并保留私有设备库存；核链接、文件清单和语义，然后只切消费者的 skill 绑定并用其原生加载器读回。若现有 skill 与 runtime 共用 release/current，优先提出解耦该绑定的精确候选，不默许移动运行时 current。新目标、绑定或登记变更仍属独立安装／暴露授权；本文件只给方案，不执行部署。

## OMP 实验性宿主适配

只在获准的新建自有 RPC 实例单次加载：`omp --mode rpc --no-ui --no-extensions -e /absolute/call-cli/omp-extension.mjs`。使用完整的固定 commit 目录；`omp-extension.mjs` 相邻必须有 `omp_bridge.py`，以及[集成手册依赖闭包](https://github.com/Loveacup/call-cli/blob/main/docs/integrations.md#运行时依赖闭包)的同版文件。公共 skill 的 root/references/templates 独立打包，个人库存和 Hermes 专属说明按各安装原文合并，不能从公开模板覆盖。

实测 API 为 OMP 18.18.1。原生 ctx 生成 session/agent 身份；每个 binding 的受管 1 秒 timer 对活跃 task 做 one-shot Python wait0，批次后再计时，通过 followUp 入队，不是 stdout 推送或 steer。首次并发 bind 共用一次请求，各 task 跟踪和错误隔离；业务状态、续轮及 writer 互斥复用 core。退出／切 session 只停止投递，不取消任务；普通 TUI、ACP defer、重启恢复及真实最终业务 E2E 均未验。加载后先验证工具注册和不调用模型的 list，真实业务与原会话投递另验；RPC prompt ACK 不是 extension close 完成，须等待 handler 回执及原生 no-session。

候选 manifest 逐文件记录 commit、SHA-256、相邻资源和 skill 链接校验；以实际 import／文件读取依赖生成，不能从旧文件数量加减。发布准备不切 current、物理插件或个人 skill 绑定；父方核验本 worker 已结算关闭及其他相关活动任务后，再按备份、精确文件清单、原子切换和哈希读回步骤接管。旧 release 与失败证据保留，磁盘切换不等于当前宿主已加载，不为刷新而 restart/reload。
