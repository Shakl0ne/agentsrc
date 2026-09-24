---
title: agent-loop：一个可换的默认驱动
---

# agent-loop：一个可换的默认驱动

前两篇把地基铺完了：插件树靠什么组装（Cordis），插件怎么共存怎么回滚（五个原语）。这一篇走进树的主干——`agent-loop`，`dsh` 的默认驱动。它的源文件头注释只有两句话，却是整篇的地图：

> Default Agent driver over queued turns and step-boundary input. Every request is derived from the session log.

排队的回合、步边界上的输入、全部从会话日志推导的请求。读完这一篇，你会带着三个问题的答案离开：

- 一个回合在哪里打开、在哪里关闭，为什么会存在"零步回合"？
- 用户消息、运行中插话、后台注入，三种输入凭什么走同一个入口却行为不同？
- 都叫"默认驱动"了，把整个循环换掉要动多少代码？

step 内部"请求怎么从日志里长出来"的细节是下一篇的主角，工具执行的裁决链放在第五篇；这里只管循环本身的骨架。

## 一、step 与 turn：两个不可混的量纲

`dsh` 给执行模型定了两个单位，`docs/architecture.md` 的定义是整座建筑的地基：

> A **step** is one model request plus the tools it calls. A **turn** is zero or more steps: it opens before its first input is claimed and closes once nothing is owed.

step 是一次模型请求加它调用的工具；turn 是零或多个 step，开在第一次认领输入之前，关在"不再欠任何东西"之时。两个边界都值得抠：turn **开得早**——`turn/start` 先落日志，输入才被认领；**关得晚**——step 全部结束后，还要过一道"是否还有未清偿的输入"检查。

驱动这个边界的是 `agent.ts` 里一个 84 行的 `turn()`，主循环长这样：

```ts
// packages/core/agent-loop/src/agent.ts:312-347（节选，signal 检查与 try/finally 包裹略）
while (true) {
  signal.throwIfAborted()
  const step = phase.step + 1
  const decision = await this.preStep(target, { turn, step })
  if (decision.kind === 'reject') {
    turnEnds = { kind: 'blocked' }
    return false
  }
  if (turnEnds && decision.messages.length === 0) break
  // 被移除的唤醒消息或被改写为空的决策仍拥有 turn 边界，
  // 但不花任何模型调用：
  if (phase.step === 0 && decision.messages.length === 0) {
    turnEnds = { kind: 'completed' }
    return false
  }
  this.session.append('step/start', { turn, step })
  const stepEnd = await this.step(decision)
  if (turnEnds === null || turnEnds.kind !== 'max-tokens') turnEnds = stepEnd
  this.session.append('step/end', { turn, step })
  if (turnEnds && this.inbox.nextStep.length === 0) {
    await this.dispatch.serial('agent/turn-stopping', { turn, signal })
  }
  if (turnEnds && this.inbox.nextStep.length === 0) break
  target = 'next-step'
}
```

四条规则从这段循环里直接读出来：

1. **零步回合存在**。`preStep` 被拒，turn 以 `blocked` 关闭；第一步认领到空批次，turn 以 `completed` 关闭——两种情况都落了 `turn/start` 与 `turn/end`，却一步没花。
2. **turn 的关闭条件是"不欠"**。`turnEnds` 非空且 next-step 队列为空才停；工具执行中途往 next-step 塞了新输入，循环就继续走下一步。
3. **收尾要过一道串行检查**。`agent/turn-stopping` 在"本该关了"时触发，检查两次——第一次触发前后，监听者都还有机会往 next-step 里 steer 新输入，把 turn 留住。
4. **`turn/end` 在 finally 里落盘**。turn 以什么理由结束都要记录，循环自己发出的理由是五种：`blocked` / `completed` / `max-tokens` / `aborted` / `error`（类型上另有 `interrupted` 与 `forked`，分别留给 resume 修复孤儿回合与 fork 种子使用，循环不产生）。其中 `max-tokens` 有粘性——某步顶到上限后，后续正常完成的步不能把 turn 的结局改写成正常。

![turn 与 step 的边界与收尾判断](/images/deepseek/03-turn-step-flow.svg)

这套定义把"回合"从"一问一答"松绑成了"一个债务清偿区间"。模型多步调工具、审批中途打断、压缩后重试，全部装得下，而且每个边界都是日志上的事件——这是后面所有扩展（压缩、hooks、子代理）能挂上来的前提。

## 二、单一 inbox：三种输入、一次认领

输入怎么进 loop？`dsh` 的答案压在一条通道上：所有输入都进一个 inbox，由 driver 统一认领。对外的三个方法共享一个 `send()`，只差两个参数：

