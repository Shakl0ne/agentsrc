---
title: Claude Code 主循环源码拆解：一个 while(true) 为什么要写七条 continue
---

# Claude Code 主循环源码拆解：一个 while(true) 为什么要写七条 continue

上一篇拆完 51 万行的整体分层，这一篇下潜到心脏：主循环。它住在三个文件里——`screens/REPL.tsx`（5,005 行）是交互模式的会话编排层，`QueryEngine.ts`（1,295 行）是 headless 模式的会话管家，`query.ts`（1,729 行）是两者共用的循环本体。

先看一个反复出现的场景：你让 agent 修一个测试失败的模块，它读文件、改代码、跑测试，测试又挂了，它接着改，来回七八轮才收敛。整个过程你只按了一次回车。驱动这七八轮往返的，就是主循环——每个终端编程 Agent 的心脏，接收输入、调模型、执行工具、决定是否继续。

把它写出来似乎不难：`while(true)`，调 API，有工具调用就执行，没有就退出。真到生产环境，这个朴素循环要处理的状况远不止「有工具/没工具」：上下文超限了要压缩重来、输出截断了要续写、stop hook 把模型的「完成」打回重改、模型过载了要换一个重发、用户指定「+500k tokens 跑满」没跑满还得继续。`queryLoop` 里显式写出来的 `continue` 有七条，每条都要构造或重置状态、跟其他路径互不打架、还不能绕成死循环——这是 `query.ts` 那 1,729 行的核心内容。（瞬态网络错误的重试留在 API 层、用户中断走 abort 信号，不占这七条，分别在第五、六节展开。）

本文沿着一条请求的生命周期拆这套机制。你会看到三个问题的答案：

- 第一，REPL、QueryEngine、`query()` 三个角色怎么分工，交互和 headless 怎么共享同一套循环；
- 第二，循环靠什么判断该继续还是该退出，七条 continue 路径各自处理什么问题；
- 第三，循环进行中用户又敲了一句话，会排队、回灌还是打断。

压缩管线的内部实现是第四篇的主角，权限系统的完整拆解放在第六篇，这里只在循环碰到它们时点到为止。

主循环也不是进程一启动就在跑的。它是启动装配线的末端产物——环境装好、REPL 挂上去，控制权才交到循环手里：

![Claude Code 启动时序：串行装配线上的并行预取与异步 MCP](/images/claudecode/02-startup-sequence.svg)

## 一、三个角色：REPL、QueryEngine 和 query()

CC 的主循环不是一个函数，是三个角色的协作。引擎是 `query.ts` 导出的 `query()`，一个 async generator；它有两个消费者：交互模式下，`screens/REPL.tsx` 直接 `for await` 它；headless 模式（`claude -p` 和 SDK）经 `QueryEngine.ts` 的 `submitMessage` 间接调用。在 REPL.tsx 里 grep 不到一处 QueryEngine——交互路径根本不经过它。

这个切分把循环逻辑从运行环境里剥了出来：终端 UI 和 SDK 会话消费同一个生成器，测 `query()` 不需要真实的文件系统，也不需要 React。启动完成后的控制权交接，落到代码里就是这个结构：REPL 挂载完毕，从这一刻起由它驱动 `query()`。

### 1.1 REPL：交互模式的驱动者

REPL 每轮做三件事：把用户输入变成消息、调 `query()` 消费事件流、turn 结束时收尾。调用点本身很薄：

```ts
// src/screens/REPL.tsx:2793
for await (const event of query({
  messages: messagesIncludingNewMessages,
  systemPrompt,
  userContext,
  systemContext,
  canUseTool,
  toolUseContext,
  querySource: getQuerySourceForREPL()
})) {
  onQueryEvent(event);
}
```

系统提示词在调用前现场组装——`buildEffectiveSystemPrompt`（`REPL.tsx:2781`）每轮重拼一遍，最新的工具列表、MCP 状态、自定义 prompt 都赶在发起前就位。REPL 自己不维护循环状态，它只负责把 `onQueryEvent` 吐出的事件渲染到终端。

并发上也有一道守卫。`onQuery` 入口先过 `queryGuard.tryStart()`：

```ts
// src/screens/REPL.tsx:2866-2886（省略部分）
// Concurrent guard via state machine. tryStart() atomically checks
// and transitions idle→running, returning the generation number.
// Returns null if already running — no separate check-then-set.
const thisGeneration = queryGuard.tryStart()
if (thisGeneration === null) {
  // 一轮 query 还在跑，新输入不进循环，改成排队
  newMessages
    .filter((m): m is UserMessage => m.type === 'user' && !m.isMeta)
    .map(_ => getContentText(_.message.content))
    .filter(_ => _ !== null)
    .forEach((msg, i) => {
      enqueue({ value: msg, mode: 'prompt' })
    })
  return
}
```

