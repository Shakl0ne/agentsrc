---
title: Codex 沙箱引擎：如何把 AI 关进操作系统的笼子？
---

# Codex 沙箱引擎：如何把 AI 关进操作系统的笼子？

想象你让 Agent “清理一下当前项目的临时文件”，但它因为幻觉或 Prompt 注入，生成了 `rm -rf ~/.ssh`。如果你用的是普通的 Node.js 代理，这行命令会直接在你的宿主机上以当前用户的权限执行。

当 Agent 具备了执行任意 Shell 命令的能力时，靠 System Prompt 里的软约束是防不住的。

Codex 的解法是：通过执行审批拦截高危命令，并通过跨平台的 OS 底层沙箱（如 Bubblewrap、Seatbelt）限制进程的文件与网络权限。本文将剥开 Codex 的外衣，深入探究这套安全架构在代码层面的真容，揭示沙箱策略落地的全过程：

1. 当模型尝试执行一个命令时，底层的真实执行链路是怎样的？
2. `ExecPolicy` 引擎是如何通过规则树进行命令审批的？
3. Codex 是如何利用跨平台的 OS 机制实现进程级沙箱的？
4. 进程加固（Process Hardening）的作用边界在哪里？

本文只讨论底层的安全隔离与执行策略。关于多 Agent 之间如何隔离上下文，将留到下一篇《多 Agent 编排》中拆解。

## 一、一条真实的执行链：命令是如何被拦截的？

在 Codex 中，当大模型生成了一个 `shell_command` 或 `exec_command` 时，它并不是直接交给操作系统的 `exec`，而是会经历一条严密的管线：

![命令执行的安全拦截管线](/images/codex/06-execution-pipeline.svg)

1. **策略解析**：工具调用被交给 `ExecPolicyManager`。它会根据预设的规则树评估该命令，生成 `ExecApprovalRequirement`（允许、禁止、或需要审批）。
2. **执行审批**：如果需要审批，核心引擎会向前端发出 `ExecApprovalRequest`，挂起任务等待用户决策（用户通过发送 `Op::ExecApproval` 消息回复）。
3. **沙箱封装**：进入执行阶段前，请求会被交给 `SandboxManager::transform`。它会根据当前的 `PermissionProfile`（文件读写边界）和网络策略，将普通的命令转换为沙箱化的执行请求。
4. **底层执行**：最后，带有沙箱包装的命令交由底层的 `exec server` 启动子进程。

在这条链路上，文件系统权限、网络权限、命令审批和进程自保护被严格划分在不同的模块中。

## 二、核心抽象 A：ExecPolicy 命令审批

沙箱只能控制“能不能读写某个文件”，但控制不了“能不能执行某个命令”（比如 `git push`）。这就需要 `ExecPolicy`。

在 `codex-rs/execpolicy/src/` 中，系统实现了一个策略解析引擎。它会读取用户的策略配置（如“允许 `git *`，拒绝 `npm publish`，其他需审批”）。当引擎评估一条命令时，会返回三种判定结果（位于 `decision.rs`）：

- `Allow`：静默放行。
- `Forbidden`：绝对禁止。
- `Prompt`：拦截并要求人工审批。

进入核心流程后，这些决策会被转换为 `ExecApprovalRequirement::{Skip, Forbidden, NeedsApproval}`。这种设计让系统在静默自动化和高危人工兜底之间找到了平衡。

## 三、核心抽象 B：跨平台 OS 级沙箱

当命令获准执行后，`SandboxManager` 开始介入。它将抽象的安全需求转化为操作系统的底层系统调用。

![跨平台 OS 级沙箱机制](/images/codex/06-cross-platform-sandbox.svg)

### 3.1 Linux：Bubblewrap 与 Seccomp

在 Linux 平台上（位于 `codex-rs/linux-sandbox/`），目前的默认实现路径是：
- **文件系统隔离**：使用 `bubblewrap`（一个轻量级的非特权沙箱工具）来挂载受限的文件系统视图，限制读写范围。
- **网络与进程限制**：辅以 `seccomp`（Secure Computing Mode）限制危险的系统调用。
- （注：早期的 Landlock 实现作为 legacy/fallback 路径保留，不再是主力防线。）

### 3.2 macOS：Seatbelt 策略

在 macOS 平台上（位于 `codex-rs/sandboxing/src/seatbelt.rs`），系统利用了底层的 App Sandbox（又名 Seatbelt）机制。

Codex 会根据 `FileSystemSandboxPolicy` 和 `NetworkSandboxPolicy`，动态生成 `.sbpl` 格式的策略配置文件，然后强制命令通过 `/usr/bin/sandbox-exec -p <policy> <cmd>` 的形式启动。该策略不仅控制文件读写，还约束了 Unix Domain Socket、网络代理等高级权限。

（Windows 平台也有对应的 `WindowsRestrictedToken` 实现，本文从略。）

只要在受限的 Sandbox 生效状态下，即使大模型生成了 `rm -rf /`，操作系统内核也会在底层拒绝访问。

## 四、核心抽象 C：进程自保护 (Process Hardening)

除了限制 Agent 执行的子进程，Codex 还会对自己（也就是代理主进程）进行保护。

在 `process-hardening/src/lib.rs` 中，定义了 Pre-main 的加固逻辑。它并不是每次执行命令时清洗环境变量（环境变量的处理发生在执行请求构造和沙箱阶段），而是在主程序刚启动时进行的全局自保：
- 禁用 Core Dump。
- 禁止 `ptrace` attach（防止被其他进程调试并窃取内存中的 Token）。
- 移除危险的链接器变量（如 `LD_PRELOAD` 或 `DYLD_INSERT_LIBRARIES`），防止动态库劫持。

