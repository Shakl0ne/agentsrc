---
title: OpenCode 工具系统源码精读：18+ 内置工具设计
---

# OpenCode 工具系统源码精读：18+ 内置工具设计

最近不少朋友在写自己的 AI Agent，跟我说同一个困扰：**工具系统怎么设计才能既灵活又安全？**

- 让 agent 调 shell？怕它执行危险命令
- 让 agent 写文件？怕它越界改工作目录外的东西
- 让 agent 调外部 API？每次都要弹窗询问，用户体验差
- 让 LLM 自己写参数？schema 校验、错误恢复怎么做？

面试官最爱问的就是这个：「**你的 Agent 工具系统怎么设计的？怎么防越权、怎么防死循环、怎么处理 LLM 给的参数错误？**」

今天这篇就想带你从源码视角，把 OpenCode 的工具系统彻底讲明白。目标是让你看完能同时 get 三个问题：

- 第一，**Tool.Def 接口设计**——一个工具从定义到执行的全流程
- 第二，**Edit 引擎的 10 种匹配策略**——为什么编辑文件这么难，OpenCode 怎么解决
- 第三，**Permission 系统**——ask/allow/deny 三态怎么用，doom_loop 怎么防

后面我会按由浅入深的顺序，一个个讲清楚。最后还会和 Claude Code 的工具系统做一次对比，让你看清两种设计哲学的取舍。

![18 个内置工具与 Tool.Def 接口](/images/opencode/article-03-hero.png)

## 一、Tool.Def 接口设计：工具的「身份证」

### 1.1 一个工具长啥样？

OpenCode 的所有工具都遵循 `Tool.Def` 接口（`src/tool/tool.ts:53-63`）：

```ts
export interface Def<
  Parameters extends Schema.Decoder<unknown> = Schema.Decoder<unknown>,
  M extends Metadata = Metadata,
> {
  id: string                              // 工具唯一标识，如 "grep"
  description: string                     // 给 LLM 看的工具说明
  parameters: Parameters                  // Effect Schema 描述参数
  jsonSchema?: JSONSchema7                 // 可选的预生成 JSON Schema
  execute(args: Schema.Schema.Type<Parameters>, ctx: Context): Effect.Effect<ExecuteResult<M>>
  formatValidationError?(error: unknown): string   // 可选的参数错误格式化
}
```

**5 个字段，一个工具就定义完了**。

注意几个设计点：

1. **`parameters` 是 Effect Schema**——不是普通的 TypeScript 类型，而是带运行时解码能力的 schema。LLM 给的参数是 JSON，schema 负责解码成强类型对象
2. **`execute` 返回 Effect**——不是 Promise，是 Effect。这意味着执行过程中可以 yield* 访问其他服务（permission、truncate、agent 等）
3. **`formatValidationError` 是可选的**——允许工具自定义参数错误信息，让 LLM 更容易纠正

### 1.2 Context：工具运行时的「环境」

工具执行时拿到一个 `Context` 对象（`src/tool/tool.ts:34-46`）：

```ts
export type Context<M extends Metadata = Metadata> = {
  sessionID: SessionID
  messageID: MessageID
  agent: string                            // 当前 agent 名
  abort: AbortSignal                       // 中断信号
  callID?: string                          // 工具调用 ID
  extra?: { [key: string]: unknown }       // 额外上下文
  messages: MessageV2.WithParts[]          // 完整对话历史
  metadata(input: { title?: string; metadata?: M }): Effect.Effect<void>
  ask(input: Omit<Permission.Request, "id" | "sessionID" | "tool">): Effect.Effect<void>
}
```

这个 Context 提供了工具运行需要的所有信息：

- `sessionID` / `messageID` / `callID`：定位当前调用
- `abort`：用户中断信号
- `messages`：让工具能读对话历史（比如 task 工具需要看到上下文）
- `metadata`：让工具更新自己的元数据（用于 UI 显示）
- `ask`：**关键**——权限询问接口

`ask` 这个函数是 Permission 系统的入口，工具执行任何敏感操作前都要调用它（详见第六节）。

### 1.3 ExecuteResult：工具执行的结果

```ts
export interface ExecuteResult<M extends Metadata = Metadata> {
  title: string                            // 给 UI 看的简短标题
  metadata: M                              // 给 UI 看的元数据
  output: string                           // 给 LLM 看的输出
  attachments?: Omit<MessageV2.FilePart, "id" | "sessionID" | "messageID">[]
}
```

工具执行完返回这个对象——`output` 是给 LLM 看的文本，`title` / `metadata` 是给 UI 看的（让用户知道工具干了啥）。`attachments` 是可选的文件附件（如截图、PDF 等）。



## 二、Tool.define()：工具是怎么「包装」的？

工具定义完后，不是直接暴露给 LLM，而是要经过 `Tool.define()` 包装一层：

