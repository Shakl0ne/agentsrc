# 主循环：Submission 驱动的 Turn 系统

## 〇、引言

上一篇文章我们从宏观上俯瞰了 Codex 的整体架构：三个二进制入口、100+ 个 crate、Rust 工程哲学。但你可能会问——**用户敲下 `codex` 之后到底发生了什么？**

这篇文章深入 Codex 的主循环，回答三个问题：

1. **Codex 的主循环长什么样？**——不是轮询，是事件驱动的 reactor
2. **一次 Turn 的生命周期是什么？**——从 User Input 到模型回复，经过了哪些阶段
3. **和 CC 的主循环比有什么区别？**——两种工程哲学的碰撞

![Codex 事件驱动 Reactor：submission_loop + 4 个 Op 生产者](/images/codex/02-hero.png)

## 一、Codex 没有「主循环」

### 1.1 这不是一个玩笑

这是一个有点反直觉的说法，但我先说出来：**Codex 没有一个类似 CC 中 `queryLoop()` 那样的函数**。

CC 的主循环在 `query.ts` 的第 200-1677 行，是一个约 1,477 行的巨型 `async function* queryLoop()`。它用 while-true 驱动一切——等待用户输入、调模型、执行工具、压缩上下文，全都在同一个函数里按顺序跑。

Codex 完全不是这样。

它的「主循环」严格来说不是一个循环，而是一个**事件分发器**：`submission_loop`（`handlers.rs:738`）。

```rust
// handlers.rs:738-887（简化）
pub(super) async fn submission_loop(
    sess: Arc<Session>,
    config: Arc<Config>,
    rx_sub: Receiver<Submission>,
) {
    while let Ok(sub) = rx_sub.recv().await {
        match sub.op {
            Op::UserInput { .. } => user_input_or_turn(&sess, ...).await,
            Op::Compact => compact(&sess, ...).await,
            Op::Interrupt => interrupt(&sess).await,
            Op::Shutdown => shutdown(&sess, ...).await,
            // ... 共 20+ 种 Op 类型
        }
    }
}
```

它不主动"拉"工作，而是被动响应 `Receiver<Submission>` 上的消息。每个 `Submission` 包装了一个 `Op` 枚举值，告诉分发器"该干什么"。

有 20+ 种 Op 类型：

| Op 类型 | 用途 |
|---------|------|
| `UserInput` | 用户发消息，启动一轮对话 |
| `Compact` | 触发上下文压缩 |
| `Interrupt` | 中断当前任务 |
| `ExecApproval` | 执行策略审批 |
| `PatchApproval` | 代码修改审批 |
| `ThreadRollback` | 回滚对话 |
| `ThreadSettings` | 运行时设置变更 |
| `Shutdown` | 关闭会话 |
| `RunUserShellCommand` | 执行 shell 命令 |
| `RealtimeConversationStart/Audio/Text/Close` | 实时语音对话控制 |
| `InterAgentCommunication` | 子 Agent 消息传递 |
| 等等 | |

### 1.2 消息从哪来

Codex 的 Submission 通过 `async_channel`（Tokio 的 MPSC 变体）发送。这意味着：

- Producer 可以在任意协程中发送 Submission
- Consumer（`submission_loop`）在单协程中串行处理

产生 Submission 的场景包括：

- **用户输入**：TUI 或 App Server 收到用户消息，发送 `Op::UserInput`
- **子 Agent 通信**：Agent 需要给父/子 Agent 发消息，通过 `Op::InterAgentCommunication`
- **自动触发的任务**：定时检查 token 超限，自动发送 `Op::Compact`
- **审批响应**：用户批准/拒绝一个 shell 命令执行，通过 `Op::ExecApproval`

每个 Submission 的完整结构（`protocol.rs:127`）：

```rust
pub struct Submission {
    pub id: String,              // 关联事件 ID
    pub op: Op,                  // 干什么
    pub client_user_message_id: Option<String>,  // 客户端消息 ID
    pub trace: Option<W3cTraceContext>,          // 分布式追踪
}
```

复用同一个 channel 让所有操作自然地排队——用户输入不会打断审批，审批不会打断压缩。串行化是自动的，不需要锁。


## 二、一次性 Turn 的生命周期

