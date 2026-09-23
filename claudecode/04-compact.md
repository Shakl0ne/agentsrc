---
title: Claude Code 压缩源码拆解：五级流水线，把调 LLM 留到最后
---

# Claude Code 压缩源码拆解：五级流水线，把调 LLM 留到最后

一个跑了三小时的 session：中间读了上百个文件、执行了几十条命令，上下文早就悄悄越过了红线，但用户从头到尾没看到一次「上下文不足」的提示，对话节奏也没有停顿——该压的部分，在你注意到之前就已经压掉了。

问题是：窗口就 200K，等它满了再让 LLM 写一份摘要，这是所有 coding agent 都想得到的答案。但摘要本身也是一次 API 调用，要花时间、要花钱，还会把之前攒下的 prompt cache 一次性作废。怎么压，才能既省 token、又不丢关键上下文、还尽量不碰缓存？

CC 的做法是把压缩摊成五级，贵的操作一级级往后放。这一篇把 `src/services/compact/` 拆开，看每一级的触发条件、执行路径，以及它们给彼此留的余地：

- 第一，五级各自在什么时机被触发，其中哪几级从头到尾不调 LLM；
- 第二，压缩动的是消息数组，怎么做到不破坏 prompt cache；
- 第三，压缩自己失败的时候怎么自保——断路器、递归守卫、还有「压缩的压缩」。

压缩在主循环里的位置，第二篇数七条 continue 时已经见过两条（`collapse_drain_retry` 与 `reactive_compact_retry`）；session memory 的提取机制属于第八篇的记忆系统，这里只看它的产物怎么被压缩消费。

## 一、压缩要同时摆平的三件事

**窗口增长是单向的**。一轮对话里，读一个大文件可能带回数千 tokens，一条命令的输出更长，工具结果只进不出，很快逼近上限。越过窗口，API 直接回 400 `context_length_exceeded`，对话被迫中断。

**缓存让「改消息」变贵了**。Claude API 的 prompt caching 按前缀命中计费：命中的部分 0.1x，未命中的写入收 1.25x。前缀只要被改动一个字符，缓存就作废。压缩恰恰就是改消息——每压一次，紧跟着的那次请求就要按 1.25x 全量重写缓存。所以压缩策略必须挑位置下手：能不动前缀就不动前缀，能只清尾部就只清尾部。CC 走得更远，它有一条路径通过 `cache_edits` API 让服务端删缓存内容、客户端消息数组原样不动，第四节展开。

**保留多少信息是个两难**。压狠了，模型丢掉关键上下文，后续回答质量下滑；压轻了，省不出空间，隔几轮又得再压一次。CC 用分层渐进来拆这个两难：先用数据结构变换做微压缩，不够再升级到全量摘要，每一级都有明确的触发条件与成本预算。

还有两个纯工程层面的坑要提前填：一是**断路器**——上下文不可恢复地超限时（比如 prompt_too_long），auto compact 会反复失败，没有断路器就是「失败→下轮再试→再失败」的死循环；二是**递归守卫**——压缩自己会创建 forked agent 去做摘要和记忆提取，这些 fork 继承了主对话的全部消息，同样会触发压缩检查，不拦住就是「压缩→fork→fork 的压缩」的递归链。两处的解法分别在第三、七节。

## 二、五级压缩总览

压缩系统分布在 `src/services/compact/` 的 11 个文件里，按机制分成五级：

| Level | 名称 | 文件 | 触发条件 | 成本 | 质量 |
|-------|------|------|----------|------|------|
| 1 | Auto Compact | `autoCompact.ts` | token 达有效窗口约 83.5% | 中 | 中-高 |
| 2 | Micro Compact | `microCompact.ts` | 每次 API 调用前 | 低 | 中 |
| 3 | API Microcompact | `apiMicrocompact.ts` | 服务端 input_tokens 超阈值 | 无（API 侧） | 低 |
| 4 | Reactive Compact | `reactiveCompact.ts` | API 返回 413 / 媒体超限 | 中 | 高 |
| 5 | Session Memory Compact | `sessionMemoryCompact.ts` | Auto Compact 优先尝试 | 低 | 最高 |

两个文件在泄漏源码里并不存在：`reactiveCompact.ts` 被 `feature('REACTIVE_COMPACT')` 条件编译裁剪，`cachedMicrocompact.ts`（缓存编辑路径的实现）被 `feature('CACHED_MICROCOMPACT')` 裁剪，都是 ant 内部独有。但 `query.ts`、`microCompact.ts` 和 `commands/compact/compact.ts` 里保留着调用点与接口注释，行为可以从调用侧推断。

五级的关系容易误读成「逐级升级」，实际是各自占一个时机、互相补位。关键的一点：**Level 5 是 Level 1 的优先子路径，不独立触发**——auto compact 判定要压时，先试 session memory（读已提取的记忆文件，纯数据操作），失败才回退到 LLM 摘要。所以这张表按「机制」划分，不按「触发时机」划分。它们在 query 循环里的实际顺序是：

