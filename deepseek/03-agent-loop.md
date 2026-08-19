---
title: Agent 接口与默认 loop：一个 agent 到底怎么跑一个回合
---

# Agent 接口与默认 loop：一个 agent 到底怎么跑一个回合

> 本文基于 `dsh-v0.1.0-rc.7`。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前两篇：`dsh` 是一片插件的森林，Cordis 是拼森林的框架。那么问题来了——**在这棵插件树里，真正驱动 agent"跑一个回合"的那个东西，长什么样？**

上一页已经买下伏笔：它叫 `agent-loop`，是 Cordis 树上"默认驱动"那一段，而且**明确是可替换的**。这一篇我们就走进它，把一个 agent"跑一个回合"的完整过程、以及它如何在事件流上把"模型请求 + 工具调用"组织成一个闭环，拆开看。

本篇主线是一个最具体的问题：**"跑一个回合"到底拆成几个层次、每个层次在什么时候触发什么事件、谁负责让这个循环停下来、谁又决定让它继续。** 理解了它，你就理解 `dsh` 的核心执行模型——它和 OpenCode 的 while 循环、Codex 的事件 reactor 是同一件事的三种不同活法。


## 一、step 与 turn：这个系统最基础的执行单位

连续第 2 篇我们讲过 Cordis 事件。但 `agent-loop` 真正组织工作流动靠**两个量纲不可混**的概念：step 与 turn。`docs/architecture.md` 的 Turn flow 一节开头的定义是整个系统的地基：

> A **step** is one model request plus the tools it calls. A **turn** is zero or more steps: it opens before its first input is claimed and closes once nothing is owed.

翻译并放大：

- **step（步）** = 一次模型请求 + 这次请求调的工具。模型答一次、调几个工具、拿到结果，这是"一步"。
- **turn（回合）** = **零或多步**。它在你第一次认领输入之前就"开启"，在"不再欠任何东西"时"关闭"。

这个"turn 开得早、关得晚"的设计，恰恰是它与"每输入一条就回一条"的简单 agent 的最大不同。一个 turn 可以是：

- 只走一步就完（模型直接给了答案，没调工具）；
- 走很多步（模型为了完成一件事，反复"调工具 → 看结果 → 再请求模型"）；
- 甚至是零步（被拒绝了、或被改写成空，turn 开起来但一步没花）。

### 为什么让 turn 归零也能存在？

这里要给一个设计取舍。为什么 "turn" 要独立于 "step" 单独存在？因为有一个不可回避的问题：**一个 turn 该在什么时候关？**

如果 turn 在"没输入就关"，那就不可能有模型的多步工具循环；如果 turn 等到"一定有输出才关"，那就无法取消、无法在空被拒时收场。所以 Cordis 选了"turn 打开于第一认领前、关闭于全清"，让"输入认领"与"模型调用"成为两个可拆的边界。这就是 event-driven 的好处：turn/step 的边界都是**可观测事件**，谁想注入拦截，都在这两条缝上。


## 二、turn 生命周期事件流：一步步的模型循环

`agent-loop` 跑一个 turn，实际上是在**按序派发一串事件**。这些事件构成了这个系统的"骨架主循环"，`docs/architecture.md` 画得很直白：

```text
turn/start
  claim next-step input plus one queued message
  assemble prompt sections + tool schemas
  -> agent/pre-step                   reject | enter(messages)
     step/start
     append entered messages as user/message
     derive model history from the log
     agent/request -> llm/stream -> assistant/chunk* -> assistant/message
     tool/call* -> tools/pre-execute -> tools/execute -> tools/post-execute -> tool/result*
     step/end
     tools owe another request, or next-step arrived -> claim -> next step
  -> agent/turn-stopping
turn/end
```

在这条链里，你看到两类事件被**刻意分开**：

- **持久化事件（durable session events）**：`turn/start`、`step/start`、`user/message`、`assistant/*`、`tool/*`、`step/end`、`turn/end`。这些是"事实已经发生"，会**被写进 session log**（第 4 篇专门讲`log`），是模型上下文与回放、持久化的来源。
- **活的事件（live extension points）**：`agent/*`、`llm/stream`、`tools/*` 这些是"正在跑的过程"，多半是可拦截、可改写的扩展点，不进日志。

这背后就是第 4 篇"Model-visible ⟺ logged"这条全系统最硬的设计原则的**前置**：凡是模型能看到的东西，都必须能从日志重建。而事件链正是把"运行"与"记录"分成两部分的机制。

### step 内部的工具循环