```ts
// src/tool/tool.ts:149-167
export function define<
  Parameters extends Schema.Decoder<unknown>,
  Result extends Metadata,
  R,
  ID extends string = string,
>(
  id: ID,
  init: Effect.Effect<Init<Parameters, Result>, never, R>,
): Effect.Effect<Info<Parameters, Result>, never, R | Truncate.Service | Agent.Service> & { id: ID } {
  return Object.assign(
    Effect.gen(function* () {
      const resolved = yield* init
      const truncate = yield* Truncate.Service          // ← 注入截断服务
      const agents = yield* Agent.Service               // ← 注入 Agent 服务
      return { id, init: wrap(id, resolved, truncate, agents) }
    }),
    { id },
  )
}
```

这个函数做了三件事：

1. 接收一个 `init` Effect，**惰性初始化**工具
2. 获取 `Truncate.Service` 和 `Agent.Service` 两个依赖
3. 调用 `wrap()` 函数包装工具的 execute

### 2.1 wrap() 干了啥？

`wrap()` 是工具执行的核心包装层，它给每个工具的 `execute` 加了两道「保险」：

**保险 1：参数校验**

```ts
// src/session/tool.ts:109-127
const decode = Schema.decodeUnknownEffect(toolInfo.parameters)
// ...
const decoded = yield* decode(args).pipe(
  Effect.mapError(
    (error) =>
      new InvalidArgumentsError({
        tool: id,
        detail: toolInfo.formatValidationError ? toolInfo.formatValidationError(error) : String(error),
      }),
  ),
)
```

注意第 109 行的注释说：「**编译一次 parser closure，之后每次调用都复用**」——这是个性能优化。Effect Schema 的 `decodeUnknownEffect` 会编译出高效的解码器，缓存复用避免重复编译。

如果 LLM 给的参数不符合 schema，包装成 `InvalidArgumentsError`：

```ts
export class InvalidArgumentsError extends Schema.TaggedErrorClass<InvalidArgumentsError>()(
  "ToolInvalidArgumentsError",
  { tool: Schema.String, detail: Schema.String },
) {
  override get message() {
    return `The ${this.tool} tool was called with invalid arguments: ${this.detail}.\nPlease rewrite the input so it satisfies the expected schema.`
  }
}
```

这个 `message` 是给 LLM 看的——**明确告诉模型「你的参数错了，请改写后重试」**。这就是工具系统对 LLM 的「**反馈闭环**」。

**保险 2：输出截断**

```ts
// src/session/tool.ts:129-143
const truncated = yield* truncate.output(result.output, {}, agent)
return {
  ...result,
  output: truncated.content,
  metadata: {
    ...result.metadata,
    truncated: truncated.truncated,
    ...(truncated.truncated && { outputPath: truncated.outputPath }),
  },
}
```

每个工具的输出都经过 `truncate.output()` 检查——超过 2000 行或 50KB 就截断，完整内容写到文件，LLM 看到的是「**截断提示 + 后续怎么获取**」。

这两道保险让所有工具自动获得「**参数校验 + 输出截断**」能力，不用每个工具自己写。



## 三、内置工具一览：聚焦 3 个代表

OpenCode 的 `src/tool/` 目录下有 18 个核心工具。我先用一张表过一遍，然后聚焦讲 3 个最有代表性的。

### 3.1 内置工具全表

| 工具 | 文件 | 一句话说明 |
|------|------|-----------|
| `apply_patch` | apply_patch.ts | 对 GPT 非-oss/4 模型用 patch 格式做精确文件修改 |
| `edit` | edit.ts | **10 种匹配策略**的智能编辑引擎 |
| `glob` | glob.ts | 用 glob 模式快速发现文件路径 |
| `grep` | grep.ts | 用 Ripgrep 做正则内容搜索 |
| `lsp` | lsp.ts | 调用 LSP 获取诊断、补全、语义信息 |
| `plan` | plan.ts | 进入/退出 Plan 模式（实验性） |
| `question` | question.ts | 向用户提问获取信息和决策 |
| `read` | read.ts | 读文件内容，支持分页 + 自动指令文件关联 |
| `repo_clone` | repo_clone.ts | 克隆远程 Git 仓库（实验性） |
| `repo_overview` | repo_overview.ts | 仓库概览/代码库地图（实验性） |
| `shell` | shell.ts | 执行 shell 命令，支持流式输出 |
| `skill` | skill.ts | 加载专业领域 skill |
| `task` | task.ts | **创建子 agent session 委派任务** |
| `todo` | todo.ts | 读写 TODO / 任务清单文件 |
| `webfetch` | webfetch.ts | 获取 URL 内容转 markdown |
| `websearch` | websearch.ts | 通用 web 搜索 |
| `write` | write.ts | 创建/覆写文件 |
| `mcp-websearch` | mcp-websearch.ts | 通过 MCP 协议的网络搜索 |

### 3.2 工具的「**模型路由**」：apply_patch vs edit/write

