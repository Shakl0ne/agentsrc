---
title: Capability 缝体系：为什么换一个实现能牵一发动全身
---

# Capability 缝体系：为什么换一个实现能牵一发动全身

> 本文基于 `dsh-v0.1.0-rc.7`。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前五篇把 `dsh` 从"插件森林"一步步走到"工具管线"。第六篇触及它真正区别于终端 agent 的核心抽象：**capability 缝（capability seam）**。

先想一个硬问题。终端 agent 的代码里，"执行 bash""读写文件""起一个子进程"这些事**通常是写死在各自的大类里**：想给 bash 加隔离？改 bash 那个类。想搬到远程沙箱？改文件工具、改 shell 工具、改 LSP……到处都要动。`dsh` 不这么做。

它的答案是：把每个"能力"拆成**可插换的三件套**——**Service Definition（接口声明）+ Service Provider（实现）+ Consumer（消费方，通常是模型面上工具）**，三者通过 Cordis 的一个 `ctx.<key>` 连接起来。换一个实现，就只是**换一个 Provider**，三件套里其余两件毫发无损。

先放最硬的一句：

> **capability 缝 = (Service Definition / Service Provider / Consumer) 三件套。** 只做其中一个角色不构成"缝"；一个能力被正确地做出来，意味着这三个角色都能独立演化、并被可逆地组装回一个可运行的 `ctx.<key>`。

`docs/capability-seams.md`（一份机器生成的图谱）把整个产品所有的 `ctx.*` 服务，按三种角色分类列出来——`core`（不可换的核心脊柱）、`seam`（可换的能力缝）、`bundle`（组合点）。下面我们一开始用 `seam` 展开。


## 一、为什么需要三件套：把"接口 / 实现 / 用法"拆开

回顾第 2 篇，Cordis 的 Context 是"服务仓库 + 键解耦"。capability seam 就是**这套模式最常见的产品化应用**。三件套各自的职责：

| 角色 | 职责 | 例子（bash） |
|------|------|------|
| **Service Definition** | 定义一个能力的**抽象接口**：能做什么、返回什么类型、暴露哪些事件。它**依赖不了实现**。 | `@deepseek-ai/dsh-shell`：`ctx.shell` 的接口与词汇（`run`/`start`/`sandboxMode`） |
| **Service Provider** | 那个能力的**具体实现**。可以有多种；但同一时刻同层只有一个 provider 供组合。 | `@deepseek-ai/dsh-bash-local`（本地子进程）/ `dsh-bash-sandbox`（套 `ctx.sandbox` 的隔离执行器） |
| **Consumer** | **使用这个能力**的一方，通常是 model-facing 的一个工具。 | `@deepseek-ai/dsh-tool-bash`：把 bash 能力暴露给模型 |

看 `@deepseek-ai/dsh-shell` 的 README，它开头就把这个拆法摆在眼前：

> This package owns the **Service Definition** role of the bash capability, split so each role can evolve (and be swapped) independently.

——"拆分，正是为了让每个角色可以**独立演化、独立换掉**"。这回答了一个关键问题：为什么接口要单独一个包？因为**如果接口跟实现绑一起，你就无法在不改工具的前提下换掉实现**；接口独立成包，Consumer（工具）永远只对着接口编程，Provider 换谁都不影响它。

于是我们得到 capability 缝的核心本事：**一个能力 = 一个稳定的接口，被若干 Provider 实现、被 Consumer（工具）只通过接口访问；工具不 import 任何实现。** 这是它和"硬编码进 loop"最本质的区别。


## 二、"没有特权核心"与 capability 缝：一体的原因

前几篇反复说 `dsh` 没有特权核心。capability 缝正是那个"没有特权核心"**在能力层的落地**——因为一切能力都可换，就根本没有"哪个能力是产品必须内置"的说法。

想象一下，如果工具（Consumer）import 了 `dsh-bash-local` 这个 Provider 类，那么"本地 bash"就焊死在工具上了，你要换成远程执行器就得改工具。capability 缝避免了这一点：**Consumer 只在构造期通过注入拿到 `ctx.shell` 服务，运行时只调 `ctx.shell.run(...)`。** 因为注入的是接口（Service Definition），而不是实现，所以 Provider 换一次，Consumer 和整个产品都不动。

这里补一个"为什么三件套，而不是两件（接口 + 实现）"的探根：**因为"消费"这个角色也需要独立演化**。比如 `tool-bash` 作为 Consumer，它会读 Provider 暴露的 `sandboxMode` 这个"能力事实"来**主动给模型加上"要沙箱里的隔离"的说明**。可见 Consumer 不只是"调接口"，它还根据实现的能力做 UI/提示/安全上的适配——三件套里，三环都在各自的演化。


