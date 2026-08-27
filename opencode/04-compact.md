---
title: OpenCode 会话压缩：Compact 2 级机制
---

# OpenCode 会话压缩：Compact 2 级机制

长会话最怕的不是模型偶尔答错，而是上下文慢慢被塞满：前面定过的设计想不起来，上一次的改动被覆盖，甚至直接抛 `context overflow` 中断。只要会话够长，这个问题迟早会来，所以压缩不是补丁，而是 agent 框架里必须预留的一条正常通路。

OpenCode 没有把上下文压缩仅仅当成一个“逼近上限才被动响应”的兜底功能，而是将其设计为一条完整的系统链路：先判断窗口是否触发阈值，再动态选择回收策略，压缩完成后还要无缝续接当前会话。一端是不消耗 Token 的 Prune（仅清理历史工具输出）；另一端是调用 LLM 生成锚定摘要的 Compact（专注于对话上下文蒸馏）。两条路径统一共享一套溢出判定与会话续接管道。

读这一篇时，不妨先记住五个判断：

- **第一**，模型宣称的上下文上限，不等于系统实际敢用的额度；
- **第二**，压缩不是“满了才想起来”，而是主循环在判定溢出后会自动切进去的一条正常路径；
- **第三**，Prune 和 Compact 回收的不是同一种东西，前者剪工具输出，后者折叠对话历史；
- **第四**，压缩最难的不是“腾空间”，而是压完之后怎样把会话接回去；
- **第五**，OpenCode 和 Claude Code 的差别，集中在压缩的层级深度与成本结构。

这一篇只拆压缩这条链本身；上下文里还有哪些信息平时如何装配、跨会话怎样存活，会在后面的上下文章节单独展开。


## 一、溢出判定：usable() 与 isOverflow()

压缩的第一道门槛是"什么时候算塞满"。OpenCode 不拿模型的原始窗口当满额，而是先算出"敢用的额度"，再和已经占用的 token 比较。

### 1.1 模型声称的上限，不等于敢用的上限

一个模型的 context 窗口看起来是 200K，但真实历史不能一路堆到这个数。原因很直接：每一轮 LLM 调用都需要预留输出空间，如果上下文被占满，模型连一句话都无法完整生成，写到一半就会被截断，整个会话就卡死在"想回应却写不下"的状态。所以可用额度要先从上下文里扣掉一段输出预留。

这段逻辑落在 `usable()`（`src/session/overflow.ts:8`）：

```ts
const COMPACTION_BUFFER = 20_000

export function usable(input) {
  const context = input.model.limit.context
  if (context === 0) return 0

  const reserved =
    input.cfg.compaction?.reserved ??
    Math.min(COMPACTION_BUFFER, ProviderTransform.maxOutputTokens(input.model, input.outputTokenMax))
  return input.model.limit.input
    ? Math.max(0, input.model.limit.input - reserved)
    : Math.max(0, context - ProviderTransform.maxOutputTokens(input.model, input.outputTokenMax))
}
```

`reserved`（上下文预留量）的确定分为"来源判定"与"空间扣减"两步：

首先，预留值优先采用用户配置的 `cfg.compaction.reserved`；未配置时，则取默认缓冲值（20,000）与模型最大输出 Token 数中的较小者。

其次，关于可用空间的实际扣减，则根据模型 API 声明方式的不同分为两条路径：若模型显式上报了 `limit.input`，则按 `input - reserved` 扣减；若仅声明了 `context`，则按 `context - maxOutputTokens` 计算。两条路径虽起点不同，但落点一致——皆是为了给下一轮模型生成预留出足够安全的空间。

### 1.2 isOverflow：比较的是占用，而不是剩余

有了"敢用的额度"，溢出判定就压缩成一个比较（`isOverflow`，`src/session/overflow.ts:20`）：

```ts
export function isOverflow(input) {
  if (input.cfg.compaction?.auto === false) return false
  if (input.model.limit.context === 0) return false

  const count =
    input.tokens.total || input.tokens.input + input.tokens.output
      + input.tokens.cache.read + input.tokens.cache.write
  return count >= usable(input)
}
```

三处前提要一并看，它们定义了这套判定的边界：