这是个有意思的设计——同一类操作（修改文件）针对不同模型用不同工具：

```ts
// src/tool/registry.ts:317-328
const usePatch =
  input.modelID.includes("gpt-") && 
  !input.modelID.includes("oss") && 
  !input.modelID.includes("gpt-4")
if (tool.id === ApplyPatchTool.id) return usePatch
if (tool.id === EditTool.id || tool.id === WriteTool.id) return !usePatch
```

**翻译**：

- GPT 非-oss 非-4 系模型（如 gpt-5）→ 用 `apply_patch`
- 其他模型 → 用 `edit` + `write`

为什么要这样区分？因为不同模型对工具调用的格式偏好不同——GPT 系模型对 unified patch 格式理解得更好，而 Claude 系模型对「**oldString + newString**」的精确替换更在行。

这是个**工程经验**：让工具适配模型，而不是反过来。如果你的 agent 要支持多模型，这种「**同功能多工具**」的路由设计值得借鉴。

### 3.3 聚焦 1：Grep —— 简单工具的代表

Grep 工具的实现非常简洁（`src/tool/grep.ts`，156 行）：

```ts
export const Parameters = Schema.Struct({
  pattern: Schema.String.annotate({ description: "The regex pattern to search for in file contents" }),
  path: Schema.optional(Schema.String).annotate({ description: "The directory to search in..." }),
  include: Schema.optional(Schema.String).annotate({ description: 'File pattern to include...' }),
})

// 执行
const result = yield* rg.search({
  cwd,
  pattern: params.pattern,
  glob: params.include ? [params.include] : undefined,
  file,
  signal: ctx.abort,
})
```

Grep 工具底层包装的是 **Ripgrep**——这是个非常快的选择。但 Grep 工具的精妙之处在「**结果排序**」：

```ts
// 结果按文件 mtime 排序，最新修改的文件优先
// 最多返回 100 条匹配
// 每行最长 2000 字符
```

**为什么要按 mtime 排序**？

因为 AI Agent 在写代码时，最近修改的文件**最可能是用户关心的**。如果 grep `function foo`，老代码库里可能有 100 个匹配，但只有最近改的那个是相关的。**按 mtime 排序把最近的提到前面**，能让 LLM 更快定位到关键文件。

这是个非常贴合 Agent 场景的工程优化。

### 3.4 聚焦 2：Task —— SubAgent 调用的入口

Task 工具是 Agent 系统的关键（`src/tool/task.ts`，301 行）：

```ts
export const TaskTool = Tool.define(
  id,
  Effect.gen(function* () {
    const agent = yield* Agent.Service
    const background = yield* BackgroundJob.Service
    
    const run = Effect.fn("TaskTool.execute")(function* (params, ctx) {
      // 1. 派生子 session 权限
      // 2. 创建子 session
      // 3. 委派给 subagent 执行
      // 4. foreground 模式等待结果，background 模式异步
    })
  }),
)
```

Task 工具干了三件事：

1. **派生权限**：`deriveSubagentSessionPermission` 从父 session 派生子 session 的权限规则
2. **创建子 session**：独立的 session ID、独立的 agent、独立的 model
3. **执行模式选择**：
   - **foreground**：等待子 agent 完成，结果通过 XML 格式返回
   - **background**：异步执行，结果通过 `BackgroundJob.Service` 后续注入父 session

XML 结果格式：

```ts
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

详细的 Agent 系统解析见：[OpenCode Agent 系统源码精读：SubAgent 与 Claude Code 对比](/opencode/05-agents)

### 3.5 聚焦 3：Edit —— 最复杂的工具

Edit 是 OpenCode 最复杂的工具（711 行），下一节专门讲。



## 四、Edit 引擎：10 种文本匹配策略

这是 OpenCode 工具系统最值得细看的部分。

### 4.1 问题：为什么编辑文件这么难？

想象一下：LLM 想把文件里的某段代码替换成新版本。它给你 `oldString` 和 `newString`，你拿 `oldString` 去文件里找。

听起来简单？但实际会遇到这些问题：

1. **空白字符不一致**：LLM 写的 `  foo()`（2 空格缩进），文件里是 `\tfoo()`（tab 缩进）
2. **行尾差异**：LLM 写 `\n`，文件里是 `\r\n`
3. **转义字符**：LLM 写字面量 `\n`，但参数 schema 把它解成实际换行符
4. **多个匹配**：`oldString` 太短，文件里有多处匹配
5. **模糊匹配**：LLM 写的 `oldString` 接近但不完全等于文件内容
6. **缩进错误**：LLM 用了 4 空格缩进，文件用 2 空格

如果只用最简单的 `content.indexOf(oldString)`，**LLM 经常会失败**——它会陷入「编辑 → 报错 → 重试 → 又报错」的循环。

### 4.2 OpenCode 的解法：渐进式匹配策略

OpenCode 用 **9 种 Replacer** 按精度从高到低尝试，每种 Replacer 是一个 generator 函数：

```ts
// src/tool/edit.ts:213
export type Replacer = (content: string, find: string) => Generator<string, void, unknown>
```

每个 Replacer 接收文件内容和查找字符串，**yield 出所有可能的匹配候选**。

### 4.3 9 种 Replacer 一览

```ts
// src/tool/edit.ts:681-691
for (const replacer of [
  SimpleReplacer,                  // 1. 精确匹配
  LineTrimmedReplacer,             // 2. 行级 trim 匹配
  BlockAnchorReplacer,             // 3. 首尾锚点 + Levenshtein 相似度
  WhitespaceNormalizedReplacer,    // 4. 空白规范化匹配
  IndentationFlexibleReplacer,     // 5. 缩进灵活匹配
  EscapeNormalizedReplacer,        // 6. 转义字符归一化
  TrimmedBoundaryReplacer,         // 7. 边界 trim 匹配
  ContextAwareReplacer,            // 8. 上下文感知匹配
  MultiOccurrenceReplacer,         // 9. 多次出现匹配
]) { ... }
```

让我用一个例子解释每个策略在干什么：

假设文件内容是：

```
\tdef hello():
\t    print("hello")
\t    return None
```

LLM 给的 `oldString` 是：

```
def hello():
    print("hello")
    return None
