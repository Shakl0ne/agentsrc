---
title: Codex 工具系统：为什么它既是 MCP 客户端又是服务端？
---

# Codex 工具系统：为什么它既是 MCP 客户端又是服务端？

想象你写了一个 Agent，内置了执行 Bash 和读写文件的能力。但用户突然说：“我想让它能查我的飞书日历，还能读我本地的 SQLite 数据库。”如果把这些能力全硬编码进核心引擎，代码会迅速膨胀。

当工具的需求变得无限发散时，系统该如何设计，才能既支持按配置扩展，又保证执行的安全边界？

Codex 的解法是全面拥抱 MCP（Model Context Protocol）——不仅将工具抽象为动态注册的 Schema，还让自己具备了“双向插拔”的能力：
- **作为 MCP 客户端**（`codex-mcp`）：连接外部服务器，聚合外部能力。
- **作为 MCP 服务端**（`mcp-server`）：对外提供 MCP 工具，允许外部编排 Codex 会话。

本文将拆解 Codex 核心引擎中的工具管线。具体来说，我们将回答四个核心问题：

1. Codex 的 `ToolExecutor` Trait 是如何将工具定义与执行解耦的？
2. 模型调用一个外部 MCP 工具时，底层的真实执行链路是怎样的？
3. 作为 MCP 客户端，`McpConnectionManager` 除了拉起进程还做了什么？
4. 作为 MCP 服务端，Codex 暴露给外部的核心能力是什么？

本文只讨论工具的“发现与路由”。关于工具执行时如何被操作系统底层的 Landlock/Seatbelt 拦截，将留到下一篇《沙箱与安全》中详细拆解。

## 一、问题：硬编码工具的死胡同

如果所有工具都在编译期注册，Agent 的能力就被锁死了。用户无法在运行时挂载第三方服务。

必须把工具的“定义（Schema）”和“执行（Execution）”解耦，让主循环只面对一个统一的路由接口。主循环不需要知道“天气 API”怎么调，它只需要知道“有一个叫 `get_weather` 的工具，它的参数是城市名”。

## 二、核心抽象 A：ToolExecutor 与分发门面

### 2.1 万物皆 Tool 的抽象

在 Codex 中，无论是内置的 `shell_command`、`apply_patch`，还是外部 MCP 传来的工具，在核心引擎看来，都只是一个实现了 `ToolExecutor` trait 的对象。

对应到源码，位于 `codex-rs/tools/src/tool_executor.rs`：

```rust
// codex-rs/tools/src/tool_executor.rs
#[async_trait::async_trait]
pub trait ToolExecutor<Invocation>: Send + Sync {
    /// 工具的名称
    fn tool_name(&self) -> ToolName;

    /// 工具的 JSON Schema 定义
    fn spec(&self) -> ToolSpec;

    /// 控制工具是否对模型可见
    fn exposure(&self) -> ToolExposure {
        ToolExposure::Direct
    }

    /// 具体的执行逻辑
    async fn handle(
        &self,
        invocation: Invocation,
    ) -> Result<Box<dyn ToolOutput>, FunctionCallError>;
}
```

![ToolExecutor 抽象与路由分发](/images/codex/05-tool-routing.svg)

注意这里的 `exposure` 字段。它控制了工具的可见性。比如有些内部工具（如 `Hidden`）仅供路由或系统内部调用，模型是看不见的。

### 2.2 `ToolRouter`：分发层与注册表门面

当模型返回了一堆 `tool_calls` 时，谁来负责分发？答案是 `ToolRouter`。

```rust
// codex-rs/core/src/tools/router.rs
pub struct ToolRouter {
    registry: ToolRegistry,
    model_visible_specs: Vec<ToolSpec>,
}
```

`ToolRouter` 主要做三件事：管理对模型可见的 Spec 列表、将模型的 `ResponseItem` 转换为内部的 `ToolCall`、以及向 `ToolRegistry` 分发。真正的审批、沙箱拦截和生命周期事件，则分散在各 handler 和 session 模块中。

## 三、一条完整的调用链：模型调用 MCP 工具时发生了什么？

