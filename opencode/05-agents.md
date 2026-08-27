---
title: OpenCode Agent 系统：SubAgent 的委派、隔离与边界设计
---

# OpenCode Agent 系统：SubAgent 的委派、隔离与边界设计

一旦开始做多 Agent，最先变复杂的通常不是模型，而是“委派”这件事：主 agent 手里来了一个太大、太杂、最好跟当前上下文隔开的任务，到底该不该交出去？留在原会话里硬做，几万 token 的历史会拖慢它、干扰它；交给另一个执行者，又马上冒出一串工程问题：怎么描述这个执行者、怎么发起委派、怎么防它越权、结果又怎么收得回来。

OpenCode 对这套问题的处理没有走“递归再开一个 agent 调自己”的套娃模式，而是把委派收敛成一条清晰的链：先用 schema 把执行者描述出来，再用 `task` 工具把任务分发出去，执行期权限从父 session 派生，最后把结果包成可解析的 XML 回收。它不是“多开一个模型”而已，而是在主会话旁边临时拼出一个边界清晰的执行单元。

下面沿着五个部位往下拆：

- **第一**，agent 为什么先要被描述成一份 schema，而不是一段会跑的对象；
- **第二**，`task` 工具怎么把一次委派变成可调度、可回收的入口；
- **第三**，父子 session 的边界靠什么锁住，为什么子 agent 只能活在“父内缩权限”的领地里；
- **第四**，执行结果为什么要包成 XML 收回来，而不是直接把一段自然语言贴给父 agent；
- **第五**，当任务变多以后，OpenCode 又是怎么调度这些子执行单元的。

这一篇只拆 SubAgent 的委派、隔离与回收骨架；具体的多角色编排模式和更高层的计划/执行/验证分工，会放到后面的章节再讲。

## 一、委派的前提是"描述执行者"：Agent 的声明式 schema

委派的第一步，是把"把任务交给谁"讲清楚。OpenCode 的 agent 是一份 Schema 定义的数据，而不是一段会跑的对象：叫什么、能调什么、用什么模型、能跑几步，都可以被精确描述。这让"选执行者"变成一次普通查表，也让配置可以登记、注入、修改。

### 1.1 agent 是数据，不是 class

核心包 `packages/core/src/agent.ts` 里，agent 的最底层是一段 schema：

```ts
export const ID = Schema.String.pipe(Schema.brand("AgentV2.ID"))
export const Mode = Schema.Literals(["subagent", "primary", "all"])

export const Info = Schema.Struct({
  name: ID,
  mode: Mode,                       // 声明谁能被委派
  hidden: Schema.Boolean.pipe(Schema.optional),
  permission: PermissionV2.Ruleset, // 决定能调哪些工具
  model: ModelV2.Ref.pipe(Schema.optional), // 可指定专用模型
  steps: Schema.Int.pipe(Schema.optional),  // 循环步数上限
  // …description / color / system / options 等字段在此 schema 内亦存在，与委派主线无关，此处从略
})
```

`ID` 是一个 branded string，传错 agent 名时编译期报错。`Info` 把委派最关键的信息收进来：`mode` 定了"谁是执行者、谁能当主"，`permission` 是它的门控，`model` 允许把一个任务沉到特定模型，`steps` 兜住无限循环。委派的一切都建立在"这些字段可查可用"之上——一份 agent 配置本身就是一个可迭代、可注入、可编辑的数据结构。

配套的 `Interface`（`get`/`list`/`update`/`remove`/`default*`）让"agent 能不能被拿到"这一段变得可编程。`update` 收的是一个 Immer `Draft<Info>` 的变更函数——"改一份 agent"被实现成"对不可变数据打一个 patch"，而不是拿 setter 直接劈对象。这样插件可以从外部注入一套配置，而不破坏"agent 是数据"的不可变基线。

这正是"schema 化"的核心：委派的内核是一份可查询、可 patch 的稳定数据，而不是一段需要维护可变状态的类。

### 1.2 两套 schema 的分工：定义层 vs 运行层

OpenCode 实际有两份 agent schema。core 包里那份 `AgentV2`（`@opencode/v2/Agent`）是定义层；opencode 运行时那份（`packages/opencode/src/agent/agent.ts`）才是执行层。

