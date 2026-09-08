---
title: Codex 主循环图解：一条消息是如何在 Reactor 中流转的？
---

# Codex 主循环图解：一条消息是如何在 Reactor 中流转的？

想象 Agent 正在执行一个耗时 3 分钟的编译工具，此时你按下了 `Ctrl+C` 想打断，或者后台发现 Token 快爆了需要立刻触发上下文压缩。如果主循环是一个简单的 `while(true) { 请求模型 -> 执行工具 }`，系统此时是阻塞的，无法响应这些突发事件。

当 Agent 需要同时处理用户输入、UI 渲染、系统中断和后台任务时，线性的执行流该怎么破局？

Codex 的解法是采用基于 Channel 的 Reactor（反应器）模式。本文将拆解 Codex 核心引擎的任务调度机制。具体来说，我们将回答四个核心问题：

1. 外部输入和内部事件是如何被统一抽象并路由的？
2. 当用户在模型生成中途追加输入时，系统是直接强杀任务还是平滑介入（Steering 机制）？
3. 任务的取消为什么采用“协作式取消 + 兜底强杀”的三段式设计？
4. 相比 Claude Code 的 async generator，Codex 选择 Reactor 模式的工程考量是什么？

具体的上下文组装和工具执行细节留给下一篇，本文只聚焦任务的调度、并发控制与生命周期。

## 一、问题：被打破的线性执行幻觉

在单端输入的纯 CLI 脚本里，`while` 循环没有问题：系统等待用户输入，发给大模型，拿到结果，再等下一次输入。

但在 Codex 的架构里，输入来源是多端的。它不仅要处理终端的键盘事件，还要响应 App Server 传来的 IDE 并发 RPC 请求。如果沿用线性循环，一旦主线程卡在 `await llm_generate()` 或某个耗时的本地工具执行上，整个 Agent 就会失去响应能力。此时用户在 VS Code 里点击“停止生成”，系统根本无暇顾及。

要解决多端并发响应，必须把“等待模型”和“响应事件”解耦。主循环不再主动死等 LLM 结果，而是变成一个专门监听事件的调度中心；LLM 推理和工具执行被剥离为后台子任务。

## 二、核心抽象 A：Reactor 模式与 Op 消息总线

### 2.1 万物皆事件：`Op` 枚举

Codex 把所有的“动作”数据化。无论是用户说话、沙箱请求审批，还是系统要求压缩上下文，全部被打包成统一的消息。

对应到源码，这是一个叫 `Op` 的枚举（位于 `codex-rs/protocol/src/protocol.rs`）：

```rust
// codex-rs/protocol/src/protocol.rs
pub enum Op {
    UserInput {
        items: Vec<UserTurnItem>,
        environments: Vec<EnvironmentIdentifier>,
        thread_settings: Option<ThreadSettings>,
        // ...
    },
    Compact,
    Interrupt,
    ExecApproval {
        id: String,
        turn_id: String,
        decision: ExecApprovalDecision,
    },
    // ...
}
```

![核心引擎 Reactor 事件循环](/images/codex/02-reactor-engine.svg)

这个设计将系统的意图与执行彻底剥离。UI 层或网络层不需要知道怎么调用模型，它们只需要构造一个 `Op::UserInput` 并发送到 Channel 中。

### 2.2 `submission_loop`：永不阻塞的监听者

接住这些消息的，是 `submission_loop` 函数：

```rust
// core/src/session/handlers.rs
pub(super) async fn submission_loop(
    sess: Arc<Session>,
    config: Arc<Config>,
    rx_sub: Receiver<Submission>,
) {
    while let Ok(sub) = rx_sub.recv().await {
        match sub.op {
            Op::UserInput { items, .. } => {
                user_input_or_turn(&sess, items, ...).await;
            }
            Op::Compact => run_compact_task(&sess).await,
            Op::Interrupt => handle_interrupt(&sess).await,
            // ...
        }
    }
}
```

主循环阻塞在 `rx_sub.recv().await` 上。这是一个单消费者的 Channel 接收端。

Codex 能及时响应中断，核心原因并不是 `Op::Interrupt` 在队列里有特权，而是因为耗时的长任务都被扔进了后台 spawned task 里。主循环在分发完事件后会立刻回到 `recv().await` 等待下一条消息，从而保持了极高的响应吞吐率。

## 三、核心抽象 B：Steering 介入与三段式取消

### 3.1 活跃任务中收到新输入：Steer 还是 Abort？

如果模型正在生成回复，用户突然又发了一条新消息（比如“等等，顺便把测试也写了”），系统该怎么处理？