![五级压缩在 query 循环里的占位](/images/claudecode/04-compact-pipeline.svg)

## 三、Auto Compact：阈值、断路器与递归守卫

Auto compact 是唯一的「主动预判」压缩：每次 API 调用前检查 token 用量，超阈值就先压再发。决策逻辑集中在 `autoCompact.ts`（351 行）。

### 3.1 阈值是固定 token 差值

先算有效窗口：从模型上下文窗口里减去摘要输出预留。预留值有出处——注释写着摘要输出 p99.99 是 17,387 tokens，向上取整到 20K：

```typescript
// src/services/compact/autoCompact.ts:28-48
// Reserve this many tokens for output during compaction
// Based on p99.99 of compact summary output being 17,387 tokens.
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

export function getEffectiveContextWindowSize(model: string): number {
  const reservedTokensForSummary = Math.min(
    getMaxOutputTokensForModel(model),
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
  )
  let contextWindow = getContextWindowForModel(model, getSdkBetas())
  const autoCompactWindow = process.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW
  if (autoCompactWindow) {
    const parsed = parseInt(autoCompactWindow, 10)
    if (!isNaN(parsed) && parsed > 0) {
      contextWindow = Math.min(contextWindow, parsed)
    }
  }
  return contextWindow - reservedTokensForSummary
}
```

再叠加触发缓冲 `AUTOCOMPACT_BUFFER_TOKENS = 13_000`（autoCompact.ts:62）。以 200K 窗口的 Sonnet 为例：有效窗口 200K − 20K = 180K，触发线再减 13K 落在 167K，折合原始窗口的约 83.5%。注意阈值是固定 token 差值，窗口不同的模型换算成百分比会漂移——100K 窗口的模型按同一套常量算出来约 67%。

`calculateTokenWarningState()`（autoCompact.ts:93-145）在触发线之外还给出 warning / error 两级预警（各再减 20K），驱动终端 UI 的 token 进度条变色；`isAtBlockingLimit` 是 auto compact 被禁用时的最后防线——到线直接阻止 API 调用，逼用户手动 `/compact`，`MANUAL_COMPACT_BUFFER_TOKENS = 3_000` 就是给手动操作留的余量。

### 3.2 断路器：三次失败就跳闸

`AutoCompactTrackingState` 里挂着断路器状态：

```typescript
// src/services/compact/autoCompact.ts:51-60
export type AutoCompactTrackingState = {
  compacted: boolean
  turnCounter: number
  // Unique ID per turn
  turnId: string
  // Consecutive autocompact failures. Reset on success.
  // Used as a circuit breaker to stop retrying when the context is
  // irrecoverably over the limit (e.g., prompt_too_long).
  consecutiveFailures?: number
}
```

这个字段的存在有一场真实事故垫底。源码注释记录：BQ 2026-03-10 的数据显示，1,279 个 session 出现过 50 次以上连续压缩失败、最多的一个 3,272 次，全局每天浪费约 25 万次 API 调用。于是有了 `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`（autoCompact.ts:70）：连续失败三次，本 session 内不再尝试 auto compact。`turnCounter` 和 `turnId` 顺带记录「距上次压缩过了几轮」，供遥测区分同一链内的多次压缩与跨 agent 的压缩。

### 3.3 递归守卫与模式互斥

`shouldAutoCompact()` 开头是一串返回 false 的分支，先挡递归：

```typescript
// src/services/compact/autoCompact.ts:169-183
// Recursion guards. session_memory and compact are forked agents that
// would deadlock.
if (querySource === 'session_memory' || querySource === 'compact') {
  return false
}
// marble_origami is the ctx-agent — if ITS context blows up and
// autocompact fires, runPostCompactCleanup calls resetContextCollapse()
// which destroys the MAIN thread's committed log (module-level state
// shared across forks).
if (feature('CONTEXT_COLLAPSE')) {
  if (querySource === 'marble_origami') {
    return false
  }
}
```

递归的成因：压缩派生的 forked agent 继承主对话全部消息，`querySource` 标记（`'compact'`、`'session_memory'`）就是用来在这些 fork 里跳过压缩检查的。`marble_origami` 那条更隐蔽——ctx-agent 自己的上下文膨胀触发压缩后，`runPostCompactCleanup` 会重置模块级的 context collapse 状态，连带毁掉主线程的 committed log，所以也要拦。

往下还有两处「让位」逻辑：`tengu_cobalt_raccoon` 实验开关打开时主动抑制 auto compact，把 413 交给 reactive compact 处理（比较「预判阈值」与「事后反应」两种策略的 A/B）；context collapse 启用时同样让位——collapse 的 90% commit / 95% blocking 流程自己管理余量，auto compact 卡在 93% 上下，抢跑会把 collapse 正要细粒度保留的上下文一次性轰掉。

### 3.4 双路径：session memory 优先

`autoCompactIfNeeded()` 判定要压之后走两条路径，顺序是关键：

