---
title: OpenCode Agent 系统：SubAgent 与 Claude Code 对比
---

# OpenCode Agent 系统：SubAgent 与 Claude Code 对比

最近不少朋友在面试 AI Agent 岗时被问到一个高频题：「**Multi-Agent 系统怎么设计的？subagent 之间怎么通信、怎么隔离、怎么调度？**」

大家答得五花八门，但有个共同问题：**只看过 Claude Code 的实现**。面试官如果追问「**OpenCode 是怎么做的？和 Claude Code 差在哪？**」，大多数人就答不上来了。

今天这篇就想带你从源码视角，把 OpenCode 的 Agent 系统彻底讲明白，并和 Claude Code 做硬核对比。目标是让你看完能同时 get 三个问题：

- 第一，**AgentV2 类型体系**是怎么设计的？三种 mode 各代表什么
- 第二，**task 工具 → handleSubtask 调用链路**——子 agent 是怎么创建、隔离、调度的
- 第三，**OpenCode 的 tasks.pop() 和 CC 的 coordinator 模式差在哪**——两种调度模型的取舍

后面我会按由浅入深的顺序，一个个讲清楚。这次不藏着掖着——**标题里直接放对比**，整篇文章就是围绕「差在哪」展开的。

![8 个内置 Agent 角色](/images/opencode/article-05-hero.png)

## 一、AgentV2 类型体系：Schema 定义的 Agent

### 1.1 OpenCode 的 Agent 是什么？

OpenCode 的 Agent 不是「**一段代码**」，而是一个 **Schema 定义的数据结构**（`packages/core/src/agent.ts`）：

```ts
// packages/core/src/agent.ts:10-11
export const ID = Schema.String.pipe(Schema.brand("AgentV2.ID"))
export type ID = typeof ID.Type
```

`ID` 是一个 branded string，提供类型安全——你传错 agent 名字时编译期就会报错。

```ts
// packages/core/src/agent.ts:13-14
export const Mode = Schema.Literals(["subagent", "primary", "all"]).annotate({ identifier: "AgentV2.Mode" })
export type Mode = typeof Mode.Type
```

**三种 Mode**：

| Mode | 含义 |
|------|------|
| `"subagent"` | 只能被子任务调用，不能独立激活 |
| `"primary"` | 可以作为主 agent |
| `"all"` | 两种角色均可 |

**这个枚举是 OpenCode 独有的**——Claude Code 的 Agent 没有公开的等价类型系统，是隐式区分的。

### 1.2 AgentV2.Info：完整的 Agent 配置

```ts
// packages/core/src/agent.ts:16-27
export const Info = Schema.Struct({
  name: ID,                              // agent 名（branded string）
  description: Schema.optional(Schema.String),
  mode: Mode,                            // subagent / primary / all
  hidden: Schema.Boolean.pipe(Schema.optional),
  color: Schema.String.pipe(Schema.optional),
  permission: PermissionV2.Ruleset,       // ← 权限规则集
  model: ModelV2.Ref.pipe(Schema.optional),  // ← 可指定专用模型
  system: Schema.String.pipe(Schema.optional),
  options: ProviderV2.Options.pipe(Schema.optional),
  steps: Schema.Int.pipe(Schema.optional),  // ← 循环步数上限
}).annotate({ identifier: "AgentV2.Info" })
```

**关键字段**：

- `permission`：Agent 的权限规则集，决定能调哪些工具
- `model`：可以指定专用模型（比如让 compaction agent 用便宜模型）
- `steps`：循环步数上限，防止无限循环
- `hidden`：对用户不可见的内部 agent

### 1.3 AgentV2.Interface：服务接口

```ts
// packages/core/src/agent.ts:41-49
export interface Interface {
  readonly get: (agent: ID) => Effect.Effect<Info, NotFoundError>
  readonly list: () => Effect.Effect<Info[]>
  readonly update: (agent: ID, fn: (agent: Draft<Info>) => void) => Effect.Effect<void>
  readonly remove: (agent: ID) => Effect.Effect<void>
  readonly defaultInfo: () => Effect.Effect<Info, InvalidDefaultError | NoDefaultError>
  readonly defaultAgent: () => Effect.Effect<ID, InvalidDefaultError | NoDefaultError>
  readonly setDefault: (agent: ID) => Effect.Effect<void, NotFoundError>
}
```

7 个操作：`get` / `list` / `update` / `remove` / `defaultInfo` / `defaultAgent` / `setDefault`。

注意 `update` 用了 Immer 的 `Draft<Info>`——agent 配置可以被插件**可变编辑**（通过 Immer draft），但实际状态仍然是不可变的。这是个非常 Effect-TS 风格的设计。

### 1.4 AgentV2.Service：Context Tag