- `auto === false` 直接短路——用户在设置中关闭自动压缩后，OpenCode 将不再自动触发，仅保留手动触发入口。
- `limit.context === 0` 直接放行——模型根本没上报窗口大小，无从度量，就不做溢出判断。这兜住了那些报告不全的模型。
- 占用数 `count` 优先读 `tokens.total`，拿不到再退而求其次，把 input、output、cache 读、cache 写分项累加。cache 命中的历史也被计入，因为它同样占着上下文预算。

结果在 `count >= usable(input)` 这一处收敛。同一边界既用在上一轮 assistant 结束后的主动检查，也用于后面要讲的触发路径，它是整条压缩链的公共开关。

## 二、触发时机：proactive 与 reactive 两条路径

溢出判定就绪后，接下来是"什么时候去查它"。OpenCode 不在用户每句话进来时都查，而是把它挂到两个自然节点上：前一轮 assistant 结束时主动查一次，LLM 调用被截断时被动接一次。两条路径共用同一个 `create()`，差别只在参数。

### 2.1 proactive：上一轮结束后提前一轮

主路径在 runLoop 里每轮**执行完** assistant 之后（`src/session/prompt.ts:1322`）：

```ts
if (
  lastFinished &&
  lastFinished.summary !== true &&
  (yield* compaction.isOverflow({ tokens: lastFinished.tokens, model }))
) {
  yield* compaction.create({ sessionID, agent: lastUser.agent, model: lastUser.model, auto: true })
  continue
}
```

三个细节决定了这条路径的稳健性：

- **查的是 `lastFinished`**，不是正在生成的 assistant。只对"已经完整跑完一轮"的消息做溢出判断，避免把正在流式输出的半截消息误判进压缩里。
- **`summary !== true`** 把摘要消息本身排除出判定。摘要时 LLM 输出的一段文本也占用上下文，若它也参与计数，压缩完的输出又可触发下一次压缩，形成无限套娃。把它排除后，压缩产生的输出不会再触发下一次压缩。
- **`create` 后立刻 `continue`**，不在本轮里消化，而是把压缩任务甩给下一轮循环。这保持了 runLoop 主循环"每轮只处理一个任务"的结构。

### 2.2 reactive（被动触发）：被截断时的兜底机制

proactive 在"正常跑完一轮"的前提下够用，万一某轮中途被 LLM 提前掐断——比如返回 `ContextOverflowError`——就得用处理层去接。`src/session/processor.ts` 在流式响应里捕获这个错误（第 754-758 行）：

```ts
if (MessageV2.ContextOverflowError.isInstance(error)) {
  ctx.needsCompaction = true
  yield* bus.publish(Session.Event.Error, { sessionID: ctx.sessionID, error })
  return
}
```

`needsCompaction` 一旦置位，这轮响应会被截停，最后在处理器收尾处（`processor.ts:845`）返回一个特殊的 `"compact"` 结果：

```ts
if (ctx.needsCompaction) return "compact"
```

runLoop 收到 `"compact"` 后走 `create`，这次带上了 `overflow` 标记（`prompt.ts:1477`）：

```ts
if (result === "compact") {
  yield* compaction.create({
    sessionID,
    agent: lastUser.agent,
    model: lastUser.model,
    auto: true,
    overflow: !handle.message.finish,   // 流被截断，标记 overflow
  })
}
```

### 2.3 共用 create，只差 overflow

两条路径都汇到同一个 `create()`，`overflow` 参数是仅有的区别。proactive 是常态：多数会话会在跑完一轮后主动触发，把溢出拦在爆掉之前。reactive 是兜底：proactive 没拦住（模型突然吐超长 reasoning、或者某次响应跨过阈值等），reactive 接住 ContextOverflowError，把消息压回 runLoop 再走同一套 create。

`overflow` 标记的价值留给压缩与续接阶段用——它决定 process 里是否重放原用户消息，这是第七节要展的部分。

## 三、流程骨架：create 占位，process 执行

compress 的整体骨架大分为两步，拆开看是"占位 + 派发"的动作，加上一组"执行"的动作。create 不调 LLM，process 才调。

![Compact 流水线：create → process → prune](/images/opencode/04-flow.svg)

### 3.1 create：插一条占位消息，不调 LLM