```ts
export const Info = Schema.Struct({
  name: Schema.String,
  mode: Schema.Literals(["subagent", "primary", "all"]),
  native: Schema.optional(Schema.Boolean),
  hidden: Schema.optional(Schema.Boolean),
  permission: Permission.Ruleset,
  model: Schema.optional(Schema.Struct({ modelID: ModelID, providerID: ProviderID })),
  prompt: Schema.optional(Schema.String),
  steps: Schema.optional(Schema.Finite),
  // …topP / temperature / color / options 等执行期参数也在此 schema 内，此处从略
})
```

core 与运行时两份 schema 共有的委派核心字段是一致的——`mode`、`hidden`、`permission`、`model`、`steps` 谁都有。两者的差异在于：core 那份还含 `description`、`color`、`system`、`options` 等 `@opencode/v2` 契约字段；opencode 运行时那份则在共享字段上追加 `native`、`prompt`、`temperature` 等只执行时才需要的执行期参数（上表的 `topP`/`temperature`/`color`/`options` 一行省略号即代表这些）。**真正驱动 runLoop 与 task 调度的是 opencode 运行时那套**，core 的 `AgentV2` 是平行投影。这一拆分把"定义层与运行层分开"：schema 端声明的部分是稳定契约（决定 agent 是谁），运行端声明的是可注入部分（具体怎么执行），层层不跨界。

### 1.3 mode 即委派能力

`mode` 三态直接把委派关系钉死：

| mode | 含义 |
|------|------|
| `subagent` | 只能被 `task` 工具委派，不能独立激活为主 |
| `primary` | 可以作为主 agent |
| `all` | 两种角色均可 |

这一枚举直接传导到运行时的 `defaultInfo`（`packages/opencode/src/agent/agent.ts`），它挑默认 agent 时会把 `mode === "subagent"` 和 `hidden === true` 的条目都排除掉。所以"谁能当主"由 schema 锁死，而不是靠调用偶然——委派与被委派，在一开始就有清晰的 mode 边界。

## 二、委派入口：task 工具 + runLoop 的"占位式"分发

当 LLM 调用 `task` 工具时，`task` 工具不会立刻执行子 agent——它先往消息流里立一条 `subtask` 标记，真正执行要等 runLoop 的下一轮从头派发。这两步不由同一次调用完成，是"占位 + 下一轮派发"的两段式。这与第四章压缩的 create/process 同源思路一致：先落占位，再由主循环分派。

### 2.1 task 是委派契约

`task` 工具的参数（`packages/opencode/src/tool/task.ts`）：

```ts
const BaseParameterFields = {
  description: Schema.String,          // 一句话任务描述
  prompt:      Schema.String,          // 交给子 agent 的任务正文
  subagent_type: Schema.String,        // 选哪个子 agent 执行
  task_id:     Schema.optional(Schema.String), // 复用之前的子会话
  command:     Schema.optional(Schema.String), // 触发命令
}
export const Parameters = Schema.Struct({ ...BaseParameterFields, background: Schema.optional(Schema.Boolean) })
```

三个必填（`description`/`prompt`/`subagent_type`）是委派的"合同"：要做什么、给谁做。`task_id` 允许续用之前的子会话，`command` 记录触发本次任务的那条命令，`background` 决定走异步分支。一个子 agent 的"任务描述"和它的"执行者"都被压进这一小段 schema。

### 2.2 占位式分发：subtask part → runLoop 弹队列

调用 `task` 后，OpenCode 不直接跑，而是往消息流里立一条 `subtask` part。在 `runLoop` 里（`packages/opencode/src/session/prompt.ts`）：

```ts
const { user: lastUser, assistant, finished, tasks } = MessageV2.latest(msgs)
const model = yield* getModel(lastUser.model.providerID, lastUser.model.modelID, sessionID)
const task = tasks.pop()
if (task?.type === "subtask") {
  yield* handleSubtask({ task, model, lastUser, sessionID, session, msgs })
  continue
}
```

`tasks` 队列的出处很关键：`MessageV2.latest`（`packages/opencode/src/session/message-v2.ts`）只会把"最新一轮 finished assistant 之后新到的 subtask/compaction part"收集进"未处理工作"队列，已完成的旧 subtask 不重复收。这个时序过滤让 runLoop 不会对同一条子委派跑两遍。这就是"占位 — 派发"的核心：**task 工具只管立好 `subtask` 标记，runLoop 下一轮 `pop()` 到它时才真正跑 handleSubtask**。

### 2.3 handleSubtask：搭桥执行

`handleSubtask`（`packages/opencode/src/session/prompt.ts`）拿到 `task` 后做的是"搭桥"：