```ts
// packages/core/agent-loop/src/agent.ts:153-172
send(message: UserMessage, target: InboxTarget, wakeup: boolean): void {
  const wakingAfterAbort = wakeup && this.phase.kind !== 'idle'
    && this.phase.abort.signal.aborted
  const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
  this.inbox.splice(resolvedTarget, Infinity, 0, [message])
  if (wakeup) this.wakeDriver(wakingAfterAbort)
}

followup(input: UserMessage): void { this.send(input, 'next-turn', true) }
steer(input: UserMessage): void { this.send(input, 'next-step', true) }
inject(input: UserMessage): void { this.send(input, 'next-step', false) }
```

三种输入的语义完全由参数组合决定：

| 方法 | 目标队列 | 唤醒 | 语义 |
|------|---------|------|------|
| `followup()` | next-turn | 是 | 普通用户消息，开始下一个回合 |
| `steer()` | next-step | 是 | 运行中插话，塞进当前回合的下一步 |
| `inject()` | next-step | 否 | 后台注入上下文，等下次认领 |

`inject` 是三种里最值得停下的：它排队 next-step 但 `wakeup=false`，所以一个空闲中的 agent 收到注入，这条内容只是静静躺在 inbox 里，直到某条 `followup` 或 `steer` 唤醒 driver 才被一并认领。架构文档的概括只有一句：One inbox feeds the driver; injected context waits for a waking message。文件变更提醒、时间上下文这类"该知道但不该催活"的信息，全部从这里进来。

认领的语义在 `inbox.ts` 里：

```ts
// packages/core/agent-loop/src/inbox.ts:109-114
claim(target: InboxTarget, turn: number): UserMessage[] {
  const claimed = this.mutate('next-step', 0, this.nextStep.length, [], false)
  if (target === 'next-turn') claimed.push(...this.mutate('next-turn', 0, 1, [], false))
  for (const message of claimed) this.dispatch.emit('agent/inbox/claimed', { message, turn })
  return claimed
}
```

一次认领拿走**全部 next-step 加一条 next-turn**（当这个边界要消费回合时）。认领之后才插入的消息留在队列里等下一个边界——中途的 steer 不会打断已认领的批次，只影响下一步。

这个 inbox 还是耐久的。`ReactLoopInbox` 的每次变动都是一条 `agent/inbox/spliced` 会话事件，状态由投影从日志折叠出来；取消一条排队消息、清空整个队列，都作为日志事实落盘。进程重启后 inbox 自动恢复——排着队的输入不会因为一次崩溃消失。

`wakingAfterAbort` 那个分支补了一个边界情况：唤醒类输入撞上一个正在中止的活动时，会被改判为 next-turn，去开一个新回合——旧活动已经注定要放弃了。

![三种输入进入单一 inbox 的路径与认领批次](/images/deepseek/03-inbox-three-inputs.svg)

## 三、拦截面：两个 waterfall 与一个 serial

第二篇讲过 Cordis 的五种事件模式。落到这个循环上，承重的拦截点是三个，模式分配体现了意图：

**`agent/pre-step`（waterfall）——决定模型这一步看到什么。** `preStep()` 的核心是一次瀑布派发：

```ts
// packages/core/agent-loop/src/agent.ts:275-284（节选）
const decision = await this.dispatch.waterfall(
  'agent/pre-step', { messages: claimed, ...position, signal },
  (): Promise<PreStepDecision> => Promise.resolve<PreStepDecision>({
    kind: 'enter',
    messages: context === undefined ? claimed : [...claimed, context],
  }),
)
```

瀑布的默认 `next` 是"原样进入"：认领的消息加上组装好的运行时上下文。监听者可以改写 `messages`（重写、裁剪、追加），也可以返回 `{ kind: 'reject' }` 直接拒绝这一步。返回的决策是权威的——包装 `next()` 的监听者必须透传下游的消息批次和 `startsRequestSeries` 声明，除非有意替换。压缩插件就挂在这里：请求推导前检查上下文压力。

**`agent/request`（waterfall）——决定这次请求怎么发。** 在 `prepareRequest()` 里，种子配置（provider、model、reasoningEffort）先过一遍瀑布，监听者可以换路由、调参数，瀑布兜底是声明的初始路由。拿到配置后 `ctx.llm.prepareCall()` 绑定适配器，产出这次调用真正的 config。

**`agent/turn-stopping`（serial）——决定 turn 关不关。** 为什么收尾用 serial 而不是 waterfall？因为收尾是单决策事件：监听者按序执行，返回非空值即定案，没有 `next()` 链可以委托。想留住 turn 的监听者（比如"等用户确认"的审批逻辑）在这里调 `steer()` 往 next-step 塞一条输入，让循环的收尾检查失败、turn 继续；只想观察的监听者返回空值，不碰结论。

