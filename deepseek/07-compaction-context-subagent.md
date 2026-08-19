---
title: 压缩 / 上下文注入 / 子代理：装配式的长会话扩展
---

# 压缩 / 上下文注入 / 子代理：装配式的长会话扩展

> 本文基于 `dsh-v0.1.0-rc.7`。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前面六篇我们把 `dsh` 从插件森林、Cordis、agent-loop、会话日志、工具管线一路看到 capability 缝。第七篇把三件"装配另一层"的事放在一起讲，因为它们共享同一个根基——**capability 缝 + 会话日志**：

1. **压缩（compaction）**：长会话超了上下文怎么办？——把"压缩"做成一个可换的缝。
2. **上下文注入（request context）**：怎么在模型开口之外给它塞进"该知道但没问"的上下文？——`agent.inject()` + request-context 插件。
3. **子代理（subagent）**：怎么让一个 agent 把活交给一个 child agent、甚至交给另一个产品里的 agent？——provider registry + 从主会话 fork。

这三者放在一篇文章里，是因为它们**都不是 agent-loop 脊柱的一部分，而是像 bash 一样"可选的扩展"**——通过缝、注入、provider 这些第 6 篇的老朋友，松松地挂在 session log 周围。

先放这一篇的核心判断：

> **在 `dsh` 里，压缩、上下文注入、子代理都不是"内建于主循环"的魔法，而是三个独立扩展点：压缩是可换的缝（CompactionEngine），注入是 `agent.inject()` 排队，子代理是可换的 provider 注册表。** 它们的共同根基是：一切演变都写在 session log 上、一切实现都可换 Provider。


## 一、压缩：把"长会话活下来"做成一个可换的缝

### 为什么压缩必须是一个缝

先看 `dsh-compaction-basic` 是 Service Provider 的角色，`docs/subsystems/compaction.md` 开头就说：

> The compaction seam — a capability seam split like **bash**: Service Definition (`ctx.compaction`), Service Provider (a backend such as dsh-compaction-basic), and human Consumer (dsh-command-compact). Compaction is **one optional capability**, not part of the agent-loop spine.

所以**"压缩"与 bash 是完全同构的一个缝**：接口（`ctx.compaction`）+ Provider（basic 实现）+ 人为消费（`/compact` 命令）。这也回答了"为什么把压缩做成缝"：**因为不同产品对"怎么压"的策略差异极大**——有人要 token 预算、有人要模板摘要、有人接远程 summarizer。与其在 loop 里写死，不如像 bash 一样做成可换的缝：默认给 basic，需要别的就换 Provider。

### 语义：把一段表面折叠成一个摘要

压缩在 session 模型里到底做了什么？答案是**用一个新的 `user/message`（摘要）去"替换"一段旧 surface**——这正是第 4 篇 `SurfaceOp` 里那个 `{ op: 'replace', start, end }` 的具体用量。`docs/subsystems/compaction.md` 把它说得很清楚：

> the summary itself rides on a separate `user/message` with `surfaceOp: { op: 'replace', start, end }` — the only surface mutation performed by summary compaction.

也即：压缩 = 把 `[start, end]` 这段旧的模型历史，整个折叠成一个摘要节点。`start`/`end` 是 surface **位置**（不是数值区间），所以经过一次 replace 之后，新摘要节点落在旧位置，`start` 甚至可以大于 `end` —— 权威的被遮蔽节点集合是 `shadowedSeqs`。

### 触发与策略

`ctx.compaction` 的三个入口对应了三种时机：

- `compactIfNeeded(agent, trigger, signal)`：**自动**策略，按 `trigger`（`pressure` 上下文压力 / `context-overflow` 容量溢出）；
- `compactNow(agent, signal, sourceCommandId?)`：**手动** `/compact`，在 idle 期做一次"低于阈值也有用"的压缩；
- `compactRegion(start, end, agent, signal?)`：显式压缩一段指定范围。

自动压缩跑在**串行的 `agent/pre-step`** 里（在推导请求之前检查压力），或进入 `agent/request-error` 做溢出恢复。`BasicCompactionEngine` 负责把 `ctx.tokenMeter` 压力量、token 预算保留下来、并用一次 `ctx.llm.stream()` 做摘要——关键设计是"重放对话前缀以复用 provider 的 KV cache"，而不是给模型发一个"全新"的上下文。