## 三、逐个 seam 实例：把抽象落到四个真实缝

### （1）bash 能力缝：`ctx.shell`

`@deepseek-ai/dsh-shell` 定义了 `ShellExecutor` 接口：它说"**WHAT** a bash backend does（跑前台命令、起后台进程）——而不说 **HOW**"。Provider 有多个：`dsh-bash-local`（本地子进程）、`dsh-bash-sandbox`（本地机制、每个 spawn 都由 `ctx.sandbox` 约束）；Consumer 是 `dsh-tool-bash`（把 `ctx.shell` 暴露给模型的工具）。

最妙的是 `sandboxMode` 这个**能力事实**：`dsh-shell` 的定义里 base class 的 `sandboxMode` 是 `undefined`（"我不沙箱"），而 `dsh-bash-sandbox` 这个 Provider 会把它填成默认模式。Consumer `dsh-tool-bash` 在注册时**读取这个能力事实**，只有 Composition 里有沙箱 provider 时，才把 "要求沙箱" 的字段加进模型工具 schema：

```ts
// 来自 @deepseek-ai/dsh-tool-bash（简化）
// Provider 的 sandboxMode 定义了工具的加字段时机
if (shell.sandboxMode !== undefined) {
  // 添加引导字段，让模型能要求 sandbox
}
```

这印证了："Consumer 可以因 Provider 的能力而**自我适配**"——换一个沙箱型的 shell 执行器，Consumer 会自动向模型多暴露一个"请给我沙箱"的开关，不需要 Consumer 知道 `bwrap` 还是 Landlock。

### （2）文件系统缝：`ctx.fs` 的四层栈

`@deepseek-ai/dsh-fs` 更是把"缝"用到极致——它把文件栈拆成**四层**（不只是一层三件套）：

| 层 | 包 | 角色 |
|---|---|---|
| tool / executor | `dsh-tool-fs` | 模型可见的 read/write/edit + 渲染；经 `ctx.fs` 读/写/编辑，派发 `fs/*` 事件 |
| policy | `dsh-fs-observation-policy` | 观察态 + 读后编辑 + 版本守卫（经 `fs/*` 事件，无服务） |
| provider contract | `dsh-fs`（本节） | `ctx.fs`：执行世界路径、文本 IO、原子性变更原语 |
| provider | `dsh-fs-local` | 宿主文件系统的实现 |

四层里，"接口（provider contract）"与"provider"分离，而它上面还有一层 policy 把"观察 + 版本守卫"这类持久策略挂到 `fs/*` 事件上，而不必写进 provider。`fs-sandbox`、`fs-e2b` 都实现了同一个 `ctx.fs` 接口，**无需动 policy/tool 两层**——替换 provider，观测策略和模型工具都跟着新的文件系统世界走。

`ctx.fs` 的接口尤其能看出"执行世界"意识：`resolve` 返回稳定的 `FsTarget`，`processPath` 返回"该 provider 执行世界里可打开的规范路径"，`fileUrl` 返回规范 file: URI。这几项的存在是因为 **provider 可能不是宿主文件系统**——比如远程 fs-e2b 时，"文件路径"到底该怎么办，由 provider（谁打开这个世界谁就定义了世界的路径）负责。

### （3）安全缝：`ctx.sandbox`

`@deepseek-ai/dsh-sandbox` 是"进程隔离"的缝。它是 Process-sandbox Service Definition，只定义**沙箱词汇**与 `confinement` 契约：

- `SandboxMode`（`read-only` / `workspace-write` / `danger-full-access`，只限文件效果）
- `SandboxEnforcement`（`full` / `partial`，按内核 ABI）
- `SANDBOX_UNAVAILABLE`（fail-closed 错误）

provider `dsh-sandbox-local` 提供具体后端：Linux 上 `bwrap`、否则按平台走 Landlock 启动器；macOS 上 `sandbox-exec`/Seatbelt。Consumer 是 `dsh-bash-sandbox`（把自己的 spawn `['bash','-c',...]` 交给 `ctx.sandbox.confine` 包裹）。

沙箱缝有一个设计强调——**"same-world confinement"**。底层的沙箱（bwrap/Landlock/Seatbelt）与宿主共享文件系统与内核，所以它们只是修改"这个世界的路径"；而像容器、微 VM、远程执行器**不是**这个缝的 backend——它们**替换的是整组 Service Provider**（`ctx.shell`、`ctx.fs` 一起换）。这就引出下节最重要的"provider 互换"。


