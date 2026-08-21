---
title: OpenCode 主循环图解：一条消息是怎么跑完整个 Agent 的？
---

# OpenCode 主循环图解：一条消息是怎么跑完整个 Agent 的？

不少人会用终端 Agent，但一旦追问一句：**用户发出一条消息后，系统内部到底靠什么把整件事推进到底？** 很快就会卡住。

这个问题比看起来重要。因为 Agent 不是“调一次模型”那么简单。你让它改文件、跑命令、调用子任务、处理中断、应对上下文溢出，这些动作都要被一套稳定的编排机制收住。没有这套机制，工具调用、压缩、重试、并发就会互相打架。

OpenCode 把这件事收敛在一条主循环里。它的关键不在于 `while (true)` 本身，而在于：**系统把什么当成状态、把什么当成任务、又靠什么条件决定继续还是停下。**

看完这一章，你最好能记住四件事：

- OpenCode 的主循环，本质上是一台**会话状态机**
- 工具调用靠**退出条件**一路往前推，并没有单独再开一层循环
- compaction 在主循环里本来就是一条**内建分支**
- OpenCode 的可维护性来自边界清晰：`prompt.ts` 编排、`processor.ts` 落状态、`llm.ts` 适配运行时

## 一、先把问题摆出来：为什么主循环是 Agent 的心脏

### 1.1 按下回车后，系统一下子要同时处理四件事

想象你给 Agent 下这样一条指令：读 3 个文件，改 2 个函数，跑一次测试，如果失败再继续修。

表面看，这是“一次对话”。但系统内部至少同时背着四类责任：

- **把输入写进会话状态**：用户说了什么，得先变成持久化消息，而不是只存在内存里
- **防并发跑飞**：同一个 session 里，不能因为用户连按两次回车，就起两条互相踩状态的执行链
- **让模型和工具轮流推进**：模型先说话、工具再执行、结果回流给模型，直到任务真的结束
- **在风险出现时自救**：上下文超长、中断、重试、工具死循环，这些都不能把会话搞坏

如果把这四件事拆成四套互不相干的流程，系统很快就会变成一团浆糊。一个工具执行到一半触发压缩；压缩刚做完，子任务又要插队；用户这时再发一条新消息——控制流立刻爆炸。

所以主循环要处理的，是**怎么让所有待处理工作回到同一个决策口。**

### 1.2 OpenCode 的答案：一台状态机

OpenCode 的处理方式很克制。它没有把所有逻辑揉进一个巨大的入口函数里，入口被拆成了三层：

```ts
// packages/opencode/src/session/prompt.ts:1215
const prompt = Effect.fn("SessionPrompt.prompt")(function* (input: PromptInput) {
  const message = yield* createUserMessage(input)
  if (input.noReply === true) return message
  return yield* loop({ sessionID: input.sessionID })
})

const runLoop = Effect.fn("SessionPrompt.run")(function* (sessionID: SessionID) {
  while (true) {
    // ... 主循环编排 ...
  }
})

const loop = Effect.fn("SessionPrompt.loop")(function* (input: LoopInput) {
  return yield* state.ensureRunning(input.sessionID, lastAssistant(input.sessionID), runLoop(input.sessionID))
})
```

这三层分别处理三种完全不同的复杂度：

- `prompt()`：处理输入，把 user message 写进 session
- `loop()`：处理并发，保证同一 session 不会同时跑两条主循环
- `runLoop()`：处理编排，把模型、工具、压缩、子任务推进到底

这就是这章的主线：**OpenCode 先拆边界，再用统一循环收拢复杂度。**

## 二、三层入口：为什么要把“收消息”和“跑循环”拆开

### 2.1 `prompt()` 先负责把消息写进会话

先看 `prompt()`。它一上来先写消息：