当 `submission_loop` 收到 `Op::UserInput` 时，它调用 `user_input_or_turn`（`handlers.rs:88`），最终触发 `run_turn`（`turn.rs:136`）。

`run_turn` 的完整签名：

```rust
pub(crate) async fn run_turn(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    turn_extension_data: Arc<codex_extension_api::ExtensionData>,
    input: Vec<TurnInput>,
    prewarmed_client_session: Option<ModelClientSession>,
    cancellation_token: CancellationToken,
) -> Option<String>
```

一次 Turn 的生命周期可以概括为 8 个阶段：

![Turn 生命周期 8 阶段瀑布图](/images/codex/02-lifecycle.png)

```
pre-sampling compact
  ↓
记录上下文更新 + 注入 skills/plugins/hooks
  ↓
set_previous_turn_settings
  ↓
↓ → 检查 pending_input（用户在中途发的新消息）
↓ → clone_history() 组装 prompt
↓ → run_sampling_request() 调模型
↓ → 判断 token_limit_reached && needs_follow_up
↓ →   是 → run_auto_compact() → back to loop
↓ →   否 → 返回结果
  ↓  ← loop
工具执行 + 结果回写
  ↓
stop hooks
  ↓
结束
```

### 2.1 Pre-sampling Compact

每次 sampling 前（`turn.rs:150`），Codex 会先检查是否需要压缩上下文。如果当前 token 使用量超过阈值，会先触发 compact 再采样。

这里调用的函数是 `run_pre_sampling_compact`（`turn.rs:784`），它会检查 `auto_compact_token_status()` 返回的 token 状态，决定是否以及用什么方式压缩（Local / Remote v1 / Remote v2）。

```rust
// turn.rs:150
if let Err(err) = run_pre_sampling_compact(&sess, &turn_context, &mut client_session).await {
    // 如果 usage limit 超了，返回 None
    return None;
}
```

### 2.2 上下文注入

压缩检查通过后，Codex 记录上下文变更并构建注入：

```rust
// turn.rs:167
sess.record_context_updates_and_set_reference_context_item(turn_context.as_ref()).await;

let (injection_items, explicitly_enabled_connectors) =
    build_skills_and_plugins(&sess, turn_context.as_ref(), &input, &cancellation_token).await?;
```

这个过程包括：
- 上下文 diffing（仅发送变更段，复用 prompt cache）
- Skills/Plugins 注入
- Hooks 执行（`run_pending_session_start_hooks`）

### 2.3 Sampling 请求

提交给模型的 prompt 通过 `clone_history().for_prompt()` 组装（`turn.rs:237`）：

```rust
let sampling_request_input: Vec<ResponseItem> = {
    sess.clone_history()
        .await
        .for_prompt(&turn_context.model_info.input_modalities)
};
```

然后调用 `run_sampling_request`，这对应实际的模型 API 调用：

```rust
match run_sampling_request(
    Arc::clone(&sess), Arc::clone(&turn_context),
    Arc::clone(&turn_extension_data), Arc::clone(&turn_diff_tracker),
    &mut client_session, turn_metadata_header.as_deref(),
    sampling_request_input.clone(), cancellation_token.child_token(),
)
```

### 2.4 Mid-turn Compact

Sampling 返回后（`turn.rs:259`），Codex 进入一个判断逻辑：

```rust
if token_limit_reached && needs_follow_up {
    if let Err(err) = run_auto_compact(
        &sess, &turn_context, &mut client_session,
        InitialContextInjection::BeforeLastUserMessage,
        CompactionReason::ContextLimit,
        CompactionPhase::MidTurn,
    )
```

这是 mid-turn compact：模型本轮返回时说"我还要继续调用工具"，但 token 已经超限了，那就在工具执行前先压缩。压缩完成后回到 loop 顶部重新 sampling。

前一篇说过 Compact 的细节，这里不展开——第四篇会单独讲。

![Mid-Turn Token Check 决策树](/images/codex/02-token.png)

### 2.5 Loop + Stop Hooks

```
loop {
    检查 pending_input（用户中途发的新消息）
    执行 hooks + 记录输入
    clone_history() 组装 prompt
    run_sampling_request() 调模型
    判断是否需要 mid-turn compact
    需要 → compact → continue
    不需要 → break
}
执行 stop hooks
返回最终消息
```