## 四、Provider 互换 = 整套移动：fs / subprocess 共享一个执行世界

这是 capability 缝里最有信息量的一层。为什么"换一个实现"能"牵一发动全身"？恰恰因为**多个缝在共享一个概念——执行世界**。

回顾 `docs/architecture.md` 的原话：

> Seams are why one provider swap changes the whole product. Filesystem and subprocess providers share **one execution world**, so pointing them at a remote sandbox moves Bash, PTY, and LSP with them, with no provider forks.

翻译过来：本地一套 `fs-local` + `subprocess-local` + 相应普通后端，是共享"宿主文件系统 + 宿主子进程"这一整套"本地执行世界"。若要换成远程沙箱，API 应该做的是：**把 `ctx.fs` 的 provider、`ctx.subprocess` 的 provider、以及依赖它们的 bash / PTY / LSP 一起换** ——而不需要为每一个消费者单独 `fork` 一份代码。

为什么 fs 和 subprocess 必须一起动？因为**在同一个执行世界里，"文件路径" 与"进程" 是同一个世界的两个面**：一个进程能"打开哪个路径"，取决于它所在世界的文件系统。若 LSP 子进程还在宿主世界、而 fs 已指到远程世界，那个 LSP 就没法"编辑远程的文件"。所以"执行世界"是一个**协调单元**——provider 换一组，事件边界整套移动。

这就是 `docs/capability-seams.md` 图谱里 `ctx.subprocess` 那行看到的现象：它的实现是 `subprocess-local`、`subprocess-e2b`，消费者里同时有 `bash-local`、`bash-sandbox`、`terminal-bash`、`lsp-stdio`、`subagent-acp`、`subagent-codex`、`subagent-claude-code`——**bash、PTY、LSP、各子 agent 后端全都通过 `ctx.subprocess` 这个单一缝去 spawn**。所以当你把 `subprocess` 的 provider 从 local 换成 e2b 远程，这些通通跟着一起迁过去，而不用各自写一遍适配。

三、四节放一起，`capability` 缝的本质就齐了：

> **一个缝很少孤立。** 一组缝往往共享一个"执行世界"，所以换的实现要`整套移动`，而不是逐个修补——这就是"牵一发动全身"的正面含义：**一换全换、干干净净，而不是到处改。**


## 五、各个角色为何要"分开演化"：从文档纪律到运行时正确

当角色不是一道写死，而是三种能力包，背后是一套**分工纪律**。看 `docs/AGENTS.md` 与 `packages/AGENTS.md` 反复的约束就能拼接出一张纪律表：

| 目标 | 落在 Define | 落在 Provider | 落在 Consumer |
|---|---|---|---|
| 接口稳定 | 定义类型、方法、词汇 | - | - |
| 能力按产品可变 | - | 换实现 / 换后端 | - |
| 与模型交互的 schema | - | - | 生成工具 schema、暴露 `sandboxMode` 能力 |

具体说：

1. **定义不能依赖 Provider**。以 `@deepseek-ai/dsh-sandbox` 为例，其 README 明确"it depends only on cordis (+ the harness error base), never on a backend"——接口包里绝不 import 任何实现，否则 consumer 就被绑死。
2. **Consumer 应把 schema/transport 相关都留在自己层**，不要"让一个 Consumer 决定 service"（见 `packages/AGENTS.md`：Design Service Definitions for all current Consumers，警告"两鼻：一个 public service method 只有一个 internal caller"）。
3. **一个 service 恰有一个 provider 激活**（win32 上 bash/pwsh 冲突，mount 两个就 fail loud）。这保证"换 provider"是完整的原子操作——不会半套本地、半套远程。

roles 分开的**最终回报**是 `capability-seams.md` 那句维护注：「hybrid: services are discovered from Cordis declarations; interface/implementation/consumer roles are classified … with a completeness guard」——**角色的完整性是能被机器检查的**，谁少做了一个角色（接口/实现/消费），组合时就会 fail loud，而不是静默地"只有个头没有身体"。


## 六、与三栏对比：终端 agent 的"工具/沙箱"是被硬编码

把 capability 缝与三栏比较（"同能不同构"口径）：

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness（`dsh`） |
|------|-----|--------------------------|
| 工具（shell/fs） | 作为产品内建，硬编码在代码里 | 是**缝**：Definition/Provider/Consumer 三件套 |
| 换沙箱 | 改代码、改循环 | 换一个 Provider（`bash-sandbox`） |
| 让 bash+文件+LSP 一起迁 | 要全局改一堆 | 只需换共享世界的那一组 provider |
| 接口可否脱离实现 | 混在一起 | 明确 Service Definition 独立成包，只依赖接口 |

