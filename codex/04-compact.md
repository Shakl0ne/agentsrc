# Compact 系统：3 种压缩机制

## 〇、引言

第一篇我们说过 Codex 的压缩策略和 CC 截然相反——**所有压缩都涉及 LLM 调用**。这一篇展开讲清楚。

Codex 的 Compact 系统有 3 个核心维度：

1. **3 种实现**：Local / Remote v1 / Remote v2，运行时根据 provider 选择
2. **3 种触发时机**：Pre-turn / Mid-turn / Manual
3. **2 种初始上下文注入策略**：BeforeLastUserMessage / DoNotInject

读完这篇你能回答：

- Codex 怎么决定用哪种压缩实现？
- 三种实现在调用链路上有什么区别？
- 为什么 Mid-turn 必须注入完整初始上下文，而 Pre-turn 不能注入？
- 和 CC 的 5 级压缩对比，谁的策略更聪明？


![Codex 3 种 Compact 实现选择树](/images/opencode/codex-04-hero.png)

## 一、Compact 实现选择

### 1.1 三种实现的文件位置

```
codex-rs/core/src/
├── compact.rs             # Local 实现（617 行）
├── compact_remote.rs      # Remote v1 实现（485 行）
└── compact_remote_v2.rs   # Remote v2 实现（819 行）
```

三种实现都有同样的对外接口（每个文件都有 `run_inline_auto_compact_task` 和 `run_remote_compact_task` 两个公开函数），但内部实现差异很大。

### 1.2 选择逻辑：run_auto_compact

dispatcher 在 `turn.rs:862`：

```rust
async fn run_auto_compact(
    sess: &Arc<Session>,
    turn_context: &Arc<TurnContext>,
    client_session: &mut ModelClientSession,
    initial_context_injection: InitialContextInjection,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    if should_use_remote_compact_task(turn_context.provider.info()) {
        if turn_context.features.enabled(Feature::RemoteCompactionV2) {
            emit_compact_metric(&sess.services.session_telemetry, "remote_v2", false);
            run_inline_remote_auto_compact_task_v2(
                Arc::clone(sess), Arc::clone(turn_context), client_session,
                initial_context_injection, reason, phase,
            ).await?;
            return Ok(());
        }
        emit_compact_metric(&sess.services.session_telemetry, "remote", false);
        run_inline_remote_auto_compact_task(
            Arc::clone(sess), Arc::clone(turn_context),
            initial_context_injection, reason, phase,
        ).await?;
    } else {
        emit_compact_metric(&sess.services.session_telemetry, "local", false);
        run_inline_auto_compact_task(
            Arc::clone(sess), Arc::clone(turn_context),
            initial_context_injection, reason, phase,
        ).await?;
    }
    Ok(())
}
```

选择规则：

1. **`should_use_remote_compact_task(provider.info())`** —— 如果 provider 支持 remote compaction，走 remote
2. **`Feature::RemoteCompactionV2`** —— 如果 V2 feature 开启，走 v2；否则走 v1
3. **fallback** —— 都不满足，走 local

OpenAI 官方 provider（如 ChatGPT 订阅、OpenAI API）支持 remote compaction；Ollama、LM Studio 这类本地 provider 走 local。

### 1.3 三种实现的对比表

| 实现 | 文件 | 调 LLM | 调用方式 | 适用 provider |
|------|------|--------|---------|--------------|
| Local | `compact.rs:70` | ✅ 是 | 同一个模型 stream | 所有（Ollama / LM Studio / OpenAI 兜底） |
| Remote v1 | `compact_remote.rs:45` | ✅ 是 | Responses API compact endpoint | OpenAI 官方 |
| Remote v2 | `compact_remote_v2.rs:56` | ✅ 是 | 改进版 endpoint，支持更多配置 | OpenAI 官方 + Feature flag |

