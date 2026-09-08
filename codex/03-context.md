---
title: Codex 上下文管线图解：几十种状态片段如何拼装才不会乱？
---

# Codex 上下文管线图解：几十种状态片段如何拼装才不会乱？

想象你的 Agent 挂载了 50 个工具、10 个自定义 Skill、连接了外部 MCP 服务器，同时还要感知当前操作系统的沙箱权限、工作目录以及用户的 `AGENTS.md` 指令。如果只是简单地把这些文本 `string.concat` 塞进 System Prompt，大模型不仅会产生严重的幻觉，还会直接击穿 Token 限制。

当上下文来源极其复杂，且包含大量动态状态时，如何组织这些信息，才能既让模型严格遵循指令，又最大化利用 Prompt Caching（提示词缓存）？

Codex 的解法是构建一条严密的“上下文装配管线”，将所有信息严格分类、分槽（Slot）注入。本文将拆解 Codex 核心引擎中的上下文管线，看看它是如何组装 Prompt 的。具体来说，我们将回答四个核心问题：

1. 为什么 Codex 要将上下文严格划分为 Developer、Contextual User 等不同的 Slot？
2. 动态的 Context Fragments（如沙箱状态、环境变量）是如何被注入的？
3. Codex 的权限说明模板是如何根据运行时策略动态变形的？
4. 相比 Claude Code，Codex 为什么选择了指令式的装配管线？

本文只讨论上下文的“组装”。当组装好的上下文超过模型窗口上限时，系统如何进行压缩，将留到下一篇拆解。

## 一、问题：被撑爆的 System Prompt

### 1.1 朴素拼接的灾难

在简单的 Demo 里，上下文通常就是 `System Prompt + History`。但对于一个工业级 Agent，环境状态是高度动态的。

如果每次对话都把所有动态状态（如当前工作目录、Shell 状态、刚刚开启的 MCP 引导指令）随意拼接到 System Prompt 的末尾，会带来两个致命问题：

第一是**注意力涣散**。核心的安全约束（“不要执行 rm -rf”）可能被淹没在冗长的环境状态描述中。
第二是**破坏 Prompt Caching**。现代 LLM 厂商（如 Anthropic、OpenAI）的缓存机制通常依赖前缀的绝对一致性。如果把高频变化的动态状态和静态的系统指令混在一起，会导致缓存命中率极低，API 成本飙升。

### 1.2 Codex 的解题思路

必须对上下文进行结构化分层。

静态的放前面，动态的放后面；系统级的核心指令归一类，用户级的环境状态归另一类。组装 Prompt 不是写作文，而是为大模型设计“内存布局”。

## 二、核心抽象 A：Prompt 的四块来源与 Slot 架构

在 Codex 中，最终发送给模型的 `Prompt` 并不是一个简单的字符串，而是一个结构体。它的来源被严格划分为四块：

1. `Prompt.base_instructions`：最底层的系统指令。
2. `Prompt.tools`：由 `ToolRouter` 统一管理的工具 Schema 列表。
3. `Prompt.input`：包含多段消息的输入流。
4. 动态注入的 Context Updates：通过 `record_context_updates_and_set_reference_context_item` 注入的完整上下文或增量 Diff。

其中，最复杂的是动态注入的上下文。Codex 将其划分为不同的 Slot（槽位），对应源码中的 `PromptSlot` 枚举：

```rust
// codex-rs/protocol/src/prompts/mod.rs
pub enum PromptSlot {
    DeveloperPolicy,
    DeveloperCapabilities,
    ContextualUser,
    SeparateDeveloper,
}
```

![Prompt 的四块来源与组装](/images/codex/03-four-pillars.svg)

这四个 Slot 构成了 Codex 的“内存布局”：

- **Developer Slots (Policy & Capabilities)**：存放优先级最高的系统指令。包括沙箱权限说明、协作模式、Apps/Skills/Plugins 的引导指令。这部分内容在一次会话中变化较少。
- **Contextual User Slot**：存放环境状态和用户偏好。比如当前的工作目录（CWD）、Shell 状态、日期/时区、网络与文件系统状态，以及用户在当前项目根目录写的 `AGENTS.md`。
- **Separate Developer Slot**：为某些需要绝对独立、防止被其他指令污染的特殊扩展片段预留。它们会被作为独立的 Developer 消息发送给模型。

