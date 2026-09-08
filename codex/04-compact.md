---
title: Codex 压缩机制：为什么核心压缩路径要依赖 LLM？
---

# Codex 压缩机制：为什么核心压缩路径要依赖 LLM？

想象 Agent 正在处理一个复杂的 Bug，历史对话已经累积了 20 轮，包含了数百行代码和报错日志。当上下文长度不可避免地撞上模型窗口上限时，系统该如何“丢弃”信息，才能既保住 Token 预算，又不让模型丢失关键的早期需求？

Codex 的解法是：核心压缩产物依赖 LLM 语义摘要，辅以本地截断作为工程兜底。本文将顺着会话膨胀的生命周期，带你拆解 Codex 内部的 `compact` 策略，揭开它的三道防线：

1. 压缩任务是如何在 `run_turn` 中被自动触发的？
2. 为什么 Codex 要实现 Local、Remote v1、Remote v2 三套不同的压缩机制？
3. 压缩后的历史结构是如何组装的？
4. 相比 Claude Code 的多级纯文本降级策略，Codex 的压缩哲学有什么不同？

本文只讨论 Token 超限后的“压缩”策略。关于工具调用结果本身的截断策略，将留到下一篇工具系统拆解。

## 一、问题：被撑爆的 Token 预算

如果采用简单的 FIFO（先进先出）截断，直接丢弃最老的对话，模型会丢失早期的关键上下文（如用户的初始需求、早期的架构决策）。一旦这些信息丢失，模型的后续推理就会缺乏支撑。

因此，必须把长篇大论的“对话过程”变成精炼的“状态摘要”。但这个过程不能仅靠字符串截取，需要 LLM 的语义理解能力。

## 二、核心抽象 A：压缩的触发时机

在 Codex 中，压缩不是一个被动等待报错的补救措施，而是主动防御的常规管线。

### 2.1 `run_turn` 中的两道防线

在 `core/src/session/turn.rs` 中，Codex 设置了两道防线：

```rust
// 伪代码逻辑：core/src/session/turn.rs
pub(crate) async fn run_turn(...) -> Result<()> {
    // 防线一：采样前压缩
    run_pre_sampling_compact(sess).await?;
    
    // ... 组装上下文 ...
    
    loop {
        let response = run_sampling_request(sess).await?;
        
        // 防线二：自动压缩（采样后/Mid-turn）
        run_auto_compact(sess).await?;
        
        // ... 处理工具调用 ...
    }
}
```

![run_turn 中的双重压缩防线](/images/codex/04-double-defenses.svg)

- **Pre-sampling（防线一）**：在每次调用模型前，系统会调用 `auto_compact_token_status` 检查当前 Token 状态。如果达到限制（受模型配置的 limit 影响），则触发压缩。
- **Mid-turn Auto-compact（防线二）**：在单轮生成结束后，如果满足 `token_limit_reached && needs_follow_up`（即 Token 爆了，且模型还要继续调用工具或有待处理输入），则触发压缩。

### 2.2 手动触发与模型降级 (Downshift)

除了自动触发，用户也可以发送 `Op::Compact` 手动压缩。

此外，当系统发生模型切换（例如从 128K 窗口的模型切到 32K 窗口的模型）时，旧的上下文可能直接塞不进新模型。此时系统会走独立的 `maybe_run_previous_model_inline_compact` 路径，强制用旧模型先做一次压缩。

## 三、核心抽象 B：三种压缩实现的分派

Codex 根据 Provider 的支持情况和 Feature Flag，将压缩任务分派给三种不同的实现。

### 3.1 Local (Inline) 压缩

这是最基础的本地压缩（`core/src/compact.rs`）。

它的原理是：把整个历史发给模型，加上 `SUMMARIZATION_PROMPT`，要求生成摘要。如果历史太长导致压缩请求本身也报 `ContextWindowExceeded`，它会通过 `remove_first_item` 截断最早的消息并重试（这是截断机制作为兜底的体现）。