```ts
const taskModel = task.model ? yield* getModel(task.model.providerID, task.model.modelID, sessionID) : model
const assistantMessage: MessageV2.Assistant = yield* sessions.updateMessage({ ... })
let part: MessageV2.ToolPart = yield* sessions.updatePart({
  type: "tool", tool: TaskTool.id, state: { status: "running", input: {...} },
})
const result = yield* taskTool.execute(taskArgs, {
  agent: task.agent, messageID: assistantMessage.id, sessionID,
  extra: { bypassAgentCheck: true, promptOps },
  ...
})
```

它先建 assistant 消息 + 标记 `running` 的 tool part（这样 LLM 看到子 agent 在跑），再用 `taskTool.execute` 执行真子 agent，并优先采取 task 指定的 model。`bypassAgentCheck: true` 说明"权限已在 runLoop 那次判过"，task 内部不重复问。执行完，`result` 把 part 状态更新为 `completed` 并把输出写回。

这套"占位 + 下一轮派发"与第四章压缩的 create/process 两段式同源。它让 runLoop 主循环保持"纯轮询 + 分发"：每只做"弹一个 task → 交给对应 handler"，重活由各分支扛走。无论子 agent 内部多复杂，都挤不进主循环结构，runLoop 不会被子任务的复杂度撑变形——这正是"占位式分发"带来的收益。

## 三、边界隔离：子 agent 被塞进"被父锁死"的领地

子 Agent 的委派并不意味着天然安全。如果让一个全权限的子 Agent 进入只读状态的父会话，极易造成误修改；而一旦父级的拦截规则无法传递给子级，隔离机制就会形同虚设。OpenCode 的解法是 `deriveSubagentSessionPermission`：子会话的权限统一由父级派生——不仅父 Agent 的拒绝规则（deny）必须向下传递，默认权限还会进一步收紧。

### 3.1 派生子会话权限：父的限制优先

这是隔离的核心（`packages/opencode/src/agent/subagent-permissions.ts`）：

```ts
export function deriveSubagentSessionPermission(input) {
  const canTask = input.subagent.permission.some((r) => r.permission === "task")
  const canTodo = input.subagent.permission.some((r) => r.permission === "todowrite")
  const parentAgentDenies =
    input.parentAgent?.permission.filter((r) => r.action === "deny" && r.permission === "edit") ?? []
  return [
    ...parentAgentDenies,           // ① 父 agent 的 edit deny 必须传下来
    ...input.parentSessionPermission.filter(
      (r) => r.permission === "external_directory" || r.action === "deny",
    ),                              // ② 父 session 的 deny + external_directory
    ...(canTodo ? [] : [{ permission: "todowrite", pattern: "*", action: "deny" }]),
    ...(canTask ? [] : [{ permission: "task", pattern: "*", action: "deny" }]), // 默认禁递归
  ]
}
```

四条策略的判定依据与设计意图：

- ① 继承父 **Agent** 的 edit deny 规则：Plan Mode 的“禁止编辑”约束挂载于 Agent Ruleset 而非 Session 层。若子 Agent 仅继承父级 Session 权限，将静默绕过父级的编辑禁令。因此，父级 Agent 的编辑禁令必须显式下发。
- ② 继承父 **Session** 的 deny 与 external_directory 规则：属于基础权限透传，确保父级 Session 明确拒绝的访问权限，子 Session 同样继承并拒绝。
- ③ 默认禁用 `todowrite`：若子 Agent 的 Ruleset 未显式开启 Todo 权限，则默认关闭，防止其随意修改 Todo 列表并污染父级流程。
- ④ **默认禁用 `task`**：禁止子 Agent 再次调用 task 工具，直接从源头切断“子 Agent 无限递归派生”的隐患。

### 3.2 防递归有三层兜底

"禁递归"不是一个单一的开关，而是在三个维度同时收紧：

- **权限层**：`deriveSubagentSessionPermission` 的④默认 `task: deny`，让子 session 拿不到 task 的权限。
- **工具层**：`task.ts` 里给子 agent 喂 `tools` 时，`todowrite` 与 `task` 在无显式权限时被置为 `false`，工具都没带出来，LLM 想调也没得调。
- **模式层**：`defaultAgent` 发现默认 agent 是 `subagent` 会直接报错——子 agent 不能"顶替"成主。

**三层加起来把递归彻底否掉**：子 agent 只能被调、不能再生，递归在源头直接被阻止，不会留到运行时去爆。

### 3.3 隔离的另三件：model / prompt / steps