`tryStart()` 返回 null，意味着上一轮还没结束。这里的处理是排队：新输入不进循环，进队列。排队之后发生什么，是第六节的整节话题；先记住一个事实——REPL 拒绝并发开启第二轮，靠的是一个原子状态机，`tryStart()` 一步完成检查和置位，没有先查再设的窗口。

### 1.2 QueryEngine：headless 模式的会话管家

`QueryEngine` 是给 SDK 和 `claude -p` 用的。每个实例管一场会话：消息历史、usage 累积、权限拒绝记录，外加用户输入预处理和 system prompt 组装。`submitMessage` 也是个 async generator，签名把输入输出都放在明面上：

```ts
// src/QueryEngine.ts:209
async *submitMessage(
  prompt: string | ContentBlockParam[],
  options?: { uuid?: string; isMeta?: boolean },
): AsyncGenerator<SDKMessage, void, unknown>
```

主线压缩成五步：

1. 包装 `canUseTool` 追踪权限拒绝——每次工具被拒记进 `permissionDenials`，最终随 result 消息返回给 SDK 调用方；
2. 组装 system prompt（默认、自定义、append、memory prompt 的组合）；
3. 处理用户输入——解析斜杠命令、创建用户消息、处理附件；
4. transcript 先落盘。顺序有讲究：进程若在 API 响应前被杀，transcript 里至少有用户消息，`/resume` 才有东西可恢复；
5. 调用 `query()` 并 `for await` 消费流。

第五步是两个消费者的共同交接点：

```ts
// src/QueryEngine.ts:675
for await (const message of query({
  messages,
  systemPrompt,
  userContext,
  systemContext,
  canUseTool: wrappedCanUseTool,
  toolUseContext: processUserInputContext,
  fallbackModel,
  querySource: 'sdk',
  maxTurns,
  taskBudget,
})) {
  // 按 message.type 分发处理
}
```

循环产出的每条消息——流式文本块、工具调用、压缩边界、API 错误——都从这个 `for await` 流过。`QueryEngine` 对它们做四类处理：`assistant` 消息 push 进 `mutableMessages` 并 fire-and-forget 写 transcript；`stream_event` 在 `message_start` 重置单条消息 usage、`message_delta` 累积、`message_stop` 并进总量；`attachment` 处理 `max_turns_reached` 和结构化输出；`system` 处理压缩边界和 API 错误分类。

### 1.3 不设默认 turn 上限的底气

`QueryEngineConfig` 里 `maxTurns` 是可选字段，不设就没有上限——循环跑到模型返回 `end_turn` 或出错为止。多数人的直觉是给个硬限兜底，CC 信任模型自己收尾。

turn 上限主要用在子 Agent 上，`src/tools/AgentTool/forkSubagent.ts:65` 硬编码了 200。交互式 REPL 里用户随时 Ctrl+C，不设上限没有风险；无人值守的 SDK 场景才是隐患——模型反复调同一个工具不换思路，没有上限就是无限循环。CC 的应对分三层：SDK 调用方显式传 `maxTurns`；会话层用 `maxBudgetUsd` 做成本兜底，超预算立刻终止；工具层用 denial tracking 记录被拒绝的调用。三道防线各自独立，任何一道生效都能拦住失控。

### 1.4 ask()：headless 的最短路径

文件末尾的 `ask()` 干的事更直接：创建临时 `QueryEngine`，跑一次 `submitMessage`，销毁。`claude -p` 走的就是这条路径，调用方在 `cli/print.ts`。交互式 REPL 和 headless 单次调用共享同一套循环逻辑，差异只在会话生命周期的管理方式上。

## 二、queryLoop：while(true) 的骨架

进入 `query.ts`。导出的 `query()` 是薄包装，唯一额外工作是循环正常返回后把过程中消费的命令标记为 `completed`——这个通知让 UI 的命令进度条知道哪些命令处理完了。循环抛异常或被 `.return()` 中断，通知不执行，命令停在 `started` 状态。

真正干活的是 `queryLoop`（`query.ts:241`）。跨迭代的状态收在一个 `State` 结构里：