```typescript
// src/services/compact/autoCompact.ts:287-310
// EXPERIMENT: Try session memory compaction first
const sessionMemoryResult = await trySessionMemoryCompaction(
  messages,
  toolUseContext.agentId,
  recompactionInfo.autoCompactThreshold,
)
if (sessionMemoryResult) {
  setLastSummarizedMessageId(undefined)
  runPostCompactCleanup(querySource)
  if (feature('PROMPT_CACHE_BREAK_DETECTION')) {
    notifyCompaction(querySource ?? 'compact', toolUseContext.agentId)
  }
  markPostCompaction()
  return { wasCompacted: true, compactionResult: sessionMemoryResult }
}
```

session memory 路径成功，整个压缩过程零 LLM 调用——记忆是后台提前提取好的，此时只需要读文件。失败才落到 `compactConversation()` 走 LLM 流式摘要。notifyCompaction 那行也有事故背景：注释记录 BQ 2026-03-01 缺了它，`tengu_prompt_cache_break` 事件里 20% 是误报。第七节展开这条优先路径的内部。

![auto compact 的触发线是固定 token 差值，铺在 200K 窗口上](/images/claudecode/04-compact-thresholds.svg)

## 四、Micro Compact：调用前的轻量清理

Micro compact 是最轻的一级，`microCompact.ts`（530 行），每次 API 调用前都跑。它的目标只有一个：清掉旧工具结果，而且几乎不花成本。

### 4.1 谁的结果可以被清

不是所有工具都值得清，白名单写死在源码里：

```typescript
// src/services/compact/microCompact.ts:40-50
// Only compact these tools
const COMPACTABLE_TOOLS = new Set<string>([
  FILE_READ_TOOL_NAME,
  ...SHELL_TOOL_NAMES,
  GREP_TOOL_NAME,
  GLOB_TOOL_NAME,
  WEB_SEARCH_TOOL_NAME,
  WEB_FETCH_TOOL_NAME,
  FILE_EDIT_TOOL_NAME,
  FILE_WRITE_TOOL_NAME,
])
```

入选的工具有两个共同点：输出大、时效低。文件内容和命令输出在后续对话里很少被逐字引用；`TodoWriteTool`、`AgentTool` 这类结果不在名单里，因为它们对后续轮次有持续的参考价值。

### 4.2 三条路径，按优先级短路

`microcompactMessages()` 开头两步就决定了走哪条路：

```typescript
// src/services/compact/microCompact.ts:261-292（节选）
// Time-based trigger runs first and short-circuits. If the gap since the
// last assistant message exceeds the threshold, the server cache has expired
// and the full prefix will be rewritten regardless — so content-clear old
// tool results now, before the request, to shrink what gets rewritten.
const timeBasedResult = maybeTimeBasedMicrocompact(messages, querySource)
if (timeBasedResult) {
  return timeBasedResult
}

// Only run cached MC for the main thread to prevent forked agents
// (session_memory, prompt_suggestion, etc.) from registering their
// tool_results in the global cachedMCState, which would cause the main
// thread to try deleting tools that don't exist in its own conversation.
if (feature('CACHED_MICROCOMPACT')) { /* 走 cached 路径 */ }

// Legacy microcompact path removed — tengu_cache_plum_violet is always true.
// For contexts where cached microcompact is not available (external builds,
// non-ant users, unsupported models, sub-agents), no compaction happens here;
// autocompact handles context pressure instead.
return { messages }
```

**路径一：时间触发**。距离上一条 assistant 消息超过阈值（默认 60 分钟，`timeBasedMCConfig.ts`，GrowthBook 远程下发），说明服务端 1 小时的 cache TTL 已经到期，前缀反正要全量重写——此时直接把旧工具结果替换成标记字符串 `'[Old tool result content cleared]'`（microCompact.ts:36），零额外缓存成本。这条路径挑选的时机本身就是 cache 感知：只在缓存确定已死的时候动手。

**路径二：缓存编辑**。缓存还热着的时候，改消息就是烧钱。这条路径改走 Anthropic 的 `cache_edits` API，接口注释写得很清楚：

```typescript
// src/services/compact/microCompact.ts:299-303
// Key differences from regular microcompact:
// - Does NOT modify local message content (cache_reference and cache_edits
//   are added at API layer)
// - Uses count-based trigger/keep thresholds from GrowthBook config
// - Takes precedence over regular microcompact (no disk persistence)
// - Tracks tool results and queues cache edits for the API layer
```

客户端消息数组原样不动，API 请求层附带一个 `cache_edits` 块，告诉服务端「这些工具结果可以从缓存里删了」。cache key 不变、命中照旧，只是 token 计数不再包含被删的内容。已发送过的 edits 会进 `pinnedEdits` 列表（`pinned` 在固定位置），后续请求要在原位置重发，否则服务端缓存状态会和客户端对不上——`consumePendingCacheEdits()` 与 `pinCacheEdits()` 管着这个生命周期。