```ts
// packages/opencode/src/session/prompt.ts:1218-1233
const session = yield* sessions.get(input.sessionID).pipe(Effect.orDie)
yield* revert.cleanup(session)
const message = yield* createUserMessage(input)
yield* sessions.touch(input.sessionID)

if (permissions.length > 0) {
  session.permission = permissions
  yield* sessions.setPermission({ sessionID: session.id, permission: permissions })
}

if (input.noReply === true) return message
return yield* loop({ sessionID: input.sessionID })
```

这里拆开了两件事：**“用户发来一条消息”**，和 **“驱动 agent 开始回复”**。

为什么要硬拆开？因为一旦这两个动作绑定死，很多能力就做不出来：

- 程序化地往历史里插一条 user message
- 只写入消息，是否触发执行由调用方自己决定
- 只记历史、不立刻回复的场景

`noReply` 这个分支把边界划得很清楚：OpenCode 先把会话当成**持久化状态**，agent 回复则属于后续的**状态推进**。两者没有被绑死在一起。

### 2.2 `loop()` 不碰业务，它只管并发

`loop()` 里的 `ensureRunning()`，负责守住“不许同 session 并发起两条执行链”这条约束：

```ts
// packages/opencode/src/session/prompt.ts:1500-1503
const loop = Effect.fn("SessionPrompt.loop")(function* (input: LoopInput) {
  return yield* state.ensureRunning(input.sessionID, lastAssistant(input.sessionID), runLoop(input.sessionID))
})
```

`loop()` 自己不维护队列，它把并发控制交给 `ensureRunning()`。再往下看，`run-state.ts` 才是这道门闸的内部实现：

```ts
// packages/opencode/src/session/run-state.ts:51-67
const runner = Effect.fn("SessionRunState.runner")(function* (sessionID, onInterrupt) {
  const data = yield* InstanceState.get(state)
  const existing = data.runners.get(sessionID)
  if (existing) return existing
  const next = Runner.make<MessageV2.WithParts>(data.scope, {
    onIdle: Effect.gen(function* () {
      data.runners.delete(sessionID)
      yield* status.set(sessionID, { type: "idle" })
    }),
    onBusy: status.set(sessionID, { type: "busy" }),
    onInterrupt,
  })
  data.runners.set(sessionID, next)
  return next
})
```

这段代码对应的运行时约束很直接：同一个 `sessionID` 下面，只维护一个 runner。已经有 runner 在忙，新请求不会再偷偷起一个新的 `runLoop()` 去抢数据库状态。

这里解决的是**会话一致性**。如果没有它，用户连续两次发送消息，可能会出现：

- 两条 assistant message 同时写入
- 两轮工具调用交叉更新同一会话
- compaction 和普通回复互相覆盖

OpenCode 在这里拆得很开：编排逻辑交给 `runLoop()`，并发控制单独抽成门闸。不要把两件事揉在一起。也正因为这样，输入、并发、编排三种复杂度才没有被塞进同一个入口函数里。

## 三、统一主循环：OpenCode 怎么把所有“待处理工作”收进一条线

### 3.1 每轮开始先重建“当前世界状态”

进入 `runLoop()` 之后，OpenCode 每一轮先做的，都是重新读取当前消息状态：

```ts
// packages/opencode/src/session/prompt.ts:1252-1258
while (true) {
  yield* status.set(sessionID, { type: "busy" })
  yield* slog.info("loop", { step })

  let msgs = yield* MessageV2.filterCompactedEffect(sessionID)
  const { user: lastUser, assistant: lastAssistant, finished: lastFinished, tasks } = MessageV2.latest(msgs)
```

为什么每轮都要重新读？因为对 OpenCode 来说，**数据库里的消息顺序** 和 **模型应当看到的会话顺序** 不一定永远相同。

尤其是做过 compaction 之后，旧消息会先压成摘要，模型看到的会是“摘要 + 保留尾巴”，不再等同于数据库里的原始历史排列。`filterCompactedEffect()` 的作用，就是在每一轮开始时重新构造一份**模型可消费的上下文视图**。

这是一种很典型的状态机思路：**不要长期持有一份脆弱的内存真相，而是在每轮开头重新推导一次当前真相。**

