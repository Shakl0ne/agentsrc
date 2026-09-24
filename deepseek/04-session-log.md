---
title: 会话日志与上下文投影：模型看到的历史从哪来
---

# 会话日志与上下文投影：模型看到的历史从哪来

上一篇拆循环时反复出现一句话：每个请求都从会话日志推导。这一篇就把日志本身拆开。先给一个反直觉的事实：`dsh` 里**没有"历史消息"这个存储**——模型看到的上下文、UI 的回放、持久化到磁盘的会话、fork 出的分支，全部是同一份**追加式事件日志**的不同投影。

这个选择值得专门用一篇来解释。读完你会带着三个问题的答案离开：

- 只追加、不许改的日志，怎么折叠出模型要看的消息数组？
- 压缩要"删掉"一段旧对话，在一份不许改的日志上怎么做到？
- "凡是模型可见的都必须在日志里"这条约束，凭什么不是一句口号？

请求头、代际迁移这些持久化侧的机制放在其中一节带过；工具结果的修剪策略属于压缩插件自己的事，点到为止。

## 一、追加式日志：唯一事实源

一个 `Session` 就是一条只能往尾部追加的类型化事件流。每条事件的形状：

```ts
// packages/core/session/src/types.ts:493-511（节选）
export type SessionEvent<T extends SessionEventType = SessionEventType> = {
  [K in SessionEventType]: {
    type: K
    seq: SessionSeq      // 单调位置（品牌化数字）
    time: number         // epoch ms
    data: SessionEventMap[K]
    ignorable?: true     // 未知类型时可否安全跳过
  }
}[T]
```

三件事在这条类型上就能读出来。**追加式**：`seq` 连续单调，谁也不许回改历史。**带类型**：`SessionEventMap` 是声明合并扩展的联合，插件用 `declare module` 就能往会话里加新事件类型，`switch(type)` 精确窄化载荷。**可拒绝**：不认识的事件类型默认让读取方拒绝重建——除非事件标了 `ignorable: true`，声明自己是纯信息性的、跳过不影响理解。宁可拒绝读取，也不静默残缺，失败方向是定死的。

写入端有一道硬校验：`session.append()` 会把载荷做一次**无损 JSON 验证**——BigInt、循环引用、稀疏数组、`-0`、异常原型全部在写入点拒绝，日志里的每条事件都是原样落盘、原样读回的。这一步把"能持久化"和"能回放"合并成了同一件事：落盘就是把日志写出去，回放就是把日志读回来，两边共享同一份格式。

事件分两大类，上一篇已经见过分界线。**持久化事件**（`turn/*`、`step/*`、`user/message`、`assistant/message`、`tool/*`……）是"已经发生的事实"，落日志；**活事件**（`agent/*`、`llm/stream`、`tools/*`）是"正在跑的过程"，可以拦截改写，不落日志。模型上下文属于前者。

## 二、Surface 与投影：从日志到 Message[]

日志记录了一切，模型要的是消息数组。`deriveMessages()` 负责这次翻译，它的缓存实现能看清整个思路：

```ts
// packages/core/session/src/index.ts:842-856（节选）
deriveMessages(): Message[] {
  const surface = this.surface
  const nodes = surface.nodes
  const generation = surface.contentGeneration
  if (generation !== this.derivedGeneration) {
    this.derived = []
    this.derivedNodes = 0
    this.derivedGeneration = generation
  }
  for (const seq of nodes.slice(this.derivedNodes)) {
    const msg = this.deriveEventMessage(this.log[seq]!)
    // …空内容节点等特殊情形在此过滤
  }
}
```

投影不是扫整条日志，而是沿一份 **Surface（有序表面）**折叠。五种消息类事件在表面上有节点：`system/message`、`developer/message`、`user/message`、`assistant/message`、`tool/result`；其余事件（chunk、turn 边界、attempt）天然缺席推导。每个表面节点第一次被投影时深冻结缓存，之后复用；`contentGeneration` 一变（有替换或投影变更）缓存整体作废重建。返回的数组每次是新的，里面的消息对象是共享且冻结的——派生历史不可改写。