`create()`（`src/session/compaction.ts:584`）做的是最轻的事——往数据库写一条 user 消息，再在它上面挂一个 `type: "compaction"` 的 part：

```ts
const msg = yield* session.updateMessage({
  id: MessageID.ascending(),
  role: "user",
  model: input.model,
  sessionID: input.sessionID,
  agent: input.agent,
  time: { created: Date.now() },
})
yield* session.updatePart({
  id: PartID.ascending(),
  messageID: msg.id,
  sessionID: msg.sessionID,
  type: "compaction",
  auto: input.auto,        // 是否自动触发
  overflow: input.overflow, // 是否 reactive 路径
})
```

这一步只是打标记。`auto` 和 `overflow` 两个开关先存进 part，留到跑它时再读。

### 3.2 下一轮 runLoop 派发到 compaction task

create 写下的占位 user 消息，本身不是一个普通的用户提问——runLoop 跑任务队列时见到 `type: "compaction"` 的 part，会把它当作 compaction 任务来分发。这一步的输赢在 runLoop 主循环被保持了"轮询 + 分发"的纯结构：它自己不去管压缩的内部细节，只负责"创造一个 task 待下轮处理"，"下轮把它派给 compaction.process"，以及"最后启动一个后台 prune"。

### 3.3 process：调用专用 agent 的压缩引擎

被派发后，`process()`（`src/session/compaction.ts:344`，到 `create` 前约 240 行）才是整条链的主体。它按顺序做几件事：处理 overflow 找重放的用户消息、取出 compaction agent、过滤掉历史已有摘要、用 `select` 切出 head/tail、构建含锚定摘要的 prompt、调 LLM 生成 9 段摘要，再根据结果决定重放或合成 continue。

process 的执行顺序大体是：先 `select` 切出保留窗口与历史，再调 LLM 生成锚定摘要，最后按结果重放或合成 continue——这三段下文各一小节展开。这里先不细讲，只注意其中不调 LLM 的 `prune` 是异步的，在 runLoop 整轮收尾后 `Effect.forkIn(scope)` 甩到后台，不占主链路。

## 四、第一级 Prune：不调 LLM 的数据层回收

压缩的第一级是 Prune。它不碰 LLM，只看数据层做回收：把老的工具输出打上时间戳标记，让它们在序列化时"隐身"，而不是删除数据。

### 4.1 扫描与门控

`prune()`（`src/session/compaction.ts:298`）在 runLoop 收尾时被拉起（`prompt.ts:1495`）：

```ts
yield* compaction.prune({ sessionID }).pipe(Effect.ignore, Effect.forkIn(scope))
```

`Effect.forkIn(scope)` 把它 fork 成一个独立 fiber，`Effect.ignore` 让它在出错时不拖累主流程。于是用户拿到响应的那一刻，Prune 还在后台默默跑。这是"响应优先，清理不阻塞"的一处取舍。

Prune 的扫描从消息尾部倒着走，有两条硬规则：

```ts
loop: for (let msgIndex = msgs.length - 1; msgIndex >= 0; msgIndex--) {
  if (msg.info.role === "user") turns++
  if (turns < 2) continue                        // 保护最近 2 轮
  if (msg.info.role === "assistant" && msg.info.summary) break loop  // 不越过已有摘要
  for (let partIndex = msg.parts.length - 1; partIndex >= 0; partIndex--) {
    const part = msg.parts[partIndex]
    if (part.type !== "tool") continue
    if (part.state.status !== "completed") continue
    if (PRUNE_PROTECTED_TOOLS.includes(part.tool)) continue  // skill 永不剪
    if (part.state.time.compacted) break loop                // 到已修剪边界就停
    ...
  }
}
```

### 4.2 两层保护：最近 2 轮 + PRUNE_PROTECT

prune 要动手，先过两层保护（`compaction.ts:35`）：

```ts
export const PRUNE_MINIMUM = 20_000   // 少于则不值得动
export const PRUNE_PROTECT = 40_000   // 保留最近 40K tokens 的工具输出
const PRUNE_PROTECTED_TOOLS = ["skill"]
```