### compaction/* 事件：锁 + 结果都落日志

压缩也有自己的会话事件：`compaction/start`（拿锁）→ 摘要 → `compaction/summary`（记录摘要、shadowed range、token 数、模型调用）→ `compaction/end`（释放）。`docs/subsystems/compaction.md` 强调**锁是 `compaction/start` 先落、`end` 最后释放**：

> The lock brackets the **whole** operation: `compaction/start` is appended first … then `compaction/end`. Releasing the lock last turns a crash mid-operation into a detectable orphaned lock (a start with no matching end).

把锁放在日志里，就能复用第 4 篇"events是事实"的机制：崩溃造成的孤儿锁（有 start 无 end）可以从日志看出来，而不用额外一套状态机。


## 二、上下文注入：模型"该知道的事"从哪排队进

"长会话"还有一个姊妹问题：除了用户已经说的，agent 需要**主动往模型那边塞**一些东西——文件变更提醒、子目录指令、cron 通知……这些在 `dsh` 里都属于"request-context"。

### `agent.inject()`：排队但先不唤醒

第 3 篇讲 inbox 时已经见过 `agent.inject()` 的关键语义——它给 inbox 排队一条 **`next-step`、`wakeup=false`** 的消息：

```ts
// packages/core/agent-loop/src/agent.ts
inject(input: UserMessage): void {
  this.send(input, 'next-step', false)  // 排队上下文，不唤醒
}
```

这对应 `docs/subsystems/agent` 文档里的：

> inject — queue non-waking next-step context. A running driver claims it at the nearest later pre-step boundary; an idle driver leaves it pending until `followup()` or `steer()` wakes the driver.

**精髓**：注入的上下文**不会自己打断 loop**。一个 idle 的 agent 收到 `agent.inject()` 只是把它放 inbox 里排队，等某个 `followup()` 或 `steer()` 真正把它唤醒时才被认领。这样"给 model 塞背景"和"让 model 必须回话"被严格分开——被注入的内容在被唤醒的那个 step 里自然出现在 pre-step 的 batch 中。

### 谁负责注入：context 包族

`packages/context/README.md` 开头就说，"context" 组是 **"Product plugins that add model-visible request context without defining a tool"**——即一系列**加模型可见上下文、但不定工具**的插件：

| 包 | 注入什么 |
|---|---|
| `agent-instructions/` | 工作区指令（类似 dojo 的那个 AGENTS.md 内容），随创建按 agent/session 隔离 |
| `time-context/` | 当前时间 / 经过时长 |
| `tmux-context/` | tmux 位置上下文 |
| `session-reference/` | 其他会话的有界快照 |

这些插件是"上下文注入"的标准形态：它们不在模型面前定义一个工具，而是又在 `agent.inject()` / request 前把该说的东西排队进 inbox（`agent-instructions` 是默认 bundle 的一部分、可关闭；其余 opt-in）。

对比记忆（来自 OpenCode/Codex 那三栏）：OpenCode 有"指令文件 + Skill 系统"、Codex 有"上下文注入"，而 `dsh` 通过 `agent.inject()` + context 包族把"塞谁、塞什么、塞多少"也做成插件化装配。


## 三、子代理：从"子 agent"到"跨产品委托"

### 一个 provider 的注册表，而不是"一个子过程"

子代理（subagent）是第七篇最值得展开的一点，因为它和 bash 有个关键不同——**多个 Provider 同时存在**。`docs/subsystems/subagent.md` 开篇点破：

> Like bash, it is one optional capability …, not part of the agent loop. It differs ... because **multiple provider implementations coexist** in one context, registered by name (`ctx.subagents`), while bash allows only one executor.

bash 那类只允许一个执行器；子代理则是一个 **named provider 注册表**（`ctx.subagents`）——可以同时注册若干个 `spawn`、`fork`、`acp`、`codex`、`claude-code`……每个 provider 是一种"child transport"。这就是 `dsh` 里"从 fresh child 到跨产品委托"都能用一个接口实现的根因。