```ts
// packages/core/src/agent.ts:51
export class Service extends Context.Service<Service, Interface>()("@opencode/v2/Agent") {}
```

整个 Agent 系统被抽象成一个 Effect Context Service——任何 Effect 代码都可以通过 `yield* Agent.Service` 拿到 agent 服务。这是 OpenCode 全局依赖注入的基础。



## 二、内置 Agent 一览：8 个角色各司其职

OpenCode 在 `packages/opencode/src/agent/agent.ts` 第 129-281 行预定义了 8 个内置 agent。

### 2.1 完整列表

| Agent | mode | hidden | permission 特色 | prompt | 功能 |
|------|------|--------|----------------|--------|------|
| **build** | primary | - | question: allow, plan_enter: allow | 无 | 默认 agent，完全工具权限 |
| **plan** | primary | - | edit: deny 仅允许 `.opencode/plans/*.md` | 无 | 计划模式，禁止编辑 |
| **general** | subagent | - | todowrite: deny | 无 | 通用子 agent，并行多步骤 |
| **explore** | subagent | - | 大部分 deny，仅 grep/glob/list/bash/webfetch/websearch/read allow | explore.txt | 代码库探索专家 |
| **scout** | subagent | - | 同 explore + repo_clone/repo_overview allow（需 `experimentalScout` feature flag） | scout.txt | 外部库研究（实验性） |
| **compaction** | primary | ✅ | 全部 deny | compaction.txt | 上下文压缩 |
| **title** | primary | ✅ | 全部 deny, temperature: 0.5 | title.txt | 标题生成 |
| **summary** | primary | ✅ | 全部 deny | summary.txt | 对话摘要 |

### 2.2 build：默认 Agent

```ts
// packages/opencode/src/agent/agent.ts:130-144
build: {
  name: "build",
  description: "The default agent. Executes tools based on configured permissions.",
  options: {},
  permission: Permission.merge(
    defaults,                                              // 基准权限
    Permission.fromConfig({ question: "allow", plan_enter: "allow" }),
    user,                                                  // 用户配置覆盖
  ),
  mode: "primary",
  native: true,
},
```

`build` 是默认 agent，权限最宽松——继承 `defaults`（默认全 allow），加上 `question: allow` 和 `plan_enter: allow`。

### 2.3 plan：只读计划模式

```ts
// packages/opencode/src/agent/agent.ts:149-167
plan: {
  name: "plan",
  permission: Permission.merge(
    defaults,
    Permission.fromConfig({
      question: "allow",
      plan_exit: "allow",
      external_directory: { [path.join(Global.Path.data, "plans", "*")]: "allow" },
      edit: {
        "*": "deny",                                       // ← 全局禁止编辑
        [path.join(".opencode", "plans", "*.md")]: "allow",  // ← 仅允许写 plan 文件
        // ...
      },
    }),
    user,
  ),
  mode: "primary",
},
```

`plan` agent 的精妙之处在于 `edit` 权限的通配符控制：

- `*` → `deny`：禁止编辑任何文件
- `.opencode/plans/*.md` → `allow`：仅允许写计划文件

**用 Permission 系统的通配符匹配实现了「只读 + 计划文件」的精细化权限**。

### 2.4 explore：只读代码搜索专家

> `scout` agent 同属于探索类，权限结构与 explore 类似（grep/glob/webfetch/websearch/read + repo_clone/repo_overview），但为**实验性功能**，需 `experimentalScout` feature flag 开启。

### 2.4 explore：只读代码搜索专家

```ts
// packages/opencode/src/agent/agent.ts:182-204
explore: {
  name: "explore",
  permission: Permission.merge(
    defaults,
    Permission.fromConfig({
      "*": "deny",                                           // ← 默认全禁
      grep: "allow", glob: "allow", list: "allow",
      bash: "allow", webfetch: "allow", websearch: "allow",
      read: "allow",                                          // ← 只允许查询类工具
      external_directory: readonlyExternalDirectory,
    }),
    user,
  ),
  prompt: PROMPT_EXPLORE,
  mode: "subagent",
},
```

`explore` 是个严格的**只读 agent**——除了 grep/glob/list/bash/webfetch/websearch/read，其他工具全部禁用。

它的 prompt（`explore.txt`）说：

> You are a file search specialist. Use Glob for broad file pattern matching, Grep for searching file contents with regex, Read when you know the specific file path. Adapt your search approach based on the thoroughness level specified by the caller. **Do not create any files.**

### 2.5 compaction：隐藏的压缩专家

```ts
// packages/opencode/src/agent/agent.ts:235-249
compaction: {
  name: "compaction",
  mode: "primary",
  native: true,
  hidden: true,                                              // ← 对用户不可见
  prompt: PROMPT_COMPACTION,
  permission: Permission.merge(
    defaults,
    Permission.fromConfig({
      "*": "deny",                                           // ← 完全禁止工具调用
    }),
    user,
  ),
  options: {},
},
```