- **最近 2 轮不动**——倒序数到第 2 个 user 消息都跳过，只有更早的轮次才进入剪裁候选，最近的对话完整可见。
- **40K token 工具输出保护**——从尾部累积，未达 `PRUNE_PROTECT` 之前不动，只在累积超过 40K 后才开始标记更早的输出。它是一个滑动窗口，保证最近的工具输出大概率在窗口内。
- **skill 永不修剪**——`PRUNE_PROTECTED_TOOLS` 只装了 `"skill"`。skill 是按需加载的指令文件，一旦剪掉，下次要用又得重新载一，浪费 token 还可能因上下文变化产生不一致，直接护住。

另有一个最低门槛：只有当可剪部分超过 `PRUNE_MINIMUM` 才落库。省几 K token 不值得付一次数据库写入的开销，得攒够数量才动手。


### 4.3 时间戳标记，而不是删除

Prune 从不删除数据。它对待剪的 part 只做一件事（`compaction.ts:336`）：

```ts
part.state.time.compacted = Date.now()
yield* session.updatePart(part)
```

"隐藏"发生在序列化阶段（`message-v2.ts:791`）：当 part 的 `time.compacted` 被置位，工具输出在发给 LLM 时变成一行占位串：

```ts
const outputText = part.state.time.compacted
  ? "[Old tool result content cleared]"
  : truncateToolOutput(part.state.output, options?.toolOutputMaxChars)
```

数据仍然完整躺在 SQLite 里，需要回溯、审计、撤销时随时能拿出来。压缩和持久化被拆开：压缩只是在读出去的快照上做手脚，不触碰底层的存储。这让整条链"可逆"——剪出去的内容没有丢。

## 五、保留窗口：select 切 head/tail

Compact 不把全部历史一股脑塞给 LLM 去总结——最近几轮的上下文必须原样保留，因为模型只有看到"刚发生什么"才能续上。区分这两者的，是 `select()`。

### 5.1 默认 2 轮，预算 clamp

`select()`（`src/session/compaction.ts:245`）拿到消息后，先按轮切分，再预算：

```ts
const limit = input.cfg.compaction?.tail_turns ?? DEFAULT_TAIL_TURNS
if (limit <= 0) return { head: input.messages, tail_start_id: undefined }

const budget = preserveRecentBudget({ cfg: input.cfg, model: input.model })
const all = turns(input.messages)
const recent = all.slice(-limit)   // 取最近 N 轮
```

`DEFAULT_TAIL_TURNS` 是 2，也就是默认保留最近 2 个 user 场次。预算由 `preserveRecentBudget`（`compaction.ts:136`）控制：

```ts
function preserveRecentBudget(input) {
  return (
    input.cfg.compaction?.preserve_recent_tokens ??
    Math.min(MAX_PRESERVE_RECENT_TOKENS, Math.max(MIN_PRESERVE_RECENT_TOKENS, Math.floor(usable(input) * 0.25)))
  )
}
```

`MIN_PRESERVE_RECENT_TOKENS` 是 2_000，`MAX_` 是 8_000；默认值取"可用额度的 25%"夹在这两档之间。200K 上下文的模型，`usable` 约 180K，`usable*0.25` 是 45K，被 8_000 封顶——最近 2 轮最多保留 8K token。当 2 轮加起来不足 8K 就全留，超过就在那一轮里切。

保留轮数是 2 而非 1 或 5，落在"保一个完整的问答对加上一点点前文"上：只留 1 轮，模型容易忘了刚才问过什么、答案在哪一轮；留到 5 轮，8K 的预算被撑破，反而把最近的细节挤掉。2 是两者之间的平衡点。Claude Code 默认保 3-5 个 tool results + 40K 窗口，思路同源但更宽，代价是上下文压力更大时更容易推进下一层压缩。

### 5.2 splitTurn：一轮之内的切点

某一轮的 token 数已经超过剩余预算时，`select` 不会丢掉整整一轮，而是调 `splitTurn()`（`compaction.ts:161`）在这一轮内部找切点：从 turn 起点往末尾逐段估计，找到第一个能塞进剩余预算的尾偏移，把它收成新的 `tail_start_id`。

这保证了即使某轮特别长——比如用户一次性贴了 30K 的代码——也保得住最近的子片段，而不是一刀切。`select` 的最后再核对一遍：如果 keep 落在索引 0（意味着全部都要保留），就返回 `{ head: messages, tail_start_id: undefined }`——没有可截的尾巴，全部历史送摘要，不强行收窄到空。