为了看清工具管线，我们追踪一条真实的调用链路：

1. **工具发现与注册**：`McpConnectionManager::list_all_tools()` 获取远端工具 → `build_mcp_tool_exposure()` 决定可见性 → `ToolRouter::from_turn_context()` 将其包装为 `McpHandler` 并注册到 `ToolRegistry`，同时将 Schema 暴露给模型（`model_visible_specs`）。
2. **模型发起调用**：模型根据 Schema 返回一个 function call。
3. **路由分发**：`ToolRouter::build_tool_call` 解析请求，交由 `ToolRegistry` dispatch 到对应的 `McpHandler`。
4. **底层执行**：`McpHandler::handle` 接收到请求，调用 `McpConnectionManager::call_tool()`。
5. **结果回流**：远端 MCP 服务器返回 `CallToolResult`，层层包装后最终作为 `ResponseItem` 追加到上下文历史中。

对模型来说，调用 MCP 工具和调用内置工具的形态是一致的；但在运行时，它们会走完全不同的 handler 路径。

## 四、核心抽象 B：作为 MCP 客户端的动态挂载

作为 MCP 客户端，Codex 能够连接外部的 MCP 服务器，按配置和可见性策略扩展能力。

在 Codex 中，外部 MCP 服务器的生命周期由 `McpConnectionManager`（位于 `codex-rs/codex-mcp/src/connection_manager.rs`）管理。

![作为 MCP 客户端的动态挂载](/images/codex/05-mcp-client.svg)

它不仅仅是一个进程拉起器（Process Spawner），它的职责非常繁重：
- **生命周期管理**：读取配置，拉起外部进程，处理 startup 状态事件和 shutdown。
- **资源聚合**：聚合远端的 tools、resources 和 templates。
- **权限与过滤**：处理 Tool Filter，管理 OAuth 和 Bearer Token 的认证（Elicitation）。

连接建立后，远端工具会被无缝接入 Codex 的内部工具路由系统中。

## 五、核心抽象 C：作为 MCP 服务端的反向暴露

如果用户在用另一个支持 MCP 的编辑器（比如 Cursor 或 Zed），他们也想用 Codex 的能力怎么办？

Codex 的解法是：自己也变成一个 MCP 服务器（`codex-rs/mcp-server/src/`）。

在 `codex_tool_runner.rs` 中，Codex 暴露了 `codex` 和 `codex-reply` 两个核心工具。

注意，Codex 并没有直接把底层的沙箱执行器暴露出去，而是暴露了“发起一次 Codex 会话”的能力。`codex` 工具的输入不仅包含 prompt，还支持传入 `model`、`cwd`、`approval_policy`、`sandbox`、`base_instructions` 等精细配置；而 `codex-reply` 则依赖 `threadId` 续接对话。

外部客户端发来请求，Codex 在内部通过 `ThreadManager::start_thread` 创建 Codex thread，按配置进入 core 流程，处理 `ExecApprovalRequest` 等审批事件。这种设计让外部调用依然受 Codex 审批与沙箱策略的严格约束。

## 六、关键决策：双向集成的工程意义

把 Codex 的工具系统和早期的 Agent 框架对比，能发现架构上的显著差异。

传统的 Agent 往往只做单向集成（只能作为客户端调用外部 API）。而 Codex 花了巨大的代码量实现了 `codex-mcp`（客户端）和 `mcp-server`（服务端）。

这种双向集成让 Codex 变成了一个“能力路由器”。它既可以聚合外部能力为己所用，又可以作为基础设施，以会话工具的形式被其他上层应用（如 IDE、CI/CD 脚本）调用。

## 七、总结：工具系统的边界

优秀的工具系统，核心不在于内置了多少工具，而在于其路由机制的扩展性和安全性。

通过 `ToolExecutor` 的抽象、`ToolRouter` 的分发，以及 MCP 的双向集成，Codex 实现了能力的动态扩展。

但是，当外部 MCP 服务器传回一个恶意工具，或者模型产生幻觉试图执行高危命令时，仅仅靠路由是拦不住的。下一篇，我们将进入 Codex 的安全防线——跨平台沙箱与指令策略。