**所有三种都调用 LLM**——这就是和 CC 最大的不同。CC 的前 4 级压缩都是纯数据结构操作（截断 tool result、Snip、Microcompact、Context Collapse），只有第 5 级才调 LLM。


## 二、Local 实现详解

Local 是最通用的兜底实现。原理：把整个历史发给模型，要求生成摘要。

### 2.1 入口

`compact.rs:70`：

```rust
pub(crate) async fn run_inline_auto_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    initial_context_injection: InitialContextInjection,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    let prompt = turn_context.compact_prompt().to_string();
    let input = vec![UserInput::Text {
        text: prompt,
        text_elements: Vec::new(),
    }];
    run_compact_task_inner(
        sess, turn_context, input,
        initial_context_injection,
        CompactionTrigger::Auto, reason, phase,
    ).await?;
    Ok(())
}
```

注意：**input 不是用户的原始消息，而是 compact prompt**。也就是说 Codex 把"压缩请求"作为一个新的 user message 加到 history 末尾，让模型回复一个 summary。

### 2.2 核心循环

`run_compact_task_inner_impl` 在 `compact.rs:194`，关键流程：

```rust
async fn run_compact_task_inner_impl(...) -> CodexResult<String> {
    let compaction_item = TurnItem::ContextCompaction(ContextCompactionItem::new());
    sess.emit_turn_item_started(&turn_context, &compaction_item).await;
    
    let mut history = sess.clone_history().await;
    history.record_items(&[initial_input_for_turn.into()], turn_context.truncation_policy);
    
    let mut retries = 0;
    let mut client_session = sess.services.model_client.new_session();
    
    loop {
        let turn_input = history.clone().for_prompt(&turn_context.model_info.input_modalities);
        let prompt = Prompt {
            input: turn_input,
            base_instructions: sess.get_base_instructions().await,
            personality: turn_context.personality,
            ..Default::default()
        };
        let attempt_result = drain_to_completed(
            &sess, turn_context.as_ref(), &mut client_session,
            turn_metadata_header.as_deref(), &prompt,
        ).await;
        
        match attempt_result {
            Ok(()) => break,
            Err(CodexErr::ContextWindowExceeded) => {
                if turn_input_len > 1 {
                    // 砍掉最旧的一条，保留 prefix cache
                    history.remove_first_item();
                    retries = 0;
                    continue;
                }
                // 砍到只剩一条还不够，报错
                return Err(e);
            }
            Err(e) => {
                if retries < max_retries {
                    retries += 1;
                    let delay = backoff(retries);
                    // 等待重试
                } else {
                    return Err(e);
                }
            }
        }
    }
    // 构建摘要 + 替换 history
}
```

![Local Compact 内循环：ContextWindowExceeded → remove_first_item](/images/opencode/codex-04-loop.png)

### 2.3 摘要的构造

模型返回 summary 后（`compact.rs:289-323`）：

```rust
let history_snapshot = sess.clone_history().await;
let history_items = history_snapshot.raw_items();
let summary_suffix = get_last_assistant_message_from_turn(history_items).unwrap_or_default();
let summary_text = format!("{SUMMARY_PREFIX}\n{summary_suffix}");
let user_messages = collect_user_messages(history_items);

let mut new_history = build_compacted_history(Vec::new(), &user_messages, &summary_text);
```

几个关键点：

1. **`SUMMARY_PREFIX`** 是一个常量前缀，从 `codex_prompts` crate 导入（`compact.rs:48`）
2. **`summary_suffix`** = 模型返回的最后一条 assistant message
3. **`user_messages`** = 历史中所有 user message（被保留下来）
4. **`new_history`** = user messages + summary message

### 2.4 ContextWindowExceeded 处理

一个非常有趣的细节——如果 compact 自己也超了 context window：

```rust
Err(e @ CodexErr::ContextWindowExceeded) => {
    if turn_input_len > 1 {
        // 砍掉最旧的一条，保留 prefix cache
        history.remove_first_item();
        retries = 0;
        continue;
    }
    // ...
}
```