![Slot 架构与内存布局](/images/codex/03-slot-layout.svg)

通过严格的分槽，Codex 保证了核心指令的权重，同时将相对稳定的 sections 聚合在一起，有利于底层模型厂商的 Prefix Caching。

## 三、核心抽象 B：Context Fragments 与动态注入

### 3.1 什么是 Context Fragment？

系统状态是动态的。比如用户中途通过命令行启用了某个 Plugin。这些状态不能硬编码在主循环里，必须插件化。

Codex 引入了 `ContextContributor` 和 `PromptFragment` 的抽象。

```rust
// 伪代码逻辑：展示 Fragment 的抽象
pub trait ContextContributor: Send + Sync {
    fn get_fragments(&self, sess: &Session) -> Vec<PromptFragment>;
}

pub struct PromptFragment {
    pub slot: PromptSlot,
    pub content: String,
}
```

![Context Fragment 的动态注入管线](/images/codex/03-fragment-injection.svg)

各个子系统都实现了这个 Trait。在需要更新上下文时，主引擎会遍历所有注册的 Contributor，收集它们产生的 `PromptFragment`，并根据 `slot` 自动追加到对应的槽位中。

### 3.2 增量更新与 Reference Context Item

为了进一步优化性能，Codex 并不是每次都全量重新发送所有上下文。

在 `run_turn` 流程中，系统会调用 `record_context_updates_and_set_reference_context_item`。它会对比当前的上下文与上一次的参考点（Reference Context Item），如果只有局部变化（比如 CWD 变了），系统会尝试仅发送 Settings Diff，而不是 Full Initial Context。这种精细的状态管理是维持长会话稳定性的关键。

## 四、核心抽象 C：权限感知的指令模板

### 4.1 动态变形的权限说明

Agent 在“只读沙箱”和“全权限沙箱”下的行为准则完全不同。如果权限说明是一段死文本，模型很容易在只读模式下尝试执行写入命令，然后被沙箱拦截，陷入死循环。

Codex 的 Developer Slot 中包含了一棵根据运行时权限动态变形的模板树。

对应到源码，在 `codex-rs/prompts/templates/permissions/` 目录下，维护了不同的 Markdown 模板：

- `sandbox_mode/workspace_write.md`：告知模型它可以自由修改文件。
- `sandbox_mode/read_only.md`：严厉警告模型当前处于只读模式，任何修改尝试都会失败。
- `approval_policy/unless_trusted.md`：告知模型哪些操作需要用户审批。

### 4.2 运行时的动态挂载

在组装 Developer Slot 时，`PermissionsInstructions::from_permission_profile` 会根据当前的 Permission Profile、Approval Policy 和 Exec Policy，动态渲染对应的模板，组合成最终的权限说明。

这种设计将安全策略（Rust 代码里的沙箱拦截）与模型认知（Prompt 里的行为准则）对齐，降低了模型因“不知情”而触发安全拦截的概率。

## 五、关键决策：指令式装配管线

把 Codex 的上下文管线和 Claude Code 放在一起看，能发现不同的工程取舍。

Claude Code 在组装 Prompt 时，更偏向函数式的拼装，通过组合不同的纯函数来生成 `systemPrompt` 和 `userContext`。

而 Codex 的上下文管线是一个典型的指令式（Imperative）长流程。它按顺序一步步查询状态、组装 `PromptFragment`、push 进对应的 `PromptSlot`。

指令式管线虽然代码冗长，但拥有极高的**可预测性**和**可调试性**。当 Prompt 出现问题时，工程师可以清晰地打断点，看是哪个 `ContextContributor` 在哪一步 push 错了数据。这也符合 Rust 偏好明确控制流的工程文化。

## 六、总结：上下文是 Agent 的内存布局

组装 Prompt 不是写作文，而是为大模型设计“内存布局”。

通过 Prompt 的四块来源划分、Slot 分区、Fragment 动态注入，Codex 保证了 Agent 在复杂环境下的稳定性。同时，权限感知的模板树让模型认知与底层沙箱策略保持了一致。

但是，无论内存布局多精妙，物理内存总有上限。当这套庞大的上下文终于击穿了模型的 Token 窗口时，Codex 会怎么做？下一篇，我们将拆解 Codex 独树一帜的压缩机制。