```

（2 空格缩进，文件是 tab 缩进）

**策略 1：SimpleReplacer** — 直接精确匹配，失败
**策略 2：LineTrimmedReplacer** — 按行 trim 后比较，匹配成功！
**策略 3：BlockAnchorReplacer** — 用首行 `def hello():` 和末行 `return None` 作为锚点，再算中间行相似度
**策略 4：WhitespaceNormalizedReplacer** — 把所有连续空白压缩成单个空格后比较
**策略 5：IndentationFlexibleReplacer** — 去掉最小公共缩进后比较
**策略 6：EscapeNormalizedReplacer** — 把 `\n`、`\t` 等转义序列展开为实际字符
**策略 7：TrimmedBoundaryReplacer** — 整体 trim 后比较
**策略 8：ContextAwareReplacer** — 简化版的 BlockAnchor，首尾行作为锚点
**策略 9：MultiOccurrenceReplacer** — 找所有精确匹配的位置

### 4.4 决策树：怎么选最精确的匹配？

```ts
// src/tool/edit.ts:674-711
export function replace(content: string, oldString: string, newString: string, replaceAll = false): string {
  if (oldString === newString) {
    throw new Error("No changes to apply: oldString and newString are identical.")
  }

  let notFound = true

  for (const replacer of [...]) {                           // ← 按精度从高到低
    for (const search of replacer(content, oldString)) {    // ← 每个 Replacer 可能 yield 多个候选
      const index = content.indexOf(search)
      if (index === -1) continue
      notFound = false
      
      if (replaceAll) {
        return content.replaceAll(search, newString)        // ← 全局替换
      }
      
      const lastIndex = content.lastIndexOf(search)
      if (index !== lastIndex) continue                     // ← 多匹配，跳过让下一个策略处理
      
      return content.substring(0, index) + newString + content.substring(index + search.length)
    }
  }

  if (notFound) {
    throw new Error("Could not find oldString in the file...")
  }
  throw new Error("Found multiple matches for oldString. Provide more surrounding context...")
}
```

**决策逻辑**：

1. 按精度从高到低遍历 9 个 Replacer
2. 每个 Replacer 可能 yield 出 0~N 个匹配候选
3. 对每个候选，在原始内容中定位
4. 如果 `replaceAll`，直接全局替换
5. 如果单匹配（`index === lastIndex`），直接替换
6. 如果多匹配，**跳过此 Replacer，尝试下一个**——这是个非常聪明的设计

**为什么要跳过多匹配的**？因为如果策略 1（精确匹配）就找到多个，说明 `oldString` 太短不唯一。如果策略 2（更宽松）也找到多个，那就让策略 3、4...继续尝试，直到有一个策略给出**唯一匹配**。

如果所有策略都给出多个匹配，最后抛出「**找到多个匹配，请提供更多上下文**」的错误——这是个对 LLM 友好的提示。

### 4.5 BlockAnchorReplacer：最复杂的策略

策略 3 `BlockAnchorReplacer` 是最复杂的（133 行代码），它的工作原理：

1. 要求 `oldString` 至少 3 行
2. 用**首行**和**末行**作为锚点在文件中定位候选块
3. 对每个候选块，用 **Levenshtein 距离**计算中间行的相似度
4. 如果只有一个候选：相似度 ≥ 0.0 即接受（任何相似度都行）
5. 如果有多个候选：相似度 ≥ 0.3 才接受

```ts
const SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.0   // 单一候选总是接受
const MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.3  // 多候选需要 30% 相似度
```

**为什么单一候选 0% 相似度也接受**？因为如果首尾锚点都唯一匹配，中间内容差不多就行了——agent 写代码不会差得太离谱。

### 4.6 LSP 诊断反馈

Edit 工具执行完替换后，还会做一件很贴心的事：

```ts
// src/tool/edit.ts:193-197
yield* lsp.touchFile(filePath, "document")
const diagnostics = yield* lsp.diagnostics()
const normalizedFilePath = AppFileSystem.normalizePath(filePath)
const block = LSP.Diagnostic.report(filePath, diagnostics[normalizedFilePath] ?? [])
if (block) output += `\n\nLSP errors detected in this file, please fix:\n${block}`
```

**通知 LSP 文件变更**，获取诊断信息，**如果有错就把错误信息附加到工具输出里**。

这样 LLM 看到的工具输出不只是「**edit 成功**」，而是「**edit 成功，但 LSP 报告了 3 个语法错误，请修复**」。**形成「编辑 → 反馈 → 修复」的闭环**。

### 4.7 文件锁防并发

```ts
// src/tool/edit.ts:35-45
const locks = new Map<string, Semaphore.Semaphore>()
function lock(filePath: string) {
  const resolvedFilePath = AppFileSystem.resolve(filePath)
  const hit = locks.get(resolvedFilePath)
  if (hit) return hit
  const next = Semaphore.makeUnsafe(1)
  locks.set(resolvedFilePath, next)
  return next
}
```

**每个文件一个信号量**，保证对同一文件的编辑不会并发执行。

这是个细节但重要的设计——如果没有这个锁，LLM 调用两次 edit 同时改同一个文件，第二次 edit 会基于过期内容做替换，结果会出错。

## 五、Registry：工具注册流水线

工具定义好了，怎么把它们组织起来传给 LLM？这就是 Registry 干的事。

### 5.1 ToolRegistry.tools() 函数

```ts
// src/tool/registry.ts:316-361
const tools: Interface["tools"] = Effect.fn("ToolRegistry.tools")(function* (input) {
  // 1. 过滤工具列表
  const filtered = (yield* all()).filter((tool) => {
    if (tool.id === WebSearchTool.id) {
      return webSearchEnabled(input.providerID, { exa: flags.enableExa, parallel: flags.enableParallel })
    }
    const usePatch = input.modelID.includes("gpt-") && !input.modelID.includes("oss") && !input.modelID.includes("gpt-4")
    if (tool.id === ApplyPatchTool.id) return usePatch
    if (tool.id === EditTool.id || tool.id === WriteTool.id) return !usePatch
    return true
  })

  // 2. 处理每个工具的 description
  return yield* Effect.forEach(
    filtered,
    Effect.fnUntraced(function* (tool: Tool.Def) {
      const output = {
        description: tool.description,
        parameters: tool.parameters,
        jsonSchema: tool.jsonSchema,
      }
      
      // 3. Plugin hook 修改 tool definition
      yield* plugin.trigger("tool.definition", { toolID: tool.id }, output)
      
      // 4. 动态注入 description（task 和 skill 工具）
      return {
        id: tool.id,
        description: [
          output.description,
          tool.id === TaskTool.id ? yield* describeTask(input.agent) : undefined,    // ← 注入可用 agent 列表
          tool.id === SkillTool.id ? yield* describeSkill(input.agent) : undefined,  // ← 注入可用 skill 列表
        ].filter(Boolean).join("\n"),
        parameters: output.parameters,
        jsonSchema,
        execute: tool.execute,
        formatValidationError: tool.formatValidationError,
      }
    }),
    { concurrency: "unbounded" },
  )
})
```

**三件事**：

1. **过滤**：按 provider、model、flags 过滤工具列表（如前文的 apply_patch vs edit 路由）
2. **Plugin hook**：`tool.definition` 钩子让插件能修改任何工具的 description、parameters、jsonSchema
3. **动态 description**：task 和 skill 工具的 description 包含当前可用的 agent/skill 列表

### 5.2 自定义工具加载

OpenCode 支持两种方式加载自定义工具：

```ts
// src/tool/registry.ts:199-220
const dirs = yield* config.directories()
const matches = dirs.flatMap((dir) =>
  Glob.scanSync("{tool,tools}/*.{js,ts}", { cwd: dir, absolute: true, dot: true, symlink: true }),
)