### 一次 start：one-shot 委托

`ctx.subagents.start(name, request)` 选择某个 provider，起一个一次性 child：

- `request` 带 `prompt`、`parent`（父 agent）、`signal`、以及可选 `outputSchema`/`maxDepth`/`toolFilter`/`persona`；
- 每个可选能力都要求 provider 声明的 `capabilities` 里 `true`，否则 **fail loud**（`UNSUPPORTED_CAPABILITY`）拒绝，而不是接受后忽略；
- 子代理**通过 parent 的所有权/scope**，用一个 `SubagentRun`（`result` + `dispose`）承载整个一次新 run。

### 从主会话 fork 到提交

"子代理"还有一种很优雅的来源：**从主 agent 的会话 fork 出来**。`docs/subsystems/subagent.md` 的"Fork seeding"一节：

> The fork backend passes a balanced completed-turn prefix of the parent's log — the parent's events up to and including its last `turn/end` — so the seed is contiguous-from-0 and the invariants replay accepts it.

也就是 `fork` 这个 provider 拿**父会话一段"平衡的已完成回合前缀"**（到最后一个 `turn/end` 为止），把它作为 `seed` 给子代理创建一个新会话——本质上就是**在会话日志上做了一个子会话分支**，非常符合第 4 篇"fork 就是拷贝日志前缀"的思想。

### 给模型委托、给产品委托

一个 subagent 的 provider 不止"spawn/fork 两个本地后端"：`ctx.subagents` 允许你把一个 child **委托给另一个产品里的 agent**——`subagent-acp`（ACP 协议）、`subagent-codex`、`subagent-claude-code` 这些 provider 直接通过 wire protocol 把委托发给 Codex 或 Claude Code。所以"子代理"的 provider 会连在一个私行的进程中，或直接跨产品，全看你怎么注册。

`tool-subagent` 是 model-facing 的 Consumer：把"请一个子代理做事"这个东西暴露给模型，让模型决定什么时候 delegate、委托给谁。

### 小结（子代理）

子代理在 `dsh` 就是一个 provider 注册表（`ctx.subagents`），由单个 Consumer 定义了对外暴露；一次委托可以从"同进程的新 agent（spawn/fork）"到"跨产品的协议搬运（acp/codex/claude-code）"，都只是一次 `start()`，接口一样，Provider 换。


## 四、三件事共享的内核：都是"session 上的扩展"

把第七篇三个主题并起来看，它们其实是"同一根骨头的三种"：

| 扩展 | 落在 session 上做什么 | 换成 Provider 吗 |
|------|----------------------|------------------|
| 压缩 | 用 `compaction/*` 事件 + surface `replace` 折叠旧段成摘要 | ✅ `dsh-compaction` → `dsh-compaction-basic` / 别的前端 |
| 上下文注入 | `agent.inject()` / `agent.followup()` 往 inbox 排队；request-context 插件构建一段加入 | 插件（agent-instructions/time-context/...）可开关可增 |
| 子代理 | `subagent/start`/`subagent/end` 事件 + 从 parent fork 的 child session | ✅ 一个 named provider 注册表，可有多 provider 共存 |

关键线索：三件事都**复用了我们在会话日志（第 4 篇）里建立的"一切写进 session / surface"的观念**：

- 压缩 fold 成 surface 节点，token 数、shadowed seqs、锁等都记录在 `compaction/*` 事件；
- 注入经 inbox 排队，最终成为 `user/message` > 会话历史上的一条；
- 子代理的结果都以 `subagent/*` 事件记在日志里，child 就是 fork 出来的 another Session。

这就是 `dsh` 的"装配式扩展"逻辑——**外出的扩展都回写到 session 日志、都做成缝/provider**，让"长会话怎么活"成为一个可配置的装配问题，而不是写死的一个算法。


## 五、与三栏对比：把"长会话扩展"做成缝

对比三栏（同能不同构）：