## 七、章节小测

<script setup>
const q = [
  {
    question: '为什么 Codex 不能简单地将所有动态状态（如当前工作目录、Shell 状态）直接拼接到 System Prompt 的末尾？',
    options: [
      '因为这会导致 Rust 编译器的字符串生命周期检查失败，引发内存泄漏',
      '因为动态状态的高频变化会破坏大模型的前缀缓存机制，导致成本飙升',
      '因为 System Prompt 的长度被硬编码限制在 1024 个字符以内，无法容纳',
      '因为动态状态包含大量不可见的控制字符，会导致大模型解析 JSON 失败'
    ],
    correct: 1,
    explanation: '现代 LLM 的 Prompt Caching 通常依赖前缀的绝对一致性。如果将高频变化的动态状态与静态指令混在一起，会导致缓存命中率极低，同时也会造成模型注意力涣散。'
  },
  {
    question: '在 Codex 的 Prompt 组装架构中，工具的 JSON Schema 是如何传递给模型的？',
    options: [
      '作为 Contextual User Slot 的一部分，与环境变量一起发送',
      '作为 Developer Policy Slot 的一部分，放在权限说明的后面',
      '作为 Separate Developer Slot 的独立消息发送，防止被污染',
      '由 ToolRouter 统一管理，并直接赋值给 Prompt.tools 字段'
    ],
    correct: 3,
    explanation: '工具 Schema 并不在 Developer Slot 中。当前工具通过 built_tools 构建 ToolRouter，再由 build_prompt 放进 Prompt.tools 字段中，与文本指令分离。'
  },
  {
    question: 'Codex 引入 ContextContributor 和 PromptFragment 机制的核心设计意图是什么？',
    options: [
      '为了将超长的历史对话记录切分成多个小片段，以便在多线程中并行发送给模型',
      '为了实现动态状态的插件化注入，使得各子系统能在运行时追加特定 Slot 的上下文',
      '为了绕过操作系统的文件读取权限限制，将敏感文件内容伪装成内存片段传递',
      '为了在模型生成代码时，将代码片段与普通文本分离，强制模型输出结构化 JSON'
    ],
    correct: 1,
    explanation: '系统状态是动态的。ContextContributor 允许各个子系统在运行时动态生成 PromptFragment，并根据其定义的 PromptSlot 自动注入到对应的槽位中，实现了高度的解耦。'
  },
  {
    question: 'Codex 为什么要根据运行时的沙箱权限（如只读模式 vs 全权限模式）动态渲染不同的权限说明模板？',
    options: [
      '为了将安全策略与模型认知对齐，防止模型在不知情的情况下尝试越权操作陷入死循环',
      '为了减少向模型发送的 Token 数量，只读模式下的模板比全权限模式的模板字数更少',
      '为了向用户隐藏系统底层的沙箱实现细节，使得 TUI 界面的渲染逻辑更加简洁',
      '为了兼容不同厂商的大模型，某些开源模型无法理解全权限模式下的复杂系统指令'
    ],
    correct: 0,
    explanation: '如果模型不知道自己处于只读沙箱中，它可能会不断尝试生成写入文件的工具调用，然后被底层沙箱拦截，导致死循环。动态渲染权限说明解决了模型认知与底层物理限制的对齐问题。'
  },
  {
    question: '在 run_turn 流程中，record_context_updates_and_set_reference_context_item 函数的主要作用是什么？',
    options: [
      '强制清空所有历史对话记录，仅保留最新的 Reference Context Item 以节省 Token',
      '对比当前上下文与参考点，如果只有局部变化，则尝试仅发送 Settings Diff 以优化性能',
      '将所有的 PromptFragment 序列化为 JSON 格式，并写入本地磁盘作为参考备份',
      '检查当前上下文是否包含恶意指令，如果发现则立即抛出异常并中断 run_turn 流程'
    ],
    correct: 1,
    explanation: '为了优化性能，Codex 并不是每次都全量重新发送所有上下文。它会对比当前的上下文与上一次的参考点，尝试仅发送增量的 Settings Diff，这种精细的状态管理是维持长会话稳定性的关键。'
  }
]
</script>

<Quiz :questions="q"></Quiz>