`compaction` 是个特殊 agent：

- **`hidden: true`**——用户在 agent 列表里看不到它
- **`permission: { "*": "deny" }`**——完全禁止工具调用，它只能生成文本（摘要）

为什么禁止工具？防止摘要 agent 拿着工具瞎跑——它的任务就一件事：**总结**。

详细的 compaction 机制见：[OpenCode 上下文压缩：Compact 2 级机制](/opencode/04-compact)



## 三、TaskTool：SubAgent 调用的入口

LLM 想要调用子 agent 时，用的是 `task` 工具（`packages/opencode/src/tool/task.ts`）。

### 3.1 参数 schema

```ts
// packages/opencode/src/tool/task.ts:34-52
const BaseParameterFields = {
  description:   Schema.String,                              // 3-5 字任务描述
  prompt:        Schema.String,                              // 完整任务描述
  subagent_type: Schema.String,                              // 子 agent 类型
  task_id:       Schema.optional(Schema.String),             // 复用之前 session
  command:       Schema.optional(Schema.String),              // 触发命令
  background:    Schema.optional(Schema.Boolean),             // 后台模式
}
```

LLM 调用 task 工具时给出 6 个参数，其中 3 个必填。`task_id` 允许复用之前的 session，`background` 支持异步执行。

### 3.2 执行流程

```ts
// packages/opencode/src/tool/task.ts:96-301
export const TaskTool = Tool.define(
  id,  // "task"
  Effect.gen(function* () {
    const agent = yield* Agent.Service
    const background = yield* BackgroundJob.Service
    // ...

    const run = Effect.fn("TaskTool.execute")(function* (params, ctx) {
      // 1. 检查 background 模式是否需要 feature flag
      const runInBackground = params.background === true
      if (runInBackground && !flags.experimentalBackgroundSubagents) {
        return yield* Effect.fail(...)
      }

      // 2. 权限检查（除非 bypassAgentCheck）
      if (!ctx.extra?.bypassAgentCheck) {
        yield* ctx.ask({ permission: id, patterns: [params.subagent_type], always: ["*"], ... })
      }

      // 3. 获取 subagent 定义
      const next = yield* agent.get(params.subagent_type)
      if (!next) return yield* Effect.fail(...)                // "Unknown agent type"

      // 4. 复用或创建子 session
      const session = params.task_id ? yield* sessions.get(...) : undefined
      const nextSession = session ?? (yield* sessions.create({
        parentID: ctx.sessionID,                              // ← 关联父 session
        title: params.description + ` (@${next.name} subagent)`,
        permission: [
          ...deriveSubagentSessionPermission({               // ← 派生权限
            parentSessionPermission: parent.permission ?? [],
            parentAgent,
            subagent: next,
          }),
          // ...
        ],
      }))

      // 5. 确定 model：subagent 指定优先，否则继承父
      const model = next.model ?? {
        modelID: msg.info.modelID,
        providerID: msg.info.providerID,
      }

      // 6. 执行任务（foreground vs background 分支）
      // ...
    })
  }),
)
```

**6 步执行**：

1. **feature flag 检查**——background 模式需要 `experimentalBackgroundSubagents`
2. **权限询问**——除非显式 bypass，否则要问用户「是否允许调用子 agent」
3. **获取 subagent**——从 AgentV2.Service 拿到子 agent 的配置
4. **创建子 session**——独立的 sessionID、独立的 permission、关联 parentID
5. **确定 model**——子 agent 指定优先，否则继承父消息的 model
6. **执行模式选择**——foreground 等待，background 异步

### 3.3 Foreground 模式：阻塞等待

```ts
// packages/opencode/src/tool/task.ts:267-290
return yield* Effect.acquireUseRelease(
  Effect.sync(() => { ctx.abort.addEventListener("abort", onAbort) }),
  () => Effect.gen(function* () {
    const text = yield* runTask()                            // ← 阻塞执行子 agent
    return { title: params.description, metadata, output: output(nextSession.id, text) }
  }),
  (_, exit) => Effect.gen(function* () {
    if (Exit.hasInterrupts(exit)) yield* cancel              // ← 中断时取消子 agent
  }).pipe(Effect.ensuring(Effect.sync(() => { ctx.abort.removeEventListener("abort", onAbort) }))),
)
```

**`Effect.acquireUseRelease`** 是 Effect-TS 的资源管理三段式——acquire 注册 abort 监听，use 执行任务，release 清理监听器。

如果父 agent 被中断，子 agent 也会被取消（通过 `cancel` Effect）。

### 3.4 Background 模式：异步执行