实现（`cachedMicrocompact.ts`）被 `feature('CACHED_MICROCOMPACT')` 裁剪，ant 独有；且只在主线程运行，源码注释解释了原因：子 agent 的工具结果若注册进全局 `cachedMCState`，主线程会试图删除自己对话里不存在的工具。

**路径三：无操作**。前两条都不满足时直接原样返回，上下文压力交给 auto compact。这个「什么都不做」也是设计的一部分——注释写明 legacy microcompact 路径已删除，外部构建、非 ant 用户、不支持的模型、子 agent 全部落到这里。

### 4.3 不调 API 的 token 估算

判断「要不要清」需要先知道「现在多大」。`estimateMessageTokens()` 遍历消息块按类型累加：文本和 thinking 用粗估，图片和文档固定按 2000 tokens（`IMAGE_MAX_TOKEN_SIZE`，microCompact.ts:38），最后整体乘上 4/3 的保守系数。宁可高估提前动手，也不低估等着吃 400。

![micro compact 三条路径按缓存状态选择，不碰热前缀](/images/claudecode/04-cache-paths.svg)

## 五、API Microcompact：把清理外包给服务端

第三级更彻底：客户端什么都不做，只构造一份配置随请求下发，由服务端原生处理。`apiMicrocompact.ts`（153 行）定义了两种策略：

```typescript
// src/services/compact/apiMicrocompact.ts:35-61
export type ContextEditStrategy =
  | {
      type: 'clear_tool_uses_20250919'
      trigger?: { type: 'input_tokens'; value: number }
      keep?: { type: 'tool_uses'; value: number }
      clear_tool_inputs?: boolean | string[]
      exclude_tools?: string[]
      clear_at_least?: { type: 'input_tokens'; value: number }
    }
  | {
      type: 'clear_thinking_20251015'
      keep: { type: 'thinking_turns'; value: number } | 'all'
    }
```

名字里的日期后缀是 API 的版本化策略标识，客户端和服务端靠它对齐语义。`clear_tool_uses_20250919` 在 input_tokens 超过触发线（默认 180K）时清旧工具结果，目标保留 40K——两个值刻意与客户端 microcompact 对齐；`clear_at_least` 算成差值 140K，防止服务端象征性清几个工具就宣布达标。`clear_thinking_20251015` 清旧 thinking 块，空闲超过 1 小时（缓存确定已失效）时只保留最近一轮。

有个分界要注意：thinking 清理对所有用户开放，工具结果清理只对 ant 开放——`getAPIContextManagement()` 里 `process.env.USER_TYPE !== 'ant'` 直接提前返回（apiMicrocompact.ts:90），且策略还要环境变量显式开启。

这一级的账很好算：不调 LLM、不改本地消息、不多发一次请求，配置搭在正常 API 调用上。代价是质量——服务端不知道哪些结果对当前任务更重要，只能按顺序清最旧的。

## 六、Reactive Compact：413 之后的反应式压缩

前三级都在请求发出前布局，第四级反着来：等 API 拒绝了再动。`reactiveCompact.ts` 在泄漏源码里不存在（`feature('REACTIVE_COMPACT')` 裁剪，ant 独有），但调用点都在。

### 6.1 错误先扣留，再恢复

第二篇讲过 `query.ts` 的流式处理循环里有个扣留（withhold）机制，当时只点了名，这里看全四类检查：

```typescript
// src/query.ts:799-825
let withheld = false
if (feature('CONTEXT_COLLAPSE')) {
  if (contextCollapse?.isWithheldPromptTooLong(message, isPromptTooLongMessage, querySource)) {
    withheld = true
  }
}
if (reactiveCompact?.isWithheldPromptTooLong(message)) {
  withheld = true
}
if (mediaRecoveryEnabled && reactiveCompact?.isWithheldMediaSizeError(message)) {
  withheld = true
}
if (isWithheldMaxOutputTokens(message)) {
  withheld = true
}
if (!withheld) {
  yield yieldMessage
}
```

可恢复的错误（prompt-too-long、媒体超限、输出截断）先不 yield 给用户，扣在手里给恢复逻辑一个机会：恢复成功，这条错误用户永远看不到；恢复失败，错误才浮出。体验上用户只会觉得「处理得有点久」，而不是「出错之后又在偷偷修」。

### 6.2 恢复的顺序：先细后粗

413 被扣住后，恢复分两步。第一步排空 context collapse 暂存的折叠（粒度细、信息保留多），这是第二篇那条 `collapse_drain_retry` continue（query.ts:1115）。第二步才轮到 reactive compact 全量摘要：

