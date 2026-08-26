# Codex 全景：架构与定位

## 〇、前言

看完 OpenCode 和 Claude Code（以下简称 CC），还有一个我们绕不开的名字：**Codex**。

Codex 是 OpenAI 官方出品的本地编程代理。它在 2025 年 5 月开源（Apache-2.0），和 OpenAI 的 ChatGPT Codex Web（云端版）不同，CLI 版跑在你本地机器上，使用你的 ChatGPT 订阅配额或 API key。

这篇文章作为 Codex 系列的开篇，先不深入细节，而是先用一张全景地图把 Codex 的架构看清楚：

- 它用什么语言写的？为什么选 Rust？
- 它由哪几大部分组成？
- 入口在哪里？各模块各司什么职？
- 和 Claude Code 宏观上有什么不同？

后面的文章再逐一深入每个子系统。

## 整体架构速览

下面是 Codex 核心引擎 `codex-core` 的反应器 + turn 流程，本文会围绕它展开：

![Codex 反应器架构：submission_loop 分发 Ops，run_turn 驱动采样与工具](/images/codex/article-index-architecture.svg)

## 一、Codex 自顶向下看

### 1.1 一句话定位

Codex = **一个运行在本地的 AI 编程代理**，通过 `codex` 命令启动，提供交互式终端（TUI）、非交互式执行（`codex exec`）、后台守护进程（App Server）三种使用方式。

它支持的功能包括：

- 交互式对话编程（TUI 模式）
- 非交互式命令执行（`codex exec`"一次性"任务）
- IDE 集成（通过 App Server + WebSocket 连接 VS Code 等编辑器）
- MCP 服务器模式（`codex mcp-server`，作为其他 MCP 客户端的工具提供者）
- Plugins 扩展系统
- 沙箱执行（macOS Seatbelt / Linux Landlock / Windows sandbox）
- 多 Agent 编排（Agent A 可递归地 spawn Agent B）
- 状态持久化与恢复（resume/fork 会话）

### 1.2 源码布局

项目根目录结构：

```
codex/
├── codex-cli/          # npm 包 (@openai/codex) — 用户安装入口
├── codex-rs/           # Rust Cargo 工作区 — 核心实现
├── sdk/                # SDK（Python / TypeScript）
├── docs/               # 用户文档（15 个 markdown 文件）
├── scripts/            # 构建与发布脚本
└── ...
```

**关键点**：最终用户安装的是 npm 包 `@openai/codex`，但这个包只是一个薄薄的 JavaScript 壳，检测平台后 spawn 对应的 Rust 二进制。所有实际代码在 `codex-rs/` 这个 Rust Cargo 工作区里。

工作区有约 **100+ 个 crate**（全部以 `codex-` 前缀命名），核心 crate 的分组如下：

![Codex 三个二进制入口：codex / codex-tui / codex-app-server](/images/codex/01-triple.png)

| 分组 | 关键 crate | 作用 |
|------|-----------|------|
| **入口** | `codex-cli` | 子命令分发器 |
| **核心引擎** | `codex-core` | 主循环、会话管理、上下文、工具调度、沙箱、安全、技能 |
| **TUI** | `codex-tui` | 交互式终端界面（ratatui 框架） |
| **非交互模式** | `codex-exec` | `codex exec` 一次性执行 |
| **工具系统** | `codex-tools` | 工具定义、发现、执行、MCP 集成 |
| **模型** | `codex-model-provider` + `codex-models-manager` | 多后端模型抽象（OpenAI / Ollama / LM Studio / ChatGPT） |
| **后端守护** | `codex-app-server` → `codex-app-server-daemon` | IDE 集成的后台服务 |
| **沙箱** | `codex-sandboxing` + `codex-linux-sandbox` + `codex-windows-sandbox-rs` | 跨平台沙箱抽象 |
| **安全** | `codex-process-hardening` + `codex-execpolicy` | 进程加固和指令策略 |
| **多 Agent** | `agent-graph-store` + `codex-core` 内的 agent 模块 | 子 Agent 生命周期管理 |

### 1.3 三个二进制入口

Codex 不只有一个二进制，而是三个：