这是典型的系统编程防御思路，用于保护 Codex 自身的运行环境纯净。

## 五、关键决策：安全机制的工程实现优势

把 Codex 的安全机制和基于 Node.js/TypeScript 构建的 Agent 对比，能看出实现路径上的差异。

相较于主要依赖权限确认（Prompt User）或纯逻辑拦截的工具形态，Codex 在开源实现里把 OS Sandbox 做成了执行链的一部分。

调用操作系统的沙箱机制（如管理 `bubblewrap` 或 `sandbox-exec` 的参数）、控制细粒度的子进程执行，并进行系统级的进程自保护，这些实现更贴近 Rust 的系统编程优势。这也是 Codex 在底层机制上显著区别于轻量级聊天代理的地方。

## 六、总结

Agent 的能力越强，其执行高危操作的风险就越大。

Codex 通过 ExecPolicy 进行命令审批拦截，通过跨平台的 OS 机制实现文件和网络隔离，最后用 Process Hardening 保护自身内存安全。这套严密的安全管线，让它可以更安全地在后台静默执行复杂的代码任务。

现在，我们的 Agent 已经有了大脑（主循环）、记忆（上下文）、手脚（工具）和铠甲（沙箱与策略）。但如果面对一个需要修改 50 个文件的史诗级重构任务，单兵作战还是会力不从心。下一篇，我们将看看 Codex 是如何通过 Agent Tree 组建“AI 团队”的。

## 七、章节小测

<script setup>
const q = [
  {
    question: '在 Codex 中，当大模型生成了一条 shell_command 后，其真实的执行链路顺序是怎样的？',
    options: [
      '底层执行 → Sandbox 封装 → 策略解析 → 执行审批',
      '策略解析 → Sandbox 封装 → 底层执行 → 执行审批',
      '策略解析 → 执行审批 → Sandbox 封装 → 底层执行',
      'Sandbox 封装 → 执行审批 → 策略解析 → 底层执行'
    ],
    correct: 2,
    explanation: '真实的执行链路是：ExecPolicyManager 先解析策略，若需审批则发出请求（挂起等待用户）；通过后交由 SandboxManager 进行沙箱参数封装，最后启动子进程执行。'
  },
  {
    question: '在 Linux 平台上，Codex 目前实现文件系统隔离的主力防线机制是什么？',
    options: [
      '使用 Docker 容器虚拟化技术，将每个工具执行放在独立的隔离容器中',
      '使用 Linux 内核的 Landlock 特性，严格限制进程级的文件访问权限',
      '使用 bubblewrap 挂载受限的文件系统视图，并辅以 seccomp 限制系统调用',
      '使用 SELinux 或 AppArmor 安全模块，通过强制访问控制（MAC）进行拦截'
    ],
    correct: 2,
    explanation: '根据源码，Linux 环境下的沙箱默认使用 codex-linux-sandbox helper，主要依靠 bubblewrap 实现文件系统隔离，配合 seccomp 做系统调用限制，而 Landlock 是作为 fallback 保留的。'
  },
  {
    question: 'ExecPolicy（指令策略引擎）在评估一条待执行的命令时，其判定结果包含哪三种状态？',
    options: [
      'Success（成功）、Failure（失败）、Pending（等待）',
      'Allow（允许）、Forbidden（禁止）、Prompt（要求人工审批）',
      'Execute（执行）、Skip（跳过）、Retry（重试）',
      'Safe（安全）、Warning（警告）、Danger（危险）'
    ],
    correct: 1,
    explanation: '在 execpolicy/src/decision.rs 中，策略引擎对命令的判定结果为 Allow、Forbidden 或 Prompt，进入 core 流程后转换为相应的 ExecApprovalRequirement。'
  },
  {
    question: 'Codex 的 Process Hardening（进程加固）模块的主要职责是什么？',
    options: [
      '在每次执行子进程前清洗 PATH 等环境变量，并重置工作目录（CWD）以防止逃逸',
      '在主程序启动时进行全局自保，如禁用 Core Dump、禁止 ptrace attach，防止被调试或动态库劫持',
      '动态生成 macOS 的 .sbpl 策略文件，拦截所有的网络访问请求',
      '对模型生成的代码进行静态安全扫描，拦截潜在的 SQL 注入或恶意载荷'
    ],
    correct: 1,
    explanation: 'process-hardening 主要在 Pre-main 阶段运行，用于保护代理主进程自身的内存与执行环境纯净。执行时的环境变量清洗和 CWD 重置则属于执行请求构造和沙箱的职责。'
  },
  {
    question: '从沙箱与安全机制的实现角度来看，采用 Rust 作为核心语言的工程优势之一是什么？',
    options: [
      'Rust 的包管理器 Cargo 提供了比 npm 更严格的自动代码审计，防止了供应链攻击',
      'Rust 的异步生成器（Async Generator）天然支持多 Agent 环境下的隔离并发执行',
      '相比于 Node.js，Rust 更便于调用内核级沙箱 API 并精细控制子进程与系统资源',
      'Rust 的编译速度远超 TypeScript，极大缩短了本地 Agent 系统的安装时间'
    ],
    correct: 2,
    explanation: '要实现深度的进程级沙箱隔离（如配置 bubblewrap 或 sandbox-exec 参数）以及系统级的自保护（禁用 ptrace 等），贴近操作系统的系统编程能力是 Rust 相比 TS 等语言的显著工程优势。'
  }
]
</script>

<Quiz :questions="q"></Quiz>