```typescript
// src/query.ts:1119-1166（节选）
if ((isWithheld413 || isWithheldMedia) && reactiveCompact) {
  const compacted = await reactiveCompact.tryReactiveCompact({
    hasAttempted: hasAttemptedReactiveCompact,
    /* querySource、abort 信号、消息与 cache 参数略 */
  })
  if (compacted) {
    const postCompactMessages = buildPostCompactMessages(compacted)
    for (const msg of postCompactMessages) { yield msg }
    state = {
      messages: postCompactMessages,
      hasAttemptedReactiveCompact: true,  // 防止无限重试
      transition: { reason: 'reactive_compact_retry' },
    }
    continue  // 用压缩后的消息重试 API
  }
  // 恢复失败：浮出错误
  yield lastMessage
  void executeStopFailureHooks(lastMessage, toolUseContext)
  return { reason: isWithheldMedia ? 'image_error' : 'prompt_too_long' }
}
```

`hasAttemptedReactiveCompact` 只给一次机会：压缩后仍然 413 就不再试，直接把错误交出去。恢复失败后的去向也有讲究——源码注释写明不跑 stop hooks：「模型从未产生有效响应，hooks 没有有意义的评估对象。在 prompt-too-long 上运行 stop hooks 会制造死亡螺旋：error → hook blocking → retry → error → …（hook 每轮都注入更多 token）」。第二篇提过这曾烧掉数千次 API 调用，这里是它的另一半现场。

### 6.3 哲学：让 API 当裁判

Auto compact 要预测「何时该压」，83.5% 的阈值是经验值，不同对话模式下最优值并不一样。Reactive compact 干脆不预测，等 API 自己说「太长了」再动——代价是 413 后多一次重试延迟，而扣留机制把这个延迟藏在了「正在处理」的表象之下。`tengu_cobalt_raccoon` 开关做的就是这两派的 A/B：开关打开时 auto compact 整体让位（3.3 节），reactive 成为唯一压缩路径。

## 七、Session Memory Compact：把 LLM 成本前置到后台

第五级是整个体系里最新的一级：压缩时零 LLM 调用，摘要成本早在对话进行中就被后台消化了。`sessionMemoryCompact.ts`（630 行）。

### 7.1 摘要是提前做好的

`src/services/SessionMemory/sessionMemory.ts` 在对话进行中跑一个后台 forked agent，定期提取关键信息写进 markdown 文件。节奏由配置控制：

```typescript
// src/services/SessionMemory/sessionMemoryUtils.ts:31-36
// Default configuration values
export const DEFAULT_SESSION_MEMORY_CONFIG: SessionMemoryConfig = {
  minimumMessageTokensToInit: 10000,
  minimumTokensBetweenUpdate: 5000,
  toolCallsBetweenUpdates: 3,
}
```

对话到 10K tokens 才初始化，之后每增长 5K tokens 或每 3 次工具调用更新一次。提取确实调 LLM，但调用发生在后台、与主对话异步、摊在整段对话里。auto compact 触发时记忆已经躺在磁盘上，压缩只剩读文件。「零 LLM 压缩」的说法容易让人以为 LLM 消失了——它只是被挪出了压缩的关键路径。提取机制本身的细节留给第八篇。

### 7.2 保留多近的消息

压缩不能只剩一份摘要，最近几轮消息要原样保留，否则模型连「正在干什么」都不知道。保留多少由三个数决定：

```typescript
// src/services/compact/sessionMemoryCompact.ts:57-61
export const DEFAULT_SM_COMPACT_CONFIG: SessionMemoryCompactConfig = {
  minTokens: 10_000,           // 保留消息的最少 token
  minTextBlockMessages: 5,     // 最少保留 5 条含文本块的消息
  maxTokens: 40_000,           // 硬上限
}
```

`calculateMessagesToKeepIndex()` 从 `lastSummarizedMessageId`（记忆已覆盖到的位置）向后取消息，不够 10K tokens 或不够 5 条文本消息就往前扩，触到 40K 上限为止。两个最小值一个保证工作状态连续，一个保证用户意图的演变可追；向前扩展的下界是上一个压缩边界——跨过它就会破坏磁盘上消息链的连续性。

### 7.3 切分点要尊重 API 不变量

切分点不能随便落，`adjustIndexToPreserveAPIInvariants()` 处理两类约束。一是 tool_use/tool_result 配对：保留的消息里有 `tool_result`，就必须连着包含对应 `tool_use` 的 assistant 消息，否则 API 报「orphan tool_result references non-existent tool_use」。二是 thinking 块合并：流式响应会把同一次 API 调用（同一个 `message.id`）的 thinking 和 tool_use 拆成多条消息存储，发送前 `normalizeMessagesForAPI` 又要求把它们合并回去——切分点落在中间，thinking 就会在合并时丢掉。函数对着保留范围内每个 `message.id` 往前补齐同 id 的消息，把切分点顶到安全位置。

### 7.4 结果构造与恢复会话

压缩结果由 `createCompactionResultFromSessionMemory()` 组装：session memory 文件内容包成 summary 消息，配上前述保留消息，超长段落截断并注明。源码注释里有一句 `// SM-compact has no compact-API-call`——这条路径明确不产生 API 调用，`postCompactTokenCount` 与 `truePostCompactTokenCount` 因此收敛到同一个估算值。