```ts
// src/query.ts:204
type State = {
  messages: Message[]                    // 消息历史，跨迭代累积
  toolUseContext: ToolUseContext
  autoCompactTracking: AutoCompactTrackingState | undefined
  maxOutputTokensRecoveryCount: number   // 输出恢复计数，上限 3
  hasAttemptedReactiveCompact: boolean   // 响应式压缩已尝试标志
  maxOutputTokensOverride: number | undefined
  pendingToolUseSummary: Promise<...> | undefined
  stopHookActive: boolean | undefined
  turnCount: number                      // 当前 turn 数
  transition: Continue | undefined       // 上一次 continue 的原因
}
```

写法上有讲究：恢复路径的 `continue` 构造一个全新的 `State` 赋给 `state`，不在旧对象上原地改。源码注释说明了动机——continue 站点写 `state = { ... }`，替代九个分散的赋值语句，迭代顶部解构让读取保持裸名。读代码时每个 continue 位置的状态变更是一次显式赋值，不用追全局变量的修改历史。`transition` 字段记录「为什么继续」，测试不用翻消息内容就能断言哪条恢复路径被触发过。

循环体主干长这样（大幅省略）：

```ts
// src/query.ts:307（大幅省略）
while (true) {
  const { messages, turnCount, ... } = state   // 顶部解构
  yield { type: 'stream_request_start' }

  // 压缩管线：snip → microcompact → collapse → autocompact
  let messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]

  for await (const message of deps.callModel({...})) {   // 流式调用
    yield message
    if (message.type === 'assistant') {
      // 有 tool_use 块 → needsFollowUp = true
    }
  }

  if (!needsFollowUp) {
    return { reason: 'completed' }   // end_turn：过 stop hooks
  }

  // 执行工具，收集结果，检查 maxTurns
  state = {
    messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
    turnCount: nextTurnCount,
    transition: { reason: 'next_turn' },
  }
}
```

压缩管线放在迭代开头而非出错之后，是预防性设计——每次调 API 前先检查要不要压，不等 413 打回来才反应。管线顺序：snip 裁历史冗余、microcompact 按 tool_use_id 压工具结果、contextCollapse 折叠连续同类消息、autocompact 全量摘要。四级之间不是互斥开关——snip 和 microcompact 可以同轮都跑，源码注释写明两者「not mutually exclusive」；跳过关系在 collapse 与 autocompact 之间：collapse 把上下文压到阈值以下，全量摘要就不再执行，粒度更细的上下文得以保留。

`using pendingMemoryPrefetch = ...` 这个写在函数顶部的声明，用的是 TC39 Explicit Resource Management 提案（Bun 原生支持），离开作用域自动调 `[Symbol.dispose]()`，相当于 Rust 的 Drop。generator 的退出路径有三种——正常返回、抛异常、被外层 `.return()` 中断——没有 `using` 就要在每条路径手动清理，漏一处就是泄漏。

## 三、退出信号：一个布尔标志的权威性

循环什么时候停，是整个设计里最容易被写错的地方。Anthropic API 的非流式响应带 `stop_reason` 字段，看起来是天然的退出依据。CC 不用它，源码注释直接说破了原因：

```ts
// src/query.ts:553-558
// Note: stop_reason === 'tool_use' is unreliable -- it's not always set correctly.
// Set during streaming whenever a tool_use block arrives — the sole
// loop-exit signal. If false after streaming, we're done (modulo stop-hook retry).
const toolUseBlocks: ToolUseBlock[] = []
let needsFollowUp = false
```

`needsFollowUp` 的置位逻辑跟着流走：

```ts
// src/query.ts:826-835（略删）
if (message.type === 'assistant') {
  assistantMessages.push(message)
  const msgToolUseBlocks = message.message.content
    .filter(c => c.type === 'tool_use')
  if (msgToolUseBlocks.length > 0) {
    toolUseBlocks.push(...msgToolUseBlocks)
    needsFollowUp = true   // 唯一的前向继续信号
  }
}
```

整条消息流完之后它是 false，说明模型没有任何工具调用，任务完成；是 true，执行工具然后继续。判定依据从「模型声明的结束原因」换成了「模型实际发出的动作」，前者在流式场景下不可靠，后者是刚性的——有 tool_use 块就必须有对应的 tool_result 回填，这是 API 的硬约束。

流式场景还有个隐蔽的坑：`stop_reason` 不在 content block 事件里到达，它在 `message_delta` 事件里。`QueryEngine.ts:802` 的注释专门记录了这个时序——assistant 消息在 `content_block_stop` 时被 yield 出去，此时 `stop_reason` 还是 null，真值要等 `message_delta` 到达再补。假设 `stop_reason` 在消息产出时就可用，拿到的一定是 null。