```
codex-rs/
├── cli/src/main.rs     → codex（主 CLI）
├── tui/src/main.rs     → codex-tui（TUI 会话进程，由主 CLI fork）
├── app-server/src/main.rs → codex-app-server（后台守护进程）
```

- **`codex`**（主 CLI）：用户在终端敲 `codex` 时运行的入口。它是一个 clap 子命令分发器（`cli/src/main.rs:90-205`），支持 20+ 个子命令：`exec`、`login`、`logout`、`mcp`、`plugin`、`app-server`、`resume`、`fork`、`archive` 等等。如果不带子命令，则启动 TUI 交互模式。
- **`codex-tui`**（TUI 二进制）：被主 CLI fork 的子进程，运行 `ratatui` 框架的终端 UI。
- **`codex-app-server`**（守护进程）：后台持续运行的 JSON-RPC 服务，通过 stdio / Unix socket / WebSocket 与 IDE 通信。支持 V2 协议（`app-server-protocol/src/protocol/v2.rs`）。

![Codex 全景架构：核心引擎 + 工具 + 沙箱 + TUI + 多 Agent](/images/codex/01-hero.png)

## 二、核心引擎架构（codex-core）

`codex-core` 是整个系统的中枢，约 118 个源文件。它的核心模块拓扑：

```
                      ┌─────────────┐
                      │    CLI 入口   │
                      │  cli/main.rs │
                      └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │  submission_loop │  ← 事件循环
                      │ handlers.rs:738  │
                      └──────┬──────┘
                             │  Op::UserTurn / Op::Compact / Op::Interrupt / ...
                      ┌──────▼──────┐
                      │  run_turn()  │  ← 每一次"轮到模型回答"
                      │  turn.rs:136 │
                      └──────┬──────┘
                             │
         ┌───────────────────┼────────────────────┐
         ▼                   ▼                    ▼
  ┌─────────────┐    ┌──────────────┐    ┌──────────────┐
  │ 上下文组合   │    │  Compaction  │    │  工具执行    │
   │ build_initial│    │  compact.rs │    │  tools/      │
   │ _context    │    │  (3种实现)   │    │              │
   │ mod.rs:2725 │    └──────────────┘    └──────────────┘
   └─────────────┘
```

![Codex 核心引擎拓扑：submission_loop → run_turn → Context + Compact + Tools](/images/codex/01-engine.png)

### 2.1 主循环不是轮询循环

和直觉相反，Codex 的主循环**不是**一个 while-true 的轮询 loop。它是一个事件驱动的 reactor，核心是 `submission_loop`（`session/handlers.rs:738`）：

```rust
// handlers.rs:738-887（简化）
async fn submission_loop(sess: &Session, mut rx_sub: Receiver<Submission>) {
    while let Some(sub) = rx_sub.recv().await {
        match sub.op {
            Op::UserTurn { ... } => user_input_or_turn(sess, ...).await,
            Op::Compact => run_compact_task(sess).await,
            Op::Interrupt => handle_interrupt(sess).await,
            Op::Shutdown => break,
            // ...
        }
    }
}
```

这个 loop 等待 `async_channel::Receiver<Submission>` 上的消息。每个 `Submission` 包装了一个 `Op` 枚举——`UserTurn`、`Compact`、`Interrupt`、`Shutdown`、`ExecApproval`、`ThreadRollback` 等。消息可以由用户输入触发，也可以由系统内部发送（如自动 compaction 触发）。

这是一个常见的设计模式，不过和 CC 是不同思路：CC 用协程 + continuation 驱动的单线程 polling loop，Codex 用 tokio 的多线程 reactor + channel 通信。

第二章会专门深入这个 message-passing 主循环。

### 2.2 SessionTask 生命周期

每个"任务"（一次模型交互）被抽象为 `SessionTask` trait（`tasks/mod.rs:207-245`）：