`--resume` 恢复会话是个特殊场景：进程重启后内存里的 `lastSummarizedMessageId` 丢了，但记忆文件还在磁盘上。源码的处理是把起点设为消息末尾（不保留任何消息），遥测记 `tengu_sm_compact_resumed_session`，再由保留策略从尾部向前扩到满足最小值——所有消息被记忆替代，最近的几条拉回来当工作上下文。

![session memory 把摘要成本前置到后台，压缩时刻只读文件](/images/claudecode/04-sm-offload.svg)

## 八、LLM 摘要底座：compactConversation

前面各级最终都绕不开一个兜底：真要做高质量摘要时，怎么把这次 LLM 调用的成本和风险压到最低。`compact.ts`（1,705 行）就是这个兜底。

### 8.1 摘要 prompt：九段式结构

`prompt.ts` 要求模型按九个固定段落输出：Primary Request and Intent、Key Technical Concepts、Files and Code Sections（含完整 snippet）、Errors and fixes、Problem Solving、All user messages、Pending Tasks、Current Work、Optional Next Step。模型先在 `<analysis>` 标签里打草稿，再输出 `<summary>` 块；`formatCompactSummary()` 送入上下文前剥掉草稿只留正文——思考的机会给了，token 不为草稿买单。

prompt 里还有一个 `NO_TOOLS_PREAMBLE` 前缀，明确禁止摘要过程调工具。起因写在注释里：forked agent 路径为了 cache key 匹配继承了主对话的完整工具集，Sonnet 4.6 的 adaptive-thinking 模型偶尔会在摘要任务中尝试工具调用——fork 只有 1 轮预算，调用被拒就整轮落空、只能回退到直接流式路径，这个概率 4.6 上是 2.79%（4.5 只有 0.01%）。前缀把「别调工具」钉在提示最前面，堵的就是这个浪费。

### 8.2 forked agent 路径：复用主对话的缓存

`streamCompactSummary()` 优先通过 `runForkedAgent` 发起摘要请求——fork 复用主对话的 prompt cache（传递相同的 `cacheSafeParams`），避免压缩调用自己再交一次缓存写入钱。两个防护细节：压缩 agent 的 `canUseTool` 直接返回 deny，摘要过程不产生任何工具调用；fork 失败则回退到直接流式路径，用一句极简系统提示（"You are a helpful AI assistant tasked with summarizing conversations."）和最小工具集。

### 8.3 buildPostCompactMessages：所有路径的统一出口

无论哪一级压缩，结果都汇成 `CompactionResult`，再由同一个函数拼装新消息数组：

```typescript
// src/services/compact/compact.ts:330-338
export function buildPostCompactMessages(result: CompactionResult): Message[] {
  return [
    result.boundaryMarker,      // 压缩边界标记
    ...result.summaryMessages,  // 摘要消息
    ...(result.messagesToKeep ?? []),  // 保留的最近消息
    ...result.attachments,      // 附件（文件、计划、技能等）
    ...result.hookResults,      // hook 消息（CLAUDE.md 等）
  ]
}
```

固定顺序保证五级路径产出的结构一致。`boundaryMarker` 是一条 `SystemCompactBoundaryMessage`，标记压缩发生的位置——后续 `getMessagesAfterCompactBoundary()` 从这个标记开始读「压缩后的消息」，跳过全部旧历史。

### 8.4 摘要之后的现场恢复

摘要生成后，`createPostCompactFileAttachments()` 会把最近访问的文件重新带回上下文，预算写死在常量里：

```typescript
// src/services/compact/compact.ts:122-130
export const POST_COMPACT_MAX_FILES_TO_RESTORE = 5
export const POST_COMPACT_TOKEN_BUDGET = 50_000
export const POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
// Skills can be large (verify=18.7KB, claude-api=20.1KB). Previously re-injected
// unbounded on every compact → 5-10K tok/compact. Per-skill truncation beats
// dropping — instructions at the top of a skill file are usually the critical
// part. Budget sized to hold ~5 skills at the per-skill cap.
export const POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
export const POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
```

5 个文件、每个 5K、总 50K；技能内容每个 5K、总 25K——注释记录了技能曾按全文无上限重注入、每次压缩多烧 5~10K tokens 的历史。文件用 `FileReadTool` 重新读而非用缓存，已作为工具结果存在于保留消息里的跳过，避免同一内容进两遍。计划文件、plan mode 指令、已调用技能、MCP 指令 delta 也一并回灌——压缩后的模型要知道自己有哪些工具、处于什么模式。

### 8.5 部分压缩：给用户一个支点

`partialCompactConversation()` 支持选定一条消息做支点，向一个方向压缩。`from` 方向摘要支点之后的消息、保留前面——前缀不动，缓存友好；`up_to` 方向摘要支点之前的消息、保留后面——摘要顶到前面，后面的消息整体后移，缓存作废。用户还能传 `userFeedback` 注入压缩 prompt，让摘要聚焦自己关心的内容。

### 8.6 PTL 重试：压缩的压缩

