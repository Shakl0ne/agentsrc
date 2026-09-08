---
title: Codex 多 Agent 编排：如何像管理线程一样管理 AI 团队？
---

# Codex 多 Agent 编排：如何像管理线程一样管理 AI 团队？

想象你需要重构一个包含 50 个文件的前端项目。如果只用一个 Agent，它的上下文窗口会迅速被各个文件的代码塞满，随之而来的是逻辑混乱和无尽的重试。要解决这种复杂任务，你必须组建一个“AI 团队”，让它们分工合作。

当系统中同时存在多个 Agent 时，它们之间的状态如何隔离？它们怎么互相通信？如果子 Agent 又生成了孙 Agent，这颗庞大的“执行树”会不会失控甚至死锁？

Codex 的解法是将 Agent 抽象为操作系统的“线程（Thread）”——构建基于 ThreadManager 的多实例底座，并在演进中发展出了 V1（基于 ThreadId 的基础调用）和 V2（基于 AgentPath 和明确消息传递）两套协作范式。

本文将聚焦于 Codex 的多 Agent 引擎，看看它是如何像操作系统管理进程一样，去孵化、路由和回收这些并行的 AI 执行单元的。接下来的源码剖析将围绕以下几环展开：

1. Codex 是如何通过 `ThreadManager` 与 `CodexThread` 将对话实例线程化的？
2. V1 和 V2 两代工具在 Agent 寻址和通信上有什么本质区别？
3. `InterAgentCommunication` 是如何实现跨 Agent 解耦通信的？
4. 系统是如何通过并发限制与深度校验防止无限递归生成的？

本文只讨论多 Agent 架构的调度与通信机制。至于单个 Agent 内的工具路由，我们在第五章已经拆解过。

## 一、基础底座：将对话线程化 (ThreadManager)

在 Codex 中，没有所谓的“全局 Agent”单例。每一个活跃的对话实例，在底层都被抽象为一个 `CodexThread`，由 `ThreadManager` 统一管理。

![Thread Isolation & Inheritance](/images/codex/07-thread-isolation.svg)

### 1.1 `CodexThread` 与状态隔离

对应到源码，位于 `codex-rs/core/src/codex_thread.rs`，一个线程实例包含了核心的上下文：

```rust
// 伪代码逻辑：展示线程的基础状态
pub struct CodexThread {
    pub codex: Codex,                  // 核心执行上下文，包含 Session
    pub session_source: SessionSource, // 线程来源（Fork, Spawn, Resume）
    pub rollout_path: RolloutPath,
    // ...
}
```

这意味着每个线程拥有完全独立的对话历史（`Session`）。对于安全权限（如 Exec Policy 和沙箱配置），子 Agent 默认会从父 Turn 的有效配置派生或继承。这保证了不同任务的数据被强隔离，不会发生 A 任务的代码污染 B 任务的上下文。

### 1.2 `ThreadManager` 的生命周期托管

类似操作系统管理进程，`ThreadManager` 负责分配全局唯一的 `ThreadId`，并处理线程的启动（Spawn）、Fork、Remove 和 Shutdown。

所有的状态更新和任务调度，都是通过向目标 Thread 发送 `Op` 消息（如 `send_op`）来完成的。

## 二、两条演进路线：V1 与 V2 多 Agent 机制

随着系统复杂度的提升，Codex 的多 Agent 机制发生了明显的分叉。

### 2.1 V1 工具：基于 ThreadId 的硬绑定

早期的 V1 工具（如 `spawn_agent`）相对简单。父 Agent 调用工具时，系统分配一个 `ThreadId`，并启动一个新线程。父 Agent 必须通过记录这个特定的 ID，使用 `send_input` 向其发送普通的 `Op::UserInput`。这种方式虽然实现了并行，但地址是动态分配的哈希值，难以在复杂树状结构中进行逻辑追踪。

### 2.2 V2 工具：基于 AgentPath 与显式消息 (Mailbox)

V2 引入了更强大的结构化编排机制：`AgentPath` 和 `InterAgentCommunication`。

`AgentPath`（定义在 `protocol/src/agent_path.rs` 中）类似于文件系统的绝对路径，例如 `/root/researcher`。V2 工具通过 `task_name` 静态生成这套层级路径，并通过 `resolve_agent_reference` 将路径解析为底层的 `ThreadId`。