真正的"模型↔工具循环"发生在 step 内部。`agent.ts` 的 `step()` 里主循环是我们最该看的一段。它这样跑：

```ts
// packages/core/agent-loop/src/agent.ts:340-399
const stream = preparedCall?.stream(request) ?? this.loopCtx.llm.stream(request)
for await (const chunk of stream) {
  signal.throwIfAborted()
  chunkSeqs.push(this.session.append('assistant/chunk', { turn, step, chunk }).seq)
  assembler.push(chunk)
}
const toolCalls = message.content.filter(block => block.type === 'tool-call')
if (toolCalls.length === 0) return { kind: 'completed' }
const { concluded } = await executeToolCalls(...)
return concluded ? { kind: 'completed' } : null
```

这段对应上面的设计思路：

1. **流式请求**。通过 `llm.stream` 拿 chunk，每块都写进 session log（`assistant/chunk`）。
2. **如果没有 tool-call**，这次 step 直接 `completed`。
3. **如果有 tool-call**，就走 `executeToolCalls`，它返回 `concluded`；如果工具还欠另一个请求（工具自己又触发了新模型请求），循环 `while(true)` 继续，形成多步。

所以"一步"的边界，其实不是"一次模型调用"，而是"**一次模型调用 + 它带来的工具调用，一直跑到不再欠模型为止**"。


## 三、inbox：所有输入都经过一个门

agent 怎么拿到"用户消息"？它不直接从 session 读，而是通过**单 inbox（单一收件箱）**。看 `agent.ts` 的 `send()` 与三种加入方式：

```ts
// packages/core/agent-loop/src/agent.ts:122-132
followup(input: UserMessage): void {
  this.send(input, 'next-turn', true)   // 排队普通下回合消息，唤醒 driver
}
steer(input: UserMessage): void {
  this.send(input, 'next-step', true)   // 排队唤醒"下一步"的输入
}
inject(input: UserMessage): void {
  this.send(input, 'next-step', false)  // 排队上下文，不唤醒
}
```

三种输入，`InboxTarget` 与 `wakeup` 各不同：

- **followup → next-turn + 唤醒**：普通用户下一条消息，让 agent 马上动起来。
- **steer → next-step + 唤醒**：正在运行时，喂给"下一步"。
- **inject → next-step + 不唤醒**：注入上下文，**等另一个消息来唤醒后才被认领**。

最后一种（`inject`）正是"上下文注入排队"的机制：它是 `next-step` 但 `wakeup=false`，所以 idle 时它静静躺在 inbox 里，下一次 `followup` 唤醒 driver 时它才被认领。这与第 7 篇"上下文注入"里 `agent.inject()` 的行为是一致的。

`docs/architecture.md` 有一句概括：

> Input reaches the driver through **one inbox**. Some messages wake it immediately; injected context waits in the inbox until another message does.

inbox 的价值是**把"输入"从"输入源"解耦**：无论是用户、工具、子 agent 的注入还是恢复，最后都落到同一个 inbox，由 driver 统一认领，而不是每个系统各开一条路插进 loop。这给运行时提供了稳定的扩展点（`agent/inbox/*`），也一并把取消时的"撤销未认领消息"统一到一个地方。


## 四、waterfall 与 serial：拦截点在 loop 上缝出来了

第 2 篇介绍了 Cordis 的 waterfall 语义。现在看它在 agent-loop 里如何被真正用起来——**这是"在可换 loop 上做扩展"的关键机制**。

`docs/architecture.md` 指名了一族：

> `agent/pre-step`（改写/reject）、`agent/request`（配置请求）、`llm/stream`（流式请求）、`tools/*` 这些是 **waterfall**；`agent/turn-stopping` 是 **serial**（无 next，直接停）。

拆开两个最重要的：

### agent/pre-step：决定"模型这次到底看到什么"

`agent.ts` 的 `preStep()` 里，对模型历史做"认领 → 组装上下文 → 交给 waterfall 决策"：

```ts
// packages/core/agent-loop/src/agent.ts:229
const claimed = this.inbox.claim(target, position.turn)
const decision = await this.dispatch.waterfall(
  'agent/pre-step', { messages: claimed, ...position, signal },
  (): Promise<PreStepDecision> =>
    Promise.resolve<PreStepDecision>({ kind: 'enter', messages: context === undefined ? claimed : [...claimed, context] }),
)
return decision.kind === 'reject' ? decision : { ...decision, assembly }
```

waterfall 最后一个参数是**默认的 `next`**：如果没有任何监听者，`enter` 原样把 `claimed`（加 context）作为要进模型的 batch 返回。监听者可以：