```rust
pub(crate) trait SessionTask: Send + Sync + 'static {
    fn kind(&self) -> TaskKind;
    fn span_name(&self) -> &'static str;
    fn run(
        self: Arc<Self>,
        session: Arc<SessionTaskContext>,
        ctx: Arc<TurnContext>,
        input: Vec<TurnInput>,
        cancellation_token: CancellationToken,
    ) -> impl Future<Output = Option<String>> + Send;
    fn abort(
        &self,
        session: Arc<SessionTaskContext>,
        ctx: Arc<TurnContext>,
    ) -> impl Future<Output = ()> + Send;
}
```

四种实现：

| Task 类型 | 文件 | 用途 |
|-----------|------|------|
| `RegularTask` | `tasks/regular.rs` | 正常的用户-模型对话轮次 |
| `CompactTask` | `tasks/compact.rs` | 手动触发上下文压缩 |
| `ReviewTask` | — | Code Review 任务 |
| `UserShellCommandTask` | — | `codex exec` 类命令 |

`Session::spawn_task`（`tasks/mod.rs:305-314`）会先 abort 掉所有之前的任务，再 spawn 新任务。这意味着任何时候只有一个活跃的 task，但 task 内部可以有多个子协程。

### 2.3 build_initial_context：10+ 个上下文段

`build_initial_context`（`session/mod.rs:2725`）是整个系统中函数体最长的之一。它构造发送给模型的所有上下文，分为三类：

1. **Developer sections**（合并为 1 条 developer 消息）：模型切换指令、权限指令、协作模式指令、Personality 说明、Apps 指令、技能说明、Plugin 能力说明、扩展片段。

2. **Contextual user sections**（合并为 1 条 user 消息）：用户自定义指令（来自 AGENTS.md）、环境上下文（shell info / cwd / OS / subagents）、扩展的用户片段。

3. **Separate developer sections**（独立 developer 消息）：请求了 `PromptSlot::SeparateDeveloper` 的扩展片段，每条独立。

第三章会详细展开 context diffing（增量注入）和 prompt caching 优化。

### 2.4 Compact 系统：3 种压缩机制

Codex 有三种压缩方式（`compact.rs` + `compact_remote.rs` + `compact_remote_v2.rs`）：

| 实现 | 位置 | 原理 | 调用模型？ |
|------|------|------|-----------|
| **Local（Inline）** | `compact.rs:70` | 把整个历史发给模型，要求生成摘要 | **是** |
| **Remote v1** | `compact_remote.rs` | 调用 Responses API 的专用 compact endpoint | 是（API 后台） |
| **Remote v2** | `compact_remote_v2.rs` | 改进版的远程 compact | 是（API 后台） |

触发时机也有三种：

- **Pre-turn**（`turn.rs:784`）：每次 turn 开始前检查 token budget
- **Mid-turn**（`turn.rs:293`）：一轮对话中多轮 tool call 后，如果 token 超限且还要继续
- **Manual**：用户手动触发（Op::Compact → CompactTask）

对比 CC 的 5 级压缩（前 4 级纯数据结构操作，第 5 级才调 LLM），Codex 的压缩策略可以说完全相反：它的所有压缩都涉及 LLM 调用。这个发现在第四篇会详细展开。

## 三、工具系统与 MCP

工具系统在 `codex-tools` crate 中，包含：

- **Tool 定义与 Schema**：每个工具实现为一个 JSON Schema + Rust handler
- **MCP 集成**：`codex-mcp` crate 负责管理 MCP 连接（McpConnectionManager），支持外部 MCP 服务器作为工具源
- **动态工具**：Codex 可以在运行时动态加载新的工具定义

工具调用的流程大致是：

1. 模型返回一个 tool_call
2. Tool Run Loop 解析、执行、收集结果
3. 结果追加回 history
4. 如果需要继续（模型要求更多工具调用），循环回来

这一点和 CC 的 tool calling 机制结构相似，但 Codex 的抽象层级更多（MCP connection manager、exec policy、sandboxing 等）。

## 四、沙箱和安全模型

Codex 在安全上投入很大：它有完整的跨平台沙箱系统。

| 平台 | 沙箱机制 |
|------|---------|
| macOS | Seatbelt（`/usr/bin/sandbox-exec`） |
| Linux | Landlock + seccomp + Bubblewrap（可选） |
| Windows | Windows 沙箱 |