三种意图，三种模式：改写用 waterfall（多插件层层包装）、收尾用 serial（归一定案）。`dsh` 里几百个扩展点基本都是这两个原语的排列组合。

## 四、一次 step 的内部：请求从日志里来

`step()` 是最长的方法，但主线一句话能说完：**从日志推导请求，把结果写回日志**。时序文档给了权威的六步：

1. `prepareRequest()`：`agent/request` 瀑布定路由，`prepareCall` 绑定适配器；
2. 系统提示按节点调和落 `system/message`，进入的用户消息落 `user/message`，请求头按需落 `request/header`（理由是 initial / resume / change / series 四选一）；
3. `deriveMessages()` 从日志冻结出模型历史，拼上工具 schema，构成不可变请求；
4. 流式执行：chunk 推给 `agent/assistant-stream`（进程内实时帧，不落日志）；
5. 结算：成功的流落 `assistant/message`——它记录每次成功的 provider 调用，嵌入确切的紧凑流数据；失败、重试、取消、流错误的尝试落 `assistant/attempt`，不进模型历史；
6. 工具调用：没有 tool-call 直接 `completed`；有就走 `executeToolCalls`，工具执行中还能往 next-step 塞上下文（`agent.ts:518` 把一个 splice 通道递给了工具管线），返回 `concluded` 决定 step 是否完成。

两个设计点值得单独放大。其一，**重试不重复前戏**：`agent/request-error` 瀑布给出重试动作后，重试发生在当前 step 内部——重新 prepare、调和同一份已渲染的装配，但不重跑 `agent/pre-step`，也不重复用户的 admission。前戏是日志里已定的事实，不因一次网络失败作废。其二，**流的双轨**：实时性走 `agent/assistant-stream`（chunk 级、进程内、瞬态），持久性走 `assistant/message` / `assistant/attempt`（settlement 级、落日志、可重放）。进程在流中途崩溃，日志里没有这次尝试——结算点之前的流本来就是易失的。

头注释那句 "Every request is derived from the session log" 在这里兑现：请求不是内存里攒的数组，是日志状态的函数。这根线拉到下一篇就是整个会话日志系统的入口。

## 五、取消与恢复：状态都在日志里

循环的三态相位是 `idle` / `maintenance` / `running`，全部装在一个 `Phase` 联合类型里。取消的入口干净得出奇：