for (const match of matches) {
  const namespace = path.basename(match, path.extname(match))
  const mod = yield* Effect.promise(() => import(pathToFileURL(match).href))
  for (const [id, def] of Object.entries(mod)) {
    if (!isPluginTool(def)) continue
    custom.push(fromPlugin(id === "default" ? namespace : `${namespace}_${id}`, def))
  }
}

// 从插件加载
const plugins = yield* plugin.list()
for (const p of plugins) {
  for (const [id, def] of Object.entries(p.tool ?? {})) {
    custom.push(fromPlugin(id, def))
  }
}
```

**两种加载方式**：

1. **文件加载**：从 `tool/` 或 `tools/` 目录扫描 `.js`/`.ts` 文件，动态 import
2. **Plugin 注册**：插件通过 `tool` 字段注册工具

**命名规则**：

- 默认导出 → 用文件名作为工具 ID（如 `mytool.ts` → `mytool`）
- 命名导出 → 用 `filename_exportName`（如 `mytool_special`）

这是个非常优雅的扩展机制——**写一个 .ts 文件丢进 tool/ 目录就能注册新工具**，不用改任何配置。



## 六、Permission 系统：ask/allow/deny 三态

这是工具系统的「**安全层**」——所有敏感操作都要过权限检查。

### 6.1 三态权限模型

OpenCode 的权限有三种状态（`packages/core/src/permission.ts:5`）：

```ts
export const Action = Schema.Literals(["allow", "deny", "ask"])
```

| Action | 行为 |
|--------|------|
| `allow` | 静默通过 |
| `deny` | 直接拒绝，抛错 |
| `ask` | 弹窗询问用户 |

**为什么是三态而不是二态**？

因为「**ask**」是个非常重要的中间状态——有些操作不能直接放行（有风险），但也不能一概拒绝（用户可能确实需要）。**让用户决定**是最稳妥的。

![Permission 三态模型：ask / allow / deny](/images/opencode/article-03-permission.png)

### 6.2 规则评估：最后匹配胜出

```ts
// packages/core/src/permission.ts:24-31
export function evaluate(permission: string, pattern: string, ...rulesets: Ruleset[]): Rule {
  return (
    rulesets
      .flat()
      .findLast((rule) => 
        Wildcard.match(permission, rule.permission) && 
        Wildcard.match(pattern, rule.pattern)
      ) 
      ?? { action: "ask", permission, pattern: "*" }   // 默认 ask
  )
}
```

**两个关键设计**：

1. **`findLast`** = 最后匹配的规则胜出（后写的规则优先级高）。这让用户配置可以覆盖默认配置
2. **双重通配符匹配**：`permission` 和 `pattern` 都支持 `*` 通配符

### 6.3 Permission.ask() 的工作流程

```ts
// src/permission/index.ts:171-211
const ask = Effect.fn("Permission.ask")(function* (input: AskInput) {
  const { approved, pending } = yield* InstanceState.get(state)
  const { ruleset, ...request } = input
  let needsAsk = false

  for (const pattern of request.patterns) {
    const rule = evaluate(request.permission, pattern, ruleset, approved)
    if (rule.action === "deny") {
      return yield* new DeniedError({ ruleset: ... })    // ← 拒绝 → 抛出错误
    }
    if (rule.action === "allow") continue                 // ← 允许 → 跳过
    needsAsk = true                                        // ← ask → 需要用户确认
  }

  if (!needsAsk) return                                    // ← 全部 allow，静默通过

  // 创建 pending 请求，发布事件，等待用户回复
  const deferred = yield* Deferred.make<void, RejectedError | CorrectedError>()
  pending.set(id, { info, deferred })
  yield* bus.publish(Event.Asked, info)
  return yield* Deferred.await(deferred)                   // ← 阻塞等待
})
```

**核心机制**：用 Effect 的 `Deferred` 实现异步等待。当用户回复时，对应的 `Deferred` 被 `succeed` 或 `fail`，ask 函数返回或抛错。

### 6.4 用户回复的三种选择

```ts
// src/permission/index.ts:213-269
if (input.reply === "reject") {
  yield* Deferred.fail(existing.deferred, ...)
  // 级联拒绝同 session 的所有 pending 请求
  for (const [id, item] of pending.entries()) {
    if (item.info.sessionID !== existing.info.sessionID) continue
    pending.delete(id)
    yield* Deferred.fail(item.deferred, new RejectedError())
  }
  return
}