权限是主要隔离之一，隔离还包括另外三件：

- **独立 model**：`task.ts` 里子 agent 的模型取 `next.model ??`（继承父消息的 model）。子 agent 愿意时用自己的模型（比如探索型用便宜的），否则继承父。这给了"成本分层"——不必每个子 agent 都用最强模型。
- **独立 prompt**：`explore` 用 `PROMPT_EXPLORE`（只读探索专家，"Don't create any files"），`compaction` 用 `PROMPT_COMPACTION`（锚定摘要），`scout` 用 `PROMPT_SCOUT`（外部库研究）。每个子 agent 有自己的人格与指令，上下文由"人格"隔离。
- **steps 上限**：`session/prompt.ts` 的 `maxSteps`（`agent.steps ?? Infinity`）、`isLastStep` 是每个 agent 的循环刹车，既保护主 agent 不会无限跑，也兜住子 agent 不会一进循环就永不结束。

模型（成本）、prompt（人设）、steps（刹车）、permission（权限）四项联合，共同构成子 agent 的完整边界。

## 四、执行与回收：结果怎么不被丢、不卡死

委派给了、隔离设好了，子 agent 怎么跑、结果怎么回、跑一半被打断怎么办？OpenCode 分 foreground 与 background 两个方向，结果统一走一段 `<task>` XML 契约。

### 4.1 foreground：阻塞取结果，但取消可传递

默认 `task` 是前台的：子 agent 跑完才返回结果。它用 Effect 的 `acquireUseRelease` 包起来（`task.ts`）：

```ts
return yield* Effect.acquireUseRelease(
  Effect.sync(() => { ctx.abort.addEventListener("abort", onAbort) }),
  () => Effect.gen(function* () {
    const text = yield* runTask()
    return { title: params.description, metadata, output: output(nextSession.id, text) }
  }),
  (_, exit) => Effect.gen(function* () {
    if (Exit.hasInterrupts(exit)) yield* cancel
  }).pipe(Effect.ensuring(Effect.sync(() => { ctx.abort.removeEventListener("abort", onAbort) }))),
)
```

`acquireUseRelease` 分三段：acquire 注册 abort 监听，use 跑 `runTask()`，release 清理监听。信号在"取消传播"上同样打通：如果父 agent 被打断，`ctx.abort` 触发 `onAbort` → `runCancel` → `cancel`，子 agent 也被取消。父没完成的任务，子不会悬在半途占着状态。

### 4.2 background：异步拆走、完成回流

前台模式会阻塞父 agent 直到子任务返回。`background: true` 则把子任务 fork 到后台 fiber，父 agent 立刻继续（`task.ts`）：

```ts
if (runInBackground) {
  const info = yield* background.start({
    id: nextSession.id, type: id, title: params.description, metadata,
    run: runTask().pipe(
      Effect.tap((text) => inject("completed", text).pipe(Effect.ignore)),
      Effect.catchCause((cause) => Cause.hasInterruptsOnly(cause) ? Effect.void
        : inject("error", errorText(Cause.squash(cause))).pipe(Effect.ignore).pipe(Effect.andThen(Effect.failCause(cause)))),
    ),
  })
  ...
}
```

`background.start` 把 `runTask` fork 到后台 fiber，完成后 `inject("completed", text)` 把结果以合成 user 消息回流父 session，父 agent 是被"通知完成"而不是轮等。这个分离让父 agent 在等子的时候仍能做它自己的主线。后台分支需要 `experimentalBackgroundSubagents` flag，默认关。

### 4.3 XML 契约：结构化状态让父接管

回收的不只是一段文本，而是带状态的结构。`task.ts` 的 `output()` 与 `backgroundOutput`/`backgroundMessage` 都返回 `<task>` XML：