退出不依赖 `stop_reason`，流式工具执行因此有了提前量——`tool_use` 块一到就能注册执行，不用等流收尾。

## 四、七条 continue：没走通的回合怎么折返

先澄清计数。正常回合不走 `continue`：工具结果回灌后构造新的 `State`（`query.ts:1715-1727`），while 体落底，自然进入下一轮。显式写 `continue` 的地方，处理的都是没走通的回合——这一轮失败了、被截断了、或者没跑够，要重来。这样的路径恰好七条：

| 路径 | transition.reason | 触发条件 |
|---|---|---|
| 模型切换 | （裸 continue，不换 State） | 529 连续 3 次过载，换 fallback 模型重发 |
| 折叠排空 | `collapse_drain_retry` | 流式响应 413，先排空已暂存的 context-collapse |
| 响应式压缩 | `reactive_compact_retry` | 413 或媒体超限，全量摘要压缩后重试 |
| 输出上限升级 | `max_output_tokens_escalate` | 默认输出上限不够，升到 64k 重试同请求 |
| 输出续写 | `max_output_tokens_recovery` | 64k 也不够，注入「接着写」消息，最多 3 次 |
| stop hook 阻断 | `stop_hook_blocking` | stop hook 返回阻断错误，注入错误让模型修正 |
| 预算续跑 | `token_budget_continuation` | 用户指定的 token 预算未跑满，注入 nudge 继续干活 |

七条不都长一个样。恢复类的六条走同一个套路：构造带 `transition.reason` 的新 `State`，把「为什么绕回来」记进状态。模型切换是例外——它不碰 `State`，直接把这一轮攒了一半的累积变量清零，换上 fallback 模型重发整个请求。这条 `continue` 还有个落点细节：它在 `while (attemptWithFallback)` 这个内层 API 重试循环（`query.ts:654`）的 catch 块里，跳回的是内层循环的顶部——同一轮迭代内换模型重发，不重跑压缩管线，重试成功后迭代照常往下走：

```ts
// src/query.ts:894-907（省略部分）
if (innerError instanceof FallbackTriggeredError && fallbackModel) {
  currentModel = fallbackModel
  attemptWithFallback = true

  // 清空本轮累积：换模型意味着整个请求作废重发
  yield* yieldMissingToolResultBlocks(
    assistantMessages,
    'Model fallback triggered',
  )
  assistantMessages.length = 0
  toolResults.length = 0
  toolUseBlocks.length = 0
  needsFollowUp = false
```

换模型前有个清理动作：`yieldMissingToolResultBlocks` 先给已发出的 `tool_use` 补上合成的 `tool_result`，再清空累积。跳过这一步，重发的请求历史里就有悬空的 `tool_use`，API 直接 400。

逐条看两条有代表性的。

**输出续写**。模型输出被 `max_output_tokens` 截断时，CC 先试升级到 64k（`ESCALATED_MAX_TOKENS`，`utils/context.ts:25`）重发同一请求；64k 还不够，就注入一条 meta 消息让模型从断点接着说：

```ts
// src/query.ts:1224
const recoveryMessage = createUserMessage({
  content: 'Output token limit hit. Resume directly — no apology, '
         + 'no recap of what you were doing. Pick up mid-thought if '
         + 'that is where the cut happened. Break remaining work into '
         + 'smaller pieces.',
  isMeta: true,
})
```

这条消息带 `isMeta: true`——对模型可见，不计入用户可见的对话历史。提示词的措辞也讲究：「no apology, no recap」，直接防住模型看到「输出超限」后的道歉和复述倾向，让它从断点续写。计数器 `maxOutputTokensRecoveryCount` 上限 3（`MAX_OUTPUT_TOKENS_RECOVERY_LIMIT`），3 次后放弃恢复，把扣留的错误放行出去。

**stop hook 阻断**。模型说 `end_turn` 之后，stop hooks 有权拦截这个「完成」决定，注入错误让模型修正。这条路径藏着一个实践出来的死循环教训：

```ts
// src/query.ts:1292-1297
// Preserve the reactive compact guard — if compact already ran and
// couldn't recover from prompt-too-long, retrying after a stop-hook
// blocking error will produce the same result. Resetting to false
// here caused an infinite loop: compact → still too long → error →
// stop hook blocking → compact → … burning thousands of API calls.
hasAttemptedReactiveCompact,
```