```ts
// packages/opencode/src/tool/task.ts:233-258
if (runInBackground) {
  const info = yield* background.start({
    id: nextSession.id,
    type: id,
    title: params.description,
    metadata,
    run: runTask().pipe(
      Effect.tap((text) => inject("completed", text).pipe(Effect.ignore)),
      Effect.catchCause((cause) => ...),
    ),
  })
  return { title: params.description, metadata, output: backgroundOutput(nextSession.id) }
}
```

**Background 模式**：

1. 通过 `BackgroundJob.Service` 启动后台任务
2. 立刻返回 `backgroundOutput`——告诉 LLM「任务已开始」
3. 任务完成后通过 `inject` 把结果注入父 session

### 3.5 结果返回：XML 格式

```ts
// packages/opencode/src/tool/task.ts:54-56
function output(sessionID: SessionID, text: string) {
  return [
    `<task id="${sessionID}" state="completed">`,
    "<task_result>",
    text,
    "</task_result>",
    "</task>",
  ].join("\n")
}
```

Foreground 完成时返回这个 XML。LLM 看到的工具输出是 `<task id="..." state="completed">` 这种结构化格式。

Background 完成后的通知（注入到父 session）：

```ts
// packages/opencode/src/tool/task.ts:70-89
function backgroundMessage(input) {
  return [
    `<task id="${input.sessionID}" state="${input.state}">`,
    // ...
    "</task>",
  ].join("\n")
}
```

这种 XML 格式有个**重要细节**——`state="completed"` / `state="running"` / `state="failed"` 让 LLM 能解析任务状态，决定下一步怎么处理。

![TaskTool 执行流程：6 步装配线](/images/opencode/article-05-tasktool.png)

## 四、handleSubtask：runLoop 中的子任务分发

`task` 工具不是直接被调用的——它通过 `handleSubtask()` 在 runLoop 中被分发。

### 4.1 任务队列的来源

runLoop 每一轮开头都会执行：

```ts
// packages/opencode/src/session/prompt.ts:1305-1308
const task = tasks.pop()                                     // ← 从任务队列弹出

if (task?.type === "subtask") {
  yield* handleSubtask({ task, model, lastUser, sessionID, session, msgs })
  continue
}

if (task?.type === "compaction") {
  // ...
}
```

`tasks` 来自 `MessageV2.latest(msgs)`——它会把消息流里所有的 `subtask` 和 `compaction` part 收集成一个任务队列。

也就是说：**LLM 调用 task 工具 → 创建 subtask part → 下一轮 runLoop 从任务队列取出 → handleSubtask 执行**。

### 4.2 handleSubtask 的实现

```ts
// packages/opencode/src/session/prompt.ts:303-494
const handleSubtask = Effect.fn("SessionPrompt.handleSubtask")(function* (input) {
  const { task, model, lastUser, sessionID, session, msgs } = input
  const promptOps = yield* ops()
  const { task: taskTool } = yield* registry.named()

  // 1. 获取 task 指定的 model（可能不同于父 agent）
  const taskModel = task.model 
    ? yield* getModel(task.model.providerID, task.model.modelID, sessionID) 
    : model

  // 2. 创建 assistant message + tool part（状态: running）
  const assistantMessage: MessageV2.Assistant = yield* sessions.updateMessage({ ... })
  let part: MessageV2.ToolPart = yield* sessions.updatePart({
    type: "tool", 
    tool: TaskTool.id, 
    state: { status: "running", input: { prompt, description, subagent_type, command } }
  })

  // 3. 执行 task tool（传入 promptOps 作为 ctx.extra）
  const result = yield* taskTool.execute(taskArgs, {
    agent: task.agent,
    messageID: assistantMessage.id,
    sessionID,
    extra: { bypassAgentCheck: true, promptOps },               // ← 跳过 agent 检查
    ask: (req) => permission.ask({
      ...req,
      sessionID,
      ruleset: Permission.merge(
        taskAgent.permission,                                    // ← 子 agent 权限
        session.permission ?? [],                                // ← 父 session 权限
      ),
    }),
  })

  // 4. 更新 part 状态为 completed
  if (result && part.state.status === "running") {
    yield* sessions.updatePart({ 
      ...part, 
      state: { status: "completed", output: result.output, ... } 
    })
  }

  // 5. 如果是 command 触发的，添加 summary 提示
  if (!task.command) return
  // 插入 "Summarize the task tool output above and continue with your task."
})
```

**5 步执行**：

1. **确定 model**——task 可指定专用 model，否则用父 agent 的 model
2. **创建 assistant message + tool part**——状态初始化为 `running`
3. **调用 taskTool.execute**——传入 `bypassAgentCheck: true` 跳过 agent 检查（因为已经在 handleSubtask 里检查过了）
4. **更新状态**——completed 或 error
5. **command 模式的特殊处理**——如果是命令触发，添加 summary 引导消息