每种事件怎么变成消息，规则写得很细：`assistant/message` 携带拼好的完整消息（内嵌确切的紧凑流数据）；空内容的 system/developer 节点不投影（清空系统提示必须落"空替换"事件，而不只是改最后一条）；工具结果变成带 `tool-result` 块的 user 消息。

![一份日志派生多个投影](/images/deepseek/04-log-projections.svg)

关键的机制是 `SurfaceOp`。消息类事件 append 时必须声明自己的表面操作：

```ts
// packages/core/session/src/types.ts:462-464
export type SurfaceOp =
  | 'append'
  | { op: 'replace'; startSeq: SessionSeq; endSeq: SessionSeq }
```

`append` 是正常入列；`replace` 用这条新事件**遮蔽**从 `startSeq` 到 `endSeq`（含端点、按当前表面顺序）的旧节点——旧事件还在日志里，只是从表面上消失，推导不再经过它们。`sourceEventSeqs` 记录每个节点的派生来源，被遮蔽的节点可以追溯。这份"表面"是日志之上的薄薄一层可变视图，也是后文压缩一节能成立的地基。

## 三、model-visible ⟺ logged：运行时断言

现在把全系统最硬的约束摆出来。架构文档的原话：

> **Model-visible means logged.** Anything that reaches a model request must be reconstructable from the log, and a runtime invariant asserts it. A new model-visible input requires a session event.

这句话约束的是所有插件作者：想往模型上下文里加东西，必须先声明一个新的会话事件类型、从日志里渲染出来，不存在"绕过日志直接塞给模型"的口子。

它凭什么不是口号？因为有一个 invariant 伴随插件在每个 `llm/stream` 请求上逐条断言，源码只有几十行，核心是：

```ts
// packages/core/agent-loop/src/invariant.ts:40-53（节选）
const expected = session.deriveMessages()
if (JSON.stringify(options.messages) !== JSON.stringify(expected)) {
  fail(`llm request for session "${String(session.id)}" diverges from the dispatch-time durable derivation (log-reconstruction desync)`)
}
const headerMatches = options.model === header.config.model
  && options.system === undefined
  && options.temperature === header.config.temperature
  && options.maxTokens === header.config.maxTokens
  && JSON.stringify(options.stop) === JSON.stringify(header.config.stop)
  && JSON.stringify(options.tools ?? []) === JSON.stringify(header.tools ?? [])
if (!headerMatches) {
  fail(`llm request … diverges from the folded request header`)
}
```

发出去的请求，消息必须与当前日志推导完全一致，配置必须与折叠的 `request/header` 一致，系统提示必须以表面节点 0 的身份走在 `messages` 里而不是 `system` 字段。任何一条不满足，报 "log-reconstruction desync" 直接失败。前面几篇见过的机制在这里拼成闭环：loop 从日志推导请求（03 篇）、日志承载全部事实（本篇）、invariant 在每个请求出口验算两者相等。**"重放日志 = 重现这次会话"从约定被压成了断言。**

这条约束的回报有四份：回放必然成立（输出都能从日志重建）；resume 不需要快照（新进程读日志即恢复上下文）；持久化简化为存日志（不存在第二份状态失步）；审计免费（凡模型可见，日志必有一份）。

## 四、fork 与恢复：日志的再利用

日志的派生能力里有几处值得拆开看的：**fork**、**崩溃修复**，加上持久化侧的代际迁移。

`ctx.sessions.fork(source, boundary?, childSessionId?)` 从活会话拷贝一段精确的包含性事件前缀（默认到最后一 event）。`buildForkSeed` 在拷贝的前缀之后落一个 `inherited` 标记、只给**开着的 step** 补缺失的错误工具结果、然后用 `forked` 理由关掉这个 step 和 turn——子会话从"已闭合的干净前缀"起步，`ownEvents()` 从标记之后算自己的事件。边界不挑位置：`fork` 可以切在开着的 turn 中间，未闭合的尾巴由 `buildForkSeed` 补齐收口，唯一要求是边界落在一条连续存在的 seq 上。