直觉上可能会认为系统会直接强杀旧任务，然后带上新输入重新请求。但 Codex 的处理更加细腻，它引入了 Steering（介入）机制：

```rust
// 伪代码逻辑：user_input_or_turn 的流转
async fn user_input_or_turn(...) {
    // 1. 尝试介入当前活跃的 Turn
    match steer_input(sess, new_input).await {
        Ok(_) => return, // 成功将输入追加到 pending input，当前任务继续
        Err(SteerInputError::NoActiveTurn) => {
            // 2. 如果没有活跃任务，才启动新任务
            spawn_task(RegularTask::new()).await;
        }
    }
}
```

![Steering 介入机制：平滑追加而非强杀](/images/codex/02-steering-mechanism.svg)

如果当前有活跃的 `RegularTask`，新输入会被追加到当前 turn 的待处理队列中，模型在下一次工具循环间隙会感知到这个新指令。只有在没有活跃任务时，系统才会调用 `spawn_task` 启动新一轮对话。

### 3.2 协作式取消加兜底 Abort

当确实需要启动新任务（或收到 `Op::Interrupt`）时，系统会调用 `abort_all_tasks`。Codex 的任务取消采用了严谨的三段式设计：

```rust
// core/src/tasks/mod.rs
pub(crate) trait SessionTask: Send + Sync + 'static {
    fn run(...) -> impl Future<Output = Option<String>> + Send;
    
    // 任务可以覆写 abort 做额外清理，默认是 no-op
    fn abort(&self, ...) -> impl Future<Output = ()> + Send {
        async {}
    }
}
```

取消动作并不是简单粗暴的 `kill -9`，而是：
1. **协作式取消**：先调用 Tokio 的 `cancellation_token.cancel()`，向下游（网络请求、工具进程）广播取消信号。
2. **优雅等待**：等待一段 graceful timeout，期望任务自行清理并退出。
3. **兜底强杀与清理**：如果超时仍未退出，调用 `task.handle.abort()` 强杀底层协程，最后调用 task 的 `abort` hook 执行业务层的状态回滚。

这种机制保证了即使在极端情况下打断工具执行，也不会留下僵尸进程或损坏的数据库状态。

## 四、下潜到微观：run_turn 的真实流转

宏观上，Codex 靠事件驱动解耦了并发；但进入 `RegularTask::run` 后，微观的单次对话依然有一条清晰的执行链。

真实的 `run_turn` 流程比直觉中的“组装上下文 -> 调模型”要复杂得多：

```rust
// 伪代码逻辑：run_turn 的真实流转顺序
pub(crate) async fn run_turn(...) -> Result<()> {
    // 1. 采样前压缩：如果历史太长，先压一波
    run_pre_sampling_compact(sess).await?;
    
    // 2. 状态记录与上下文组装
    record_context_updates_and_set_reference_context_item(sess).await?;
    build_skills_and_plugins(sess).await?;
    
    // 3. 采样循环 (Sampling Loop)
    loop {
        let response = run_sampling_request(sess).await?;
        
        // 采样后压缩检查：如果单轮生成导致 Token 暴涨
        run_auto_compact(sess).await?;
        
        if response.has_tool_calls() {
            execute_tools(sess, response.tool_calls).await?;
            continue;
        } else {
            break;
        }
    }
}
```

![run_turn 微观流转与双重压缩防线](/images/codex/02-run-turn-loop.svg)

注意压缩机制的位置：它不仅在采样前（`run_pre_sampling_compact`）有一道防线，在每次模型返回后（`run_auto_compact`）还有一道防线。微观循环被严密地包裹在 Token 预算的监控之下。

## 五、关键决策：Reactor vs Continuation-driven

把 Codex 的主循环和 Claude Code 放在一起看，能明显感受到两种工程哲学的碰撞。

Claude Code 使用了 `async generator`（通过 `yield` 让出控制权），整个主循环写在一个巨大的函数里。代码像一条直线，直观易读。

Codex 为什么要把流程切碎成松散的事件和 Task？

首先是**并发架构的需求**。Codex 的 App Server 需要处理 IDE 发来的并发 JSON-RPC 请求。松散的事件总线天然适合多生产者（Multi-Producer）场景。UI 线程、网络线程、定时器线程都可以作为 Producer，向同一个 Channel 投递 `Op` 消息。

其次，这也更贴合 Rust/Tokio 的**工程惯性**。在 Rust 中，将复杂的会话状态跨越多个 `await` 甚至 `yield` 传递，需要处理繁琐的生命周期和借用关系；而基于 Channel 的 Actor/Reactor 模式，通过消息传递转移所有权，是 Rust 并发生态中更成熟、更稳健的解法。

## 六、总结：收拢控制流复杂度