### 4.3 关键设计：权限双重合并

```ts
ask: (req: any) =>
  permission.ask({
    ...req,
    sessionID,
    ruleset: Permission.merge(
      taskAgent.permission,                                    // ← 子 agent 的 permission
      session.permission ?? [],                                // ← 父 session 的 permission
    ),
  })
```

子 agent 执行时，权限规则集是 **子 agent permission + 父 session permission** 的合并。

这是个**双重保险**——即使子 agent 的 permission 写得很宽松，父 session 的 deny 规则仍然生效。



## 五、隔离四件套：permission / model / prompt / steps

![隔离四件套：四层防护](/images/opencode/article-05-isolation.png)

OpenCode 的子 agent 隔离不是单点的，而是**四层防护**：

### 5.1 Permission 隔离：派生子 session 权限

子 session 的权限由 `deriveSubagentSessionPermission()` 派生（`packages/opencode/src/agent/subagent-permissions.ts:17-34`）：

```ts
export function deriveSubagentSessionPermission(input: {
  parentSessionPermission: Permission.Ruleset
  parentAgent: Agent.Info | undefined
  subagent: Agent.Info
}): Permission.Ruleset {
  const canTask = input.subagent.permission.some((rule) => rule.permission === "task")
  const canTodo = input.subagent.permission.some((rule) => rule.permission === "todowrite")
  const parentAgentDenies =
    input.parentAgent?.permission.filter(
      (rule) => rule.action === "deny" && rule.permission === "edit"
    ) ?? []
  return [
    ...parentAgentDenies,                                      // ① 父 agent 的 edit deny
    ...input.parentSessionPermission.filter(                  // ② 父 session 的 deny + external_directory
      (rule) => rule.permission === "external_directory" || rule.action === "deny",
    ),
    ...(canTodo ? [] : [{ permission: "todowrite", pattern: "*", action: "deny" }]),  // ③ 默认禁止 todo
    ...(canTask ? [] : [{ permission: "task", pattern: "*", action: "deny" }]),      // ④ 默认禁止嵌套 task
  ]
}
```

**4 个关键规则**：

1. **继承父 agent 的 edit deny**——比如父 agent 是 plan mode（禁编辑），子 agent 也继承这个限制
2. **继承父 session 的 deny + external_directory**——父 session 拒绝的，子 session 也拒绝
3. **默认禁止 todowrite**——除非子 agent 自己的 permission 显式允许
4. **默认禁止嵌套 task**——子 agent 默认不能再创建子 agent（防止递归爆炸）

**最后一条特别重要**——它直接断绝了「**子 agent 再生子 agent**」的可能性，避免了递归调用的复杂度。

### 5.2 Model 隔离：可用专用模型

```ts
const model = next.model ?? {
  modelID: msg.info.modelID,
  providerID: msg.info.providerID,
}
```

每个 agent 可以指定专用 model。比如：

- `compaction` agent 可以用便宜的小模型（gpt-4o-mini）
- `explore` agent 可以用支持长上下文的模型
- `build` agent 用最强模型

这是个**成本优化**的设计——不是所有任务都需要最强模型。

### 5.3 Prompt 隔离：专用 system prompt

每个 agent 可以有自己的 prompt（通过 `prompt: PROMPT_EXPLORE` 这样的字段指定）。

比如 explore agent 的 prompt（`packages/opencode/src/agent/prompt/explore.txt`）：

```
You are a file search specialist. Use Glob for broad file pattern matching, 
Grep for searching file contents with regex, Read when you know the specific 
file path. Adapt your search approach based on the thoroughness level specified 
by the caller. Do not create any files.
```

compaction agent 的 prompt：

```
You are an anchored context summarization assistant for coding sessions.
Summarize only the conversation history you are given...
```

**Prompt 隔离让每个 agent 有不同的「人格」**——build 是个通用工程师，explore 是个搜索专家，compaction 是个总结专家。

### 5.4 Steps 隔离：循环步数上限

```ts
// packages/core/src/agent.ts:25
steps: Schema.Int.pipe(Schema.optional),                     // ← 循环步数上限
```

每个 agent 可以设置 `steps`，控制 runLoop 的循环次数上限。

```ts
// packages/opencode/src/session/prompt.ts:1341-1345
const agent = yield* agents.get(lastUser.agent)
const maxSteps = agent.steps ?? Infinity                     // ← 默认无限制
const isLastStep = step >= maxSteps
```

到了 maxSteps 时，runLoop 会插入一条 `MAX_STEPS` 提示消息，告诉 LLM「**你已经达到步数上限，请收尾**」。

这是防止子 agent 无限循环的最后一道防线。



