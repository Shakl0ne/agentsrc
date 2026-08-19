---
title: 会话日志与上下文投影：模型看到的上下文从哪来
---

# 会话日志与上下文投影：模型看到的上下文从哪来

> 本文基于 `dsh-v0.1.0-rc.7`（master @ 99f6f02，2026-08-17）。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前三篇我们走过了：森林（插件）→ 框架（Cordis）→ 默认 driver（agent-loop）。现在到了整个 `dsh` 里**最硬、也最能代表它设计哲学**的一个问题——**模型看到的上下文到底从哪来？**

你可能会觉得这是个老问题：不就是把历史消息攒一份传给模型吗。`dsh` 的答案非常不"当然"：它**不存"历史消息"这个状态**，而是只存一份**追加式的事件日志**，模型的上下文是每次从日志里**推导（derive）**出来的。这一篇就讲清楚：为什么这么做、deriveMessages 怎么推、以及"model-visible ⟺ logged"这条不变量为什么是全文最硬的锚点。

先给这一篇的核心判断：

> **在 `dsh` 里，"能回放"和"持久化"是同一件事——因为它们共享同一个单一事实源：追加式的 `SessionEvent` 日志。** 模型历史、UI 回放、fork、resume、标题、telemetry，全是这份日志的不同投影。


## 一、追加式日志：一份不可变的单一事实源

`docs/subsystems/session.md` 开头就点破了这个模型：

> A `Session` is an **append-only log** of typed `SessionEvent`s — the single source of truth for an agent's whole interaction history. The LLM message history is *derived* from the log, never stored separately; replay is re-derivation from the same events.

拆开来有四个设计点：

1. **追加式（append-only）**。事件只能往尾部加，编号 `seq` 连续单调（`seq = log.length`）。谁也不许回改历史。
2. **带类型（typed）**。`SessionEvent` 是一个**可判别联合**（discriminated union over `type`），`switch(type)` 就能精确窄化 `data`，而不用到处 cast。
3. **单一事实源（single source of truth）**。整个 agent 的所有交互过程，只有一个权威记录。别的视图（上下文、回放、UI）都由它派生。
4. **可扩展（merge-extensible）**。`SessionEventMap` 允许插件用声明合并（declaration merging）往里追加新事件类型——这又一次接回第 2 篇的"一切皆插件"：连"一条会话里能记录哪种事件"都是插件可扩展的。

### 一个 log 条目长什么样

`SessionEvent` 的核心字段，见 `session/subsystems/session.md` 的类型（这里取其运行相关的字段，源码类型还含依 `SurfaceEventType` 条件追加的 surface 元数据分支，下节会讲）：

```ts
type SessionEvent<T extends SessionEventType = SessionEventType> = {
  [K in SessionEventType]: {
    type: K
    seq: number          // 单调位置，seq = log.length
    time: number         // epoch ms
    data: SessionEventMap[K]
    ignorable?: true     // 未知类型时可否跳过
  }
}[T]
```

`seq`/`time` 是"位置 + 时间"，`data` 是该事件自己的载荷。`ignorable` 是个很细但重要的点：一条最新 / 插件新增的**纯信息事件**可以标 `ignorable: true`，让旧读者在不认识它时安全跳过；而不标这个标记的未知事件，读者必须**拒绝重建**，因为它可能改变整个日志之后所有条目的理解。这是"宁可过度拒绝、不静默残缺"的失败方向设计。

为什么"日志即事实源"能成立，得益于一个层叠契约：**所有 `event.data` 必须可无损 JSON 序列化**，而 `Session.append` 会在写入源头做校验——非 JSON 可序列化（BigInt、函数、class 实例、循环引用等）直接拒。这样一来"日志能落盘"和"日志能回放"合二为一：既然日志格式本身保真，那么"持久化"只需把日志原样存下来，"回放"只需原样读回来——**两者共享这段日志，不需要第二份"历史消息"存储**。


## 二、deriveMessages()：从日志推导模型历史