### 3.2 `latest()` 先看还有哪些工作没收尾

从实现看，`latest()` 要先推导当前最关键的四类绑定：

```ts
// packages/opencode/src/session/message-v2.ts:1078-1093
export function latest(msgs: WithParts[]) {
  let user: User | undefined
  let assistant: Assistant | undefined
  let finished: Assistant | undefined
  for (const msg of msgs) {
    if (info.role === "user" && (!user || info.id > user.id)) user = info
    if (info.role === "assistant" && (!assistant || info.id > assistant.id)) assistant = info
    if (info.role === "assistant" && info.finish && (!finished || info.id > finished.id)) finished = info
  }
  const tasks = msgs.flatMap((m) =>
    finished && m.info.id <= finished.id ? [] : m.parts.filter((p) => p.type === "compaction" || p.type === "subtask"),
  )
  return { user, assistant, finished, tasks }
}
```

这里最关键的是 `tasks`。

很多人直觉里会把主循环理解成“你一句、我一句”的对话循环。可在 OpenCode 里，推动循环往前走的，是那些**还没结清的工作单**。就 `latest()` 这一步来说，它显式找出来的是两类工作：

- 还没处理的 `subtask`
- 还没执行的 `compaction`

而工具调用的续跑，不在 `tasks` 里，而是由下一节要讲的退出条件单独判断：只要 `hasToolCalls` 还成立，主循环就不会退出。

一旦你从“对话”切换到“工作”视角，后面的设计就都说得通了：为什么 compaction 会插队，为什么每轮都得先重建状态，为什么工具调用虽然不是 `tasks` 的一员，却依然会被主循环继续推进。

### 3.3 为什么只留一个 `while (true)`

先抛个问题：为什么不把工具执行单独做成一个循环？比如这样：

```ts
while (hasToolCalls) {
  executeTools()
  callLLM()
}
```

乍看很直观，实际会立刻撞墙。因为真实系统里，工具调用并不是唯一会插队的工作：

- 子任务可能在当前轮之前就排队等着
- 上一轮生成结束后，系统可能发现上下文已经快溢出

如果工具、压缩、子任务各自都有一套局部循环，主循环很快就会退化成“循环里嵌套循环，分支里再套分支”。

OpenCode 走的是另一条路：**所有需要继续推进的情况，都回到同一个 `while (true)` 顶部重新判断。**

这样写下来，所有“是否继续”的决策都会回到同一个地方。

## 四、停下与继续：工具循环靠什么往下走

### 4.1 主循环什么时候停，决定了工具循环如何存在

`runLoop()` 在这里先看的是退出条件：

```ts
// packages/opencode/src/session/prompt.ts:1262-1290
const lastAssistantMsg = msgs.findLast(
  (msg) => msg.info.role === "assistant" && msg.info.id === lastAssistant?.id,
)
const hasToolCalls =
  lastAssistantMsg?.parts.some(
    (part) => part.type === "tool" && !part.metadata?.providerExecuted && !isOrphanedInterruptedTool(part),
  ) ?? false

if (
  lastAssistant?.finish &&
  !["tool-calls"].includes(lastAssistant.finish) &&
  !hasToolCalls &&
  lastUser.id < lastAssistant.id
) {
  break
}
```

这里的规则很直接：

> **只有当最新 assistant 已经完成、也不再请求工具、当前没有残留工具工作时，主循环才会停下。**

顺着这个退出条件往下看，OpenCode 的工具循环也就清楚了：

- 工具调用不是额外再开一层 `while`
- 它只是让退出条件暂时不成立
- 所以主循环自然会再跑一轮，把工具结果送回去继续推进

工具循环没有额外写成一段显式结构。退出条件还不成立，主循环就继续往下走。

### 4.2 工具调度为什么能藏在主循环里

主循环之所以能只管“继续还是停下”，是因为脏活都被下沉到了两块边界：

