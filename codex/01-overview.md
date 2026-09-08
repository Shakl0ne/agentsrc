# Codex 全景：系统级 Agent 的架构与分层

想象你要写一个能接管终端、能和 IDE 通信，同时还要绝对防止 AI 幻觉导致“删库跑路”的本地 Agent。当 Agent 的定位从一个“轻量级的 Node.js 聊天脚本”变成一个“高危的系统级进程”时，架构该怎么变？Codex 给出的答案是：用 Rust 重写，做系统级的物理隔离与事件驱动。本文的任务，就是先搭起 Codex 的这张总地图。读完这篇，你会建立以下认知：

- 第一，它为什么不做一个单体应用，而是拆成了三个独立的二进制？
- 第二，高达 100+ 个 Rust Crates（包）是如何分层协作的？
- 第三，它的核心主链路和常见的轮询代理有什么本质不同？

具体的沙箱拦截机制和主循环源码留给后续文章，这一篇我们只看骨架。

## 一、定位与形态：三个二进制入口

用户在终端通过 `npm install -g @openai/codex` 安装 Codex。但这个 npm 包只是一个薄薄的 JavaScript 壳（入口在 `codex-cli/bin/codex.js`）。它的作用不是在运行时下载代码，而是检测当前操作系统架构，定位到由 optional dependencies 提前安装好的、预编译的 native `codex` 二进制文件，然后将其 `spawn` 起来。

Codex 的所有核心逻辑都在 `codex-rs` 这个 Rust 工作区里。它没有把所有功能塞进一个单一的执行流，而是拆分成了三个核心入口：

```text
codex-rs/
├── cli/src/main.rs        → codex（主 CLI 分发器）
├── tui/src/main.rs        → codex-tui（交互式终端）
├── app-server/src/main.rs → codex-app-server（后台守护进程）
```

![Codex 宏观拓扑结构：npm 壳与底层 Rust 引擎](/images/codex/01-macro-topology.svg)

主 CLI `codex` 负责解析命令行参数（如 `codex exec` 或 `codex login`），执行完一次性任务就退出。如果用户不带子命令直接回车，主 CLI 会调用 `codex_tui::run_main` 接管屏幕渲染（基于 Ratatui 框架）。

而 `codex-app-server` 则是完全独立的后台常驻守护进程。如果用户在 VS Code 里使用 Codex，IDE 会拉起这个 Server。这种物理隔离的设计意图很明确：**生命周期解耦**。UI 渲染的生命周期、一次性脚本执行的生命周期，以及后台常驻服务的生命周期完全不同，拆分能保证核心服务的稳定性。

## 二、总体分层：100+ Crates 的庞大工程

进入 `codex-rs` 目录，你会看到 100 多个以 `codex-` 开头的 crate（注：下文统一使用 crate name，实际源码目录名可能略有不同，例如 `core/` 目录对应 `codex-core`）。为了不迷失在包海里，我们可以把它们归拢为四个核心层：

### 核心中枢层
包含 `codex-core`、`codex-api` 等。这里是系统的主干，负责维护对话会话、组装上下文、管理 Token 预算，以及调度模型请求。

### 工具与扩展层
包含 `codex-tools`、`codex-mcp` 等。它不仅定义了内置的文件读写、Bash 执行工具，还实现了一套完整的 MCP（Model Context Protocol）双向集成机制。

### 安全与沙箱层
包含 `codex-sandboxing`、`codex-execpolicy`、`codex-process-hardening`。这是 Codex 区别于其他 Agent 的核心壁垒。它直接调用操作系统底层的 Landlock（Linux）或 Seatbelt（macOS）机制，把 Agent 的执行环境锁死在安全边界内。

### IDE 协议层
包含 `codex-app-server`、`app-server-protocol` 等。这一层将核心引擎的能力包装成标准的 JSON-RPC 2.0 服务，通过 stdio 或 Unix Socket 与外部编辑器通信。

## 三、主链路：核心引擎的流转骨架

把视线拉回 `codex-core`，看看一次对话是怎么流转的。

和 Claude Code 依靠 `async generator` 驱动的单线程轮询循环不同，Codex 的主循环是一个基于 Channel 消息传递的 **Reactor（反应器）模式**。

1. **入口接收**：CLI 或 TUI 将用户的输入打包成一个 `Op::UserInput` 消息，扔进 Channel。
2. **事件分发**：中枢函数 `submission_loop`（位于 `core/src/session/handlers.rs`）在后台持续监听这个 Channel。匹配到 `Op::UserInput` 后，流程进入 `user_input_or_turn` 函数。
3. **任务互斥**：系统会先终止当前可能正在运行的旧任务，然后通过 `spawn_task(RegularTask::new())` 启动一个新的任务。
4. **执行轮次**：任务进入 `RegularTask::run`，最终驱动 `run_turn` 流程。系统依次执行上下文组装（`build_initial_context`）、压缩检查，最后调用模型并执行工具。

这种基于事件驱动的设计，让 Codex 能够从容应对后台自动压缩、用户随时 Ctrl+C 中断等复杂的并发场景。

## 四、外围补齐：系统级能力