补出来的错误结果分两种，措辞是给模型读的：孤儿工具调用（有 `tool/call` 无结果）补 `TOOL_OUTCOME_UNKNOWN`，明确告诉模型"结果未知；只读或幂等操作可以直接重试，有副作用的先核实外部状态"；连 start 记录都没有的补 `TOOL_NOT_STARTED`。模型拿到的不是一句干巴巴的 error code，而是带操作指引的判断依据。

**崩溃修复**是 fork 语义的孪生兄弟：`repair.ts` 冷修复崩溃孤儿日志（比如 03 篇的 `interrupted` 收尾理由就来自这里）；持久化侧则有代际迁移——JSONL v0 用 `session.jsonl[.zstd]`，v1 起用小写的 `session.vN.jsonl[.zstd]`，已提交的代际路径永不改名、不被覆盖、不被删除；格式升级走"邻接迁移链"，每个迁移包只负责一步 `vN → vN+1`，读取方按最高代际选择、只认识当前逻辑格式。会话数据的演进同样遵守"只追加"的世界观：不修改历史，发布下一代。

## 五、压缩：在只追加的日志上"删"东西

长会话终究要压缩。`dsh` 的答案分两层：**机制**给到会话日志（surface replace），**策略**整个做成一条 capability 缝——`ctx.compaction` 是接口、`dsh-compaction-basic` 是默认实现、`/compact` 命令是消费方，三件套的机制留给下一篇专讲。文档原话：Compaction is one optional capability, not part of the agent-loop spine——压缩与 bash 同构，loop 脊柱里没有它的位置。

压缩对日志做三件事。第一，**摘要以一条独立的 `user/message` 落地**，带 `surfaceOp: { op: 'replace', startSeq, endSeq }`——这是 summary 型压缩执行的唯一一次表面变更。被替换的旧段从此对推导不可见，`shadowedSeqs` 记录权威的被遮蔽节点集。有个容易绕住的细节：`shadowedRange` 是表面位置跨度，做过一次 replace 之后，新摘要节点落在旧位置上，`start` 完全可以大于 `end`——认 `shadowedSeqs`，别按数值区间理解。

第二，**锁写进日志**。压缩事务由三条 log-only 事件夹住：`compaction/start`（拿锁，记 turn 号或手动时的 `null`）→ 摘要 → `compaction/summary`（记摘要、被遮蔽范围、token 数、模型调用）→ `compaction/end`（放锁）。锁最后释放是有意设计：操作中途崩溃，日志里留下的是"有 start 无 end"的**孤儿锁**——可检测、可修复；顺序反过来就会留下一条谎称压缩已完成的 `end`。活跃的未匹配 start 会阻塞所有压缩入口。

第三，**触发与恢复挂在 loop 的拦截点上**。压力压缩跑在 `agent/pre-step`（请求推导前检查上下文压力），溢出恢复跑在 `agent/request-error`（413 之后在开着的 step 内重试，且只有表面替换代际推进了才重试，否则原错误保持权威）。三个入口对应三种时机：`compactIfNeeded`（自动策略，trigger 是 `pressure` 或 `context-overflow`）、`compactNow`（手动 `/compact`，空闲时低于阈值也压）、`compactRegion`（显式范围）。

对 KV cache 的影响也记录在案：append 保前缀，`replace` 从第一个被遮蔽的消息开始作废复用——日志保持只追加，缓存的前缀却断了，这笔账 Model Experience 文档写得很清楚。

![压缩事务：日志里的锁与表面替换](/images/deepseek/04-compaction-lock.svg)

## 六、与三栏对比：谁把"日志"当得最彻底

| 维度 | OpenCode | Codex | DeepSeek Harness |
|------|----------|-------|------------------|
| 模型历史来源 | messages 数组 + SQLite | 会话累积的 Message[] | `deriveMessages()` 从日志投影 |
| 另存一份"历史" | 是 | 是 | 否，唯日志 |
| 压缩落点 | 自有压缩策略 | 自有压缩策略 | surface replace + 独立压缩缝 |
| 运行时保证 | schema 校验 | 部分 | 逐请求 invariant 断言 |