- `SessionTools.resolve()`：把可用工具整理成模型能调用的工具集
- `processor.process()`：消费模型事件流，把工具调用与结果落回 session

先看工具解析：

```ts
// packages/opencode/src/session/tools.ts:75-85
for (const item of yield* registry.tools({
  modelID: ModelID.make(input.model.api.id),
  providerID: input.model.providerID,
  agent: input.agent,
})) {
  const schema = ProviderTransform.schema(input.model, ToolJsonSchema.fromTool(item))
  tools[item.id] = tool({
    description: item.description,
    inputSchema: jsonSchema(schema),
    execute(args, options) {
```

这里先抓住一点：**主循环拿到的，已经是一组包装好的工具。** 内置工具、MCP 工具、权限询问、输出截断，都已经在 `resolve()` 里被整理过一遍。

再看 `runLoop()` 调用 `processor`。这里的 `handle`，就是当前这轮 processor 的执行句柄：

```ts
// packages/opencode/src/session/prompt.ts:1435-1455
const [skills, env, instructions, modelMsgs] = yield* Effect.all([
  sys.skills(agent),
  sys.environment(model),
  instruction.system().pipe(Effect.orDie),
  MessageV2.toModelMessagesEffect(msgs, model),
])
const system = [...env, ...instructions, ...(skills ? [skills] : [])]
const result = yield* handle.process({
  user: lastUser,
  agent,
  sessionID,
  system,
  messages: [...modelMsgs, ...(isLastStep ? [{ role: "assistant" as const, content: MAX_STEPS }] : [])],
  tools,
  model,
})
```

到这里，`runLoop()` 只做三件事：

1. 组装系统提示词和模型消息
2. 交给 `process()` 去跑一轮
3. 根据结果决定 `break / continue / compact`

这意味着 `runLoop()` 不去展开某个工具的具体生命周期。它只关心这轮推进之后，会话是不是还需要再来一轮。

### 4.3 两种 LLM 运行时，怎么还能共用同一套主循环

OpenCode 这里还有一道清晰的边界：它支持两种运行时，但主循环不用跟着分叉。

`llm.ts` 先尝试 native runtime，不行就回退到 AI SDK：

```ts
// packages/opencode/src/session/llm.ts:220-272
if (flags.experimentalNativeLlm) {
  const native = LLMNativeRuntime.stream({ ... })
  if (native.type === "supported") {
    return { type: "native", stream: native.stream }
  }
}

return {
  type: "ai-sdk",
  result: streamText({
    tools: prepared.tools,
    messages: prepared.messages,
    model: wrapLanguageModel({ model: language, middleware: [...] }),
  }),
}
```

但无论底下走哪条路，往上交付的都必须是同一种 `LLMEvent` 流：

```ts
// packages/opencode/src/session/llm.ts:343-364
const result = yield* run({ ...input, abort: ctrl.signal })
if (result.type === "native") return result.stream

const state = LLMAISDK.adapterState()
return Stream.fromAsyncIterable(result.result.fullStream, (e) =>
  e instanceof Error ? e : new Error(String(e)),
).pipe(
  Stream.mapEffect((event) => LLMAISDK.toLLMEvents(state, event)),
  Stream.flatMap((events) => Stream.fromIterable(events)),
)
```

OpenCode 的做法是先把不同运行时收敛成统一事件流，再往上交给主循环。provider 差异先在这一道边界里被处理掉了。

`runLoop()` 不必知道底下是 OpenAI、Anthropic，还是 AI SDK 自带的工具 dispatch。它只认 `LLMEvent`。

## 五、保护机制：一条工业级主循环，必须会自救

### 5.1 compaction 在主循环里的位置

很多系统会把上下文压缩放进报错后的补救逻辑里。OpenCode 把 compaction 直接写成主循环里的一条常规路径：