## 六、调度模型对比：tasks.pop() vs coordinator

这是 OpenCode 和 Claude Code 在 SubAgent 上的最大差异点。

### 6.1 OpenCode 的 tasks.pop() 模型

OpenCode 的子 agent 调度非常简洁——**任务队列 + pop**：

```ts
// packages/opencode/src/session/prompt.ts
while (true) {
  // ...
  const { tasks } = MessageV2.latest(msgs)
  const task = tasks.pop()                                    // ← 从队列弹出
  
  if (task?.type === "subtask") {
    yield* handleSubtask(...)                                 // ← 同步执行
    continue
  }
  
  if (task?.type === "compaction") {
    // ...
  }
  
  // 主循环逻辑
}
```

**特点**：

- **默认串行**——一次只处理一个 task
- **同步阻塞**——foreground 模式下，handleSubtask 会阻塞主循环。`taskTool.execute` 在 `runInBackground = false` 时通过 `yield* runTask()` 串行走完才返回（`src/tool/task.ts:267-290`）
- **Background 模式**——设置 `background: true`（需环境变量 `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`）时，任务通过 `background.start()` fork 到后台 fiber 执行，主循环立即继续 pop 下一个 task，后台结果通过 `inject("completed", text)` 以合成 user message 回注父 session（`src/tool/task.ts:233-258`）
- **任务来源**——LLM 调用 task 工具时插入 subtask part
- **简单直接**——没有复杂的调度算法

### 6.2 Claude Code 的 coordinator 模式

CC 有个「**Coordinator Mode**」（多 agent 协调模式），主 agent 退化成纯协调者：

```
Coordinator (主 agent)
  ├── Worker 1 (执行任务 A)
  ├── Worker 2 (执行任务 B)
  └── Worker 3 (执行任务 C)
```

**特点**：

- **并行执行**——多个 worker 同时跑
- **协调者合成**——coordinator 收集所有 worker 结果，合成最终输出
- **Continue vs Spawn**——决定是复用已有 worker 还是创建新的
- **Worker 工具限制**——每个 worker 只能用特定工具集

### 6.3 两种模型的对比

| 维度 | OpenCode tasks.pop() | Claude Code coordinator |
|------|----------------------|------------------------|
| **执行模型** | 串行 | 并行 |
| **调度复杂度** | 低（FIFO 队列） | 高（协调者决策） |
| **响应时间** | 慢（一个一个跑） | 快（并行） |
| **成本** | 低（一次一个 LLM 调用） | 高（多个 LLM 同时跑） |
| **状态管理** | 简单（任务队列） | 复杂（多 worker 状态） |
| **错误恢复** | 简单（任务失败继续下一个） | 复杂（worker 失败怎么处理） |
| **上下文共享** | 通过 XML 注入父 session | 通过 coordinator 合成 |

**取舍**：

- **OpenCode 的串行模型**：简单、可控、便宜。适合「**线性任务流**」——一个 agent 探索代码，一个 agent 写代码，一个 agent review
- **CC 的并行模型**：复杂、强大、贵。适合「**任务可以并行**」的场景——同时调研 3 个不同的方案

**哪个更好？没有绝对答案**。OpenCode 的串行模型对大多数场景已经够用——子 agent 本来就是「**主 agent 委派的特定任务**」，串行执行符合直觉。CC 的并行模型是为 Anthropic 内部大规模场景设计的，对单用户场景可能过度工程。

## 七、什么时候该用哪个？—— 决策视角

我建议你先停 10 秒想想：**什么场景该用 SubAgent，什么场景该让主 agent 直接干？**

我的判断标准是三个：

### 7.1 用 SubAgent 的三个场景

**场景 1：上下文隔离**

主 agent 的对话历史已经很满，再做新任务会触发 compaction。这时用 SubAgent 可以**给子 agent 一个干净的上下文**。

例子：用户问「调研一下 React 19 的新特性」，主 agent 调用 explore subagent 去查文档——explore 用自己的上下文，不污染主 agent。

**场景 2：权限隔离**

主 agent 有完整权限，但某个任务需要严格限制（比如只读查询）。用 SubAgent 可以**隔离权限**。

例子：在 plan mode 下，主 agent 不能编辑文件，但用户想生成一份计划文档。调用一个有 edit 权限的 subagent 去写 `.opencode/plans/xxx.md`。

**场景 3：专用模型**

某个任务适合用便宜模型（比如生成标题、压缩摘要）。用 SubAgent 可以**指定专用模型**。

例子：compaction agent 用 gpt-4o-mini 生成摘要，省 token 又够用。

### 7.2 不用 SubAgent 的两个场景

**场景 1：任务太简单**

如果任务一句话就能完成，调 SubAgent 的开销（创建 session、注入 prompt、解析 XML 结果）反而比直接做更贵。