最极端的情况：对话长到连压缩请求本身都超限。`truncateHeadForPTLRetry()` 从最旧的消息组开始丢，直到把超限的 token 缺口填平，整个丢弃重试最多 3 轮（`MAX_PTL_RETRIES`）：

```typescript
// src/services/compact/compact.ts:243-291（节选）
export function truncateHeadForPTLRetry(
  messages: Message[],
  ptlResponse: AssistantMessage,
): Message[] | null {
  const groups = groupMessagesByApiRound(input)
  if (groups.length < 2) return null
  const tokenGap = getPromptTooLongTokenGap(ptlResponse)
  let dropCount: number
  if (tokenGap !== undefined) {
    let acc = 0
    dropCount = 0
    for (const g of groups) {  // 精确丢弃：按 token gap 累加
      acc += roughTokenCountEstimationForMessages(g)
      dropCount++
      if (acc >= tokenGap) break
    }
  } else {
    dropCount = Math.max(1, Math.floor(groups.length * 0.2))  // 模糊丢 20%
  }
  // 保留至少一个组用于摘要
}
```

错误信息里的 token 缺口解析得出来就按缺口精确丢，解析不出来（部分 Vertex/Bedrock 的错误格式）就模糊丢 20% 的组。注释称这是 CC-1180 的最后逃生舱——丢上下文有损，但总比卡死强。这里的分组单位来自 `grouping.ts` 的 `groupMessagesByApiRound()`：按 assistant `message.id` 划分，同一次 API 调用产生的 thinking 和 tool_use 块共享 id、归入同组，正好是可安全丢弃的最小单元。

### 8.7 手动 /compact：三条路径的汇合点

手动压缩（`commands/compact/compact.ts`，287 行）与 auto compact 的差别在于用户在场：错误要展示（`addErrorNotificationIfNeeded()`），还接受自定义指令注入压缩 prompt。路径优先级是 session memory → reactive → 传统 `compactConversation`，前者成功就短路后者（第六篇讲命令系统时从调度器视角看过这个汇合点）。传统路径里手动压缩会先跑一遍 `microcompactMessages()` 清工具结果再摘要——先做便宜的清理，摘要请求本身也能小一点。

## 九、主循环集成：一条管线的出场顺序

五级怎么挂进 `query.ts` 的循环，第二篇给过骨架，这里补压缩侧的细节。集成走依赖注入，`query/deps.ts` 把四个可替换依赖交给测试：

```typescript
// src/query/deps.ts:21-39
export type QueryDeps = {
  callModel: typeof queryModelWithStreaming
  microcompact: typeof microcompactMessages
  autocompact: typeof autoCompactIfNeeded
  uuid: () => string
}
```

每个迭代里压缩管线的实际顺序是：**snip → microcompact → context collapse → auto compact → blocking 检查 → API 调用（含 API microcompact 配置）→ reactive compact（如 413）**。低成本的先跑，`messagesForQuery` 把每一步的产出传给下一步，直到最重的 LLM 摘要只在前面全部失守时才出场。

microcompact 排在 auto compact 前面有实际意义：auto compact 看到的 token 数是清理之后的。工具结果清得够多，阈值检查可能直接通过，这一轮根本不需要摘要。「先做便宜的清理、再判断要不要贵的摘要」，这个顺序本身就是五级设计的缩影。

## 十、横向对比与要点回顾

| 维度 | Claude Code | OpenCode | Codex |
|------|-------------|----------|-------|
| 压缩层级数 | 5 级 | 2 级 | 3 级 |
| 数据结构级压缩 | 4 条路径 | 无 | 无 |
| LLM 调用 | 仅摘要兜底 + 后台提取 | 每次压缩均调 | 每次压缩均调 |
| 触发方式 | 阈值 + API 错误 + 时间间隔 + 会话边界 | 阈值 | 阈值 + 手动 |
| Prompt cache 感知 | cache_edits 不破坏缓存 | 无 | 有 |
| 后台异步摘要 | Session memory | 无 | 无 |
| 断路器 | 连续失败 3 次停止 | 无 | 无 |
| 服务端协作 | API microcompact + cache_edits | 无 | 无 |
| 部分压缩 | from / up_to 双向 | 无 | 无 |
| PTL 重试 | 有（3 次） | 无 | 无 |

对比里最有分量的差异在关键路径上。OpenCode 和 Codex 每次压缩都同步等一个完整的 LLM 响应，期间对话停摆；CC 在 session memory 可用时压缩是毫秒级的读文件加数据变换，摘要兜底也尽量走 forked agent 复用缓存。服务端协作是另一道护城河：`ContextManagementConfig` 和 `cache_edits` 都是 Anthropic API 的原生能力，CC 作为官方产品知道这些能力存在、并且敢把压缩策略押在上面。

四条设计原则收束这一篇：