三个终端 Agent 的持久化都是"平行于模型上下文的另一份存储"；`dsh` 把模型上下文本身做成日志的投影，持久化与回放与上下文三件事共用一个源，再用运行时断言把这个等式焊死。代价也明摆着：每次请求都要走一遍投影与校验，写路径多了一层不可绕过的纪律。换来的是"日志永不撒谎"——这对一个要被任意产品装配的引擎，比省下的那点开销值钱。

下一篇离开日志，去看它消费得最重的邻居：capability 缝——为什么换一个 provider 能牵一发动全身，工具裁决链又挂在这张网的哪个位置。

## 源码索引

- `packages/core/session/src/types.ts` — `SessionEvent`、`SessionEventMap`、`SurfaceOp`
- `packages/core/session/src/index.ts` — `Session`、append 校验、`deriveMessages` 缓存
- `packages/core/session/src/surface.ts` — 有序表面投影与替换验证
- `packages/core/session/src/fork.ts` — `buildForkSeed` 与孤儿工具结果
- `packages/core/session/src/repair.ts` — 崩溃孤儿日志的冷修复
- `packages/core/agent-loop/src/invariant.ts` — 请求重建断言
- `packages/core/session/README.md` — 事件溯源契约与投影规则
- `docs/subsystems/compaction.md` — 压缩缝、事件表、锁语义
- `docs/architecture.md` — Session log 与代际迁移

## 章节小测

<script setup>
const q = [
  {
    question: '`dsh` 不单独存"历史消息"，直接收益是什么？',
    options: ['回放、持久化、上下文共用一个源', '减少了内存里的对象数量', '让模型请求变快了', '日志文件比数据库更小'],
    correct: 0,
    explanation: '单一事实源使"能回放 == 能持久化 == 能构建上下文"，三份状态不会失步。B/C/D 都不是这个设计的目标，投影本身反而多了一层工作。'
  },
  {
    question: '`SurfaceOp` 的 `replace` 到底做了什么？',
    options: ['把旧事件从日志里删除', '把整条日志重排一次', '遮蔽旧段，旧事件保留', '把旧消息冻结成只读'],
    correct: 2,
    explanation: 'replace 只改表面视图：旧节点从推导中消失、新摘要落在原位置，日志本身保持追加式不变，被遮蔽节点靠 shadowedSeqs 追溯。A 违反 append-only，B/D 都不是它的语义。'
  },
  {
    question: '运行时 invariant 在每个 `llm/stream` 请求上断言的核心等式是？',
    options: ['请求消息与日志推导逐条一致', '请求 token 数不超过窗口上限', '工具调用都有配对的结果', '事件时间戳严格单调递增'],
    correct: 0,
    explanation: 'invariant 逐请求比对请求载荷与日志推导（含配置对折的 request/header），不一致即报 log-reconstruction desync。B/C/D 是别的检查或不变量，不是这条的核心。'
  },
  {
    question: '压缩的锁为什么 start 先落、end 最后落？',
    options: ['让锁的持有时间最短', '减少日志事件的写入量', '让摘要事件排在最前面', '崩溃留下可检测的孤儿锁'],
    correct: 3,
    explanation: '中途崩溃留下"有 start 无 end"，可检测可修复；反过来会留下一条谎称压缩已完成的 end。锁是日志里的事实，不需要额外的锁状态机。A/B/C 与这个顺序设计无关。'
  },
  {
    question: 'fork 前缀里发现一个"有 tool/call 无结果"的调用，补的 `TOOL_OUTCOME_UNKNOWN` 会告诉模型什么？',
    options: ['直接重试即可，无需判断', '该调用已被系统主动取消', '只读或幂等可重试，副作用需核实', '需要用户手动补一个结果'],
    correct: 2,
    explanation: '补的结果带操作指引：结果未知时只有只读或幂等操作可安全重试，可能有副作用的先核实外部状态或询问用户。A 放弃了判断，B 把"结果未知"说成了"已取消"，D 不是机制的一部分。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