Stop hooks（`run_turn_stop_hooks`）在 turn 结束前执行，和 CC 的 stop hooks 概念类似——允许用户注册在对话结束后运行的逻辑。


## 三、SessionTask 抽象

Codex 有不同的任务类型。不是每一次"模型回复"都是标准的用户对话：

| Task 类型 | 定义位置 | 用途 | 触发器 |
|-----------|---------|------|--------|
| `RegularTask` | `tasks/regular.rs` | 标准用户-模型对话 | `Op::UserInput` |
| `CompactTask` | `tasks/compact.rs` | 上下文压缩 | `Op::Compact` |
| `ReviewTask` | `tasks/review.rs` | Code Review | `review()` 在 handlers.rs:702 |
| `UserShellCommandTask` | `tasks/user_shell.rs` | `codex exec` 一次性命令 | `Op::RunUserShellCommand` |

它们都实现 `SessionTask` trait（`tasks/mod.rs:207`）：

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

`Session::spawn_task`（`tasks/mod.rs:305`）会先 abort 所有之前的任务，再 spawn 新任务。任何时候只有一个活跃的 `SessionTask`。

![SessionTask trait 层次：4 种实现](/images/codex/02-tasks.png)

## 四、Session 状态的并发访问

Codex 的所有会话状态存储在 `Session` 结构体中，内部通过 `Arc<Mutex<SessionState>>` 保护：

```rust
// session.rs（简化）
pub struct Session {
    state: Arc<tokio::sync::Mutex<SessionState>>,
    // ...
}
```

关键点：

1. **短锁持有**：访问 state 的操作都非常短，拿到锁 → 读/写 → 释放
2. **事件是异步的**：`Session::send_event()` 不会等 UI 渲染完，只发到 event channel
3. **历史是集成的**：`Session` 内部持有关联的 `HistoryManager`，通过 `clone_history()` 获取快照

这意味着 `submission_loop` 虽然是单协程处理，但协程内部通过 `.await` 暂停时，其他协程（如 UI 线程或监控协程）有机会执行。


## 五、Tool Calling 的处理

Tool calling 在 Codex 中不单独属于一个文件。它分散在几个层面：

1. **Sampling 阶段**（`run_sampling_request`）：模型返回 tool call，由 `stream_events_utils` 解析
2. **Tool 路由**（`tools/` 模块）：`ToolRouter` 根据 tool name 分派到具体 handler
3. **结果回写**：工具结果写回 history，然后 loop 判断是否继续

这与 CC 的 tool calling 机制结构上是相似的（模型发 tool_call → 执行 → 结果写回），但 Codex 的抽象层级更多：

- Tool 定义和路由在独立的 `codex-tools` crate
- MCP 连接由 `codex-mcp` 管理
- 执行权限由 `codex-execpolicy` 控制
- 沙箱由 `codex-sandboxing` 隔离

不过这些不在本文的范围内，第六篇会专门讲工具系统。


## 六、Codex vs Claude Code：主循环对比

### 6.1 核心差异

| 维度 | Codex | Claude Code |
|------|-----------|-------------|
| **主循环文件** | `handlers.rs:738`（~220 行） | `query.ts:200-1677`（~1,477 行） |
| **模式** | 事件驱动 reactor | continuation-driven polling |
| **并发** | tokio channel + 协程 | 单线程 async generator |
| **状态结构** | `Arc<Mutex<SessionState>>` | 显式 `State` 对象 + mutable |
| **Task 抽象** | 4 种 SessionTask trait | 无（都塞在主循环里） |
| **Op 枚举** | 20+ 种，enum dispatch | implicit（通过 yield/return 控制流） |
| **Tool 循环** | 嵌在 run_turn 的 loop 里 | 嵌在 queryLoop 的 while true 里 |

### 6.2 为什么 Codex 选 reactor？

这和工程语言直接相关。

Rust 的所有权模型让显式状态管理更安全：`Arc<Mutex<SessionState>>` 在 Rust 里是惯用模式，编译器保证你不会误用。而在 TypeScript 里，同样的显式锁就需要更小心。

CC 的 queryLoop 选择 continuation-driven 是 TypeScript 自然的选择——async generator 让状态隐式保持在函数栈帧里，用 `yield` 退出再 `next()` 恢复，不需要额外的状态机。