`select` 的产出是 `{ head, tail_start_id }`：`head` 送去 LLM 锚定摘要，`tail_start_id` 之后的尾巴内容由 `filterCompacted` 在续接时插回摘要后。

## 六、第二级 Compact：锚定摘要与 9 段模板

第 2 级的核心是让一个 LLM 把送进 `head` 的旧历史压缩成结构化摘要。它用独立的 compaction agent，走锚定摘要增量更新。

### 6.1 专用 agent：hidden + deny all + native

compaction agent 在 `src/agent/agent.ts:235` 定义：

```ts
compaction: {
  name: "compaction",
  mode: "primary",
  native: true,
  hidden: true,
  prompt: PROMPT_COMPACTION,
  permission: Permission.merge(
    defaults,
    Permission.fromConfig({ "*": "deny" }),
    user,
  ),
  options: {},
},
```

三个字段是它和普通 agent 的分界：

- `hidden: true`——用户的 agent 列表看不到它，纯内部使用。
- `permission: { "*": "deny" }`——完全禁止工具调用，它只能输出文本。
- `native: true`——走 native runtime，不做多余的中间转换。

禁工具有两层理由。一是收窄行为边界：摘要 agent 只写文本，衔不到任何危险操作。二是防嵌套压缩：如果它也能调工具，万一它自己在摘要过程中把上下文撑爆，就会触发又一次压缩，而压缩的输出又制造压缩——这正是要给"摘要消息"单独挂门控的原因。让它只能"说完一句话"结束，也就止住了这个递归。

### 6.2 anchored summary：增量，而不是重新生成

摘要的生成不是每次把全部历史重读一遍，而是基于上一次的摘要做增量更新。`buildPrompt`（`compaction.ts:123`）把两种模式拼接出来：

```ts
const anchor = input.previousSummary
  ? [
      "Update the anchored summary below using the conversation history above.",
      "Preserve still-true details, remove stale details, and merge in the new facts.",
      "<previous-summary>",
      input.previousSummary,
      "</previous-summary>",
    ].join("\n")
  : "Create a new anchored summary from the conversation history above."
return [anchor, SUMMARY_TEMPLATE, ...input.context].join("\n\n")
```

首次压缩走 "Create" 模式；之后的压缩把 `<previous-summary>` 裹进来，让 LLM 在它基础上合并新事实。对应到 `src/agent/prompt/compaction.txt` 里那句 "Update it with the new history by preserving still-true details, removing stale details, and merging in new facts"。

这套设计的经济性在于：每次压缩只需处理"新产生的对话 + 上一次摘要"，而不是"全部历史"。维持摘要的连续性，同时把重复阅读的 token 省下来。

### 6.3 SUMMARY_TEMPLATE：9 段结构化

摘要输出被一个固定的 `SUMMARY_TEMPLATE`（`compaction.ts:42`）锁死结构。模板是 7 个顶级段，其中 `Progress` 又拆成 Done / In Progress / Blocked 三个子段，摊平下来一共 9 个信息块：

```markdown
## Goal                        # 用户在做什么
## Constraints & Preferences   # 偏好、约束、规格
## Progress                    # Done / In Progress / Blocked 三子段
## Key Decisions               # 决策和为什么
## Next Steps                  # 有序下步动作
## Critical Context            # 技术事实、错误、open question
## Relevant Files              # 涉及文件与理由
```

每段都对应"续上工作需要的某类信息"：Goal 让模型知道当下要做什么；Constraints & Preferences 防止踩用户的偏好；Progress 拆成 done/in-progress/blocked，把"已完成 / 正在 / 卡住"摊平；Key Decisions 防止推翻已定结论；Next Steps 列出接下来该做什么；Critical Context 拉起错误与待解问题；Relevant Files 省得模型再 grep 一遍。

模板里有一条硬规则：

```
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Do not mention the summary process or that context was compacted.
```

"Keep every section, even when empty"强制 LLM 对空段显式写 "(none)"，而不是删掉段落。这维持了结构到下一次锚定增量时的稳定——不管哪个段没货，它的位置和索引都留着，更新时可对位，不会乱套。（其"不要提压缩过程"则是顺从了 summary 消息不触发判定的前提。）