在核心引擎之外，Codex 搭载了三套让它区别于轻量级脚本的系统级能力：

- **OS 级沙箱**：不依赖 Prompt 警告，而是用操作系统底层的权限控制，从物理层面阻断恶意工具调用。
- **多 Agent 编排**：支持 Agent Tree 树状结构。遇到复杂任务，父 Agent 可以派生出多个子 Agent 并行处理，并通过 `AgentPath`（如 `/root/worker`）进行路由寻址和通信。
- **MCP 双向集成**：Codex 既可以作为客户端连接外部 MCP 服务器获取能力，也可以作为服务端（`codex mcp-server`）通过 MCP 暴露 Codex 会话能力，底层仍由 Codex 的审批与沙箱管线兜底。

## 五、总结构论：设计假设与后续路线

Codex 的架构完全建立在**“不信任”**的假设上。

它不信任模型，所以要用 OS 级沙箱和指令策略拦截高危操作；它不信任单一进程的稳定性，所以要把 UI、引擎和后台服务拆分成独立的二进制逻辑。这是一种典型的“系统级软件”思维，也是它采用 Rust 重写的根本原因。

这张总地图搭好后，接下来的文章我们将逐一下潜，拆解这些机制的具体实现。下一篇，我们先从系统的心脏跳动开始——看看 Codex 是如何用 Reactor 模式重构 Agent 主循环的。

## 六、章节小测

<script setup>
const q = [
  {
    question: '用户通过 npm 安装的 @openai/codex 包，其在系统架构中的真实角色是什么？',
    options: [
      '包含所有业务逻辑的 Node.js 核心引擎，直接负责调度模型',
      '一个 JavaScript 壳，检测平台并 spawn 预安装的 native 二进制',
      '通过 WebAssembly 编译的 Rust 运行时，在 V8 引擎内执行',
      '负责与 VS Code 插件通信的中间件，处理所有的 JSON-RPC'
    ],
    correct: 1,
    explanation: 'npm 包只是一个分发渠道和启动壳。它的作用是检测当前操作系统架构，定位到由 optional dependencies 提前安装好的 native 二进制文件并启动它，所有核心逻辑都在 Rust 侧。'
  },
  {
    question: 'Codex 的主循环（submission_loop）采用了哪种并发设计模式？',
    options: [
      '基于 async generator 的单线程轮询模式，按顺序让出控制权',
      '基于 Channel 消息传递的 Reactor 模式，事件驱动任务流转',
      '基于多进程共享内存的轮询模式，通过锁机制同步状态数据',
      '基于定时器的定时轮询模式，每隔固定时间检查是否有新输入'
    ],
    correct: 1,
    explanation: 'Codex 采用了基于 Channel 的 Reactor 模式。外部输入被打包成 Op 消息放入 Channel，submission_loop 在后台监听并分发事件，这与 Claude Code 的 async generator 轮询模式有本质区别。'
  },
  {
    question: '在 Codex 的主链路中，当 submission_loop 接收到 Op::UserInput 消息后，系统是如何处理任务调度的？',
    options: [
      '将新任务加入队列，等待当前任务执行完毕后再按顺序执行',
      '直接在当前线程中同步执行新任务，阻塞后续的所有消息接收',
      '终止当前可能正在运行的旧任务，然后启动一个新的 SessionTask',
      '忽略新任务并向用户报错，直到当前对话轮次完全结束'
    ],
    correct: 2,
    explanation: '在 user_input_or_turn 流程中，系统会先 abort 掉当前正在运行的旧任务，然后通过 spawn_task(RegularTask::new()) 启动新任务。这种互斥机制保证了状态的一致性。'
  },
  {
    question: 'Codex 架构中，负责跨平台 OS 级沙箱（如 Landlock/Seatbelt）和指令策略拦截的模块属于哪个核心分层？',
    options: [
      '核心中枢层，与对话上下文组装和 Token 预算管理强耦合在一起',
      '工具与扩展层，作为动态工具注册和 MCP 协议解析的核心组成部分',
      '安全与沙箱层，直接调用操作系统底层机制从物理层面阻断恶意操作',
      'IDE 协议层，通过 JSON-RPC 拦截并过滤编辑器发来的所有执行指令'
    ],
    correct: 2,
    explanation: '沙箱和指令策略（codex-sandboxing, codex-execpolicy）是 Codex 的重武器，独立于核心引擎和工具层，通过 OS 底层机制提供物理级别的安全隔离。'
  },
  {
    question: 'Codex 在整体架构设计上，最底层的核心假设是什么？',
    options: [
      '信任模型生成的代码，重点优化单进程内的执行效率与系统资源占用',
      '假设用户环境是绝对安全的，Agent 仅作为轻量辅助脚本提供代码建议',
      '建立在不信任的假设上，既不信任模型幻觉也不信任单一进程的稳定性',
      '假设所有工具都通过远程网络调用，因此将网络并发作为最高优先级'
    ],
    correct: 2,
    explanation: 'Codex 的架构（OS 级沙箱、拆分独立的 CLI/TUI/AppServer 二进制）都是为了防范模型幻觉和进程崩溃，体现了典型的“系统级软件”防范思维。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