```ts
// packages/opencode/src/session/prompt.ts:1303-1329
const task = tasks.pop()

if (task?.type === "subtask") {
  yield* handleSubtask({ task, model, lastUser, sessionID, session, msgs })
  continue
}

if (task?.type === "compaction") {
  const result = yield* compaction.process({ messages: msgs, parentID: lastUser.id, sessionID, auto: task.auto, overflow: task.overflow })
  if (result === "stop") break
  continue
}

if (lastFinished && lastFinished.summary !== true && (yield* compaction.isOverflow({ tokens: lastFinished.tokens, model }))) {
  yield* compaction.create({ sessionID, agent: lastUser.agent, model: lastUser.model, auto: true })
  continue
}
```

这三步的顺序，本身就在说明优先级：

1. 先看有没有显式排队的 `subtask`
2. 再看有没有要执行的 `compaction` task
3. 最后才做 proactive overflow 检查

从调度角度看，compaction 和普通执行属于同一级的待处理工作。它本来就在正常执行路径里，不是临时补救动作。

这里顺手也要区分两层“停下”语义：这一段里的 `result === "stop"`，说的是 **compaction 这条分支自己要求主循环结束**；后面 `processor.process()` 返回的 `"stop"`，说的是 **这一轮正常执行不再继续推进**。名字一样，层级不同。

### 5.2 `processor` 在这里干什么

主循环能保持简洁，是因为 `processor.ts` 把最脏的一层状态处理接走了。

最值得看的是两个事件：`tool-call` 和 `step-finish`。

先看工具调用：

```ts
// packages/opencode/src/session/processor.ts:377-423
case "tool-call": {
  const toolCall = yield* ensureToolCall(value)
  const input = toolInput(value.input)
  yield* updateToolCall(value.id, (match) => ({
    ...match,
    tool: value.name,
    state:
      match.state.status === "running"
        ? { ...match.state, input }
        : { status: "running", input, time: { start: Date.now() } },
    metadata: match.metadata?.providerExecuted
      ? { ...value.providerMetadata, providerExecuted: true }
      : value.providerMetadata,
  }))
```

这段代码先把工具调用写成一份**会话状态里的 tool part**。主循环推进的，也就是一份份可查、可恢复、可中断的状态记录。

再看一步结束：

```ts
// packages/opencode/src/session/processor.ts:555-615
case "step-finish": {
  const usage = Session.getUsage({ model: ctx.model, usage: value.usage ?? new Usage({}), metadata: value.providerMetadata })
  ctx.assistantMessage.finish = value.reason
  ctx.assistantMessage.cost += usage.cost
  ctx.assistantMessage.tokens = usage.tokens
  yield* session.updateMessage(ctx.assistantMessage)
  yield* summary.summarize({ sessionID: ctx.sessionID, messageID: ctx.assistantMessage.parentID }).pipe(Effect.ignore, Effect.forkIn(scope))
  if (!ctx.assistantMessage.summary && isOverflow({ cfg: yield* config.get(), tokens: usage.tokens, model: ctx.model })) {
    ctx.needsCompaction = true
  }
  return
}
```

这一步会落下三件事：

- 把本轮 finish reason、cost、token usage 回写到 assistant message
- 触发摘要更新
- 判断当前是不是该转进 compaction

放在这条链路里看，`processor` 就是事件流和会话状态之间的翻译层。

### 5.3 Doom Loop 检测：盯住没有进展的重复调用

Agent 系统里更麻烦的一种坏状态，是模型没有停，但已经卡住，只是在重复做同一件事。

OpenCode 在 `tool-call` 分支里专门做了 Doom Loop 检测：

```ts
// packages/opencode/src/session/processor.ts:424-448
const parts = MessageV2.parts(ctx.assistantMessage.id)
const recentParts = parts.slice(-DOOM_LOOP_THRESHOLD)

if (
  recentParts.length !== DOOM_LOOP_THRESHOLD ||
  !recentParts.every(
    (part) =>
      part.type === "tool" &&
      part.tool === value.name &&
      part.state.status !== "pending" &&
      JSON.stringify(part.state.input) === JSON.stringify(input),
  )
) {
  return
}

yield* permission.ask({ permission: "doom_loop", patterns: [value.name], sessionID: ctx.assistantMessage.sessionID, metadata: { tool: value.name, input }, always: [value.name], ruleset: agent.permission })
```