**场景 2：需要主 agent 的完整上下文**

如果任务依赖主 agent 的对话历史（比如「**根据我们刚才讨论的设计，修改这个文件**」），用 SubAgent 反而要复制大量上下文，得不偿失。



## 八、OpenCode vs Claude Code：Agent 系统对比

最后用一张表把两个框架的 Agent 系统全面对比一遍：

| 维度 | Claude Code | OpenCode |
|------|-------------|----------|
| **Agent 类型系统** | 隐式区分（无公开 schema） | AgentV2.Mode (subagent/primary/all) |
| **内置 Agent** | 不公开 | 8 个（build/plan/general/explore/scout/compaction/title/summary） |
| **SubAgent 创建** | AsyncLocalStorage + Agent tool | TaskTool + handleSubtask |
| **隔离机制** | AsyncLocalStorage + Fork 继承 | 四件套：permission/model/prompt/steps |
| **权限继承** | Fork 继承精确 prompt 字节 | deriveSubagentSessionPermission（合并 deny） |
| **调度模型** | coordinator 并行模式 | tasks.pop() 串行 |
| **并行能力** | 真·并行（AsyncLocalStorage） | Effect fiber（逻辑并行） |
| **Fork 模式** | 实验功能（继承精确 prompt） | 无 Fork 概念 |
| **超时策略** | 同步超时可转后台任务 | 依赖 steps 限制 |
| **Background 模式** | 异步 subagent + task-notification | BackgroundJob.Service + inject |
| **结果格式** | task-notification XML | `<task>` XML |
| **嵌套限制** | Fork 子节点禁止递归 fork | 默认禁止子 agent 再调 task |
| **模型指定** | 子 agent 用父模型 | 子 agent 可指定专用模型 |
| **专用 Agent** | 内置专用 agent（工具白名单限制） | 专用 compaction/title/summary agent |
| **Plan Mode** | 不公开 | plan agent + edit 权限通配符 |

### 关键差异点

**1. 类型系统：OpenCode 显式，CC 隐式**

OpenCode 用 Schema 定义 Agent 类型，有明确的 Mode 枚举。CC 的 Agent 是隐式区分的（通过 `querySource` 标记），没有公开的类型系统。

**OpenCode 的优势**：类型安全、可编程、易于扩展。**CC 的优势**：实现简单、运行时灵活。

**2. 隔离方式：CC 用进程隔离，OpenCode 用权限隔离**

CC 的 AsyncLocalStorage 实现了进程级的隔离——每个 subagent 有独立的「执行上下文」。Fork 模式甚至继承了父会话的精确 prompt 字节，可以共享 Prompt Cache。

OpenCode 用权限隔离——子 session 派生父 session 的部分权限，但运行在同一个 Effect runtime 里。

**CC 的优势**：真·并行，能利用多核。**OpenCode 的优势**：实现简单，没有进程间通信开销。

**3. 调度模型：CC 并行，OpenCode 串行**

这是最大的差异。CC 的 coordinator 模式让多个 worker 并行执行，主 agent 合成结果。OpenCode 的 tasks.pop() 是严格串行。

**CC 的优势**：响应快（并行执行）。**OpenCode 的优势**：简单可控，适合大多数场景。

**4. 嵌套限制：两者都禁**

OpenCode 默认禁止子 agent 再调 task（通过 `deriveSubagentSessionPermission`）。CC 的 Fork 模式也禁止递归 fork。

**这是个趋同演化**——两个框架都意识到「**递归 subagent**」是个巨大的复杂度黑洞，干脆禁掉。



## 最后

写到这里，OpenCode 的 Agent 系统基本就扒完了。

回过头看，这套系统不是简单的「**主 agent 调子 agent**」，它在**类型体系、内置角色、调用链路、隔离机制、调度模型**每一个维度都做了精致的设计：

- **AgentV2 类型体系**用 Schema 定义，三种 Mode 清晰区分 primary/subagent/all
- **8 个内置 Agent** 各司其职——build/plan 通用，explore/scout 专用，compaction/title/summary 隐藏
- **task 工具 + handleSubtask 链路**——LLM 调 task 创建 subtask part，下一轮 runLoop 弹出执行
- **隔离四件套**（permission/model/prompt/steps）层层防护
- **权限双重合并**——子 agent permission + 父 session permission
- **默认禁止嵌套 task**——避免递归爆炸
- **Background 模式**——通过 BackgroundJob.Service 异步执行，结果后续注入

每一块拆开看都不是啥复杂技术，但组合在一起，就成了一个既能灵活委派、又能严格隔离的工业级 Agent 系统。