```ts
// packages/core/agent-loop/src/agent.ts:174-180
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

默认连 inbox 一起清掉（排队的工作随取消作废，且作废本身是日志事实）；传 `keepInbox: true` 则只中止进行中的 turn、保留待办。cause 是封闭的四种：`user` / `parent` / `disposed` / `hook`，abort signal 把它一路带进正在跑的流、工具执行、瀑布中间，每个 `throwIfAborted()` 都把它变成该处的异常，最终落到 `turn/end` 的 `{ kind: 'aborted', reason }`。

唤醒的锁存是这个状态机里最细的一笔：wake 撞上 `maintenance` 或正在中止的活动时不会丢失，而是记在 `wakeRequested` 上；活动收敛回 idle 时检查锁存与队列，有活就重新起 driver。所以"维护中来了用户消息"不会出现"维护吞了消息"——它要么排队要么触发下一轮。

恢复走另一条完全不同的路：`ctx.agents.resume()` 加载持久化的会话，在它上面重建 agent。循环本身不需要任何"恢复逻辑"——`turnBoundary` 投影从日志算出 `lastTurn`，inbox 投影从 splice 事件算出排队输入，下一条 `request/header` 的理由标记为 `resume`。取消是"停一下"，恢复是"从日志再水合"，两边都不需要一个专门的恢复协议，因为状态从头到尾只有一个来源。

![driver 的三态相位与取消、唤醒锁存路径](/images/deepseek/03-phase-machine.svg)

## 六、可换的默认驱动

最后回到标题。`agent-loop` 凭什么敢叫"默认"驱动？`dsh-agent` 包的实现说明把分离摆在第一位：

> The package is built on one separation: the public `Agent` surface and registry live here, while construction and driving live in the loop package behind a registered factory. Consumers therefore depend on `dsh-agent` and never on `dsh-agent-loop`, keeping the driver swappable.

消费方——UI、hooks、编排器、工具插件——拿到的都是 `Agent` 接口与 `ctx.agents` 注册表，具体驱动藏在注册的 factory 后面。想换循环的产品 mount 一个自己的驱动插件、注册 factory，`dsh` 内部代码一行不动。包边界从第一天起就是这么设计的：`agent-loop` 自己也只是通过 `ctx.agentLoop` 键挂在树上的一个普通插件。

| 维度 | OpenCode | Codex | DeepSeek Harness |
|------|----------|-------|------------------|
| 主循环形态 | while(true) 的 runLoop | 事件 reactor | turn/step 边界驱动 |
| 循环归属 | 产品中心，写死 | 产品中心，写死 | 注册 factory 后的默认实现 |
| 消费方依赖 | 依赖产品本体 | 依赖产品本体 | 只依赖 `dsh-agent` 接口 |
| 换循环的代价 | 改核心代码 | 改核心代码 | mount 一个新驱动插件 |

三种循环没有高下，差别在"循环是产品的固定件，还是框架的一个可换件"。`dsh` 选了后者，于是这篇拆的所有机制——零步回合、单一 inbox、三个拦截点、五态收尾——都只是"当前默认实现"的行为，而非产品的宿命。

下一篇顺着 step 内部那句"请求从日志推导"往下挖：会话日志凭什么敢当唯一事实源，`deriveMessages()` 怎么投影，以及那条全系统最硬的不变量——model-visible ⟺ logged。

## 源码索引

- `packages/core/agent-loop/src/agent.ts` — ReactLoopAgent：turn/step 主循环、preStep、cancel、相位机
- `packages/core/agent-loop/src/inbox.ts` — ReactLoopInbox：耐久投影与认领语义
- `packages/core/agent/README.md` — Agent 接口、注册表、swappable 分离设计
- `docs/architecture.md` — Turn flow 与事件清单
- `docs/agent-lifecycle.md` — turn/step 全时序图
- `packages/core/agent-loop/src/tool-calls.ts` — 工具调用的分类与执行（第五篇展开）

## 章节小测

<script setup>
const q = [
  {
    question: '关于 step 与 turn 的关系，正确的说法是？',
    options: ['turn 是零或多个 step 组成的区间', 'step 和 turn 是同一层的别名', 'turn 必须至少包含一个 step', 'step 可以横跨多个 turn'],
    correct: 0,
    explanation: 'step 是一次模型请求加其工具调用；turn 开于首次认领前、闭于无欠项，可以是零步（被拒或空批次）。B 等同两者，C 否认零步回合，D 把层次说反。'
  },
  {
    question: '`inject()` 与 `followup()` 的关键差别在于？',
    options: ['inject 排队 next-turn 且立即唤醒', 'inject 排队 next-step 但不唤醒 driver', 'inject 的内容不进会话日志', 'inject 只能在 turn 中间调用'],
    correct: 1,
    explanation: 'inject = next-step + wakeup=false：内容排队等待下次认领，不唤醒 driver；followup = next-turn + 唤醒。A 是 followup 的参数，C 说反了——排队本身落日志，D 没有这种限制。'
  },
  {
    question: '`agent/pre-step` 的监听者返回 `{ kind: \'reject\' }`，会发生什么？',
    options: ['当前 step 跳过，turn 继续下一步', '整个 turn 以 blocked 关闭，不花一步', '请求照发，结果被丢弃', '框架抛错并中止 driver'],
    correct: 1,
    explanation: 'reject 是权威决策：认领的批次已从 inbox 移除，打开的 turn 以 blocked 收尾、一步不花。A 说不花代价地继续，C/D 都不是这套语义。'
  },
  {
    question: '`agent/turn-stopping` 为什么用 serial 而不是 waterfall？',
    options: ['serial 更容易实现异步等待', 'waterfall 不允许监听者返回值', 'serial 的监听者可以改写消息批次', '收尾是单决策定案，没有可委托的 next 链'],
    correct: 3,
    explanation: 'turn 收尾要的是归一决策：监听者按序执行、返回非空即定案，想留住 turn 就 steer 一条输入进去。waterfall 的层层包装语义在这里没有对应物，A/B/C 与两种模式的语义无关。'
  },
  {
    question: '一个产品想换掉默认 agent 循环，正确的路径是？',
    options: ['fork 整个仓库后改 agent.ts', '给 agent-loop 提交补丁重新装配', 'mount 一个新驱动插件并注册 factory', '修改 cordis.yml 中 agent-loop 的配置行'],
    correct: 2,
    explanation: '驱动藏在注册的 factory 后面，消费方只依赖 dsh-agent 接口；换循环就是挂一个注册了新 factory 的插件，dsh 内部代码不动。A/B 违背"无特权核心"，D 只能改配置不能换实现。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