### 6.4 序列化时的处理：stripMedia + 2K 截断

摘要调用 `toModelMessagesEffect` 时带两组参数（`compaction.ts:406`）：

```ts
const modelMessages = yield* MessageV2.toModelMessagesEffect(msgs, model, {
  stripMedia: true,
  toolOutputMaxChars: TOOL_OUTPUT_MAX_CHARS,   // = 2_000
})
```

`stripMedia` 起因是摘要不在乎图片/文档附件，直接去掉，省 token。`toolOutputMaxChars` 固定 2_000：摘要只关心"某个工具干了什么"的结论，不关心工具的完整输出，所以工具正文在喂给摘要时只留 2K 字符。这个 2K 是常量，不可配置。

## 七、续接语义：filterCompacted 重排与重放

压缩完成只是写完摘要，让接下来的对话"不断"的是续接。模型看到的上下文序列需要被重排成"摘要在前、保留尾巴在后"的自洽顺序；reactive 场景还要重放原用户消息。

### 7.1 writeCompacted：把尾巴挪到摘要之后

数据库里按写入顺序是这样一串：

```
[old-user1, old-assistant1, old-tool1, ...,
 compaction-user, summary-assistant,
 recent-user, recent-assistant, ...]
```

但发给 LLM 的顺序必须把保留的 `recent-*` 尾巴搬到摘要后面，而不是留在摘要前面。`filterCompacted()`（`src/session/message-v2.ts:1014`）先正向扫描，遇到已完成 compaction 的 user，读它的 `tail_start_id`；随后 `result.reverse()`、再按 tail_start_id 定位，最后把三段拼起来：

```ts
return [
  ...result.slice(compactionIndex, summaryIndex + 1),  // 压缩 user + summary
  ...result.slice(tailIndex, compactionIndex),          // 保留的尾巴
  ...result.slice(summaryIndex + 1),                    // 之后的普通消息
]
```

于是 LLM 看到的上下文是：compaction-user 空请求 → summary 9 段 → 最近的 tails → 正常后续。这看起来好像"上下文根本没断"——旧的已压成摘要，新的原样保留，中间档由 `tail_start_id` 无缝衔接。

![Context Reordering 数组重排](/images/opencode/04-reorder.svg)

### 7.2 reactive 重放原消息，其余合成 continue

压缩完模型还要继续回应当下的问题。这里按 `overflow` 分了两条支路（`process` 内，`compaction.ts:477`）：

- **reactive 触发（`overflow: true`）**——用户的最后一条消息在生成半途被截断，没有完整响应。此时 `input.overflow` 让 process 在溢出分支里先向前找到那条被截断的 user 消息，记为 `replay`，压缩完成后把它原样重放（媒体附件退化为文本占位串，避免媒体过大再撑爆）。摘要结束后的模型会对这条指令重新给出完整响应，最新诉求不会丢。
- **proactive 触发（非 overflow）**——上一轮已经完整结束，无需重答。此时合成一条 user 消息：`"Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."`，并标 `synthetic: true` 与 `metadata.compaction_continue`。让模型自己决定还有没有下一步，不硬塞。

这个分支的取舍是：reactive 场景丢了答案，必须重放问句；proactive 场景上一轮已收尾，交给模型决定是否继续，而不是强扭。

## 八、横向取舍：OpenCode 2 级 vs Claude Code 5 级

把 OpenCode 整条链放回生态对比一次。Claude Code 的压缩是 5 级阶梯，OpenCode 是 2 级。看图再展开。


### 8.1 CC 前 4 级零 LLM，第 5 级才调

Claude Code 按查询循环的执行顺序有 5 个层级：

| 层级 | 机制 | 调用 LLM | 触发条件 |
|------|------|---------|----------|
| Level 1 | Tool Result Budget | 否 | 单条 tool result 超 50K 字符，落盘留 2KB 预览 |
| Level 2 | Snip Compact  | 否 | token 超阈值 + 13K |
| Level 3 | Microcompact  | 否 | 每次 API 调用前 |
| Level 4 | Context Collapse | 否 | ~90% 利用率 |
| Level 5 | Auto-compact | **是** | 前 4 级不足时 |