yield* Deferred.succeed(existing.deferred, undefined)     // ← 允许
if (input.reply === "once") return                          // ← "once" 只放行当前请求

// "always" → 加入永久批准列表
for (const pattern of existing.info.always) {
  approved.push({ permission: existing.info.permission, pattern, action: "allow" })
}
```

**三种回复**：

| Reply | 含义 |
|-------|------|
| `once` | 只允许这一次，下次还会问 |
| `always` | 永久允许（写入 approved 列表） |
| `reject` | 拒绝，并级联拒绝同 session 的所有 pending |

**「always」的妙处**：用户批准一次「**always**」后，**剩余的 pending 请求会自动检查是否能放行**：

```ts
for (const [id, item] of pending.entries()) {
  if (item.info.sessionID !== existing.info.sessionID) continue
  const ok = item.info.patterns.every(
    (pattern) => evaluate(item.info.permission, pattern, approved).action === "allow",
  )
  if (!ok) continue
  pending.delete(id)
  yield* Deferred.succeed(item.deferred, undefined)         // ← 自动放行
}
```

也就是说，**用户一次「always」可以解锁多个 pending 请求**——避免了「问 10 次同样的权限」的烦人场景。

### 6.5 external_directory：工作区边界

这是 Permission 系统最常见的应用——防止 agent 修改工作目录外的文件：

```ts
// src/tool/external-directory.ts:24-48
export const assertExternalDirectoryEffect = Effect.fn("Tool.assertExternalDirectory")(function* (
  ctx: Tool.Context,
  target?: string,
  options?: Options,
) {
  if (!target) return
  if (options?.bypass) return

  const ins = yield* InstanceState.context
  const full = process.platform === "win32" ? AppFileSystem.normalizePath(target) : target
  if (containsPath(full, ins)) return                       // ← 在工作区内 → 静默通过

  const kind = options?.kind ?? "file"
  const dir = kind === "directory" ? full : path.dirname(full)
  const glob = path.join(dir, "*").replaceAll("\\", "/")

  yield* ctx.ask({
    permission: "external_directory",
    patterns: [glob],
    always: [glob],                                         // ← 用户允许后永久批准
    metadata: { filepath: full, parentDir: dir },
  })
})
```

**逻辑**：

1. 检查目标路径是否在工作区内（`containsPath`）
2. 如果在 → 静默通过
3. 如果不在 → 触发 `external_directory` 权限询问

这是个**默认 ask** 的权限——也就是说，agent 第一次尝试修改工作区外文件时，会弹窗询问用户。用户选 `always` 后，那个目录的后续访问不再询问。

### 6.6 Doom Loop 检测：权限系统的妙用

这是个隐藏大招——**Doom Loop 检测**也是通过 Permission 系统实现的：

```ts
// src/session/processor.ts:424-449
const recentParts = parts.slice(-DOOM_LOOP_THRESHOLD)        // ← 取最近 3 个 part