- 改写 `messages`（重写 / 裁剪、注入什么）；
- 或 `{ kind: 'reject' }` 直接不执行这一步。

这是 `agent/pre-step`"决定模型看到什么"的入口。

### agent/turn-stopping：在没有 next 的情况下停 turn

与 `pre-step` 是 waterfall 不同，**`agent/turn-stopping` 是 serial 事件，没有 `next()`**。`agent.ts` 里：

```ts
// packages/core/agent-loop/src/agent.ts:295-298
if (turnEnds && this.inbox.nextStep.length === 0) {
  await this.dispatch.serial('agent/turn-stopping', { turn, signal })
}
```

它只在一个 turn "本该关" 但之前调用一次，`serial` 按注册顺序执行，谁"返回非 null/false/undefined"就停（bail）。谁监听它来**绝对停 turn**（比如"需要用户确认才能继续"），就可以返回值拦下那条结论；而普通观察者不会破坏循环。

**waterfall vs serial 的设计分工**：waterfall 表达"一道请求经过多个插件层层包装、任一层可截断"；serial（+ bail）表达"一次收尾决策、谁定案就是定案"。`dsh` 把"进 step 前的协商"用 waterfall、"turn 收尾的决定"用 serial，正好把"可多步定案"与"必须归一"两种意图焊在两个不同事件上。


## 五、取消与恢复：heavy 中断/重启的兜底

一个 `agent-loop` 处理了这么久，必然要处理"被中断"。最直观的就是**取消**。`agent.cancel(cause)` 看 `agent.ts` 的 `cancel()`：