关键在于：系统检测到这种重复后，不会直接封禁。

为什么？因为重复调用本身可能是合法策略。轮询任务状态、等待外部资源、重复读取变化中的文件，这些都可能需要重复。

系统要拦的，是这种情况：

> **模型在重复调用，却没有表现出新的推进。**

OpenCode 把这个判断交还给权限系统，避免框架替用户过早做决定。框架没有假装自己一定比用户更懂现场。

### 5.4 中断、重试、清理：失败路径也被设计进去了

一条工业级主循环，不能只会在理想路径上跑通。它还得会优雅地失败。

OpenCode 的 `processor.process()` 最后只返回三种结果：

```ts
// packages/opencode/src/session/processor.ts:780-847
yield* stream.pipe(
  Stream.tap((event) => handleEvent(event)),
  Stream.takeUntil(() => ctx.needsCompaction),
  Stream.runDrain,
)

if (ctx.needsCompaction) return "compact"
if (ctx.blocked || ctx.assistantMessage.error) return "stop"
return "continue"
```

这个三态返回很清楚：

- `compact`：这轮不能继续正常推进，先去压缩上下文
- `stop`：这轮应当停止继续推进，通常是错误、阻塞或完成
- `continue`：主循环继续下一轮

再配合 `cleanup()` 和 `halt()`，OpenCode 能把半完成状态收尾干净：

- 中断时把 assistant message 标成可识别的 aborted/error 状态
- 未完成 tool call 会被标成 interrupted
- `ContextOverflowError` 不走普通重试，而是转成 compaction 信号

对这类系统来说，要紧的是**失败后还能回到一致状态**。

## 六、从 runLoop 看 OpenCode 的工程哲学

### 6.1 它收拢的是控制流复杂度

把 `runLoop()` 看完，最后留下来的，是几条很朴素的工程原则：

- **单一状态机优先于多套局部循环**：工具、压缩、子任务都回到同一个决策口
- **统一事件流优先于 provider 分叉逻辑**：不同运行时先适配，再进入主循环
- **状态落库优先于纯内存推进**：每轮都能重新推导当前真相
- **保护机制内建优先于事后补丁**：compaction、doom loop、interrupt cleanup 都在正常流程里

OpenCode 这套写法没有追求花哨，它关心的是怎么把控制流爆炸的风险压下来。

### 6.2 和 Claude Code 对比：复杂度放在了哪里

把它和 Claude Code 放在一起看，更容易看出设计取舍。这里只看代码组织层面，不做强弱裁判：

| 维度 | OpenCode | Claude Code |
|---|---|---|
| 主循环组织 | 主循环较薄，更多能力分散在 session 服务边界里 | 更多编排逻辑集中在主干查询流程里 |
| 工具调度方式 | 更多通过运行时与 `processor` 协作完成 | 更强调流式工具执行与主循环协同 |
| 运行时抽象 | 统一适配成 `LLMEvent` 再往上交 | 运行时与主流程的耦合更集中 |

两边的取舍不一样，最明显的区别，是复杂度落在了不同位置。

OpenCode 的长处在于边界干净，代码更像一套可组合的系统服务。Claude Code 的长处在于很多调度策略更容易直接挂在主流程上。

如果只看这一章，OpenCode 更像是在回答：**怎样用一台统一状态机，把 Agent 的通用复杂度收住。**

### 6.3 看完这一章，应该记住什么

最后收五条：

- 用户消息会先进入 session，变成一份持久化状态
- 工具循环靠退出条件暂不成立来继续推进
- compaction 在 OpenCode 里属于正常调度分支
- `processor` 是模型事件和会话事实之间的翻译层
- OpenCode 的可维护性，来自边界清晰地拆掉了输入、并发和编排三种复杂度