### 3.2 Remote v1 压缩

如果 Provider（如 ChatGPT）支持 `remote_compaction`，系统会走 Remote v1 路径。

它调用专门的 `compact_conversation_history` API endpoint。系统在请求前会调用 `trim_function_call_history_to_fit_context_window` 裁剪冗长的工具输出，保证请求能发出去。API 返回的直接是新的、已压缩的 transcript。

### 3.3 Remote v2 压缩 (Under Development)

Remote v2 是一个正在开发中的新机制。

它不再调用专用的 compact endpoint，而是往普通的 Responses stream 里加入一个 `CompactionTrigger`。服务端处理后，会在流中返回一个特殊的 `ResponseItem::Compaction` 节点。客户端收集到这个节点后，再在本地构造新的历史。

## 四、核心抽象 C：替换历史的结构组装

压缩完成后，旧的冗长历史会被替换。不同的实现，替换后的结构有所不同。

以 Remote v2 的 `build_v2_compacted_history` 为例：

```rust
// 伪代码逻辑：展示 Remote v2 压缩后的历史结构
fn build_v2_compacted_history(...) -> Vec<ResponseItem> {
    let mut history = Vec::new();
    
    // 1. 保留特定消息并按 Token 预算截断
    let retained_messages = truncate_retained_messages_for_remote_compaction(...);
    for item in retained_messages {
        history.push(item);
    }
    
    // 2. 将 LLM 生成的摘要节点追加到末尾
    history.push(compaction_output);
    
    history
}
```

![混合压缩历史结构](/images/codex/04-hybrid-history.svg)

注意这里的结构：它是**先保留部分原始消息（按预算截断），然后再把 `Compaction` 节点（摘要）放在最后**。

而 Local 压缩的结构则是：保留最近的用户消息子集，再追加一个包含了系统生成的摘要文本的 User Message。

无论哪种实现，Codex 都没有把所有历史全压成摘要，而是采用了“保留部分原始消息 + 摘要”的混合结构。这保证了模型既能获取早期的全局共识，又不会丢失最近的短期上下文。

## 五、关键决策：语义摘要为主，截断为辅

把 Codex 的压缩机制和 Claude Code 放在一起看，能发现不同的工程侧重。

Claude Code 采用了 5 级降级策略。前 4 级全是纯数据结构操作：截断工具输出、Snip 掉中间对话、Context Collapse。直到第 5 级，才调用 LLM 生成摘要。

Codex 的核心压缩产物（无论是 Local 还是 Remote）都依赖 LLM 生成语义摘要。纯文本截断（如 `trim_function_call_history` 或 `remove_first_item`）在 Codex 中主要作为**保障 Compact 请求本身能够成功发送的工程兜底**，而不是首选的压缩手段。

这种设计反映了 Codex 对长会话逻辑连贯性的侧重：宁可消耗 Token 调用 LLM 做语义压缩，也要尽量避免纯文本截断带来的信息断层。

## 六、总结：收拢长会话复杂度

压缩不是简单的字符串截断。

通过 Pre-turn 与 Mid-turn 的触发防线、Local/Remote 的多路实现分派，以及“保留部分原始消息 + 摘要”的混合结构，Codex 将长会话的复杂度收拢在了一套严密的管线中。

到这里，主循环和上下文管线都已经拆解完毕。Agent 已经有了大脑和记忆，接下来它需要手和脚。下一篇，我们将进入 Codex 的工具系统，看看它是如何动态发现并执行工具的。

## 七、章节小测