```ts
function output(sessionID, text) {
  return [`<task id="${sessionID}" state="completed">`, "<task_result>", text, "</task_result>", "</task>"].join("\n")
}
function backgroundOutput(sessionID) {
  return [`<task id="${sessionID}" state="running">", "<task_result>",
    "Background task started. ...", "</task_result>", "</task>"].join("\n")
}
```

foreground 完成 → `state="completed"`；后台启动 → `state="running"`；后台失败 → `state="error"`（内容包在 `task_error` 里）。这样 LLM 能从工具输出解析任务状态，决定"要不要继续、要不要等、要不要看错误"。回到父 Agent 手里的是一段带状态的契约，父 Agent 只需要**"读取 state + 提取内容"，就能完全控制和调度该子任务的后续流程**。

## 五、调度取舍：tasks.pop() 串行 vs coordinator 并行

到这里才到多 Agent 系统最需要决策的一层：一次来了多个子任务，怎么跑？OpenCode 用极简方式——任务队列 + `pop()`，一次一个，串行。这与 Claude Code 的 coordinator 并行正相反，是两种哲学的断点。

### 5.1 OpenCode：单队列，一次一个

回看 runLoop 的 `const task = tasks.pop()`，`tasks` 正是 `MessageV2.latest` 收集的 subtask part。运行层维护的是单个任务队列，每轮用 `pop()` 从中取一个交给对应的 handler——**一次只处理一个 task**，子 agent 排队执行，OpenCode 一个一个跑完。

优：**简单、可控、便宜**——一次一个，无并发状态，上下文顺序稳定，成本低（一次只发一个 LLM 调用）。缺：**慢**——如果一次派了 3 个子 agent，OpenCode 只能串行跑完一个再跑下一个，等待会累积。

### 5.2 Claude Code：coordinator 并行

CC 用 coordinator 模式：一个主 agent 当协调者，派生多个 worker 并行执行，最后由 coordinator 把它们的输出合成。

```text
Coordinator (主 agent)
  ├── Worker A (任务 A)
  ├── Worker B (任务 B)
  └── Worker C (任务 C)