把 `hasAttemptedReactiveCompact` 重置为 false，循环就是：压缩 → 还是太长 → 错误 → stop hook 阻断 → 再压缩 → ……注释里说这个过程烧掉了「thousands of API calls」。修复方式是保持标志位，让 stop-hook 阻断路径不再触发已经失败过的压缩。七条路径之间不是孤岛，这类标志位就是路径间的协调机制。

**预算续跑**是七条里最特别的一条：别的路径在处理错误，它在处理「太早成功」。用户在输入里写 `+500k` 或「use 2M tokens」（`utils/tokenBudget.ts` 的正则解析这些写法），系统就把这个数字设为本次 turn 的输出 token 目标。模型中途说完成了但只用了 20%，循环注入一条 nudge 消息——「Stopped at 20% of token target. Keep working — do not summarize.」——让模型继续干活。防失控的逻辑在 `query/tokenBudget.ts`：完成阈值 90%（`COMPLETION_THRESHOLD`），且连续 3 次续跑、每次增量低于 500 token（`DIMINISHING_THRESHOLD`）时判定为边际收益递减，停止续跑。子 Agent 不参与这套机制。

七条路径共用同一个 `State` 结构，靠两种手段避免互相污染：计数器（`maxOutputTokensRecoveryCount`、`turnCount`）限制单条路径的重试次数；标志位（`hasAttemptedReactiveCompact`、`stopHookActive`）在路径之间传递「这条路已经试过」的信息。正常回合每次都会重置恢复相关的字段（`query.ts:1720-1721`）——正常回合天然是干净的状态。

## 五、流式工具执行与错误扣留

两个设计直接压掉了「模型说话」和「工具执行」之间的串行等待，值得单独拆。

### 5.1 StreamingToolExecutor：工具不等模型说完

`needsFollowUp` 在流式过程中置位，工具执行同样可以提前。`StreamingToolExecutor`（530 行）让工具在模型还在输出时就开始跑：`tool_use` 块一到就 `addTool()` 注册，`getCompletedResults()` 同步弹出已完成的工具结果。循环内的消费点：

```ts
// src/query.ts:837-844
if (
  streamingToolExecutor &&
  !toolUseContext.abortController.signal.aborted
) {
  for (const toolBlock of msgToolUseBlocks) {
    streamingToolExecutor.addTool(toolBlock, message)
  }
}
```

效果：模型输出到第 3 个 `tool_use` 块时，前 2 个工具可能已经执行完、结果已经收好。流结束后剩下的工具走 `getRemainingResults()` 收尾。

工具之间的并发有自己的规矩，两套调度器遵循同一条规则：按 `isConcurrencySafe` 分区，只读工具可并行、写操作独占。非流式的 `runTools` 路径用 `partitionToolCalls`（`toolOrchestration.ts:91`）分批，并发批上限 10（环境变量 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 可调）；`StreamingToolExecutor` 在注册时逐个判定并发安全性（`StreamingToolExecutor.ts:105`），靠 `canExecuteTool` 维持互斥。对比 OpenCode 每轮工具串行执行、Codex 并行串行混合，CC 把并行点提前到了流式过程中——「模型说完才动手」的等待被消掉了。这个能力由 `config.gates.streamingToolExecution` 控制，关闭时回退到 `runTools` 常规路径。

流式执行有个额外收益：中断时不留孤儿 `tool_use`。abort 之后 `getRemainingResults()` 会为排队中和执行中的工具生成合成的 `tool_result` 块，保证每个 `tool_use` 都有配对——这是 API 的硬约束，缺了配对下一轮请求直接 400。

### 5.2 扣留机制：先试恢复，再报错误

流式过程中出现的可恢复错误（prompt-too-long、max-output-tokens、媒体超限），CC 不立即 yield 给外层，先扣下来：

```ts
// src/query.ts:799-825（简化）
let withheld = false
if (contextCollapse?.isWithheldPromptTooLong(message, ...)) {
  withheld = true
}
if (reactiveCompact?.isWithheldPromptTooLong(message)) {
  withheld = true
}
if (mediaRecoveryEnabled &&
    reactiveCompact?.isWithheldMediaSizeError(message)) {
  withheld = true
}
if (isWithheldMaxOutputTokens(message)) {
  withheld = true
}
if (!withheld) {
  yield yieldMessage
}
```

扣住之后尝试恢复——413 先排空 staged collapses（粒度细、信息保留多），不行再全量摘要压缩；输出截断按上一节的升级/续写处理；媒体超限走响应式压缩的 strip-retry。恢复成功，这条错误消息永远不会到达用户眼前；恢复失败，错误才被 yield 出去。