<script setup>
const q = [
  {
    question: '为什么简单的 FIFO（先进先出）截断策略不适合作为 Agent 上下文压缩的首选手段？',
    options: [
      '因为 FIFO 截断会导致 Rust 编译器的字符串生命周期检查失败，引发内存泄漏',
      '因为 FIFO 截断会直接丢弃最老的对话，导致模型丢失早期的关键需求和架构决策',
      '因为 FIFO 截断无法处理多模态数据，遇到图片或二进制文件时会直接崩溃',
      '因为 FIFO 截断会破坏大模型的前缀缓存机制，导致 API 调用成本大幅飙升'
    ],
    correct: 1,
    explanation: '如果直接丢弃最老的对话，模型会丢失早期的关键上下文（如用户的初始需求、早期的架构决策）。一旦这些信息丢失，模型的后续推理就会缺乏支撑，因此需要语义摘要。'
  },
  {
    question: '在 run_turn 流程中，Mid-turn 的 run_auto_compact 触发条件是什么？',
    options: [
      '当单轮生成结束后，只要系统发现模型吐出了超过 1000 个 Token 的内容就会立刻触发',
      '当系统检测到 token_limit_reached 并且 needs_follow_up（模型还要继续工具或有待处理输入）时触发',
      '当模型连续三次返回相同的工具调用（Doom Loop），系统会强制触发以打破循环',
      '当用户在终端手动输入 /compact 命令时，系统会暂停当前任务并触发'
    ],
    correct: 1,
    explanation: 'Mid-turn 压缩并不是单轮生成长了就压，而是必须同时满足 Token 爆了（token_limit_reached）且任务还需要继续（needs_follow_up），才会触发压缩以保证后续流程能走下去。'
  },
  {
    question: '在 Codex 的压缩管线中，纯文本截断（如 trim_function_call_history）主要扮演什么角色？',
    options: [
      '作为首选的压缩手段，以避免调用 LLM 生成摘要带来的昂贵 API 成本',
      '作为保障 Compact 请求本身能够成功发送给 LLM 的工程兜底机制',
      '作为专门用于处理图片和多模态二进制数据的预处理步骤',
      '作为在 TUI 界面上渲染对话历史时的显示截断逻辑，不影响发给模型的数据'
    ],
    correct: 1,
    explanation: 'Codex 的核心压缩产物依赖 LLM。纯文本截断（如裁剪冗长的工具输出或移除最早的消息）主要是在历史太长导致压缩请求本身都发不出去时，作为兜底机制使用。'
  },
  {
    question: '在 Remote v2 的 build_v2_compacted_history 中，压缩后的历史结构是如何组装的？',
    options: [
      '先将 LLM 生成的摘要节点放在最前面，然后再追加最近 N 轮的原始消息',
      '完全丢弃所有原始消息，只保留一个包含了全局摘要的 System Prompt',
      '先保留部分原始消息（按预算截断），然后再把 Compaction 节点（摘要）放在最后',
      '将摘要节点与原始消息交替穿插，以保持对话的时间线顺序'
    ],
    correct: 2,
    explanation: '在 Remote v2 的实现中，系统会先保留部分原始消息（retained_messages），然后再把 LLM 生成的 Compaction 节点追加到末尾，这与 Local 压缩的结构有所不同。'
  },
  {
    question: '对比 Claude Code 的 5 级降级策略，Codex 在压缩策略上的核心差异是什么？',
    options: [
      'Codex 追求“成本优先”，尽量使用纯数据结构操作来避免昂贵的 LLM 调用',
      'Codex 的核心压缩产物依赖 LLM 语义摘要，而 Claude Code 前 4 级全是纯数据结构操作',
      'Codex 采用多线程并发压缩技术，将压缩延迟降到最低，而 Claude Code 是单线程',
      'Codex 在压缩前会强制进行敏感词过滤和沙箱权限检查，而 Claude Code 不会'
    ],
    correct: 1,
    explanation: 'Claude Code 倾向于纯文本截断和折叠（成本优先），直到最后才调 LLM；而 Codex 的三种实现（Local, Remote v1, v2）的核心产物都是 LLM 摘要，侧重逻辑连贯性。'
  }
]
</script>

<Quiz :questions="q"></Quiz>