if (
  recentParts.length !== DOOM_LOOP_THRESHOLD ||
  !recentParts.every(
    (part) =>
      part.type === "tool" &&
      part.tool === value.name &&                          // ← 同一工具
      part.state.status !== "pending" &&
      JSON.stringify(part.state.input) === JSON.stringify(input),  // ← 完全相同参数
  )
) {
  return                                                    // ← 不满足条件，放行
}

yield* permission.ask({
  permission: "doom_loop",
  patterns: [value.name],
  ruleset: agent.permission,
})
```

**触发条件**：连续 3 次调用同一工具，参数完全相同（用 `JSON.stringify` 比较）。

**触发后**：调用 `permission.ask()` 询问用户。用户可以选择：

- `allow` 这次调用继续
- `reject` 拒绝，agent 被迫换策略
- `always` 永久允许（不推荐，会让死循环继续）

这个机制在 runLoop 篇也讲过，但这里再强调一下：**Doom Loop 检测是建立在 Permission 系统之上的**——没有独立的死循环检测服务，复用了现有的权限询问机制。**这是个非常优雅的复用**。

### 6.7 默认权限配置

```ts
// src/agent/agent.ts:106-108
const defaults = Permission.fromConfig({
  "*": "allow",                                            // 默认全允许
  doom_loop: "ask",                                         // 死循环默认询问
  external_directory: {
    "*": "ask",                                             // 外部目录默认询问
    ...Object.fromEntries(whitelistedDirs.map((dir) => [dir, "allow"])),  // 白名单目录允许
  },
})
```

默认配置非常宽松——除了 `doom_loop` 和 `external_directory`，其他都是 `allow`。

这是个**用户体验优先**的取舍——如果默认全 `ask`，用户会被烦死；默认 `allow` 配合 `doom_loop` 兜底，既流畅又安全。



## 七、工具输出截断：50KB / 2000 行双限制

最后讲一个细节但重要的设计——工具输出截断。

### 7.1 截断常量

```ts
// src/tool/truncate.ts:16-19
export const MAX_LINES = 2000
export const MAX_BYTES = 50 * 1024                         // 50KB
export const DIR = TRUNCATION_DIR                          // "$DATA_DIR/tool-output"
```

**双限制**：行数 ≤ 2000 **且** 字节数 ≤ 50KB。任意一个超过就截断。

### 7.2 截断逻辑

```ts
// src/tool/truncate.ts:86-142
const output = Effect.fn("Truncate.output")(function* (text, options = {}, agent?) {
  const resolved = yield* limits()
  const maxLines = options.maxLines ?? resolved.maxLines
  const maxBytes = options.maxBytes ?? resolved.maxBytes
  const direction = options.direction ?? "head"             // ← 默认保留开头
  const lines = text.split("\n")
  const totalBytes = Buffer.byteLength(text, "utf-8")

  if (lines.length <= maxLines && totalBytes <= maxBytes) {
    return { content: text, truncated: false }               // ← 不需要截断
  }

  // 逐行截断，同时检查行数和字节数
  // ...

  // 完整输出写入文件，返回预览 + 提示
  const file = yield* write(text)
  const hint = hasTaskTool(agent)
    ? `The tool call succeeded but the output was truncated...Use the Task tool...`
    : `The tool call succeeded but the output was truncated...Use Grep or Read...`

  return {
    content: `...${removed} ${unit} truncated...\n\n${hint}`,
    truncated: true,
    outputPath: file,
  }
})
```

**关键设计**：

1. **完整输出写到文件**：`$DATA_DIR/tool-output` 目录下，7 天后自动清理
2. **LLM 看到的是「截断提示 + 后续获取指引」**：
   - 如果有 task 工具：「**Use the Task tool to read the full output**」
   - 否则：「**Use Grep or Read**」
3. **截断方向**：默认 `head`（保留开头），也支持 `tail`（保留结尾）

这个设计让 LLM 知道「**输出太长了被截断**」，并明确告知它怎么获取完整内容——而不是把 LLM 留在黑暗中猜。

![输出截断：50KB / 2000 行双限制](/images/opencode/article-03-truncate.png)

## 八、OpenCode vs Claude Code：工具系统对比

最后用一张表把两个框架的工具系统对比一遍：

| 维度 | Claude Code | OpenCode |
|------|-------------|----------|
| **工具数量** | ~30+ | 18 个核心 |
| **工具定义方式** | Zod Schema | Effect Schema |
| **参数校验** | Zod parse | `Schema.decodeUnknownEffect`（编译缓存） |
| **Edit 策略** | 不公开（推测有 fuzzy 匹配） | 9 种 Replacer 渐进式决策树 |
| **输出截断** | 单条 > 50K 字符写磁盘 + 2KB 预览 | 双限制（2000 行 + 50KB）+ LLM 指引 |
| **权限系统** | PreToolUse hooks（外部） | ask/allow/deny 三态 + 通配符匹配 |
| **Doom Loop 检测** | ❌ 无（社区强烈要求） | ✅ 连续 3 次同参数触发 ask |
| **External Directory** | 有类似机制 | 显式 `external_directory` 权限 |
| **自定义工具** | 通过 MCP | 文件加载 + Plugin 注册 |
| **LSP 集成** | 有 | 有（Edit 后自动诊断） |
| **工具路由** | 模型无关 | GPT 系用 apply_patch，其他用 edit/write |
| **Plugin Hook** | PreToolUse / PostToolUse | `tool.definition` / `tool.execute.before` / `tool.execute.after` |
| **背景任务** | 异步 subagent | BackgroundJob.Service |

**关键差异**：

1. **OpenCode 有 Doom Loop 检测，CC 没有**——这是 OpenCode 的工程亮点，CC 社区在 GitHub Issue #30150 强烈要求添加
2. **Edit 策略的可见性**——OpenCode 的 9 种 Replacer 决策树是开源的，CC 的匹配算法不公开
3. **工具路由**——OpenCode 按模型选择 edit/apply_patch，CC 没有这种设计（推测是因为 CC 只支持 Claude）
4. **Permission 集成度**——OpenCode 把 Doom Loop 检测、External Directory 都集成进 Permission 系统，CC 用独立的 hooks 机制



## 最后

写到这里，OpenCode 的工具系统基本就扒完了。

回过头看，这套系统不是简单的「**写一堆工具函数给 LLM 调**」，它在**抽象层、注册流水线、匹配策略、权限系统、输出截断**每一个维度都做了精致的设计：

- **Tool.Def 接口**用 Effect Schema 让参数校验编译一次复用，输出截断自动包装
- **Edit 引擎 9 种 Replacer** 按精度从高到低渐进式匹配，解决 LLM 写代码时各种「**空白/缩进/转义**」不一致问题
- **Permission 三态系统** + `always` 永久批准 + 自动级联，既安全又不烦人
- **Doom Loop 检测**复用 Permission 系统，连续 3 次同参数触发 ask，优雅防死循环
- **External Directory 边界**让 agent 不能偷偷改工作区外文件，保护用户数据
- **工具输出截断**双限制 + 文件保存 + LLM 指引，让 LLM 知道怎么获取完整内容
- **Plugin Hook** 让插件能修改任何工具的 definition，扩展性极强

每一块拆开看都不是啥复杂技术，但组合在一起，就成了一个既能灵活扩展、又能安全可控的工具系统。

更难得的是，OpenCode 用 675 行 tool.ts + registry.ts + 711 行 edit.ts + 312 行 permission 实现了 Claude Code 用更多代码（含 hooks 系统、独立工具）才实现的事——**简化的代价是放弃了 PreToolUse hooks 的灵活性**，但换来的是**统一的权限模型和内置 Doom Loop 检测**。

今天分享就到这里，我们下篇见！

> 上一篇：[OpenCode 主循环 runLoop 源码精读](/opencode/02-runloop)
> 
> 下一篇：[OpenCode 上下文压缩源码精读：Compact 2 级机制](/opencode/04-compact)