前 4 级都是纯数据结构操作：截断、占位符替换、读缓存感知——不碰 LLM。多数会话的回收压力在这些层级就被消化掉，走到 Auto-compact 的少。它的深度依赖 Anthropic 的服务端缓存删除机制 `cache_edits`，把"替换为占位符"和缓存失效绑在一起，让改动尽量便宜。

### 8.2 OpenCode 的 2 级

| 层级 | 机制 | 调用 LLM | 触发条件 |
|------|------|---------|----------|
| Level 1 | Prune | 否 | runLoop 退出后异步 fork |
| Level 2 | Compact | **是** | `isOverflow()`=true |

只有 2 级，Compact 级必调 LLM。Prune 不调 LLM但只覆盖工具输出；对话历史一律交给 LLM 摘要。这背后藏着一个代价：OpenCode 每次压缩都要付一次 LLM 调用费，少了一批 CC 里靠"占位符 + cache"换来的低成本。

### 8.3 两种哲学：省复杂度 vs 省成本

差异落在两处关键：`cache_edits` 依赖，以及源码规模。

CC 的第 5 级之所以便宜，是因为 Microcompact 与 cache_edits 都在 Claude 服务端上，且前 4 级零 LLM 的回收只在它的生态里做得到。它的哲学是"能不调 LLM 就不调"，用缓存感知和大量占位符去压成本，代价是 5 级之间的协同、特性开关与 cache 集成都烧进工程里——对应源码约 3,960 行。

OpenCode 只有 `compaction.ts` 639 行 + `overflow.ts` 32 行。它不依赖 Anthropic 的内建缓存，所以可以跨多家模型厂商跑，`model.cache` 自己有 prompt cache，但没有 CC 那套 cache_edits。它的哲学是"简单 + 数据可逆"：用时间戳标记替代物理删除，把可逆性做进机制；代价是每次压缩都要付一次 LLM 调用的费用。

两家的保护窗口表面看近似——OpenCode 是 40K + 最近 2 轮，CC 是 40K + 最近 3-5 个 tool result。面对"上下文压到哪、还得保留最近哪些"同一类问题，两套代码给出了相近的护栏答案，这是工程经验的趋同。

### 8.4 DOOM 取舍表

| 维度 | CC 占优 | OpenCode 占优 |
|------|---------|--------------|
| 高频长对话成本 | ✅ cache_edits + 低 LLM 频次 | |
| 中小型会话简单 | | ✅ 2 级 |
| 数据可逆性 | | ✅ 时间戳标记 |
| 跨模型厂商 | | ✅ 不依赖 Anthropic |
| 代码可读性 | | ✅ 639 行 vs 3960 行 |

这套取舍的价值不在"多少行"，而在于每个 agent 系统都想清楚两件事：**在哪个维度值得投入复杂度，在哪个维度值得靠"简单 + 可逆"兜住**。CC 把精度压到服务端缓存，OpenCode 把精度压在"别删数据"上，得失都清楚。

## 九、收束：设计要点回收

把"判定 → 触发 → 骨架 → 两级 → 续接"这条链收拢成几条线：

- **溢出判定是一个公共开关**——`usable()` 预留输出空间，`isOverflow()` 用占用与可用额度比较，auto 关闭或 limit 为零都短路，成为 proactive 与 reactive 的公共前提。
- **触发拆两条**——proactive 跑完 assistant 主动查，reactive 接 ContextOverflowError 兜底，共用 `create` 只差 `overflow`。
- **骨架做成占位 + 派发**——create 只插占位 user；runLoop 只轮询分发；process 才调 LLM；prune 在收尾处异步拉起，不阻塞响应。
- **什么保留**——Prune 保护最近 2 轮 + 40K + skill；select 默认保 2 轮，预算 clamp 在 8K，超了就 splitTurn 找切点。
- **摘要可逆增量**——专用 agent 禁工具防嵌套，锚定摘要增量更新，9 段模板空段也保留。
- **续接要把 LLM 看似没断**——filterCompacted 把尾巴挪到摘要后；overflow 重放原问句，否则合成 continue。
- **可逆性 + 两段分离**——Prune 用时间戳标记而非删除，数据在 DB 可回溯；Compact 才对上下文做实际的改写。