| 维度 | OpenCode | Codex | Claude Code | DeepSeek Harness |
|------|----------|-------|-------------|------------------|
| 压缩 | 2 级（Prune+Compact） | 3 种 | 5 级 | **做成可换缝**（`ctx.compaction` + 可换 provider） |
| 上下文注入 | 指令+Skill（5 层） | context diffing | claude.md+sessionMemory | **`agent.inject()` 排队 + request-context 插件族** |
| 子代理 | SubAgent 广播 | 多 Agent | AgentTool | **`ctx.subagents` provider 注册表（spawn/fork/acp/codex/claude-code）** |

核心差异一句话：

> 三栏把压缩 / 上下文 / 子代理当作"产品自己的"机制各写各的（压缩有 2/3/5 种策略、上下文有 5 层）；`dsh` 把这三件都**统一成"可换的缝 / provider"**，让"长会话怎么活"变成装那个产品时"换谁做、怎么配"，而非"改核心算法"。

这跟第六篇的 capability 缝的哲学一脉相承，也是 `dsh` 作为"通用引擎"最后一以贯之的表现——**从模型适配、到上下文、到压缩、到子代理，全部是可缝/可配的轮子，而引擎骨架只有"缝一致性 + 会话日志"两根柱子。**


## 结语 / 下一篇

第七篇把三件"长会话怎么活下来"的话题拆开了：压缩是可换缝、注入是 `agent.inject()` 排队、子代理是可换 provider 注册表。它们无一不是"会话日志上的扩展"。

但 `dsh` 的野心还没完——最"魔法"的一层我们留到最后一篇：**agent 能不能改自己？** 它能不能检查自己当前 mount 了哪些插件、主动 mount/unmount 新的插件、再配合主流 CLI（Claude Code / Codex）的 hook 协议握手？那就是全专栏最后一篇：**08 自改 / hooks 桥 / 生态**。


## 章节小测

<script setup>
const q = [
  {
    question: '压缩（compaction）在 session 的模型历史上到底做了什么？',
    options: ['把一段旧表面折叠成一个摘要节点', '把全部内容从头删光', '把消息顺序重新排一遍', '用摘要把旧段逐条抹平'],
    correct: 0,
    explanation: '压缩 = 用 surface `{op: replace}` 把 [start,end] 旧段折叠成一个摘要 user/message。其余三项（清空/重排/逐条抹平）都不符合一次 replace 的语义。'
  },
  {
    question: '为什么把压缩做成一个可换的缝（Service Definition + Provider）？',
    options: ['不同产品的压策略差异大，做成缝便于可换', '这样强制所有产品共用同一个后端', '做成函数比缝省一层抽象', '不换 provider 就无法触发压缩'],
    correct: 0,
    explanation: '压缩是个 seam（Definition=ctx.compaction、Provider=basic 等、Consumer=/compact）。不同产品的压缩策略差异大，做成可换缝后换 provider 即可，loop 无需改。'
  },
  {
    question: '压缩事务的锁是怎么释放的？',
    options: ['start 先落、end 后落，方便检测孤儿锁', 'end 先落、start 后落', '锁不进日志，由内存变量单管', '每次压缩都新建一个新锁'],
    correct: 0,
    explanation: '锁属于事件：`compaction/start` 先、`compaction/end` 最后。崩溃留下的"有 start 无 end"在日志里可检测，无需另设状态机。'
  },
  {
    question: '`agent.inject()` 与 `agent.followup()` 的关键区别是？',
    options: ['inject 排队上下文而不唤醒，followup 唤醒', '两者都唤醒但程度不同', 'inject 直接改摘要，followup 改日志', '两者都只清空 inbox'],
    correct: 0,
    explanation: 'inject = next-step 且 wakeup=false（补充信息、不打断），followup = next-turn 且唤醒=true（要求干活）。这是"补充背景" vs "触发响应"的区分。'
  },
  {
    question: '子代理缝和 bash 缝最大的不同是？',
    options: ['subagent 允许多个 provider 并存', 'bash 允许多个并发执行器', 'subagent 不能跨产品委托', 'subagent 不承载任何能力'],
    correct: 0,
    explanation: '子代理是 `ctx.subagents`——named provider 注册表，spawn/fork/acp/codex/claude-code 多者共存；而 bash 只允许一个 executor。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