日志是"记录了一切"的仓库，但模型要看到的是"装饰好的消息数组"。这个转换就是 `deriveMessages()`——`docs/subsystems/session.md` 是这么说的：

> `Session.deriveMessages()` projects the event log into the `Message[]` the model sees — cached (each surface node projected once) and frozen.

一条消息怎么从事件推出来，原则如下（`session.md` 的 projection rules）：

- `user/message`（用户输入）→ 一个带精确 `content` 的 user 消息；
- `assistant/message`（拼好的助手消息）→ 带 provider/model 的 assistant 消息；**原始 `assistant/chunk` 事件属于回放/UI 数据，推导时被跳过**——已拼好的消息才是权威；
- `tool/result`（工具结果）→ 一个携带 `tool-result` 块的 user 消息；
- 其它（`turn/*`、`step/*`）是**结构性事件，不投影成消息**。

还有一个容易被忽略的边界：**空 content 的 `assistant/message` 也不投影**。max-tokens 把一次输出打断成零内容时，日志仍会留一条 `assistant/message`（只是为了记录 usage/provider/model），但它不能以"空助手消息"的身份混进模型历史——否则模型会看到一堆空洞的助手回合。

这个"跳过 chunk、用已拼好消息"的取舍是核心：原始 chunk 保留**回放保真**（UI 想逐 token 还原），而**模型历史**用的是 `assistant/message` 这个"已经拼好的权威"，两者职责分离。

### Surface：推导时"哪些事件算数"

更精确地说，推导不是傻白地扫日志，而是维护一个**Surface（有序表面）**。`SessionEvent` 上只有三种事件带"表面元数据"（`SurfaceEventType = user/message | assistant/message | tool/result`），它们各自用 `surfaceOp` 声明自己怎么进表面：

```ts type-equiv
type SurfaceOp =
  | 'append'
  | { op: 'replace'; start: number; end: number }
```

- `append` = 正常向尾部加；
- `{ op:'replace', start, end }` = 用这条新节点**替换**从 `start` 到 `end` 的表面节点，被换掉的（shadowed）必须能被 `sourceEventSeqs` 追溯到——这正是**压缩（compaction）**做"摘掉一段旧对话、用一个摘要替换"用的机制（第 7 篇专门展开）。

所以 `Session.surface` 返回一个**只读的最新表面投影**（`nodes` = 当前模型可见顺序 + `replaceGeneration`），`deriveMessages()` 沿这个表面折叠出消息。这样：

- 未带 surface 标记的事件（chunk、turn 边界）**天然缺席**推导；
- 压缩的 `replace` 从推导里**删掉被遮蔽的旧节点**——模型看到的是摘要，不是被删的历史。

**deriveMessages 的缓存**值得一句：每个 surface 节点第一次见到时投影一次并**深冻结**，之后复用；一个 `replace`（重写）会重建缓存。每次调用返回**新数组**（后面 append 不回充给已持有的调用者），但里面的 `Message` 对象是共享、深冻结的。这样派生历史不可改写：凡是能无损保留的，都在日志里以冻结形式存在，投影只能读、不能改。


## 三、"model-visible ⟺ logged"：全文最硬的不变量

如果只有"从日志用它"，还不够硬。`dsh` 把它推到了极致：**任何能到达模型的东西，都必须能从日志重建；否则这条模型的可见输入不该存在。** `docs/architecture.md` 原话：

> **Model-visible means logged.** Anything that reaches a model request must be reconstructable from the log, and a runtime invariant asserts it. This is why a new model-visible input requires a new session event: extend `SessionEventMap` and render from the log.

把这句话反过来读，它在**约束所有插件作者**：

- 如果你要往模型的上下文里加一个新东西（注入一段指令、塞一个来自子 agent 的结果），
- 你不许"偷偷传一个还没写日志的值"，
- 你必须**先新增一个 `SessionEventMap` 事件**，把这件事记录下来，再从日志渲染给模型。

为什么这个不变量是"最硬"的？因为四个后果是它想保证的：