砍掉最旧的一条再试。注意注释："Trim from the beginning to preserve cache (prefix-based)"——**从最旧的开始砍是为了保留 prefix cache**。

这反映了一个细节：**Codex 的 prompt cache 是前缀 cache**，越靠前的内容 cache 价值越高。所以砍旧不砍新。

### 2.5 COMPACT_USER_MESSAGE_MAX_TOKENS 限制

`compact.rs:49`：

```rust
const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;
```

这是单次 user message 的 token 上限。超过这个长度的 user message 会被 truncate。


## 三、Remote v1/v2 实现

### 3.1 Remote v1 入口

`compact_remote.rs:45`：

```rust
pub(crate) async fn run_inline_remote_auto_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    initial_context_injection: InitialContextInjection,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    run_remote_compact_task_inner(
        &sess, &turn_context,
        initial_context_injection,
        CompactionTrigger::Auto, reason, phase,
    ).await?;
    Ok(())
}
```

内部调用 `run_remote_compact_task_inner`（`compact_remote.rs:89`），关键的实现标记：

```rust
let compaction_metadata = CompactionTurnMetadata::new(
    trigger, reason,
    CompactionImplementation::ResponsesCompact,  // ← 标记实现类型
    phase,
);
```

`ResponsesCompact` 表示调用的是 Responses API 的专用 compact endpoint。

### 3.2 Remote v2

`compact_remote_v2.rs:56` 是改进版，主要差异：

1. 支持更多元数据（如 trace context）
2. 改进的失败日志（`log_remote_compaction_request_failure` 在 `:389`）
3. 增强的 compaction output 收集（`collect_compaction_output` 在 `:406`）
4. 通过 `Feature::RemoteCompactionV2` feature flag 开关

实际调用 endpoint 的实现在 `run_remote_compaction_request_v2`（`compact_remote_v2.rs:332`）。

### 3.3 Remote vs Local 的本质区别

| 维度 | Local | Remote |
|------|-------|--------|
| 调用谁 | 当前 session 的同一个模型 | OpenAI 服务端 compact 服务 |
| 是否占用模型配额 | 是（用户配额） | 是（API 服务） |
| Latency | 取决于模型速度（可能很慢） | 服务端优化（更快） |
| 摘要质量 | 取决于模型能力 | 服务端优化（更高） |
| 适用场景 | 本地模型 / 兜底 | OpenAI 官方 |

Local 实现的最大问题：**用同一个模型压缩自己的对话历史**——大模型压缩自己的上下文，token 成本翻倍。Remote 实现把压缩交给服务端，可能有专门的轻量模型。


## 四、InitialContextInjection 策略

`compact.rs:61` 定义了一个关键的枚举：

```rust
/// Controls whether compaction replacement history must include initial context.
///
/// Pre-turn/manual compaction variants use `DoNotInject`: they replace history with a summary and
/// clear `reference_context_item`, so the next regular turn will fully reinject initial context
/// after compaction.
///
/// Mid-turn compaction must use `BeforeLastUserMessage` because the model is trained to see the
/// compaction summary as the last item in history after mid-turn compaction; we therefore inject
/// initial context into the replacement history just above the last real user message.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum InitialContextInjection {
    BeforeLastUserMessage,
    DoNotInject,
}
```

### 4.1 两种策略的区别

**Pre-turn / Manual compaction** 使用 `DoNotInject`：

- 替换 history 为 summary
- 清空 `reference_context_item`
- 下一次正常 turn 时，因为 reference 是 None，会触发 `build_initial_context` 全量注入

**Mid-turn compaction** 使用 `BeforeLastUserMessage`：

- 替换 history 为 summary + 初始上下文
- 把初始上下文插入到最后一条真实 user message 之前
- 保留 `reference_context_item`