两种模式没有绝对的优劣。但有一个观察：**Codex 能天然支持并行子 Agent 和后台任务，因为它有 channel + 多协程的基础设施；CC 的 queryLoop 想要加并行子 Agent 就需要大规模重构。** 这就是架构决策的长期影响。

### 6.3 一个有趣的类比

CC 的 queryLoop ≈ 单线程 event loop（如 Node.js 的事件循环）
Codex 的 submission_loop ≈ 单消费者消息队列（如 Kafka consumer）

两者都串行处理，但串行的方式不同：
- CC：同协程内 cede control（yield），由外部调度器决定何时恢复
- Codex：总在新协程中处理每个 Op，channel 保证顺序


## 七、小结

| 你学到什么 | 对应文件 |
|-----------|---------|
| submission_loop 事件分发 | `handlers.rs:738-887` |
| Op 枚举（20+ 种操作） | `protocol.rs:498` |
| Submission 结构 | `protocol.rs:127` |
| run_turn 生命周期（8 阶段） | `turn.rs:136` |
| Pre-sampling compact | `turn.rs:150` + `turn.rs:784` |
| Mid-turn auto compact | `turn.rs:292-301` |
| SessionTask trait | `tasks/mod.rs:207` |
| 4 种 Task 实现 | `tasks/regular.rs`, `compact.rs`, `review.rs`, `user_shell.rs` |

## 章节小测

<script setup>
const q = [
  {
    question: 'Codex 的 submission_loop 与 Claude Code 的 queryLoop 核心设计差异是什么？',
    options: ['Codex 通过 while-true 轮询方式检查事件并同步执行回调', 'submission_loop 基于 channel 事件驱动分发 Op 消息到各 Handler', 'Codex 使用多线程并行并行处理用户请求与后台压缩任务', '两套系统架构设计完全相同仅底层编程语言实现存在差异'],
    correct: 1,
    explanation: 'submission_loop 通过 async_channel 接收 Submission（包装 Op 枚举），被动响应消息分派到不同 handler；queryLoop 是一个 1,477 行的 async generator，用 while-true 驱动一切——等待输入、调模型、执行工具、压缩全在同一个函数里。'
  },
  {
    question: 'Codex 的 SessionTask trait 为什么设计了 abort 方法？',
    options: ['提供程序退出时统一清理后台会话资源的关闭入口', '确保 spawn 新任务前终止当前活跃任务避免并发冲突', '允许用户在运行时手动中断正在执行的长时间任务', '配合 Rust Drop 语义在 SessionTask 析构时自动回收资源'],
    correct: 1,
    explanation: 'Session::spawn_task 会先 abort 所有之前的任务再 spawn 新任务，确保任何时候只有一个活跃的 task。这种设计避免了并发冲突，简化了状态管理。'
  },
  {
    question: 'Mid-turn compact 的触发条件是什么？',
    options: ['只要上下文 token 达到设定的自动压缩阈值即触发压缩', 'token 超限且模型要求继续执行未完成工具调用时触发', '由用户通过 TUI 界面或 API 接口手动触发上下文压缩', '每轮对话的 sampling 阶段结束后自动无条件触发压缩'],
    correct: 1,
    explanation: '两个条件必须同时满足：token 超限（token_limit_reached）且模型要求继续（needs_follow_up，如还有未执行完的 tool call）。如果模型已返回 final message 但 token 超限，不 compact——下一轮 pre-turn compact 会处理。'
  },
  {
    question: 'Codex 选择事件驱动 reactor 模式而非 CC 的 continuation-driven polling，带来了什么长期影响？',
    options: ['通过减少异步状态转换显著降低核心循环的整体代码行数', 'channel 多协程基础设施天然支持并行子 Agent 与后台任务', '牺牲部分运行时吞吐性能换取更严格的内存安全保证', '利用 tokio 零成本抽象缩短每次模型调用的响应延迟'],
    correct: 1,
    explanation: 'Codex 的 channel + 多协程基础设施让并行子 Agent 和后台任务成为自然能力；CC 的 queryLoop 是单线程 async generator，要加并行子 Agent 需对核心循环进行大规模重构。这就是架构决策的长期影响。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