这个设计的边界也值得看清。它让体验干净，但恢复路径有 bug 时用户完全不知道中间发生了什么——内部靠 `transition.reason` 记录路径供日志和测试断言，外部对中间错误不可见。扣留逻辑还有个必须对齐的细节：扣留侧和恢复侧的 feature gate 必须同值（源码在流循环前把它 hoist 成 `mediaRecoveryEnabled`，注释强调「withhold-without-recover would eat the message」）——一侧启用一侧没启用，扣住的消息就凭空消失了。

### 5.3 withRetry：另一层的重试

扣留机制之外，API 调用还包着一层 `withRetry`（822 行），处理瞬态错误：401/403 刷新 OAuth token 重来，429 按 `retry-after` 指数退避，连接断开禁用 keep-alive 重连，最多 10 次（`DEFAULT_MAX_RETRIES`，`withRetry.ts:52`）。529 过载特殊——连续 3 次（`MAX_529_RETRIES`，`withRetry.ts:54`）后若配了 fallback 模型，抛 `FallbackTriggeredError`，由循环层捕获并换模型重发——也就是第四节表格的第一条 continue。

529 的重试资格还有一道筛选，`FOREGROUND_529_RETRY_SOURCES`（`withRetry.ts:62`）只放行用户正在等待的请求来源（REPL 主线程、SDK、agent、compact 等）；标题生成、建议生成这类后台任务直接放弃。注释给了理由：容量级联期间每次重试是 3-10 倍的网关放大，而这些失败用户根本看不到。服务过载时优先保前台体验，牺牲后台任务——一个面向生产容量现实的取舍。

分层之后整个错误处理图景是：`withRetry` 管瞬态错误（网络、限流、过载），循环层的 continue 路径管结构性错误（上下文溢出、输出截断、hook 阻断），两层各管一段，互不越界。

## 六、循环进行中，用户又敲了一句：排队为主，打断为例外

前五节都在看循环内部，还有一类事件从外面进来：模型跑了三分钟没停，用户等不及又敲了一句话。打断吗？

CC 的答案写在 1.1 节那段守卫代码里：不打断，排队。队列本体在 `utils/messageQueueManager.ts`——一个模块级的 `commandQueue`，完全独立于 React 状态，用 `useSyncExternalStore` 订阅。排队项带优先级，`now > next > later`：用户输入是 `next`，任务通知是 `later`，`now` 留给特殊来客。

排队之后有两次消费机会。第一次在 turn 内部：每轮工具执行完，`queryLoop` 从队列取快照，把排队的用户输入变成 attachment 塞进 toolResults 回灌给模型（`query.ts:1570-1590`）——模型下一轮就知道用户补充了什么，不用等整个 turn 结束。斜杠命令被明确排除在这条通道外，源码注释写着「must go through processSlashCommand after the turn ends」，命令有自己的处理管线：

```ts
// src/query.ts:1570-1578（略删）
const queuedCommandsSnapshot = getCommandsByMaxPriority(
  sleepRan ? 'later' : 'next',
).filter(cmd => {
  if (isSlashCommand(cmd)) return false
  if (isMainThread) return cmd.agentId === undefined
  // Subagents only drain task-notifications addressed to them
  return cmd.mode === 'task-notification' && cmd.agentId === currentAgentId
})
```

这段代码还交代了队列的归属：进程级单例，coordinator 和所有 in-process subagent 共用，每个循环只取走「寄给自己」的那部分——主线程拿 `agentId` 为空的，子 agent 只拿发给自己的 task-notification，用户 prompt 永远只进主线程。

第二次在 turn 结束：`useQueueProcessor`（`hooks/useQueueProcessor.ts`）盯着 `queryGuard` 的状态，`isQueryActive` 为 true 时直接 return，空闲后才把队列交给 `processQueueIfReady` 冲刷。排队命令的完整生命周期就是：turn 内能塞就塞进模型上下文，塞不进就等 turn 结束再执行。

打断只留给 `now` 优先级。REPL 有个 useEffect 监控队列，出现 `priority === 'now'` 的命令（比如 chat-UI 客户端经 UDS 发来的消息）时调 `abortControllerRef.current?.abort('interrupt')`（`REPL.tsx:4100-4104`）。abort 的 reason 是字符串 `'interrupt'`——query 内部会检查 `signal.reason`，reason 为 `interrupt` 时不注入「用户中断了」的提示消息（`query.ts:1046-1050`），因为紧随其后的用户消息自带上下文，再插一条中断声明是噪音。