抽象层在 `codex-sandboxing`，平台实现在 `codex-linux-sandbox`、`codex-windows-sandbox-rs` 等。

除此之外还有：

- **Process Hardening**（`codex-process-hardening`）：进程级别加固
- **Exec Policy**（`codex-execpolicy`）：执行策略引擎，控制什么命令可以/不可以执行


## 五、多 Agent 编排（Codex vs CC 两种哲学）

Codex 拥有完整的层次化多 Agent 系统：

- **Agent Tree**：Root Agent → 子 Agent → 孙 Agent，形成树结构
- **Task-path 路由**（V2）：Agent 通过规范路径 `{root}/task1/task_3` 寻址
- **并行执行**：多个子 Agent 互不阻塞，父 Agent 可以 wait 或持续工作
- **消息传递**：`send_message` / `followup_task` / `wait_agent` / `close_agent` 体系
- **批处理模式**：`spawn_agents_on_csv` — 对 CSV 每行 spawn 一个工作 Agent，map-reduce 风格
- **深度限制**：`exceeds_thread_spawn_depth_limit()` 防止递归失控

第五篇会深入这个系统。


## 六、模型管理的 Provider 抽象

Codex 使用 Provider 模式抽象模型后端：

| Provider | 后端 | 用途 |
|----------|------|------|
| `codex-model-provider` | — | 统一抽象层 |
| `codex-models-manager` | — | 模型目录、选择、迁移 |
| `codex-ollama` | Ollama | 本地模型 |
| `codex-lmstudio` | LM Studio | 本地模型 |
| `codex-chatgpt` | ChatGPT 云 | ChatGPT 订阅用户 |
| `codex-realtime-webrtc` | WebRTC | 实时语音对话 |

模型切换时，Codex 会自动触发 compaction（`maybe_run_previous_model_inline_compact`，`turn.rs:810`），因为不同模型的上下文窗口不同。


## 七、Codex vs Claude Code：宏观对比

![Codex vs Claude Code 宏观对比](/images/codex/01-vs.png)

### 7.1 工程语言：Rust vs TypeScript

| 维度 | Codex | Claude Code |
|------|-----------|-------------|
| **主语言** | Rust（~100+ crates） | TypeScript（主包） + Rust（tectonic DB） |
| **构建系统** | Bazel + Cargo | esbuild |
| **包管理** | npm wrapper 发布 | npm 纯 Node.js |
| **并发模型** | tokio async + channels | async generator + 协程 |
| **启动方式** | 多二进制入口 | 单 Node.js 进程 |
| **内存管理** | 零成本抽象 + 所有权 | V8 GC |

Rust 的选择让 Codex 天然具备了更激进的沙箱和安全能力（Landlock、seccomp、Seatbelt 都依赖系统级调用）。CC 在 Rust 方面只使用了 tectonic DB。

### 7.2 架构哲学

| 维度 | Codex | Claude Code |
|------|-----------|-------------|
| **主循环模式** | 事件驱动 reactor（channel） | continuation-driven polling |
| **压缩策略** | 所有压缩调 LLM（3 种实现） | 5 级压缩，前 4 级纯数据结构操作 |
| **多 Agent** | ✅ 原生支持（Agent Tree） | ✅ Swarm（跨进程）+ AgentTool（同进程） |
| **沙箱** | ✅ 完整跨平台沙箱 | ❌ 无沙箱 |
| **认证** | ChatGPT OAuth + API key | 仅 API key |
| **IDE 集成** | App Server 守护进程 | 终端内使用 |
| **扩展系统** | Plugins + MCP + Skills | Skills + Hooks |
| **代码定位** | 开源引擎 + 闭云服务 | 完全开源 |

### 7.3 一个有趣的细节：Build

Codex 项目的构建依赖 Bazel，是一个相当重量级的构建系统：

```
MODULE.bazel  BUILD.bazel  defs.bzl  rbe.bzl  .bazelversion
```

而 CC 就是标准的 npm 项目，`tsup` 打包。这个差异反映了两个项目的工程规模和团队偏好——Codex 的 crate 数量 ~100+，CC 的 TypeScript 源文件约 120+。规模相当但构建哲学不同。