### 4.2 为什么 Mid-turn 必须注入完整初始上下文？

注释说："the model is trained to see the compaction summary as the last item in history after mid-turn compaction"——模型被训练成看到 summary 在历史末尾。

Mid-turn 场景下，模型正在调用工具循环中，压缩完还要继续 sampling。如果不注入初始上下文，模型会丢失 developer instructions / permissions / environment 等关键信息，导致后续 sampling 行为异常。

Pre-turn 场景下，压缩完整个 turn 就结束了，下一个 turn 会重新走 `record_context_updates_and_set_reference_context_item` 的逻辑，自然全量注入。

![BeforeLastUserMessage vs DoNotInject 注入策略](/images/opencode/codex-04-inject.png)

### 4.3 注入实现

`compact.rs:297-308`：

```rust
if matches!(
    initial_context_injection,
    InitialContextInjection::BeforeLastUserMessage
) {
    let initial_context = sess.build_initial_context(turn_context.as_ref()).await;
    new_history =
        insert_initial_context_before_last_real_user_or_summary(new_history, initial_context);
}
let reference_context_item = match initial_context_injection {
    InitialContextInjection::DoNotInject => None,
    InitialContextInjection::BeforeLastUserMessage => Some(turn_context.to_turn_context_item()),
};
```

注意：调用的就是第三篇讲的 `build_initial_context`——**Compact 是 reference_context_item 重置的两个路径之一**（另一个是新会话/fork）。


## 五、Compact 的触发时机

回顾第一篇的 3 种触发时机，现在补全代码位置：

| 时机 | 位置 | 调用栈 |
|------|------|--------|
| Pre-turn | `turn.rs:150` → `run_pre_sampling_compact` (`turn.rs:784`) | 在第一次 sampling 之前检查 token budget |
| Mid-turn | `turn.rs:293` → `run_auto_compact` (`turn.rs:862`) | 一轮对话中，sampling 返回后 token 超限且模型要求 follow-up |
| Manual | `handlers.rs:834` → `compact()` → `run_compact_task` (`compact.rs:97`) | 用户主动 `Op::Compact` |

### 5.1 Pre-turn 触发条件

```rust
// turn.rs:150
if let Err(err) = run_pre_sampling_compact(&sess, &turn_context, &mut client_session).await {
    let error = err.to_codex_protocol_error();
    sess.emit_turn_error_lifecycle(turn_context.as_ref(), error.clone()).await;
    if error == CodexErrorInfo::UsageLimitExceeded {
        // ... 处理 usage limit
    }
    return None;
}
```

`run_pre_sampling_compact` 内部检查 `auto_compact_token_status` 返回的 token 状态。如果 `token_limit_reached`，调用 `run_auto_compact`，使用 `InitialContextInjection::DoNotInject` + `CompactionPhase::PreTurn`。

### 5.2 Mid-turn 触发条件

```rust
// turn.rs:293
if token_limit_reached && needs_follow_up {
    if let Err(err) = run_auto_compact(
        &sess, &turn_context, &mut client_session,
        InitialContextInjection::BeforeLastUserMessage,  // ← 关键差异
        CompactionReason::ContextLimit,
        CompactionPhase::MidTurn,
    ).await
```

**两个条件都满足**才会 mid-turn compact：

1. `token_limit_reached` —— token 超限
2. `needs_follow_up` —— 模型要求继续（如返回 tool call 还没执行完）

如果模型本轮已经返回 final message 但 token 超限，不 compact——因为下一轮 pre-turn compact 会处理。

### 5.3 Manual 触发

用户通过 TUI 或 API 主动触发压缩：

```rust
// handlers.rs:834
Op::Compact => {
    compact(&sess, sub.id.clone()).await;
    false
}
```

调用 `CompactTask`（`tasks/compact.rs`），最终走 `run_compact_task`（`compact.rs:97`），使用 `CompactionTrigger::Manual` + `CompactionReason::UserRequested` + `CompactionPhase::StandaloneTurn`。