普通的中断（Ctrl+C、SDK 取消）走同一个 `AbortController`，信号沿三条路径传播。第一条进流式 API：`callModel` 把 signal 透传给 SDK（`query.ts:664`），SDK 收到 abort 中断 SSE 流，`for await` 抛出。第二条是循环层守卫：多个 continue 路径前检查 `signal.aborted`，避免 abort 后还发起新一轮 API 调用。第三条落在权限检查上，那里也要感知中断：

```ts
// src/hooks/useCanUseTool.tsx:34
if (ctx.resolveIfAborted(resolve)) {
  return  // 已 abort，立即短路返回，不再继续权限流程
}
```

`useCanUseTool.tsx` 在权限流程的每个 await 点都调 `resolveIfAborted`，一共 5 处。权限检查是异步的——等用户点确认、等分类器预判——用户在等待期间按了 Ctrl+C，权限流程必须立即短路，而不是等确认流程走完才退出。加上 `using` 声明兜底清理（第二节），这套「一个信号源 + 多点守卫 + 自动清理」让中断在任何时机都能安全落地，不留半执行的工具调用。

权限检查在循环中的位置：工具执行前先过 `canUseTool`，返回 allow / deny / ask 三种行为，ask 走交互式确认并暂停循环（`toolExecution.ts:916-930`）。七种权限模式怎么改变 ask 的判定，是第六篇的主菜。

## 七、三种主循环放一起看

CC、OpenCode、Codex 的主循环摆在一起，差异集中在「流程控制权在谁手里」：

| 维度 | Claude Code | OpenCode | Codex |
|------|------------|----------|-------|
| 循环模式 | continuation-driven | while-true 显式步骤 | event-driven reactor |
| 流程控制 | API 响应驱动分支 | 7 步确定性序列 | channel 消息分发 |
| 异常处理 | 循环内 7 条 continue 路径 | Effect-TS Channel | supervisor + thread rollback |
| Turn 限制 | 可选，默认无上限 | Doom Loop 检测 | 可配置 |
| 工具执行 | 流式提前启动 | 每轮串行 | 并行串行混合 |

**OpenCode** 的 `runLoop` 每次迭代跑固定的 7 个步骤，流程走向写在代码里，可预测性强。代价是灵活性——异常情况要么插进步骤序列变成分支，要么单独处理。它适配 Effect-TS 的 generator 语义，每步是可组合可中断的 Effect。

**Codex** 连显式循环都没有：`submission_loop` 从 Tokio channel 拿 `Submission` 消息，按 `Op` 类型 match 分发到 20 多个 handler。解耦彻底，代价是流程整体感被打散——理解一次完整 turn 要在多个 handler 之间跳转。

**CC** 取中间态：有显式的 `while(true)`（像 OpenCode），但分支由 API 响应内容驱动（像 Codex 的消息驱动）。七条 continue 路径相当于七个内联的 handler，只是触发方式不是消息分发，而是在同一个函数体里检查响应内容。住在 1,729 行文件里的 `queryLoop` 是三者最大的单文件循环，复杂度集中的回报是控制流线性可读——从头读到尾，每个 `continue` 和 `return` 的位置都可见，不用跨函数追。

集中也有代价。新能力（TOKEN_BUDGET、CONTEXT_COLLAPSE、HISTORY_SNIP）持续往循环里加 continue 路径，`feature()` 编译期裁剪缓解了包体积，但函数本身的复杂度在涨。要是路径继续增加，往 Codex 式的分发结构重构是可预见的方向。

## 八、要点回顾

1. **三个角色共享一个生成器**：REPL 直接消费 `query()`，QueryEngine 服务 headless；循环逻辑与运行环境解耦，两个环境一套循环。
2. **退出信号是 `needsFollowUp` 布尔标志**：`stop_reason` 在流式场景不可靠，判定依据落在模型实际发出的 tool_use 块上。流式工具执行是它的自然延伸。
3. **七条 continue 各管一种没走通的回合**：六条构造带 reason 的新 State，模型切换在内层重试循环里清零累积、换模型重发；计数器限次、标志位跨路径协调、`transition.reason` 供断言。`hasAttemptedReactiveCompact` 的保持是死循环教训的直接产物。
4. **流式工具执行压掉串行等待**：工具不等模型说完，`tool_use` 块一到就注册执行；两套调度器同按 `isConcurrencySafe` 分区；abort 时合成 tool_result 保证配对完整。
5. **错误处理分层**：`withRetry` 管瞬态，continue 路径管结构性，扣留机制让可恢复错误对用户不可见。
6. **输入排队为主、打断为例外**：统一队列三级优先级，turn 内 attachment 回灌、turn 后冲刷，只有 `now` 才 abort；一个 AbortController 加多点守卫，中断处处安全落地。