把控制权交还给事件循环，LLM 推理只是众多可以被随时掐断的子任务之一。引入 Reactor 模式、Steering 介入机制和三段式取消，是 Codex 应对多端并发和复杂状态管理的核心手段。

既然任务调度的骨架已经理顺了，那送给模型的上下文是怎么组装的？几十种系统指令、环境变量、历史记录如何拼接才不会乱？下一篇，我们将进入 Codex 的上下文管线，拆解 `build_initial_context`。

## 七、章节小测

<script setup>
const q = [
  {
    question: '在 Codex 的架构中，submission_loop 函数在没有事件输入时，处于什么状态？',
    options: [
      '阻塞在对 LLM 推理 API 的网络请求回调上，等待模型返回生成的文本',
      '阻塞在 async_channel 的 recv() 方法上，等待接收新的 Submission 消息',
      '处于高频的 while-true 自旋轮询状态，不断检查各个工具的执行进度',
      '阻塞在终端标准输入的 read_line 方法上，等待用户敲击键盘输入字符'
    ],
    correct: 1,
    explanation: 'submission_loop 是一个单消费者接收端，它通过 rx_sub.recv().await 阻塞等待。这种设计使得它能在等待期间随时被其他来源（如系统定时器、IDE 请求）的消息唤醒。'
  },
  {
    question: '当 Codex 正在执行一个耗时的代码生成任务时，用户突然输入了新指令，系统默认会如何处理？',
    options: [
      '将新指令加入待处理队列，等当前代码生成任务完全结束后再按顺序启动新任务',
      '直接在当前线程中同步执行新指令，阻塞后续的所有模型请求和本地工具调用',
      '通过 steer_input 尝试介入当前活跃任务，将新输入追加到当前 Turn 的待处理队列',
      '调用当前 SessionTask 的 abort 方法强制终止旧任务，然后立刻清空历史并重启'
    ],
    correct: 2,
    explanation: 'Codex 引入了 Steering（介入）机制。如果当前有活跃的 RegularTask，新输入会被追加到当前 turn 的 pending input 中，模型在下一次工具循环间隙会感知到这个新指令，而不是直接强杀。'
  },
  {
    question: '在 Codex 的任务取消机制中，系统是如何安全地掐断正在进行的网络请求或工具进程的？',
    options: [
      '通过操作系统的 kill -9 信号直接杀死后台运行的 Tokio 工作线程以释放内存',
      '采用协作式取消（CancellationToken）、优雅等待超时，最后兜底强杀与清理',
      '修改数据库中的 Session 状态位，让底层的工具进程自行轮询并决定是否退出',
      '断开与 App Server 的 WebSocket 连接，强制引发底层的网络读写异常来中断'
    ],
    correct: 1,
    explanation: 'Codex 的取消是三段式的：先触发 CancellationToken 广播取消信号，等待一段 graceful timeout 期望任务自行清理，如果超时仍未退出，再调用 task.handle.abort() 强杀并执行 abort hook。'
  },
  {
    question: '对比 Claude Code，Codex 选择 Reactor 模式而非 async generator 的核心工程考量是什么？',
    options: [
      'async generator 在处理海量上下文时会导致严重的内存泄漏和系统性能衰退',
      'Reactor 模式能显著降低 LLM 推理的延迟，提升首字响应速度与用户体验',
      '适配多端并发输入需求，并更贴合 Rust/Tokio 生态中基于消息传递的工程惯性',
      'async generator 无法支持动态工具注册和 MCP 协议的双向集成与权限拦截'
    ],
    correct: 2,
    explanation: 'Reactor 模式天然适合 App Server 处理 IDE 并发请求的多生产者场景。同时，通过 Channel 传递消息转移所有权，比在 Rust 中跨 yield 维护复杂借用关系更符合工程惯性。'
  },
  {
    question: '在 run_turn 的真实流转中，上下文压缩（Compaction）发生在什么时机？',
    options: [
      '仅在用户手动发送 Op::Compact 消息时，才会暂停当前任务执行上下文压缩',
      '仅在模型返回 tool_calls 之前，系统会统一对历史工具结果进行一次压缩',
      '在采样循环开始前有一道防线，在每次模型返回后（自动压缩）还有一道防线',
      '仅在整个 run_turn 彻底结束后，系统会在后台起一个独立线程进行异步压缩'
    ],
    correct: 2,
    explanation: 'run_turn 流程中，首先会执行 run_pre_sampling_compact，然后在采样循环内部，每次模型返回后还会执行 run_auto_compact，微观循环被严密地包裹在 Token 预算监控之下。'
  }
]
</script>

<Quiz :questions="q"></Quiz>