当 `/root/researcher` 需要向父节点汇报结果时，它不再发送普通的 UserInput，而是构造一条 `InterAgentCommunication` 消息：

```rust
// codex-rs/protocol/src/protocol.rs
pub struct InterAgentCommunication {
    pub author: AgentPath,
    pub recipient: AgentPath,
    pub content: String,
    pub trigger_turn: bool,
    // ...
}
```

![V2 Agent Communication (Actor Model)](/images/codex/07-agent-communication.svg)

这种机制类似于 Actor 模型的消息信箱（Mailbox）。如果 `trigger_turn` 为 true，这条消息不仅会被送达，还会触发处于 Idle 状态的接收方开启新一轮 Turn。

## 三、一条真实的调用链：Spawn Agent

为了看清机制，我们追踪一条派生子 Agent 的真实调用链路：

1. **发起派生**：父 Agent 通过工具发起调用（如 `spawn_agent`）。
2. **构建配置**：系统调用 `build_agent_spawn_config`，合并父 Turn 的权限和环境设置。
3. **分配资源**：进入 `AgentControl::spawn_agent_with_metadata`。
4. **启动线程**：`ThreadManagerState::spawn_new_thread_with_source` 创建新的 `CodexThread`，并为其分配 `ThreadId`。
5. **首次通信**：通过 `send_input`（V1）或 `Op::InterAgentCommunication`（V2）将初始任务描述注入子线程，子线程开始运行。

在这个过程中，父 Agent 不会被阻塞。它可以继续自己的工作，或者通过显式的 `wait_agent`（V2）等待信箱状态更新。

## 四、安全熔断：防止无限派生的两道防线

大模型是有幻觉的。如果父 Agent 陷入死胡同，不断 spawn 新的子 Agent 去尝试修复 Bug，系统资源会在几秒内被数十个新进程耗尽。

Codex 提供了两道防线来限制这种无度扩张：

### 4.1 深度限制 (Depth Limit - V1 主力)
在 V1 的 `spawn_agent` 流程中，系统会严格检查 `agent_max_depth`。如果配置的限制是 3 层，当模型试图派生第四层时，底层工具会直接拒绝并返回 Error。

### 4.2 并发总数限制 (Max Concurrent Threads - V2 主力)
在 V2 机制中，虽然针对某些协作工具忽略了深度限制（以支持更灵活的网状结构），但 `AgentRegistry::reserve_spawn_slot` 强制引入了 `max_concurrent_threads_per_session` 的总数限制。无论树多深，一旦当前会话的活跃 Agent 数量达到上限，新的派生请求将被拦截。

此外，当任务结束时，通过显式的 `close_agent` 或 `shutdown_all_threads_bounded`，系统可以按 Spawn Tree 级联清理线程及其后代，确保不留下资源僵尸。

## 五、关键决策：接近 Actor 模型的工程取舍

把 Codex 的多 Agent 设计和早期的实验性框架放在一起看，能感受到工程实现上的显著差异。

很多早期的多 Agent 框架采用的是黑板模式（Blackboard）。所有的 Agent 共享同一个上下文池，自由读写。这种模式在 Agent 数量增加时，极易发生脏读写引发状态冲突。

Codex 选择的是更接近 Actor 模型的显式消息传递。通过 `CodexThread` 将会话状态强隔离，通过 `AgentPath` 与 `InterAgentCommunication` 实现明确的定址投递，并通过派生树继承权限配置。

这完全符合它“系统级软件”的底层架构思维——用操作系统的进程调度与 IPC 机制，来管理不确定性的 AI 代理。

## 六、总结：从单兵作战到协同网络

让多个 AI 协同工作，靠的不是一段花哨的“你扮演产品经理，我扮演程序员”的魔法 Prompt，而是严密的系统级调度：线程隔离、路径寻址、显式消息投递以及防止资源耗尽的并发与深度控制。

至此，我们已经拆解了 Codex 作为核心引擎的全部机制。但对于一个开发者来说，最爽的体验莫过于在 IDE 里边写代码边获得 Agent 的支援。

下一篇，也就是本系列的最后一篇，我们将看看这台引擎是如何被包装成后台守护进程，通过 App Server 协议与 VS Code 等工具无缝集成的。