把它看成“一个无限循环里不断调 LLM”，很容易抓偏。更接近这套实现的理解，是把它看成一台总调度台：每一轮先读当前世界，再判断还有什么工作，选择是推进、压缩、插队，还是停下。工具、上下文、运行时、中断，最后都被收成这台状态机的一部分。

理解了这一点，后面再看 OpenCode 的工具系统、compact、subagent、上下文注入，都会顺很多。因为你已经知道：这些模块最后都不是平行散落的，它们都会回到这条主循环里，变成一次次“还要不要继续”的判断。

## 章节小测

<script setup>
const q = [
  {
    question: 'OpenCode 为什么把 `prompt()`、`loop()`、`runLoop()` 拆成三层，而不是都塞进一个入口函数？',
    options: ['因为 Effect 写法要求入口函数必须拆成三段', '因为输入、并发、编排属于三种不同复杂度', '因为 TypeScript 单文件函数超过长度后性能会下降', '因为工具系统只能从单独的 loop 函数中读取配置'],
    correct: 1,
    explanation: '这三层分别处理消息写入、会话并发控制和主循环编排，是对三种复杂度的边界拆分。不是 Effect 的语法要求，也不是 TypeScript 性能问题。工具系统也不是因此才可用。'
  },
  {
    question: 'OpenCode 的工具循环为什么没有显式写成独立 `while`？',
    options: ['因为工具调用都由数据库触发，无法在内存里循环', '因为工具调用只是让主循环的退出条件暂时不成立', '因为 AI SDK 不支持连续两轮之间保留工具上下文', '因为独立工具循环会破坏 TypeScript 的类型收窄'],
    correct: 1,
    explanation: 'OpenCode 的设计是把“是否继续”统一收敛到主循环顶部判断。模型要求调工具或存在未完成 tool part 时，退出条件不成立，循环自然继续。问题不在数据库、AI SDK 或类型系统。'
  },
  {
    question: '为什么 `filterCompactedEffect()` 会在每一轮主循环开头重新执行？',
    options: ['因为上一轮的消息对象会在下一轮被垃圾回收，必须重建', '因为 compaction 之后模型可见顺序与数据库原始顺序可能不同', '因为 OpenCode 每轮都会重新生成 system prompt，必须清空旧消息', '因为工具结果只能在下一轮被转成 user message 重新发送'],
    correct: 1,
    explanation: '做过 compaction 后，模型应看到的是“摘要 + 保留尾巴”的重排结果，不一定等于数据库中原始消息排列。所以每轮都要重新推导当前可消费上下文，而不是依赖上一轮内存状态。'
  },
  {
    question: '从主循环角度看，compaction 在 OpenCode 里更接近哪种角色？',
    options: ['一种普通调度分支，与 subtask 一样属于待处理工作', '一种只在 provider 报错后才启动的异常修复补丁', '一种专门属于工具系统的后处理步骤', '一种只用于 structured output 的模型格式化机制'],
    correct: 0,
    explanation: 'OpenCode 把 compaction 显式建模为 task，并在主循环里与 subtask、overflow 检查并列处理，所以它是常规调度路径的一部分，而不是只在异常后才进入的补丁，也不属于工具后处理或结构化输出。'
  },
  {
    question: 'Doom Loop 检测最能体现 OpenCode 的哪种设计取向？',
    options: ['框架发现重复后直接封禁，优先保证执行效率', '框架发现重复后交给权限系统判断，而不是武断地拦截', '框架通过增加重试次数来消化重复调用，减少人工干预', '框架把重复工具调用自动改写成批处理请求，避免循环'],
    correct: 1,
    explanation: 'OpenCode 识别到连续重复调用后，不直接 ban，而是走权限询问。因为重复本身可能是合法策略，系统要拦的是“没有进展的重复”。其他选项都不是源码里的实际策略。'
  },
]
</script>

<Quiz :questions="q"></Quiz>