```

优：**并行**，多个独立子任务同时跑、吞吐高、等待少。缺：**高**——多 worker 状态、合成、错误处理、上下文如何共享都成了复杂度。

### 5.3 对比表 + 取舍哲学

| 维度 | OpenCode tasks.pop() | CC coordinator |
|------|---------------------|----------------|
| 执行 | 单队列 + pop（一次一个） | 多 worker 并行 |
| 调度复杂度 | 低（一个队列 + pop） | 高（协调 + 合成） |
| 上下文共享 | 通过 XML 回流父 session | coordinator 上下文合成 |
| 成本 | 低（一次一个 LLM 调用） | 高（并发调用） |
| 响应时间 | 慢（一次一个） | 并行更快 |

CC 的哲学是**为大负载准备协调复杂度**——多 worker 并行合成，强大但贵。OpenCode 的哲学是**靠数据结构简单**——一个队列一个 `pop()`，把"子任务"当一段可排队的工作，用放弃并行的代价换来实现简单。OpenCode 的取舍不在能力短板，而在成本面：把"委派"做成最简单可控的主线，把并发留给真正需要它的场景（那时可以上 background 或 CC 式协调）。

## 六、横向收束：委派 → 隔离 → 回收的关系

走完整条链，把五段收成一些设计原则，再放回生态里总对比，最后落一条工程启示。

### 6.1 总对比表

| 维度 | OpenCode | Claude Code |
|------|---------|-------------|
| Agent 类型系统 | Schema 化（`Mode = subagent/primary/all`、可 patch） | 隐式（command-driven、无公开 schema） |
| 内置 Agent | 8 个（build/plan/general/explore/scout/compaction/title/summary） | 内置专用 agent（工具白名单限制） |
| 隔离机制 | 派生权限（`deriveSubagentSessionPermission`）+ model/prompt/steps | Fork 隔离（继承父 prompt 精确字节） |
| 调度模型 | `tasks.pop()` 串行队列 | coordinator 并行（worker + 合成） |
| 子结果回收 | `<task>` XML + 注入父 session | task-notification XML |
| 递归 | 默认禁（task deny + 三维收紧） | 默认禁（递归 fork 拒绝） |
| 模型 | 子可指定专用 model | 子默认用父模型 |

一条总项：OpenCode 把"委派"做成 schema（命令 + 数据），再用派生权限、独立 prompt、独立 model、steps 四件套隔离，用串行队列做简单调度；CC 走的是"worker 并行 + Fork 模式上下文继承"的高并行路线。两者目标一致——都允许在边界内委派轻量子 agent、且都默认禁止递归——但实现语义不同：一个靠 schema 收敛，一个靠并行强化。

### 6.2 设计复盘：何时该用子 agent，何时别

- **该用**：① 上下文隔离——子 agent 用自己的上下文，不被父污染，也不需要父的过往历史；② 权限隔离——Plan Mode 编辑受限要透传，或只读子 agent 只查不写；③ 专用模型——把重活或便宜活引到特定 model。

- **别用**：任务太简单（起子会话、填 prompt、收 XML 的开销比直接做贵）；或任务重度依赖主对话上下文（要把大量上下文复制给子 agent，这种复制的成本比直接干活还高）。

落到工程启示：**多 Agent 也讲究"小分摊"**。OpenCode 没把每个任务都推成小 agent，而是把"委派"收敛成一条可 schema 化、可隔离、可回收的标准管道，要不要用由主 LLM 根据任务判断。调度线做简（一个 pop），隔离线做硬（权限派生 + 防递归），回收线做稳（XML 契约）。如果真需要并行，它有 background 兜住异步，也能留给 CC 的 coordinator。绝大多数"委派即简单子任务"的场景，OpenCode 用最低的复杂度给了最稳的答案——取舍的价值在于站得稳，而不是把每个能力都武装到顶。

## 章节小测

<script setup>
const q = [
  {
    question: 'OpenCode 的子 agent 权限由 deriveSubagentSessionPermission 派生，其中"默认禁止 task（子不能再次委派）"这一规则主要解决什么问题？',
    options: ['让子 agent 的每次调用都重新申请独立授权', '防止子 agent 递归委派生成无限深度的子代理', '让子 agent 只能调用固定类型的任务防止操纵', '把子 agent 的任务调用限制在单个回合内'],
    correct: 1,
    explanation: '默认禁 task + 工具层把 task 置 false + 默认 agent 拒绝 subagent 当主，这三层从源头掐死"子 agent 再生子 agent"的递归。这样避免了多 agent 递归爆炸这个复杂度黑洞。',
  },
  {
    question: 'OpenCode 用 schema 定义 agent（Mode=subagent/primary/all），Claude Code 用隐式区分。相比隐式，schema 化的核心优势在哪里？',
    options: ['让每个子 agent 在运行时分配独立线程来提升吞吐', '让 agent 配置可查询、可注入、可编译期校验', '让子 agent 用独立内存加速其单次调用时延', '把多个子 agent 在调度层合并成单个 agent 提高并行'],
    correct: 1,
    explanation: 'schema 把"agent 是谁"变成可查询、可 patch 的数据（permission/model/mode/steps），强类型让传错 agent 名编译期暴露，配置可被插件 Immer 编辑。隐式方案（如 CC）拿不到这些工程保障。',
  },
  {
    question: 'OpenCode 里子 agent 完成执行后，父 agent 以什么形态收回结果状态与正文？',
    options: ['一个持久的共享引用让父子 agent 双向直接读写', '一段带 state 的 XML 工具输出由父 LLM 自行解析', '把子 agent stdout 通过日志通道原样转交给父', '父 agent 多次轮询子 agent 以后再合并结果'],
    correct: 1,
    explanation: '结果是统一的 `<task id=.. state=..>` 形态（内容包 task_result）的 XML 工具输出。LLM 从 state 与正文读取任务是否完成、结果是什么，再决定下一步。这是用结构化状态让父整体接管子任务的回路的写照。',
  },
  {
    question: 'task 工具把"子 agent 执行"推迟到 runLoop 的下一轮才真正执行，设计意图是什么？',
    options: ['在下一轮给子 agent 分配独立线程以获得并行 compute', '先趁早把子 agent 权限校准到主线程避免取消冲突', '让 runLoop 主循环保持只轮询 + 分发、不陷入子任务内部', '利用局部变量缓存子 agent，以便下一轮直接调度子任务'],
    correct: 2,
    explanation: 'task 只负责先立一个 subtask part 占位、再丢给下一轮 runLoop 从 tasks.pop() 弹出来执行。这样 runLoop 只做"轮询+分发"，重活由 handleSubtask 分支承担，结构保持纯粹。这与第四章压缩的 create/process 两段式同源。',
  },
  {
    question: 'OpenCode 调度是串行 tasks.pop()，相对 Claude Code 并行 coordinator，串行的设计侧重是？',
    options: ['串行让子 agent 复用共享的 ProviderId 从而简化凭证管理', '串行在产物量固定时可保证中间结果输出时序稳定', '串行以一个队列一次弹一个换取实现简单但放弃并行响应', '串行依赖子会话唯一 id 而并行必须靠多队列做路由'],
    correct: 2,
    explanation: 'tasks.pop() 单队列一次一个，实现最简单、成本最低，适合线性任务流。代价是放弃并行（多子任务不能同时跑）。CC coordinator 并行更强大更贵。这一对比落在"靠数据结构简单 vs 靠协调复杂度"的取舍上。',
  }
]
</script>

<Quiz :questions="q"></Quiz>