1. **回放即重演**：因为上次的模型输出都能从日志重建，重放整个日志就能得到完全一样的推导 + 一样的模型历史。UI/agent 的 replay 不是"顺便支持"，而是"必然成立"。
2. **resume 天然可行**：新进程/新对话拿到日志就可以重建上下文，不用额外存一份"上下文快照"，不会因为漏存某份快照而丢上下文。
3. **持久化简化为日志**：因为上下文能重建，落盘只要存日志、不需要另存"模型历史"，也就天然避免了两份状态失步。
4. **审计/telemetry 免费**——凡模型可见的，日志都有；凡是日志的，都可以被 telemetry / 调试观测到。

为了让这条不变量"不只是文档、而是运行时真保证"，`dsh` 配了 companion **invariant**（`@deepseek-ai/dsh-agent-loop/invariant`）——它在每次 `llm/stream` 请求上断言：请求携带的 `messages` 必须与 `session.deriveMessages()` 一致、`request/header` 必须能从日志重建，否则报"log-reconstruction desync"。换句话说，**"这次发给模型的请求 == 就此日志推导出的请求"是在运行时逐请求校验的**。这一层的关键意义在第 3 篇也埋过伏笔：`deriveMessages()` 从日志读上下文、loop 又往日志写事件，两者闭环——**一个模型请求的输入 = 在它之前日志里全部事件的某个函数**，因此"日志能重建模型请求"就等于"重放日志 == 重游那次会话"，而 invariant 把这一关系从"约定"压成了"断言"。


## 四、有资格共享这个单一事实源的：万物皆派生

现在可以把第 2 篇那句口号顺下来了：**不是"模型 context 与持久化分开存"，而是"模型 context（deriveMessages） 的展示、回放都从同一个日志派生"**。`dsh` 里有多个"从日志派生"的东西：

| 能力 | 怎么来 | 说明 |
|------|-----------|------|
| 模型历史 | `deriveMessages()` / surface | 上面第二节 |
| Web UI 回放 | `surface` 投影 + `assistant/chunk` | 逐 token 保真回放 |
| fork（分支） | `ctx.sessions.fork(source, boundary)` | 取一段稳定前缀、在边界（默认当前末尾）之前、在一个 turn 之后，深拷贝种子到子会话 |
| resume（恢复） | `resumeSessionId` + 日志重建 | 从持久化会话把 agent 重新水合 |
| 标题（title） | `sessionTitle` 从日志摘要 | 派生标题 |
| telemetry | 订阅 `session/event` | 观测追加事件 |

关键在 fork 的设计：`fork(source, boundary, childSessionId)` 要求选择的前缀**不落在开着的 turn 中间**（拒绝"分裂半途 step"这种边界），而是从稳定点、深克隆 seed 到子会话。这就是"从日志做执行分支"——**分支 = 拷贝一份日志前缀 + 一个全新结尾**，而不是复制一段内存状态。

（还有"transcript"：human-facing transcript 读日志的追加原始事件，而 Surface 因为会被 replace 遮蔽前段，所以 transcript 读的是 append 来源、不是 surface。）


## 五、与三栏对比：谁的"日志即单一事实源"最彻底

三种终端 Agent：OpenCode 用 `zod` 校验消息 + SQLite 持久化；Codex 会话有 message chunk。它们的日志/持久化**都是"平行于模型的另一份存储"**；而 `dsh` 把"模型上下文本身"就做成了"从日志推导"，二者**同一份**：持久化就是要"store 日志"，回放/上下文就是要"derive 日志"，模型上下文同样是"日志的一个函数"。

| 维度 | OpenCode | Codex | DeepSeek Harness |
|------|----------|-------|------------------|
| 模型历史来源 | 单独攒的 messages 数组 | 会话头 / chunk 累积成一个 Message[] | **`deriveMessages()` 从日志投影** |
| 是否另存"历史" | 是（messages 数组 + DB） | 是（Message 构建） | **否：唯日志，消息靠 derive** |
| 回放 | 有 | 有 | 与持久化/上下文是**同一条**日志 |
| 运行时保证 | 校验 schema | 部分 | **"model-visible ⟺ logged" invariant 断言** |