### 7.4 为什么要注意这些差异

这些差异不是随机的。它们反映了两个团队的核心设计假设：

- **CC 假设**：代理工作在单一进程内，以 RESTful 方式与外部工具交互。安全由用户自己保证。
- **Codex 假设**：代理可能被滥用，需要进程隔离（沙箱）、策略控制（exec policy）、多 Agent 拆分复杂问题。

这个设计哲学差异贯穿了整个系列。最后一篇会专门讨论。


## 八、小结

| 了解什么 | 对应源码 | 后续文章 |
|---------|---------|---------|
| 主循环与消息传递 | `handlers.rs:738` | 第 2 篇 |
| 上下文组合与增量注入 | `mod.rs:2725` | 第 3 篇 |
| Compact 3 种压缩机制 | `compact.rs` | 第 4 篇 |
| 多 Agent 编排 | `agent/control.rs` | 第 5 篇 |
| 工具系统与沙箱 | `tools/` + `sandboxing/` | 第 6 篇 |
| 模型管理 | `model-provider/` | 第 7 篇 |
| 设计哲学对比总结 | 全系列 | 第 8 篇 |

## 章节小测

<script setup>
const q = [
  {
    question: 'Codex 为什么选择 Rust 作为主要实现语言，而 Claude Code 使用 TypeScript？',
    options: ['利用 Rust 所有权模型降低运行时内存安全风险', '依赖 Rust 系统级能力实现沙箱与进程级安全加固', '借助 TypeScript 异步生态加速 Agent 循环迭代效率', '为 WebAssembly 跨平台分发保留统一的编译目标'],
    correct: 1,
    explanation: 'Rust 的选择源于 Codex 的"系统级软件"定位——沙箱、进程加固、WebSocket 都需要系统级能力。CC 选 TypeScript 则是因为开发速度快、与 npm 生态无缝集成、async generator 天然适合 agent 循环抽象。'
  },
  {
    question: 'Codex 的 npm 包 @openai/codex 的本质是什么？',
    options: ['将 Rust 核心逻辑编译为跨平台 Node.js 原生模块分发', '一个轻量 JavaScript 入口壳按平台下载并启动 Rust 二进制', '通过 WebAssembly 在 Node.js 运行时内执行全部 Rust 逻辑', '全部功能由纯 JavaScript 实现通过 npm 包直接分发执行'],
    correct: 1,
    explanation: '用户安装的是 npm 包，但这是一个薄薄的 JS 壳，实际所有代码在 codex-rs/ 这个 Rust Cargo 工作区里，npm 包只负责检测平台并启动对应的 Rust 二进制。'
  },
  {
    question: 'Codex 的主循环与 Claude Code 的主循环在设计模式上的本质区别是什么？',
    options: ['Codex 使用同步 while-true 轮询来等待事件并同步分派', 'Codex 采用事件驱动 reactor 经 channel 异步消息分发', '两套系统均采用事件驱动架构仅编程语言实现不同', 'Codex 全程同步阻塞而 CC 全程采用异步运行时'],
    correct: 1,
    explanation: 'Codex 的 submission_loop 是事件驱动的 reactor，通过 async_channel 接收 Submission 消息后再分派；CC 的 queryLoop 是 continuation-driven 的 polling loop，用 async generator 在同一个函数里按顺序驱动一切。'
  },
  {
    question: 'Codex 的压缩策略与 Claude Code 最根本的不同是什么？',
    options: ['Codex 内置比 CC 更多的上下文压缩级别与触发时机', 'Codex 全部压缩都调 LLM 而 CC 前四级仅操作数据结构', 'Codex 仅在手动触发时执行压缩而 CC 全程自动触发', 'Codex 完全不压缩上下文而 CC 对所有上下文进行压缩'],
    correct: 1,
    explanation: 'Codex 的 3 种压缩实现（Local/Remote v1/Remote v2）都调用 LLM，是"质量优先"设计；CC 的 5 级压缩中前 4 级是纯数据结构操作（截断/Snip/Microcompact/Context Collapse），第 5 级才调 LLM，是"成本优先"设计。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