## 六、Compact 后的 Warning

`compact.rs:319` 在 compact 完成后发一个 warning：

```rust
let warning = EventMsg::Warning(WarningEvent {
    message: "Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible to keep threads small and targeted.".to_string(),
});
sess.send_event(&turn_context, warning).await;
```

这是个有意思的细节——Codex 在 compact 后**主动建议用户开新会话**。这反映了 LLM 工程的真相：**任何压缩都是有损的**，多次 compact 后模型行为可能变怪。


## 七、Codex vs CC：压缩哲学对比

### 7.1 完整对比表

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| **压缩层级数** | 3 种实现 | 5 级 |
| **Local 调 LLM** | ✅ 是（同模型） | ❌ 前 4 级不调 |
| **远程压缩** | ✅ Responses API | ❌ 无 |
| **触发时机** | Pre-turn / Mid-turn / Manual | 每次调用前 / 超限时 |
| **触发条件** | token_limit_reached | 阈值 + 13K / 90% 利用率等 |
| **摘要生成** | 都用 LLM | 仅第 5 级 |
| **prefix cache 保护** | ✅（remove_first_item） | ✅（Snip 砍中间） |
| **初始上下文注入** | 2 种策略（BeforeLastUserMessage / DoNotInject） | 隐式（每轮都注入） |
| **完成后 warning** | ✅ 主动建议开新会话 | ❌ 无 |

### 7.2 设计哲学差异

**CC 的"5 级渐进式"哲学**：
- 优先用便宜的操作（数据结构变换）省空间
- 只有迫不得已才调 LLM
- 大多数会话走不到第 5 级

**Codex 的"3 种 LLM 调用"哲学**：
- 任何压缩都涉及 LLM（保证摘要质量）
- 选择 LLM 调用方式（本地/远程）优化成本
- 通过 InitialContextInjection 控制是否需要重注入

两种哲学各有道理：

- CC 的策略对**长会话**更经济——前 4 级把空间省出来，避免反复调 LLM
- Codex 的策略对**短会话**更简单——直接压缩，没有多级判断的复杂度

### 7.3 一个 Codex 的独特点：InitialContextInjection

CC 没有 `InitialContextInjection` 这个概念——它每次 turn 都重新构造 system prompt，所以 compact 后不需要担心初始上下文丢失。

Codex 用 diffing 优化（见第三篇），所以 compact 后必须显式决定"是否需要重注入"。`BeforeLastUserMessage` vs `DoNotInject` 就是这个决策的体现。

这是一个二阶复杂度——**优化（diffing）带来了新的约束（compact 后必须重注入）**。CC 用不优化换来了简单性。

![Codex vs Claude Code：压缩哲学对比](/images/opencode/codex-04-vs.png)


## 八、小结

| 你学到什么 | 对应源码 |
|-----------|---------|
| 3 种压缩实现选择 | `turn.rs:862-917` (`run_auto_compact`) |
| `should_use_remote_compact_task` 判断 | `compact.rs:66` |
| Local 实现 | `compact.rs:70` + `compact.rs:194-324` |
| Remote v1 实现 | `compact_remote.rs:45` |
| Remote v2 实现 | `compact_remote_v2.rs:56` |
| `SUMMARY_PREFIX` / `SUMMARIZATION_PROMPT` | `codex_prompts` crate |
| `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000` | `compact.rs:49` |
| `InitialContextInjection` 枚举 | `compact.rs:61-64` |
| Pre-turn 触发 | `turn.rs:150` → `turn.rs:784` |
| Mid-turn 触发 | `turn.rs:293-301` |
| Manual 触发 | `handlers.rs:834` |
| Compact 后 warning | `compact.rs:319` |
| ContextWindowExceeded 时砍最旧 | `compact.rs:251-260` |