`dsh` 最彻底的一点是**把"能回放、能持久化、能构建上下文"三项统一到"日志这一件事"上**，并用一个运行时 invariant 去 assert 它。这等于直接对插件作者声明了一条纪律：**"你往模型里塞的任何东西，必须先从日志里长出"**——它用架构和断言把"上下文一致性"这个软绵绵的愿望，压成了一条硬约束。


## 六、小结与下一站

把这一篇的线收拢：

1. **会话日志** = 追加式不可变事件流，`seq` 连续、JSON 保真，是**唯一**事实源。
2. **deriveMessages()** = 从日志投影出模型要看的消息；chunk 保回放、assistant/message 供推导。
3. **Surface** = 给"哪些消息进推导"加一层有序表面，压缩用 `replace` 遮蔽旧段。
4. **model-visible ⟺ logged** = 最硬不变量：任何模型可见输入必须能从日志重建，invariant 断言。
5. **fork/resume/title/telemetry** = 全是同一个日志的不同投影。

下一篇，环路终于要回到"行动"：agent 已经能用上下文去请求模型、用工具去改变环境——**工具的注册、prompt 注入、执行管线** 到底是怎样把"模型请求一个工具"变成"真实落盘/受限步骤"的？那就走进第 5 篇：**工具系统与执行管线（tools pipeline）**。


## 章节小测

<script setup>
const q = [
  {
    question: '`dsh` 为什么"不存 messages"，而是存一份 Event 日志再推导？',
    options: ['让模型历史、回放、持久化共用同一份源，避免三份状态失同步', '因为 JS 内存不够存 messages', '为了故意让代码更难读', '因为模型只接受 event 数组作为输入'],
    correct: 0,
    explanation: '单一事实源使"能回放 == 能持久化 == 能构建上下文"，这是设计核心；其余把日志当成单纯的"接口要求"。'
  },
  {
    question: '为什么 `deriveMessages()` 投影时"跳过 `assistant/chunk`、用 `assistant/message`"？',
    options: ['chunk 保留回放保真，推导用拼好的整块，才做职责分离', '因为 chunk 太小无法表示', '因为消息已经不需要 chunk', '因为 chunk 不包含 provider'],
    correct: 0,
    explanation: 'chunk 是逐 token 的 UI/回放数据；推导要的是拼好的 assistant/message（含 provider/model）。二者目的不同，所以分开。'
  },
  {
    question: '画一下，"model-visible ⟺ logged" 对插件作者最直接的约束是？',
    options: ['往模型里加任何可见输入必须先新增 SessionEventMap 事件并可从日志渲染', '要每隔几 turn 手动同步一次消息数组', '日志里可以只记录模型输出不必记录输入', '只要模型识别了就行，是否记录无所谓'],
    correct: 0,
    explanation: 'invariant 要求"模型可见 ⟺ 已 log（可重建）"；新增模型可见输入必须先加日志事件。其余把"要不要记"交给拍脑袋，违背了这条硬约束。'
  },
  {
    question: '`SurfaceOp` 里的 `{op:"replace", start, end}` 在压缩（compaction）里做什么？',
    options: ['用一条新的 replace 节点遮蔽旧段、derive 时旧节点消失', '把整个日志重新排序', '把 session 复制一份副本', '把消息改成不可变供并行读'],
    correct: 0,
    explanation: 'replace 用新节点替换 [start,end] 的旧 surface 节点，旧段被 shadow 不在 derive 里，正是压缩"缩段放进摘要"的机制。'
  },
  {
    question: '`ctx.sessions.fork(source, boundary)` 从日志分支（fork）要求边界满足什么？',
    options: ['前缀必须在 turn 之间结束，不能再落在开着的 turn 里面', '前缀必须落在某个 turn 的正中间', '边界点必须位于日志的开头', 'fork 后必须有整整一份历史删除'],
    correct: 0,
    explanation: 'fork 要求所选前缀结束于稳定 turn 之间（拒绝在开着的回合里分裂），这是从日志提供分支的前提。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