回看这一整条链，OpenCode 的 2 级压缩值钱的地方在"取舍与可逆性"，而不是"压缩量最大"。它没把精力花在中心化维护一个缓存感知的多级阶梯上，而是把"删不删、怎么续"这几个关键决策做清楚了，再用数据可逆兜住风险。复杂度与成本之间的这个平衡，比把上下文压到极限更值得一个 agent 系统先想清楚。

## 章节小测

<script setup>
const q = [
  {
    question: 'Claude Code 有 5 级压缩（前 4 级零 LLM 调用），OpenCode 只有 2 级（Compact 必调 LLM）。OpenCode 选择 2 级的核心原因是什么？',
    options: ['CC 的 5 级方案已开源验证且 OpenCode 可直接复用', 'OpenCode 选择 2 级为达到垂直行业中最优压缩比', 'CC 多级依赖 Anthropic 专有而 OpenCode 需跨厂商兼容', '2 级设计在上下文利用率上经实测优于 CC 的 5 级方案'],
    correct: 2,
    explanation: 'CC 的 Microcompact 热路径用了 Anthropic 的 cache_edits API（服务端缓存删除机制），只能在 Claude 上享受性能优势。OpenCode 为跨 8 家模型厂商，不依赖任何厂商特定 API，所以选择了 2 级简单设计——Prune（纯数据操作）+ Compact（一定调 LLM）。代价是每次压缩都付 LLM 调用费。',
  },
  {
    question: 'OpenCode 的 Prune 为什么用「时间戳标记（compacted 时间戳）」而不是「物理删除」工具输出？',
    options: ['时间戳标记在批量 compaction 场景下具有更高的标记吞吐量', '物理删除虽节省存储但破坏了审计回溯所需的数据完整性', '物理删除会触发数据库外键级联删除导致意外丢失关联 parts', '时间戳标记在序列化时压缩快照体积上比物理删除效率更高'],
    correct: 1,
    explanation: '这是非常聪明的工程决策——prune 没有删除任何数据，只给 part 的 state.time 字段加 compacted 时间戳。真正的隐藏发生在序列化时，输出变为 "[Old tool result content cleared]"。数据仍在 DB 里，需要回溯审计时都能拿回来。',
  },
  {
    question: '锚定摘要（Anchored Summary）的核心机制是什么？',
    options: ['每次压缩将全部历史消息重新发给 LLM 以生成最准确的摘要', '每次压缩基于上次摘要增量更新避免重读全部原始对话历史', '压缩仅保留最近 N 轮对话原文并丢弃所有更早的上下文消息', '压缩将摘要持久化至文件系统并在后续轮次中直接读文件复用'],
    correct: 1,
    explanation: '首次压缩从原始对话生成摘要 A，后续压缩不重新读所有原始对话（太贵），而是基于摘要 A + 新对话生成更新后的 A\'。这样每次压缩只需处理「新产生的对话 + 上一次摘要」，省 token 又保证连续。',
  },
  {
    question: '为什么 compact 的 9 段摘要模板要求「Keep every section, even when empty」？',
    options: ['空段以维持卡片布局与排版间距的一致性', '空段保留确保锚定更新时各段按固定索引对齐', '空段满足下游 Markdown 对段数的最低数量要求', '空段使压缩前后消息量一致便于 Token 用量预估'],
    correct: 1,
    explanation: '强制 LLM 显式说 "(none)" 而不是只删掉空段，这样下次锚定更新时结构对得上，不会乱套。这是个「结构稳定性」的设计——让增量更新可预测。',
  },
  {
    question: 'Prune 的 40K tokens 保护窗口中，为什么 skill 的输出被强制保护（永不剪枝）？',
    options: ['skill 含高频数据剪掉可省计算', 'skill 按需加载的指令文件剪掉后需要重新加载', 'skill 只读查询输出幂等性强可丢弃', 'skill 输出 Token 低于 PRUNE_MINIMUM 阈值'],
    correct: 1,
    explanation: 'skill 工具输出包含完整的指令内容，是 LLM 按需加载的「工作流定义」，剪掉后下次需重新加载，浪费 token 又可能因上下文变化产生不一致。直接保护掉最稳。',
  }
]
</script>

<Quiz :questions="q"></Quiz>