## 七、章节小测

<script setup>
const q = [
  {
    question: '在处理复杂并行任务时，如果仅使用单体 Agent，最容易导致哪种负面后果？',
    options: [
      '由于缺乏多线程支持，单体 Agent 无法同时发起多个 HTTP 请求',
      '不同任务的信息高度交织会导致 Token 极速消耗并引发逻辑断层',
      '单体 Agent 的本地 SQLite 数据库在写入并发日志时极易发生表锁死',
      '操作系统会强制限制单个进程能够使用的最大内存上下文空间'
    ],
    correct: 1,
    explanation: '多线作战会让大量无关信息充斥上下文，不仅干扰推理，还会迅速撑爆 Token 预算，频繁触发压缩，最终导致局部细节丢失和逻辑断层。'
  },
  {
    question: '在 Codex 的 ThreadManager 架构中，子 Agent（CodexThread）是如何获得它的沙箱权限和指令策略的？',
    options: [
      '子 Agent 拥有默认的绝对独立沙箱，完全不受父任务的影响',
      '子 Agent 默认从父 Turn 的有效配置中派生或继承权限，并可通过角色配置调整',
      '子 Agent 必须每次在启动前强制要求用户在终端重新授权输入密码',
      '子 Agent 统一采用全局的只读沙箱策略，不允许执行任何写入操作'
    ],
    correct: 1,
    explanation: '子 Agent 拥有独立的对话上下文（Session），但其安全权限（如 Exec Policy 和 approval_policy）通常是从父 Turn 派生或继承的。'
  },
  {
    question: '对比 V1 和 V2 版本的 Agent 通信机制，V2 的 AgentPath 带来了什么核心改变？',
    options: [
      'V2 使用文件系统的真实路径替换了内存通信，将所有消息持久化到磁盘文件中',
      'V2 通过静态生成的层级路径（如 /root/researcher）进行寻址，并构造 InterAgentCommunication 显式传递消息',
      'V2 彻底放弃了 ThreadId 概念，所有底层的网络调用完全依靠进程名识别',
      'V2 将所有的子 Agent 合并到一个全局黑板（Blackboard）上进行无锁的共享内存读写'
    ],
    correct: 1,
    explanation: 'V1 主要依靠分配的 ThreadId 进行硬绑定通信；V2 则引入了类似绝对路径的 AgentPath 进行更结构化的寻址，并使用显式的 Mailbox 消息（InterAgentCommunication）。'
  },
  {
    question: '为了防止子 Agent 被模型因为幻觉无限嵌套派生导致资源耗尽，Codex 在 V2 机制中主要依赖哪种防御手段？',
    options: [
      '在 ThreadManager 中设置硬性的嵌套深度限制（Depth Limit）',
      '在 AgentRegistry 中引入 max_concurrent_threads_per_session 进行总并发数限制',
      '强制限制所有子 Agent 运行在内存低于 128MB 的 Docker 容器内',
      '在模型层面修改 System Prompt，警告其“不准连续派生超过 3 个子任务”'
    ],
    correct: 1,
    explanation: 'V1 的主力防线是 Depth Limit，但在 V2 中为了支持更灵活的网状协作，某些路径会忽略深度限制，转而依靠 AgentRegistry 的 reserve_spawn_slot 进行总并发线程数限制。'
  },
  {
    question: '对比早期的“全局共享黑板（Blackboard）”模式，Codex 的多 Agent 机制在工程上的核心优势是什么？',
    options: [
      '极大地简化了代码实现，使得 Agent 的启动速度提升了数个数量级',
      '更接近 Actor 式的显式消息传递，将会话状态强隔离，避免了并发脏读写引发的状态冲突',
      '允许所有 Agent 实时共享同一个庞大的 Prompt 缓存池，降低 API 调用成本',
      '使得系统能够完全脱离底层操作系统运行，实现真正意义上的跨平台编译分发'
    ],
    correct: 1,
    explanation: '黑板模式在并发量大时极易发生脏读写。Codex 通过接近 Actor 模型的设计，利用 CodexThread 实现了强隔离，并通过 InterAgentCommunication 进行安全的解耦通信。'
  }
]
</script>

<Quiz :questions="q"></Quiz>