结论很直白：终端编程 agent 把工具与沙箱**当成产品中心的"硬需求"**写进循环；`dsh` 把它们做成了**可插换的缝**，并且更进一步——共享同一个执行世界的多个缝（fs/subprocess）绑定成一组替换，让"目标环境变了"成了一次性的整套迁移，而不是满仓库到处打补丁。

**它把"加一个能力"从"改 loop"变成了"设计三件套"，又把"换一个环境"从"改整个 loop"变成了"换一组 provider"。** 这正是 `dsh` 作为"被装配的通用引擎"和终端 agent 作为"产品"的根本差别。


## 七、小结与下一步

capability 缝带走了三件事：

1. **缝 = 三件套**（接口/实现/消费）。完整的缝是三件套一起演化，缺一不算 seam。
2. **interface 独立成包**，Consumer 只依赖接口不依赖实现——这是"换 Provider 不容易坏"的根因。
3. **共享世界决定"换一组"**。fs/subprocess 共享一个执行世界，换 provider 时 bash/PTY/LSP 成套迁移，无 provider 分叉。

到这里，capability 缝把"换实现"这类需求变成"换 Provider"。但还剩一个更魔法的收尾话题，直接踩在第 6 篇的产物上：

> 工具（Consumer）能换，是因为 Provider 能换；那 **agent 本身能不能被 model 去"改自己"** 呢？——自改/钩子桥，正是把"可插缝"推到了"agent 工具化的极端"。

那就是全专栏的最后两篇：**07 压缩/上下文/子代理**（把压缩也做成可换的缝），以及 **08 自改 / hooks 桥 / 生态**。下一篇先把"长会话怎么活下来"讲清楚——compaction 缝、`agent.inject()` 与 subagent provider。


## 章节小测

<script setup>
const q = [
  {
    question: '要完整地做出一个"能力"，最少要设计哪三种可独立演化的角色？',
    options: ['接口、一个实现、一个开关', '接口、一个实现、一段日志', '实现、消费方、周边工具包', '服务定义、服务实现、服务消费者'],
    correct: 3,
    explanation: 'capability 缝 = Service Definition + Provider + Consumer（后者一般是 model-facing tool）。只做其中一个角色不构成 seam。'
  },
  {
    question: '为什么 Consumer 只依赖服务接口（Service Definition）而不 import 具体 Provider？',
    options: ['因为接口往往更短更好读', '因为 import 一个包就会死循环', '因为否则换 Provider 时 Consumer 也得改', '因为 Provider 不对外暴露方法'],
    correct: 2,
    explanation: 'Consumer 只依赖服务接口、不 import 实现，所以换 Provider 时 Consumer 与整个产品都不动。这是"可换缝"成立的关键。'
  },
  {
    question: '"Provider 互换会整套移动" 最根本的原因是什么？',
    options: ['多个缝往往共享同一个执行世界', '新 Provider 通常代码量更少', '因为产品只允许单进程', '因为 Provider 决定 CPU 频率'],
    correct: 0,
    explanation: 'fs 与 subprocess 共享"执行世界"，bash/PTY/LSP 都经 ctx.subprocess 这条缝去 spawn，换一组 provider 时这些能力成套迁移、无分叉。'
  },
  {
    question: 'seam 中 Service Definition 的职责，最匹配的是哪一项？',
    options: ['定义接口、词汇、事件，并独立于实现', '把接口与实现写进同一个包', '只负责本地的具体落盘实现', '生成给模型看的精确参数 schema'],
    correct: 0,
    explanation: 'Definition 定义接口与词汇并独立于后端；Provider 是具体实现；Consumer 才提供模型 schema——三者分工明确。'
  },
  {
    question: '`sandboxMode` 这个能力事实的作用是什么？',
    options: ['它是仅供内部参考的调试标记', 'Provider 填一个事实，Consumer 据它决定是否暴露沙箱字段', '它强制模型每步都必须用沙箱', '它让所有工具天生都带沙箱'],
    correct: 1,
    explanation: 'Provider 在 `ctx.shell` 上填 `sandboxMode`；Consumer 读它来决定是否给工具 schema 加"要求沙箱"字段，而无需知道 bwrap/Landlock 等后端细节。'
  }
]
</script>

<Quiz :questions="q"></Quiz>