## 八、章节小测

<script setup>
const q = [
  {
    question: '为什么 Codex 要将工具抽象为 ToolExecutor Trait 而不是硬编码在主循环中？',
    options: [
      '为了绕过 Rust 编译器的生命周期检查，避免在闭包中捕获可变引用',
      '为了将工具的定义与执行解耦，支持在运行时动态注册外部 MCP 工具或 extension tools',
      '为了在不同操作系统上分别使用不同的二进制分发，以提升跨平台兼容性',
      '为了将模型推理、工具执行和网络请求分配到不同进程加速执行效率'
    ],
    correct: 1,
    explanation: '硬编码会锁死 Agent 的能力。ToolExecutor 的抽象使得主循环只面对统一的接口，从而支持运行时动态挂载外部 MCP 工具或自定义扩展。'
  },
  {
    question: '在 Codex 的工具管线中，ToolRouter 扮演了什么角色？',
    options: [
      '负责拦截恶意命令，直接调用操作系统的沙箱机制进行物理隔离',
      '作为分发层和注册表门面，负责管理模型可见的 Spec 列表并向 ToolRegistry 分发调用',
      '负责对工具的输出进行纯文本截断，以防止超长的执行结果撑爆 Token 预算',
      '负责与外部大模型厂商的 API 通信，将工具调用的 JSON 转换为 HTTP 请求'
    ],
    correct: 1,
    explanation: 'ToolRouter 主要做 model-visible specs 管理、ResponseItem 到 ToolCall 的转换、以及向 ToolRegistry 分发。真正的审批和沙箱拦截分散在其他模块中。'
  },
  {
    question: '作为 MCP 客户端，McpConnectionManager 的主要职责是什么？',
    options: [
      '管理与大模型厂商（如 OpenAI、Anthropic）的 WebSocket 长连接',
      '动态拉起外部 MCP 进程，处理生命周期事件，并聚合远端的 tools、resources 和 templates',
      '监控本地网络流量，拦截所有未经授权的 HTTP 请求以保证沙箱安全',
      '维护一个本地的 SQLite 数据库，持久化保存所有工具调用的历史记录'
    ],
    correct: 1,
    explanation: 'McpConnectionManager 不仅仅是拉起进程，它还负责处理 startup 状态事件、聚合远端资源、处理 Tool Filter 以及认证（Elicitation）等繁重职责。'
  },
  {
    question: '作为 MCP 服务端，Codex 暴露给外部客户端的核心能力是什么？',
    options: [
      '直接暴露底层的 shell_command 工具，允许外部客户端在宿主机上执行任意命令',
      '暴露 codex 和 codex-reply 会话工具，外部请求按配置进入 core 流程，受审批与沙箱策略约束',
      '暴露本地的文件系统读写权限，允许外部客户端直接修改工作区代码',
      '暴露大模型的 API Key，允许外部客户端绕过 Codex 直接调用底层模型'
    ],
    correct: 1,
    explanation: '为了安全，Codex 并没有直接暴露底层的高危工具，而是暴露了发起和续接 Codex 会话的能力。外部请求在 Codex 内部启动线程，依然受沙箱和策略的保护。'
  },
  {
    question: 'Codex 实现 MCP 双向集成（既是客户端又是服务端）的核心工程意义是什么？',
    options: [
      '为了兼容旧版本的 API 协议，旧版本 API 强制要求必须同时实现客户端和服务端',
      '为了减少向模型发送的 Token 数量，服务端的响应格式比客户端更精简',
      '使其成为一个“能力路由器”，既能聚合外部工具，又能以会话工具形式被外部 MCP client 调用',
      '为了在 TUI 界面上能够正确渲染对话历史，双向集成是 Ratatui 框架的硬性要求'
    ],
    correct: 2,
    explanation: '双向集成使得 Codex 不仅能聚合外部生态，还能作为基础设施赋能其他 IDE 或脚本。这种能力路由器定位是系统级 Agent 的核心标志。'
  }
]
</script>

<Quiz :questions="q"></Quiz>