```js
// packages/core/agent-loop/src/agent.ts:134-140
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

它分两步：清 inbox（除非 keepInbox）+ abort 当前 phase。abort 会顺着 signal 一路传播到正在跑的 stream / 工具执行 / pre-step 的 waterfall 里，每一个 `signal.throwIfAborted()` 都把取消变成"该处抛错"，最终走到 `turn/end` 的 `{ kind: 'aborted', reason }`，并把整个 driver 的 activity 收敛关闭。

而"**厚重中断 / 重启**"这条，其实是" 持久化"那一侧收到的。因为事件是 session log 里的**事实**（turn/step/user/assistant/tool），agent 死后，另一个进程可以**resume**（`resumeSessionId`）从日志重建上下文。`agent-loop` 的 plugin 里，`restoreOrCreateConfigured`/`resumeWith` 就是干这个：从一个持久化的 session id 把 agent **rehydrate** 起来，而不是从零新建。

这给一个很关键的设计点：**取消/恢复不是写一堆代码去"停/起"，而是建立在"事件日志是唯一事实源"之上**——停就是"停一下 inbox + abort"，重启就是"从日志再水合一次"，两者都不需要一个破坏性的 global 状态。


## 六、为什么 "agent-loop" 可以随便换（swappable）

批判了一路，最后收在这个最"dsh 味"的点上。我们开头就说过，`dsh` 最像 OpenCode/Codex 的地方，是它允许**换掉整个循环**。

`docs/architecture.md` 明确 `ctx` 表格里，agent 是"定义接口"，`agent-loop` 是"**默认驱动**"。这就足够回答了：如果某个产品不想用默认 loop，它完全可以：

1. 在它的 Cordis 树里 mount 一个**自己的 agent 插件**，实现 `Agent` 接口，并用 `ctx.agents.register()` 注册；
2. 不必改 `dsh` 任何一行内部代码。

再看 `core/agent` 的 README，开篇就点题：

> Agent interface, registry, process-local initiator scope, and `agent/*` event vocabulary. Every plugin (UI, hooks, orchestrators) programs against the Agent handle defined here — **it has zero loop dependency, so the loop is swappable**.

"zero loop dependency"—— 绝大多数插件只依赖 `Agent` 接口那层，不依赖 `agent-loop` 这个具体类。这一层解耦，是把"默认 loop 可换"变成可行性的关键：如果所有插件都硬依赖 `ReactLoopAgent`，那 loop 就换不了；因为它们只依赖通用 `Agent` 接口，才让你能任意换一种实现。

所以这节的结论是：**agent-loop 并不是" dsh 的心脏"，而是"dsh 的默认心脏"**。它的价值恰在于它是默认——当你要定制产品，你换的是那个"默认驱动"，而不是"整个架构"。


## 七、三终端 Agent 的同能不同构：三种主循环

到这里，把本文主角对三栏终端 agent 的主循环放一张对比表（延续全专栏的"同能不同构"）：

| 维度 | OpenCode | Codex | DeepSeek Harness（`dsh`） |
|------|----------|-------|---------------------------|
| 主循环形态 | while(true) 7 步 runLoop | 事件 reactor + SessionTask | **event-driven 插件循环（turn/step）** |
| 一次请求 → 输出 | runLoop 内一次 step | Turn 生命周期 | turn 内 step 展开工具循环 |
| 输入怎么进 | 用户消息压入 | submission 入队 | **单一 inbox（next-turn/next-step）** |
| 可拦截点 | 内建策略函数 | handler 细分 | **Cordis 事件（waterfall/serial 缝）** |
| 循环可否换 | 硬编码进二进制 | 硬编码进二进制 | **`agent-loop` swappable** |

核心差异浓缩成一句：

> **OpenCode 与 Codex 的循环硬编码进二进制，它们稳定、快，但不可替换；`dsh` 把"主循环"降级成一个可插拔的默认实现，换引擎不影响其它插件。** 三种循环没有肥瘦对错，差别在"循环是产品中心的固定件，还是框架层的一个可换件"。

再到这一篇收个尾，也把全专栏的线续上：

> `dsh` 的 agent-loop 不是"心脏"，而是"默认心脏"——它先给一个好用的默认，再把"换掉它"的权力交给每一个拿它做产品的公司。

### 下一篇：走进 session log

理解了一个 agent 怎么"跑一回合"，最硬的设计问题随之而来：**模型看到的上下文到底从哪来？为什么"能回放"与"持久化"是同一件事？** 这就走进第 4 篇——会话日志与上下文投影，`deriveMessages()` 与 "model-visible ⟺ logged" 那条全系统最硬的不变量。

## 章节小测

<script setup>
const q = [
  {
    question: '"step 是循环里的最小工作单位"，关于 step 与 turn 哪个说法正确？',
    options: ['一轮模型调用加它的工具调用；turn 是 0+ 个 step', 'step = 整个回合，turn = 单次模型调用', 'step 与 turn 完全等价，只是命名不同', 'step 只记录文本，turn 只记录工具'],
    correct: 0,
    explanation: 'step 是一个模型的请求+其工具调用；turn 是 0 或多个 step（先开于认领前、后闭于无欠。B/C/D 皆把两者概念写反或等同。'
  },
  {
    question: '为什么 `send()` 用单一 inbox 而不用各输入源直插 loop？',
    options: ['所有输入落到同一队列，拿到统一认领/撤销语义', '为了省去处理一条消息要写的少量代码', '因为 inbox 一次只容纳一条消息', '因为这样就能把消息丢掉而不被记录'],
    correct: 0,
    explanation: '单一 inbox 解耦输入源与 driver，并提供稳定的 claim/discard 语义与可取消边界。后三项只是把 inbox 当成限制/容错，不是设计动机。'
  },
  {
    question: '关于 `agent/request` 这个 waterfall 点的语义，下面哪句最准确？',
    options: ['多个插件串行改写同一份请求，末尾有默认 next 兜底', '它是写死在主循环内部、不可拦截的逻辑', '它只做观察，不对请求做任何影响', '它和 `serial` 语义完全等价'],
    correct: 0,
    explanation: '`agent/request` 是 waterfall：监听者可逐层改写请求配置，最后兜底到默认 next；可委托、可截断，与 serial（一次 bail 定案）不同。'
  },
  {
    question: '为什么让 turn 在"首次认领前"开、在"无欠项"后关？',
    options: ['为了能驱动多步工具/模型循环，也记空回合', '为了减少整体模型调用次数', '因为 turn 至少要执行一次 step', '因为 turn 必须保证有输出才闭'],
    correct: 0,
    explanation: 'turn 开于首次认领前、闭于无欠，才有空间容纳多步模型/工具循环、以及一个零 step 的空回合；其余选项把 turn 的必要性理解偏了。'
  },
  {
    question: '为什么不把 `agent/turn-stopping` 做成瀑布（pre-step 那种），而是 serial？',
    options: ['收尾是定案点：按序执行，遇 bail 即停，无 next 链可改', '因为 turn 收尾不需要任何事件参与', '因为 serial 比 waterfall 更好写成', '因为模型无法接收瀑布调用'],
    correct: 0,
    explanation: '`agent/turn-stopping` 是 turn 的收尾定案，serial 按顺序+遇 bail 停，无 next()，是"归一"而非"委托"；pre-step 需要多插件改写才用 waterfall。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