更难得的是，OpenCode 用 Schema 类型系统 + tasks.pop() 串行模型，做到了 Claude Code AsyncLocalStorage + coordinator 并行模型才能做的事——**简化的代价是放弃了并行能力**，但换来的是**类型安全和实现简单**。

OpenCode 的 Schema 类型系统让 Agent 配置可以编程化、可验证、可扩展，这是 Claude Code 隐式 Agent 做不到的。而 CC 的并行 coordinator 模式在大规模任务调度上更强大，这也是 OpenCode 的串行模型比不了的。

**没有更好的，只有更适合的**——这就是工程取舍的魅力。

今天分享就到这里，我们下篇见！

## 章节小测

<script setup>
const q = [
  {
    question: 'OpenCode 的 AgentV2 类型系统有三种 Mode（subagent/primary/all），而 Claude Code 的 Agent 是隐式区分。这个 schema 化的 Agent 定义带来了什么核心优势？',
    options: ['在 Agent 运行时显著提升多模态消息的并行处理吞吐效率', '以 TypeScript 泛型实现 Agent 类型编程扩展与编译期验证', '通过精简 Agent Mode 枚举值降低框架代码的整体维护成本', '原生支持跨进程的 Agent Fork 与分布式调度协调能力'],
    correct: 1,
    explanation: 'OpenCode 用 Schema 定义 Agent 类型（branded ID、Mode 枚举、permission 规则集），让 Agent 配置可编程、可验证、可扩展。CC 的 Agent 是隐式区分（通过 querySource 标记），没有公开的类型系统。OpenCode 的优势在于类型安全和工程保障。'
  },
  {
    question: 'handleSubtask 中权限检查用的是 Permission.merge(taskAgent.permission, session.permission)。这个双重合并的设计意图是什么？',
    options: ['子 agent 的 Permission 取代父 Session 规则以简化判断', '父 Session 的 Deny 规则作为兜底覆盖子 Agent 的宽松权限', '子 Agent 与父 Session 的规则各自独立评估后取并集放行', '子 Agent 的规则优先但父 Session 冲突时以协商结果为准'],
    correct: 1,
    explanation: '子 agent 执行时，权限规则集是子 agent permission + 父 session permission 的合并。这意味着父 session 拒绝的操作，子 agent 也无法执行。即使子 agent 的 permission 写得宽松，父 session 的限制仍然有效。'
  },
  {
    question: 'OpenCode 用 tasks.pop() 串行调度 SubAgent，Claude Code 用 coordinator 并行模式。OpenCode 选择串行的核心取舍是什么？',
    options: ['串行模型隐藏延迟实际吞吐高于并行模式', '串行实现简单适合线性任务流但牺牲了并行响应速度', 'Effect 系统强制所有 Fiber 以串行方式执行', '并行模型无法保证 SubAgent 权限隔离故放弃并行'],
    correct: 1,
    explanation: 'OpenCode 的串行模型简单可控，适合「线性任务流」（探索代码→写代码→review）。代价是放弃并行能力，响应时间比 CC 慢。CC 的 coordinator 并行模式在大规模任务调度上更强大，但实现复杂。这是「实现简单 vs 性能强大」的取舍。'
  },
  {
    question: 'OpenCode 默认禁止子 agent 再调 task（嵌套 task）。Claude Code 的 Fork 模式也禁止递归 fork。为什么两个框架都做了这个设计？',
    options: ['递归嵌套 Agent 会引入不可预测的 Token 消耗超出预算控制范围', '递归 SubAgent 使状态管理与错误恢复指数级复杂化故直接禁掉', 'OpenCode 架构上递归调用与 Effect 的 Resource 管理存在冲突', '父 Agent 与子 Agent 共享同个模型上下文导致嵌套时上下文污染'],
    correct: 1,
    explanation: '两个框架都意识到「递归 subagent」会让状态管理指数级变复杂（错误恢复、死循环检测、资源管理），这是个趋同演化——各自独立做了相同的设计决策。'
  },
  {
    question: 'explore agent 的 permission 设置是「*: deny」然后显式白名单 grep/glob/list/bash/webfetch/websearch/read。plan agent 用通配符控制 edit 权限（*: deny 但 .opencode/plans/*.md: allow）。这两种模式区别在哪？',
    options: ['属于配置文件书写方式上的表达差异', 'Explore 全局 Deny 加白名单 Plan 加路径级 Allow', 'Explore 白名单基于工具类型 Plan 基于目标路径', 'Plan 通配符比显式白名单扩展性更高'],
    correct: 1,
    explanation: 'explore 的全 deny+白名单适合严格只读角色，不允许任何意外操作。plan 的 deny+路径通配符 allow 实现了更精细的权限——禁止编辑任何文件，但允许写 plan 目录下的计划文件。两种模式针对不同场景，体现了 Permission 通配符系统的灵活性。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