1. **能用数据结构变换解决的，就不调 LLM**。五级里四条路径不碰 LLM，摘要永远是最后出场的兜底——每升一级都有明确的成本预算。
2. **压缩必须感知缓存**。时间路径只在缓存确定已死时动手，cache_edits 路径干脆不碰消息数组，摘要兜底也要 fork 复用前缀。
3. **贵的成本要么前置、要么外移**。session memory 把摘要前置到对话后台，API microcompact 把清理外移给服务端，关键路径上剩下的都是便宜操作。
4. **每一级都要有失败预案**。断路器（3 次跳闸）、`hasAttemptedReactiveCompact`（一次机会）、PTL 重试（压缩的压缩）、递归守卫（fork 不再触发压缩）——压缩系统的健壮性一半来自「怎么压」，另一半来自「压不动了怎么办」。

下一篇进入 Agent 协作，看 subagent 怎么被孵化、路由和回收——其中会再次遇到压缩：每个 teammate 都要有自己独立的压缩状态。

## 章节小测

<script setup>
const q = [
  {
    question: 'Claude Code 五级压缩机制的核心设计思想是什么？',
    options: [
      '在所有对话历史环节追求最大化的 token 节省效果',
      '保证每次 API 调用前都执行一次全量上下文压缩操作',
      '优先用数据结构变换解决问题仅在必要时调用 LLM 做摘要',
      '始终调用 LLM 以获取最高质量的语义摘要结果'
    ],
    correct: 2,
    explanation: '四条不调 LLM 的路径：micro compact 的两条（时间触发清工具结果、cache_edits 删缓存）、API microcompact、session memory。reactive compact 本身要做摘要，是调 LLM 的；413 后先排空的 context collapse 属于另一套机制。compactConversation 摘要是最后兜底，与 OpenCode 和 Codex 每次压缩都调 LLM 形成对比。其余选项或违背分层设计（每次全量压缩），或把兜底当常态（始终调 LLM）。'
  },
  {
    question: 'Cached microcompact 路径比直接修改本地消息更优的核心原因是什么？',
    options: [
      '完全免去客户端侧计算降低本地资源占用开销',
      '通过 cache_edits 让服务端删内容而客户端消息不变',
      '实现代码量远小于传统修改消息的压缩方案',
      '利用服务端精确估算以实现更准确的 token 计数'
    ],
    correct: 1,
    explanation: 'cache_edits 块附在 API 请求层，客户端消息数组原样不动、cache key 不被破坏，但服务端 token 计数不再包含被删内容——这是「不破坏缓存前提下压缩」的核心。已发送的 edits 进 pinnedEdits 固定位置重发。其余选项的错误：它仍需客户端跟踪与注册工具结果；代码量不小；token 估算仍是客户端粗估。'
  },
  {
    question: 'Auto compact 的断路器（连续失败 3 次停止重试）依据什么数据设计的？',
    options: [
      '线上数据监测显示超长会话连续失败可达数千次严重浪费',
      '线下压力测试中反复暴露的死循环导致 API 烧费急剧增加',
      '模型训练阶段统计出的上下文压缩最佳失败容限值',
      '缺乏充分的线上数据支撑仅为工程经验的主观猜测'
    ],
    correct: 0,
    explanation: '源码注释记录 BQ 2026-03-10 的数据：1,279 个 session 出现 50+ 次连续失败、最多 3,272 次，全局每天浪费约 25 万次 API 调用。断路器让该 session 内不再尝试 auto compact。这不是线下压测发现、更非训练统计或主观经验。'
  },
  {
    question: 'Session memory compact 实现「零 LLM 调用压缩」的根本手段是什么？',
    options: [
      '只压缩纯工具调用结果完全不做语义级别的摘要处理',
      '基于纯规则匹配替代模型推理以实现零推理成本压缩',
      '直接丢弃旧消息不做任何摘要处理以追求极致速度',
      '将 LLM 调用成本前置到对话进行中的后台异步提取环节'
    ],
    correct: 3,
    explanation: '后台 forked agent 在对话进行中定期提取关键信息写文件（对话 10K tokens 起步、每 5K tokens 或 3 次工具调用更新），压缩触发时只剩读文件。LLM 成本没有消失，只是被挪出了压缩的关键路径。session memory 本身仍是 LLM 生成的摘要，不是规则匹配，也不是丢弃。'
  },
  {
    question: 'query 循环里 413（prompt too long）错误为什么要先「扣留」（withhold）而不立即展示给用户？',
    options: [
      '等待遥测系统完整记录错误上下文后再决定上报策略',
      '避免用户在错误提示期间继续输入打断重试的完整性',
      '给恢复逻辑留出机会成功后错误就无需再让用户看到',
      '权限系统需要先确认错误内容是否包含敏感的信息'
    ],
    correct: 2,
    explanation: '扣留机制先把可恢复错误扣在手里：413 依次尝试排空 context collapse、reactive compact 压缩后重试，成功则错误消息永不 yield 出去，失败才浮出。用户体验是「处理得久一点」而非「出错后偷偷修」。扣留与遥测记录、防打断、权限检查均无关系。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