下一篇拆工具系统——40 多个内置工具的接口设计、`checkPermissions` / `isReadOnly` / `validation` 的三态模型，MCP 扩展怎么融入这套体系。循环决定了「什么时候调工具」，工具系统决定「工具怎么执行、结果怎么回来」，两边合起来才是完整的执行链路。

## 章节小测

<script setup>
const q = [
  {
    question: '`queryLoop` 判断循环是否继续的依据是什么，为什么不用 `stop_reason`？',
    options: [
      "'用 stop_reason 字段，API 契约保证其可靠性'",
      "'用 needsFollowUp 标志，stop_reason 流式下不可靠'",
      "'用 turnCount 计数，到达默认上限即退出'",
      "'用 message_stop 事件，它总是最后到达'"
    ],
    correct: 1,
    explanation: '源码注释明确 stop_reason === tool_use 在流式场景设置不一致，不可靠。CC 改用 needsFollowUp 布尔标志：流式处理中看到 tool_use 块就置位，流结束后为 false 才终止。判定依据从模型声明的结束原因换成模型实际发出的动作。'
  },
  {
    question: 'stop_hook_blocking 路径必须保持 hasAttemptedReactiveCompact 标志，原因是？',
    options: [
      "'保持 TypeScript 类型系统对 State 的完备性检查'",
      "'避免向遥测系统重复上报压缩事件'",
      "'防止压缩失败后重试陷入死循环烧掉大量 API 调用'",
      "'让 stop hook 能识别上一轮的压缩产物格式'"
    ],
    correct: 2,
    explanation: '重置为 false 会导致：压缩 → 还是太长 → 错误 → stop hook 阻断 → 再压缩 → ……的死循环，注释记录曾烧掉数千次 API 调用。保持标志让 stop-hook 阻断路径不再触发已失败的压缩，这是路径间协调机制。'
  },
  {
    question: 'StreamingToolExecutor 带来的核心改变是？',
    options: [
      "'把多个工具的执行从串行改为完全并行'",
      "'工具在模型流式输出过程中就开始执行'",
      "'将工具结果缓存后批量回填给下一次请求'",
      "'把权限检查移到工具执行完成之后进行'"
    ],
    correct: 1,
    explanation: 'tool_use 块一到就 addTool 注册执行，getCompletedResults 同步弹出已完成结果——模型输出第 3 个工具块时前 2 个可能已执行完。压掉的是「模型说完才动手」的串行等待；工具并发仍按 isConcurrencySafe 分区、权限检查也仍在前。'
  },
  {
    question: '模型还在跑一个 turn，用户又输入了一句普通文本，CC 的处理是？',
    options: [
      "'立即中断当前 turn，新输入优先处理'",
      "'排队等待，turn 内作为附件回灌给模型'",
      "'直接丢弃，提示用户等待当前任务完成'",
      "'暂停流式响应，等用户确认后再继续'"
    ],
    correct: 1,
    explanation: 'queryGuard.tryStart() 返回 null 时输入进统一队列（now > next > later），不打断。turn 内每轮工具执行后，queryLoop 把排队输入变成 attachment 塞进 toolResults 回灌（query.ts:1570-1590），turn 结束后 useQueueProcessor 冲刷剩余项。只有 now 优先级才触发 abort；斜杠命令不参与 turn 内回灌，等 turn 结束走自己的管线。'
  },
  {
    question: '可恢复错误（如 prompt-too-long）在流式过程中被「扣留」，这个设计的边界是什么？',
    options: [
      "'扣留有 10 次上限，超过后强制 yield 给用户'",
      "'恢复失败时错误消息会随 result 消息一起返回'",
      "'恢复路径有 bug 时用户可能看不到中间错误'",
      "'扣留只对 max_output_tokens 类错误生效'"
    ],
    correct: 2,
    explanation: '扣留后先试恢复：成功则用户无感，失败才 yield 错误。代价是恢复路径自身有 bug 时外部完全看不到中间错误，只能靠 transition.reason 在日志和测试里追踪。扣留覆盖 prompt-too-long、媒体超限和 max-output-tokens，且扣留侧与恢复侧的 gate 必须同值，否则消息会凭空消失。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
