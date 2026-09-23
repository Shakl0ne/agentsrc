---
title: Claude Code 工具系统拆解：50+ 个工具怎么装进一个接口
---

# Claude Code 工具系统拆解：50+ 个工具怎么装进一个接口

上一篇拆主循环时留了个尾巴：每轮 `tool_use` block 从模型流出来，调度器找到对应工具、做权限检查、执行、收集结果、再喂回模型。这个被反复触达的对象——Tool 本身——是这一篇的主角。

想一下你日常用 agent 的画面：让它改个测试，它连着调了 Read、Edit、Bash 三个工具，顺手又起了一个子 agent 去查文档——这几下里，工具的权限判定谁说了算、输出爆了怎么办、50 个工具模型先看见哪几个，全在这一层被决定。

先看一个数字。`src/tools/` 下有 42 个子目录，`Tool.ts` 里定义的接口有 30 多个方法，算上 feature-gated 的内部工具，总数超过 50 个。这 50 个工具里，有的是纯函数式的文件读取，有的是需要弹对话框问用户的危险操作，有的是接到 MCP server 上的动态代理，还有的能修改后续所有工具的执行环境。它们全部塞进同一个接口，还要保证权限系统认得出、调度器排得开、终端 UI 渲染得出来。

一个接口怎么装下这么大的差异？答案分两层：`Tool.ts`（792 行）负责定义契约的形状，`tools.ts`（389 行）负责把所有实现装配成发给 API 的数组。这一篇沿着这两层往下拆，最后并进挂在装配线旁的斜杠命令系统——那条不进模型的用户侧入口。你会看到五个问题的答案：

- 第一，工具怎么声明自己——schema、描述、权限判定为什么都写成方法，而且大多接收 input 参数；
- 第二，`buildTool` 工厂的默认值怎么设计，才能让 50 个实现不写重复代码还不留安全漏洞；
- 第三，装配线怎么排序，才能让 MCP server 随时接入拔出而不打爆 prompt cache；
- 第四，超长输出落盘、延迟加载这两个机制，怎么把工具数量和结果体积从上下文压力里解出来；
- 第五，斜杠命令怎么绕开模型完成用户操作，三种执行模型各走哪条路。

权限系统的完整拆解放在第六篇，子 agent 的调度细节留给第五篇，这里只在工具接口的边界上停住。

## 一、先回答一个问题：工具是不是系统提示的一部分

很多终端 Agent 框架的做法，是把工具描述写成自然语言塞进系统提示，让模型按约定格式输出 JSON，再由应用解析。CC 不走这条路，它把每个工具的 JSON Schema 作为 API `tools` 字段直接提交给 Anthropic Messages API。

这个差别决定了后面所有机制的位置。schema 是数据，随请求组装；模型按 `tool_use` block 协议产出结构化参数，参数由 API 按 schema 校验。停下来看两个后果。

第一个后果：工具的调用格式不需要模型从自由文本里反推。模型看到的工具参数类型、必填项、描述，都由 `input_schema` 严格声明（Zod 推导出 JSON Schema），调用错误率因此下降。改一个工具的 schema 也不会碰坏系统提示的格式，两边是分离的。

第二个后果：工具可以动态增删。MCP server 暴露的工具是运行时才知道的，CC 在不重启进程的情况下把它们加进 `tools` 数组发出去。schema 既然是数据，加几十个工具和加几十行文本没有本质区别。

接口里的 `description` 进一步利用了这个动态性——它是个方法，签名是 `description(input, options)`。同一个工具对不同输入可以返回不同描述，BashTool 面对一条 `git push` 和一条 `ls` 可以给出不同的风险提示。这一点在后面权限和对比的章节还会用到。

## 二、Tool 接口：30 多个方法分五组

`Tool.ts` 里的类型是一个泛型别名：

```ts
// src/Tool.ts:362
export type Tool<
  Input extends AnyObject = AnyObject,
  Output = unknown,
  P extends ToolProgressData = ToolProgressData,
> = { ... }
```

三个类型参数分别管输入 schema（Zod 类型）、输出类型、进度数据类型。直接读这个 30 多个方法的大接口会迷路，按职责分五组来读：身份、执行、权限、渲染、加载策略。

### 2.1 身份组：名字不止一个

```ts
// src/Tool.ts:371-385 + 386-393 + 518-523（签名节选）
aliases?: string[]
searchHint?: string
description(
  input: z.infer<Input>,
  options: {
    isNonInteractiveSession: boolean
    toolPermissionContext: ToolPermissionContext
    tools: Tools
  },
): Promise<string>
prompt(options: { getToolPermissionContext: () => ...; tools: Tools; agents: AgentDefinition[] }): Promise<string>
```

`name` 是模型在 `tool_use.name` 里使用的稳定标识。`aliases` 处理改名后的兼容——工具换了名字，旧名字继续能查到它。`searchHint` 是 3–10 个词的能力短语，供 ToolSearch 做关键词匹配，注释特意要求「prefer terms not already in the tool name」：NotebookEdit 的 searchHint 写了 jupyter，因为工具名里没这个词，模型搜 jupyter 才能命中。

`description` 和 `prompt` 都是异步方法，返回面向模型的说明文本，会拼在 schema 旁边。异步意味着描述可以依赖运行时状态——权限模式、当前工具列表、agent 定义都在参数里。

### 2.2 执行组：call 与它的三个搭档

```ts
// src/Tool.ts:379-385 + 402-416（签名节选）
call(
  args: z.infer<Input>,
  context: ToolUseContext,
  canUseTool: CanUseToolFn,
  parentMessage: AssistantMessage,
  onProgress?: ToolCallProgress<P>,
): Promise<ToolResult<Output>>
isConcurrencySafe(input: z.infer<Input>): boolean
isReadOnly(input: z.infer<Input>): boolean
interruptBehavior?(): 'cancel' | 'block'
```

`call` 是主入口，五个参数各管一件事。`context` 是工具访问外部世界的唯一入口，后面单独拆。`canUseTool` 作为参数注入，工具执行中需要二次确认时直接调它——BashTool 遇到破坏性命令就是这么走的。`onProgress` 是显式的进度回调，工具执行中随时可以推数据出去，第八节展开。

返回的 `ToolResult` 不止带数据：

```ts
// src/Tool.ts:321-336
export type ToolResult<T> = {
  data: T
  newMessages?: (UserMessage | AssistantMessage | AttachmentMessage | SystemMessage)[]
  // contextModifier is only honored for tools that aren't concurrency safe.
  contextModifier?: (context: ToolUseContext) => ToolUseContext
  mcpMeta?: { _meta?: Record<string, unknown>; structuredContent?: Record<string, unknown> }
}
```

`newMessages` 让工具往对话历史里注入额外消息。`contextModifier` 让工具修改后续工具调用的 context——注释里写明了它只对非并发安全的工具生效，原因不难想：并发执行的多个工具同时改 context 会产生竞态，这个能力只能给串行工具。

`isConcurrencySafe(input)` 决定调度器能不能并行跑多个同类工具实例，注意它同样接收 input——只读性判断是按调用粒度做的，同一个工具对不同输入可以给出不同答案。

### 2.3 权限组：判定逻辑长在工具身上

```ts
// src/Tool.ts:489-516（节选）
validateInput?(input, context): Promise<ValidationResult>
checkPermissions(input, context): Promise<PermissionResult>
preparePermissionMatcher?(input): Promise<(pattern: string) => boolean>
getPath?(input): string
```

这组方法没有 `permissions: PermissionType[]` 这种声明式结构。CC 走函数式判定：`validateInput` 先做语义校验（路径是否合法、参数是否互斥），`checkPermissions` 是工具自己的「我是否需要问用户」逻辑，返回的 `behavior` 字段取 `allow`、`deny`、`ask`、`passthrough` 四种。

`preparePermissionMatcher` 稍微绕一点，它服务 hook 的 `if` 条件。用户配置了 `Bash(git *)` 这条规则，系统需要一个匹配器判断 `git push` 命中没命中这条规则；这个方法在权限检查前调用一次，把昂贵的命令解析做掉，返回一个闭包逐条 pattern 匹配。

`getPath` 把「这个工具操作哪个文件」抽象出来，路径类的权限规则统一按它匹配。

### 2.4 渲染组：React 组件化的终端 UI

CC 的终端 UI 用 React + Ink 渲染（第一篇拆过）。Tool 接口里的渲染方法有八个，覆盖工具的整个生命周期：

```ts
// src/Tool.ts:566-694（签名节选）
renderToolUseMessage(input: Partial<z.infer<Input>>, options): React.ReactNode
renderToolResultMessage?(content, progressMessages, options): React.ReactNode
renderToolUseProgressMessage?(progressMessages, options): React.ReactNode
renderGroupedToolUse?(toolUses, options): React.ReactNode | null
```

排队、开始、执行中、完成、被拒绝、出错，每个阶段一个方法，外加 `renderGroupedToolUse` 把多个并行同类工具合并展示——模型一次发 5 个 Grep，UI 合成一组，避免刷屏，返回 null 时回退逐个渲染。

这里有个细节：`renderToolUseMessage` 接收的是 `Partial<Input>`。工具参数是流式从 API 返回的，参数还没收完就可能要先渲染——用户不用等模型把整个 `tool_use` block 生成完，就能看到「它要读 src/foo.ts 了」。

渲染组里还有两个「给别的子系统看」的方法：`extractSearchText` 给 transcript 搜索索引用，注释的要求很严格——必须返回屏幕上实际渲染的文本，返回了屏幕上没有的内容会造成「索引命中但高亮找不到」的幽灵 bug，漂移由专门的渲染保真测试兜住。`getActivityDescription` 给 spinner 提供「Reading src/foo.ts」这样的活动描述。

### 2.5 加载策略组：shouldDefer 与 alwaysLoad

```ts
// src/Tool.ts:442-449
readonly shouldDefer?: boolean
readonly alwaysLoad?: boolean
```

这两个字段控制工具进不进初始 prompt，第六节展开。

## 三、ToolUseContext：工具看到的世界

`call` 的第二个参数 `ToolUseContext` 是个 40 多个字段的大对象，几乎把整个会话状态暴露给工具。逐字段列举没有意义，挑四个有设计含量的说。

**`readFileState: FileStateCache`**。一个 LRU 缓存，记录每个文件最近被 Read 时的 mtime 和内容状态。它的核心消费者是 FileEditTool：Edit 匹配不到 `old_string` 时，会检查缓存里的 mtime 和磁盘当前值是否一致，不一致说明文件被外部改过，拒绝执行并要求重新 Read。这是防止「基于过期内容做编辑」损坏数据的关键一环。LRU 意味着繁忙会话里早期文件的状态会被驱逐——模型先读 A、做一堆别的操作、再 Edit A，可能 A 的状态已经没了，Edit 会要求重新读。宁可多读一次，不基于可能过期的状态动手。

**`setAppStateForTasks`**。主线程的 `setAppState` 在异步子 agent 里被替换成 no-op，防止子 agent 直接改全局状态；但后台 bash 任务需要注册到根 store 才能被正确清理。这个字段永远指向根 store，无论 agent 嵌套多深，后台任务都能注册上。BashTool 的 `call` 里显式写了 `setAppState: toolUseContext.setAppStateForTasks ?? setAppState`，注释说明就是为了让异步 agent 的后台任务「actually registered (and killable on agent exit)」。

**`contentReplacementState`**。上下文预算管理的状态。对话历史里 tool result 累积过多时，`query.ts` 根据它把早期结果替换成摘要或文件引用。主线程初始化一次永不重置；子 agent 通过 `createSubagentContext` 克隆父 agent 的状态——克隆的动机写在注释里：cache-sharing fork 要做出和父 agent 一致的内容替换决策，否则同样的历史在父子两边被替换成不同内容，cache 就对不上了。

**`renderedSystemPrompt`**。fork subagent 实验路径专用。它把父 agent 在 turn 开始时渲染好的系统提示冻结下来传给子 agent。注释解释了为什么不用现成的 `getSystemPrompt()` 重算一遍：fork 时 GrowthBook 的 feature flag 状态可能已经从 cold 变成 warm，重算出来的提示和父 agent 的不一致，prompt cache 直接失效。冻结字节流，是为了字节级一致。

这四个字段有一个共同点：每个都是为了让「子 agent 和主线程共享某类状态」这件事不出错。工具接口不直接管子 agent，但它暴露的 context 决定了子 agent 能正确继承什么、被隔离什么。

## 四、buildTool：默认值怎么设计才敢放开 50 个实现

30 多个方法的接口，50 个实现，每个实现都得写一遍所有方法吗？写不下。`Tool.ts` 末尾的 `buildTool` 工厂解决这个问题：

```ts
// src/Tool.ts:707-714
type DefaultableToolKeys =
  | 'isEnabled'
  | 'isConcurrencySafe'
  | 'isReadOnly'
  | 'isDestructive'
  | 'checkPermissions'
  | 'toAutoClassifierInput'
  | 'userFacingName'

export type ToolDef<...> = Omit<Tool<...>, DefaultableToolKeys> &
  Partial<Pick<Tool<...>, DefaultableToolKeys>>
```

`ToolDef` 把七个方法的实现变成可选，`buildTool` 补齐缺的部分。工具实现时写 `buildTool({ ... } satisfies ToolDef<InputSchema, Output>)`，类型安全不丢，代码量砍掉一大截。

默认值的方向是这条设计的主心骨：

```ts
// src/Tool.ts:757-769
const TOOL_DEFAULTS = {
  isEnabled: () => true,
  isConcurrencySafe: (_input?: unknown) => false,
  isReadOnly: (_input?: unknown) => false,
  isDestructive: (_input?: unknown) => false,
  checkPermissions: (input, _ctx?) =>
    Promise.resolve({ behavior: 'allow', updatedInput: input }),
  toAutoClassifierInput: (_input?: unknown) => '',
  userFacingName: (_input?: unknown) => '',
}
```

七个默认值里，安全相关的三个全是收紧方向：`isConcurrencySafe` 默认 false——新工具默认不被并行执行；`isReadOnly` 默认 false——默认被当成会写文件的操作对待；`toAutoClassifierInput` 默认空字符串——默认不进 auto 模式的安全分类器，要进必须显式重写。放松任何一个限制都需要实现方显式写出来。

反过来的设计会怎样？假设 `isReadOnly` 默认 true，开发者写新工具时忘了改，这个工具会被权限系统当成只读操作放行——安全漏洞藏在「忘记写」这个最常见的失误里。fail-closed 把默认失误的方向对准了安全一侧：忘了写最多损失一点性能（不该串行的被串行了），不会损失安全性。

类型层面还有一段体操。`BuiltTool<D>` 在类型系统里模拟运行时的 `{ ...TOOL_DEFAULTS, ...def }`：调用方提供了具体实现的字段用调用方的类型，省略或写成 optional 的字段用默认值的类型。源码注释很直白：「The type semantics are proven by the 0-error typecheck across all 60+ tools」——60 多个工具类型检查零错误，这套推导成立。

## 五、装配线：tools.ts 的流水

接口有了，实现有了，`tools.ts` 把它们装成发给 API 的数组。三个函数接力：源头清点、过滤、合并排序。

![Claude Code 工具装配流水线：三个函数接力产出 API tools 数组](/images/claudecode/03-assembly-pipeline.svg)

### 5.1 getAllBaseTools：谁有资格进列表

`getAllBaseTools()` 返回所有内置工具，每个工具进不进列表由三种条件决定，对应的实现手法各不相同。

**编译期消除**。Bun 的 `feature()` 在打包时做死代码消除，Anthropic 内部构建开启某些 flag，公开发布的构建不含这些代码：

```ts
// src/tools.ts:25-28
const SleepTool =
  feature('PROACTIVE') || feature('KAIROS')
    ? require('./tools/SleepTool/SleepTool.js').SleepTool
    : null
```

SleepTool、Cron 系列、MonitorTool 这些实验工具走这条路，发布版本的二进制里压根没有它们的代码。REPLTool、SuggestBackgroundPRTool 用 `process.env.USER_TYPE === 'ant'` 判断，只有内部用户的构建才包含。

**运行时过滤**。feature flag 和环境变量在运行时判断：TodoV2 开了才挂上四个 Task 工具，`ENABLE_LSP_TOOL` 设了才挂 LSPTool，worktree 模式开了才挂 Enter/ExitWorktreeTool。

**循环依赖打破**。TeamCreateTool、TeamDeleteTool、SendMessageTool 三个工具反向引用 `tools.ts`，静态 import 会成环。解法是 lazy require：

```ts
// src/tools.ts:63-65
const getTeamCreateTool = () =>
  require('./tools/TeamCreateTool/TeamCreateTool.js')
    .TeamCreateTool as typeof import('./tools/TeamCreateTool/TeamCreateTool.js').TeamCreateTool
```

`require` 是运行时调用，把循环从模块加载期挪到首次调用期；后面的 `as typeof import(...)` 把类型签名对齐到静态 import 的类型——运行时绕开了环，类型信息一点没丢。

### 5.2 getTools：逐轮过滤

`getTools(permissionContext)` 在 base 列表上过筛子。第一轮把 ListMcpResources、ReadMcpResource、SyntheticOutput 这三个特殊工具从默认列表摘掉——它们有专门的使用路径。第二轮 deny 规则过滤，用和运行时权限检查同一套 matcher，把被 blanket deny 的工具在 schema 提交前剔除，模型压根看不到它们。REPL 模式启用时还有一轮条件过滤，把被 REPL 包装的原始工具藏起来。最后一轮逐个调 `isEnabled()`，工具自己声明当前可不可用。

deny 过滤放在 schema 提交前有明确的收益：模型视野里直接没有不合规的工具，省 token 也省一轮无效的工具调用尝试。

### 5.3 assembleToolPool：排序是为了 cache

合并内置工具和 MCP 工具的函数，藏着全文注释密度最高的一段：

```ts
// src/tools.ts:354-366
// Sort each partition for prompt-cache stability, keeping built-ins as a
// contiguous prefix. The server's claude_code_system_cache_policy places a
// global cache breakpoint after the last prefix-matched built-in tool; a flat
// sort would interleave MCP tools into built-ins and invalidate all downstream
// cache keys whenever an MCP tool sorts between existing built-ins. uniqBy
// preserves insertion order, so built-ins win on name conflict.
const byName = (a: Tool, b: Tool) => a.name.localeCompare(b.name)
return uniqBy(
  [...builtInTools].sort(byName).concat(allowedMcpTools.sort(byName)),
  'name',
)
```

Anthropic API 的 prompt caching 按前缀匹配，工具 schema 是 prompt 的重要组成部分，顺序一变 cache 就废。这段代码的排序策略是：内置工具排成一段连续前缀，MCP 工具作为可变后缀接在后面。用户接入或断开一个 MCP server，失效的只有 MCP 那一段的 cache，内置工具的前缀纹丝不动。

如果偷懒做全局扁平排序会怎样？MCP 工具按字母序插进内置工具中间，任何一个 MCP server 的变动都会移动后面所有工具的位置，下游所有 cache key 报废。一个 `concat` 顺序的选择，背后是整个缓存策略。

`uniqBy('name')` 在名字冲突时保留先出现的——内置工具优先。旁边还有个 `getMergedTools` 函数只做简单拼接，不做过滤和去重，注释写明它是给「统计完整工具数量」的场景用的（ToolSearch 的阈值计算、token 计数），两个函数各有用途，不能混用。

## 六、延迟加载与超长落盘：两个压力释放阀

50 个工具全量挂上，再接几个 MCP server，schema 的体积和单次结果的大小都会顶到上下文窗口。CC 用两个机制分别泄压。

### 6.1 ToolSearch：schema 按需暴露

工具搜索开启时，所有 MCP 工具和标记 `shouldDefer: true` 的内置工具不再出现在初始 prompt 里——它们的 schema 以 `defer_loading: true` 标记提交，模型只看得到名字。要用，先调 ToolSearch，搜到的工具完整 schema 才出现在结果里，此后和普通工具一样可调。

判定逻辑在 `isDeferredTool`，判定顺序本身就是设计：

```ts
// src/tools/ToolSearchTool/prompt.ts:62-71（末尾兜底 return 在 107 行）
export function isDeferredTool(tool: Tool): boolean {
  // Explicit opt-out via _meta['anthropic/alwaysLoad'] — tool appears in the
  // initial prompt with full schema. Checked first so MCP tools can opt out.
  if (tool.alwaysLoad === true) return false
  // MCP tools are always deferred (workflow-specific)
  if (tool.isMcp === true) return true
  // Never defer ToolSearch itself — the model needs it to load everything else
  if (tool.name === TOOL_SEARCH_TOOL_NAME) return false
  ...
  return tool.shouldDefer === true
}
```

规则按优先级排：`alwaysLoad` 最先判，MCP server 的作者可以通过 `_meta['anthropic/alwaysLoad']` 声明自己的核心工具必须第一轮就出现——控制权交给 server 作者。MCP 工具默认全部延迟——它们是「工作流特定」的，多数会话用不上。ToolSearch 自己绝不延迟——它是加载一切的前提。

搜索本身的实现是纯本地的关键词匹配，没有模型调用。三种查询形态：`select:Read,Edit,Grep` 按名字精确取，`notebook jupyter` 关键词模糊搜，`+slack send` 要求所有带 `+` 的词必须在名字或描述里命中，其余词参与排序。打分规则按命中位置加权，主要几档是：名字段精确命中 10 分（MCP 工具 12 分）、名字段子串命中 5 分（MCP 6 分）、searchHint 命中 4 分、描述命中 2 分。

要不要开启工具搜索本身也有档位，由 `ENABLE_TOOL_SEARCH` 环境变量控制：`true` 强制开、`false` 强制关、`auto` 按阈值自动（默认上下文窗口的 10%，MCP 工具描述的 token 量超过它才延迟）。注意默认值是开——工具一多延迟就是常态，不延迟反而要显式配置。

### 6.2 maxResultSizeChars：结果落盘

工具输出超长时（BashTool 跑一次 `npm test` 可能吐几万行），CC 不把全文塞回 messages，而是落盘到会话目录，模型只拿到带文件路径的预览：

```ts
// src/utils/toolResultStorage.ts:189-197
export function buildLargeToolResultMessage(result: PersistedToolResult): string {
  let message = `${PERSISTED_OUTPUT_TAG}\n`
  message += `Output too large (${formatFileSize(result.originalSize)}). Full output saved to: ${result.filepath}\n\n`
  message += `Preview (first ${formatFileSize(PREVIEW_SIZE_BYTES)}):\n`
  message += result.preview
  ...
}
```

预览是文件开头 2000 字节，模型需要更多细节时用 FileReadTool 读文件。落盘路径按会话组织（`projectDir/sessionId/tool-results/`），写入用 `wx` flag 防竞争——tool_use_id 每次调用唯一，同一个 id 重放时文件已存在就跳过，microcompact 回放原始消息不会重复写。

每个工具声明自己的上限：BashTool 30,000 字符，MCPTool 100,000，还有一个全局默认 50,000 封顶。FileReadTool 是特例，设成 `Infinity`——落盘 Read 的结果会形成「Read → 落盘文件 → 模型再 Read」的环，而且 Read 自己有截断策略，不需要外层再管。判定逻辑里 `Number.isFinite` 检查放在最前面，GrowthBook 的阈值覆盖也拿它没办法，这个 opt-out 是硬的。

两个机制方向相反：落盘管单次结果的体积，延迟加载管 schema 的总量。合起来看，上下文压力被拆到三个位置——初始 prompt 只装高频工具的 schema，单次结果超限走磁盘，历史结果由下一篇的压缩系统接管。

![上下文压力分流：初始 prompt、磁盘、压缩系统三个出口](/images/claudecode/03-context-pressure-valves.svg)

## 七、两个代表工具的实现取舍

接口和装配线是骨架，看两个实现才能感到这套设计落到代码里是什么样。挑了差异最大的两个：一个是系统里最重的 BashTool，一个是几乎全空的 MCPTool。

### 7.1 BashTool：把流式做到进程级

BashTool 是所有工具里最复杂的一个，目录下 18 个文件，主文件 1,143 行。它的 `call` 主线抽出来是这样：

```ts
// src/tools/BashTool/BashTool.tsx:624-683（大幅省略）
async call(input, toolUseContext, _canUseTool, parentMessage, onProgress) {
  if (input._simulatedSedEdit) {
    return applySedEdit(input._simulatedSedEdit, toolUseContext, parentMessage)
  }
  const stdoutAccumulator = new EndTruncatingAccumulator()
  const preventCwdChanges = !toolUseContext.agentId  // 子 agent 禁改 cwd
  const commandGenerator = runShellCommand({
    input, abortController, preventCwdChanges,
    setAppState: toolUseContext.setAppStateForTasks ?? setAppState,
  })
  do {
    generatorResult = await commandGenerator.next()
    if (!generatorResult.done && onProgress) {
      onProgress({ toolUseID: `bash-progress-${progressCounter++}`,
                   data: { type: 'bash_progress', ... } })
    }
  } while (!generatorResult.done)
  ...
}
```

四个点分开说。

**sed 模拟**。入口第一行就检查 `_simulatedSedEdit`——用户在预览里批准了一份 sed 编辑结果后，BashTool 直接把预览的内容写入文件，不再执行真的 sed。schema 里这个字段被显式 omit 掉，模型看不到它，注释解释了原因：暴露它等于让模型用「无害命令 + 任意文件写入」绕过权限检查和沙箱。用户预览到什么，就写什么，两边严格一致。

**generator 化的执行**。`runShellCommand` 是 async generator，每个 yield 是一段输出 chunk，`call` 边消费边推 onProgress——终端上看到的是实时滚动的 stdout，不是等命令跑完的一次性输出。

**头部截断累加器**。`EndTruncatingAccumulator` 反着读才对——保留头部、截掉尾部。容量超限时只把还能装下的字符追加进去，后续输出直接丢弃，最后在 `toString` 里补一行 `... [output truncated - ${KB}KB removed]`。类名的「End」指被截掉的是 end，不是保留 end。超长输出保头不保尾，配合前一步的落盘机制：完整输出在磁盘文件里，累加器里的这段只需要够模型判断命令干了什么。

**cwd 防线**。`preventCwdChanges = !isMainThread`——子 agent 里不允许 `cd` 生效。子 agent 改掉 cwd 会污染主线程后续操作的相对路径，执行后检查、越界就拉回项目根。
### 7.2 MCPTool：壳与实体的分离

MCPTool 走了另一个极端。文件只有 77 行，`call` 是空壳，几乎每个方法上面都有一行注释「Overridden in mcpClient.ts」：

```ts
// src/tools/MCPTool/MCPTool.ts:29-51（节选）
export const MCPTool = buildTool({
  isMcp: true,
  name: 'mcp',
  maxResultSizeChars: 100_000,
  // Overridden in mcpClient.ts with the real MCP tool name + args
  async description() { ... },
  // Overridden in mcpClient.ts
  async call() { return { data: '' } },
  ...
})
```

具体工具实例在 `services/mcp/client.ts` 里动态生成——每个 MCP server 暴露的 tool 都基于 MCPTool 用对象展开创建一个重写了 `name`、`description`、`call`、`checkPermissions` 的实例。

为什么是壳？因为 MCP server 暴露的工具是运行时才知道的，编译期不存在一个可以静态 import 的实现列表。壳的作用是提供一个类型和行为的基座，让动态生成的实例满足 `Tool` 接口的全部约束——权限系统、调度器、UI 都能像对待内置工具一样对待它。

调用侧的 `callMCPTool` 也有几个防坑设计。MCP SDK 自带的超时在 SSE 流断开时不可靠，外面再套一层 `Promise.race` 自己计时；server 返回 `-32042` URL elicitation 错误时最多重试三次，每次让用户完成 OAuth 流程；执行中每 30 秒打一条 debug 日志（`Tool 'X' still running (Ys elapsed)`），诊断卡住的 server 全靠它。SDK 的 progress notification 通过 `onprogress` 回调透传成 CC 的 `MCPProgress`，链路是「server 推进度 → SDK 转发 → 工具转发 → UI 渲染」，MCP server 也能享受实时进度展示，前提是 server 自己实现了这个协议。

## 八、进度回调：长任务不黑盒

`onProgress` 是 `call` 的第五个参数。进度数据是强类型的 union，每个工具有自己的 Progress 类型：

```ts
// src/types/tools.ts（Tool.ts:64-73 re-export）
export type ToolProgressData =
  | BashProgress
  | MCPProgress
  | AgentToolProgress
  | SkillToolProgress
  | TaskOutputProgress
  | WebSearchProgress
  | REPLToolProgress

// BashProgress 节选
type BashProgress = {
  type: 'bash_progress'
  output: string        // 当前 chunk
  fullOutput: string    // 累积输出
  elapsedTimeSeconds: number
  ...
}
```

链路是：工具在 `call` 执行中随时回调 → QueryEngine 把进度收进消息的 ProgressMessage 列表 → `setInProgressToolUseIDs` 更新进行中集合 → React 状态变化触发重渲染 → 工具自己的 `renderToolUseProgressMessage` 决定怎么画。BashTool 画实时滚动输出，AgentTool 画子 agent 的活动状态，WebSearchTool 画 Searching。

设计上有两个细节。一是进度不强制——`onProgress` 是可选参数，不推进度的工具照常工作，只是 UI 上安静一点。二是 `filterToolProgressMessages` 这个小工具函数把 hook 的进度消息从工具进度里滤掉，两类进度来源不同，渲染方法只该收到自己那一类。

对比没有进度回调的实现（Codex 的工具是调完等结果），差别在信任感：用户能看到命令在做什么，发现卡住可以及时按 Ctrl+C，工具内部把 `abortController.signal` 透传给子进程，中断是即时的。

## 九、斜杠命令：不进模型的另一条入口

工具链路的前提是模型在转。但终端里还有一类高频操作——`/compact` 压上下文、`/cost` 看花费、`/theme` 换主题——用户敲下回车这一刻不需要任何智能。这类输入以 `/` 开头，在进入主循环之前就被截走，由前端直接解析、分派、执行。命令系统就建在这条旁路上，注册中心是 `src/commands.ts`（754 行），它要回答两个问题：一条命令怎么执行，以及一条命令凭什么出现在你的输入框里。

### 9.1 三种执行模型

`Command` 类型是元数据与三种执行形态的交集：`prompt`、`local`、`local-jsx`。

| 类型 | 执行方式 | 是否触发模型 | 典型命令 |
|------|---------|-------------|---------|
| `prompt` | 构造提示词注入对话 | 是 | `/commit`、`/review`、`/init` |
| `local` | 同步函数，返回文本 | 否 | `/compact`、`/cost`、`/clear` |
| `local-jsx` | 渲染 Ink 交互组件 | 否 | `/mcp`、`/config`、`/model` |

`prompt` 类型最有意思：它自己不执行业务逻辑，工作是动态构造一段提示词交给模型。`/commit` 的实现就是一份提示词模板加一个工具白名单：

```ts
// src/commands/commit.ts（节选）
const ALLOWED_TOOLS = [
  'Bash(git add:*)',
  'Bash(git status:*)',
  'Bash(git commit:*)',
]
```

模型拿到提示词后只能在 `git add/status/commit` 三个动作里打转，配合权限规则实现无确认执行。命令在这里是「预设的工具调用脚本」——用户侧的快捷方式，最终落回工具系统。

`local` 返回 `text / compact / skip` 三种结果，其中 `compact` 是 `/compact` 专用的特殊通道：返回的是压缩后的完整消息列表，调度器走完全不同的拼装路径（这条管线第四篇会拆）。`local-jsx` 渲染 React 组件，命令完成后通过 `onDone` 回调把结果交还调度器，还能顺带预填下一条输入。

### 9.2 六路来源，一条优先级链

内置命令之外，`getCommands(cwd)` 还要在运行时汇入六路来源：bundled skills、内置插件 skills、skill 目录、workflow 命令、插件命令、插件 skills，最后叠上 `COMMANDS()` 数组。MCP 命令的发现是异步的，REPL 渲染后再通过 `useMergedCommands` 二次叠加；插件命令与插件 skills 在 `loadAllCommands` 内部异步汇入。lodash `uniqBy` 去重保留先出现的——合并顺序就是优先级：已加载 skills/plugins > 动态 skills > 内置命令。

会话中途动态发现的 skills（比如用户刚创建的 `.claude/skills/xxx/`）插入位置在内置命令之前：

```ts
// src/commands.ts:505-516（节选）
const insertIndex = baseCommands.findIndex(c => builtInNames.has(c.name))
return [
  ...baseCommands.slice(0, insertIndex),
  ...uniqueDynamicSkills,
  ...baseCommands.slice(insertIndex),
]
```

加载贵，所以处处 memoize：`COMMANDS()` 延迟到调用时才读配置，`loadAllCommands(cwd)` 按目录缓存磁盘 I/O 的结果。最重的例外是 `/insights`——实现有 113KB（3200 行），`commands.ts` 里为它手写了一个 shim，把模块解析推迟到命令真正被触发的那一刻。清缓存也有讲究：外层 memoize 命中时永远不会触达内层，只清内层是无效操作，必须从外到内逐层清。

### 9.3 八层门控，两种安全模型

一个命令要出现在 typeahead 里，得过八道门：feature flag（编译期消除）、`INTERNAL_ONLY_COMMANDS`（ant-only）、`availability`（认证类型）、`isEnabled()`（运行时开关）、`isHidden`、`userInvocable`，远程模式再加 `REMOTE_SAFE` 与 `BRIDGE_SAFE` 两道。前六道决定「存在与可见」，后两道决定「谁能触发」。

远程这两道白名单背后有个事故。PR #19134 记录了「`/model` from iOS was popping the local Ink picker」——手机上敲 `/model`，终端里弹出一个根本没法操作的交互面板。修复没有做危险命令黑名单，而是全部禁止，然后逐个放开。固化成三行判定：

```ts
// src/commands.ts:672-676
export function isBridgeSafeCommand(cmd: Command): boolean {
  if (cmd.type === 'local-jsx') return false
  if (cmd.type === 'prompt') return true
  return BRIDGE_SAFE_COMMANDS.has(cmd)
}
```

`local-jsx` 永远不能远程触发，手机端没有终端 UI；`prompt` 展开成文本、默认安全；`local` 必须进显式白名单——目前只有 `compact`、`clear`、`cost`、`summary`、`releaseNotes`、`files` 六个。先一刀切禁止，再靠白名单逐个评估放开：收紧的代价是一次 PR，放开的代价是每次审计，方向不对称，所以默认站在收紧一侧。

### 9.4 解析器为什么只有 60 行

一个常见误解是斜杠命令用 Commander.js 解析。Commander.js 确实在代码里（`main.tsx` import），但它只负责 CLI 启动参数——`claude --print`、`claude --resume` 这一层的 flag 解析。REPL 内的 `/xxx` 走的是 `slashCommandParsing.ts` 里一个 60 行的解析器，逻辑一句话讲完：按空格切，第一个词是命令名，剩下的是参数。

这么简是有意的。所有命令都是「名字 + 自由文本」的形态，参数怎么解释由命令自己决定——`/mcp` 按空格切 `action target`，`/compact` 把整段参数当自定义总结指令。最小公共协议加命令自治，比给每个命令声明 flag schema 的维护成本低。MCP 命令的特殊语法 `/server:tool (MCP) args` 也只是检查第二个词是不是 `(MCP)` 字面量。

### 9.5 与工具系统共享一套底座

命令和工具看似两套系统，底座是同一套。`prompt` 命令的 `allowedTools` 复用工具权限规则；skills 既能被用户用 `/name` 触发，也能被模型通过 SkillTool 调用——`userInvocable: false` 只许模型调，`disableModelInvocation` 只许用户调，两个开关把可见性划成四个象限。命令系统的边界因此清晰：它是工具系统的用户侧投影，共享装配、门控与权限的下半身，只在上半身换了三种执行形态。

![斜杠命令按类型走三条路径，只有 prompt 路径会触达模型](/images/claudecode/03-command-dispatch.svg)

## 十、和 OpenCode、Codex 放一起看

三个框架的工具系统并排，差异比想象中大：

| 维度 | Claude Code | OpenCode | Codex |
|------|-------------|----------|-------|
| 接口形态 | 泛型类型别名 + buildTool 工厂 | `Tool.Def` 接口 | Rust `Tool` trait |
| 描述生成 | `description(input)` 动态方法 | 静态字段 | 静态字符串 |
| 权限判定 | 工具自实现 `checkPermissions` | 三档配置 | `ExecPolicy` 沙箱 |
| 进度回调 | 强类型 union + 每工具渲染 | 回调式 onProgress | 无 |
| UI 渲染 | React + Ink，8 个生命周期方法 | 纯文本拼接 | 无 |
| 延迟加载 | shouldDefer + ToolSearch | 无 | 无 |
| 超长结果 | 落盘 + 预览 | 调用方截断 | 调用方截断 |
| 工具数量 | 50+（含 gated） | ~15 | ~10 |

几行表格之外，有一层差异更根本：三个项目对「工具是什么」的假设不同。Codex 把工具当系统调用——每个工具做一件事，权限由外部沙箱统一管，接口薄。OpenCode 把工具当配置项——接口比 Codex 丰富一点，有进度回调，但渲染和权限走统一路径。CC 把工具当 React 组件——工具知道自己的权限需求、渲染自己的 UI，`contextModifier` 让它能修改后续执行环境，`shouldDefer` 让它能声明自己的加载策略。

这个假设的代价和收益都清楚。代价是单个工具重：写一个 CC 工具要考虑的事比写一个 Codex 工具多得多，接口 30 多个方法（好在 `buildTool` 砍掉了大半）。收益是表达力：BashTool 能做到「sed 预览即写入、进程级流式、语义化错误解释」，这些能力没有一个能塞进「薄工具 + 外部沙箱」的模型里。工具越像组件，组合出的行为就越丰富，前提是你养得起这么重的工具。

数量是另一个分水岭。50 个工具带来的问题——schema 撑爆 prompt、结果撑爆上下文、冷门工具稀释模型注意力——逼出了 ToolSearch 和落盘这两个 CC 独有的机制。OpenCode 和 Codex 的工具少一个数量级，这些问题还没到需要专门机制的程度。反过来问也成立：如果它们的工具涨到 50 个，现在这套薄接口还撑不撑得住。

## 十一、四条设计原则

回头看全文，CC 工具系统里最值得带走的是四条设计。

**动态优先于静态**。`description(input)` 是动态的，`isConcurrencySafe(input)` 是动态的，`checkPermissions(input, ctx)` 也是动态的。工具的行为按具体调用调整，BashTool 能对 `rm -rf` 和 `ls` 给出截然不同的权限判定，静态标签做不到这一点。代价是权限系统不能查表，必须实际调用工具的方法——用一点运行时开销换判断精度。

**fail-closed 的默认值**。`buildTool` 的默认值全部朝收紧方向：不并发、视为写、不进分类器。放宽要显式声明，忘记写的失误被默认值兜在安全一侧。50 个实现共享一套默认，任何一个新的加入都不需要重新审一遍安全边界。

**顺序也是设计**。`assembleToolPool` 的排序不是排版问题，是缓存策略：内置工具排成连续前缀，MCP 工具作为可变后缀。工具列表的任何顺序选择都在和 API 的前缀匹配缓存博弈——把「什么最稳定」放在最前面，是这类系统装配函数的通用思路。

**先收紧，再靠名单放开**。Bridge 命令白名单来自一次事故：`/model` 从 iOS 触发，本地终端弹出一个无法操作的 Ink 选择面板。修复选择全部禁止加显式白名单，逐个审计放开。这与 `buildTool` 的 fail-closed 是同一个思想的两处落地：接口的默认值、系统的默认策略，都把失误的代价压向最小。

下一篇进入对话压缩系统，看 CC 如何在 5 个层级上管理上下文窗口的增长——工具结果的落盘只是那套体系的第一道门。

## 源码索引

- `src/Tool.ts` — Tool 接口、ToolDef、buildTool、ToolUseContext、ToolResult
- `src/tools.ts` — getAllBaseTools、getTools、assembleToolPool、filterToolsByDenyRules、getMergedTools
- `src/tools/BashTool/BashTool.tsx` — BashTool 主实现（call / sed 模拟 / generator 消费）
- `src/tools/MCPTool/MCPTool.ts` — MCP 工具壳（Overridden in mcpClient.ts）
- `src/services/mcp/client.ts` — MCP 工具实例动态生成、callMCPTool（超时/进度/elicitation 重试）
- `src/tools/ToolSearchTool/ToolSearchTool.ts` — 工具搜索实现（select / 关键词 / +required 三种查询）
- `src/tools/ToolSearchTool/prompt.ts` — isDeferredTool 判定顺序
- `src/utils/toolSearch.ts` — ENABLE_TOOL_SEARCH 档位与阈值
- `src/utils/toolResultStorage.ts` — 超长结果落盘（preview / wx 写入 / 阈值解析）
- `src/commands.ts`（754 行）— 命令注册中心：`COMMANDS()`、`getCommands()` 多来源合并、`INTERNAL_ONLY_COMMANDS`、`REMOTE_SAFE_COMMANDS`、`BRIDGE_SAFE_COMMANDS`、`isBridgeSafeCommand()`
- `src/types/command.ts` — Command 三态类型：`CommandBase` 与 `PromptCommand`、`LocalCommand`、`LocalJSXCommand`
- `src/utils/slashCommandParsing.ts`（60 行）— 极简斜杠命令解析（按空格切分）
- `src/utils/processUserInput/processSlashCommand.tsx` — 三种 type 的分派执行、`executeForkedSlashCommand`
- `src/hooks/useMergedCommands.ts` — 插件与 MCP 命令的运行时二阶段合并
- `src/utils/embeddedTools.ts` — bfs/ugrep 内嵌时移除 Glob/Grep 的判定
- `src/utils/fileStateCache.ts` — Read 状态 LRU（Edit 防过期编辑）
- `src/types/tools.ts` — ToolProgressData 及各 Progress 类型

## 章节小测

<script setup>
const q = [
  {
    question: '`buildTool` 的默认值设计里，`isReadOnly` 默认 false 的安全含义是什么？',
    options: [
      '权限系统默认按写操作检查工具，不会误放只读',
      '工具默认无法读取文件，需要显式声明读权限',
      'UI 渲染默认把工具归类为写操作显示红色标记',
      '调度器默认跳过只读工具以减少并发冲突'
    ],
    correct: 0,
    explanation: '默认 false 意味着新工具默认被当成会写文件的操作对待，权限检查按写操作的严格度走。反例是默认 true：开发者忘了重写，工具被权限系统当只读放行，漏洞藏在最常见的「忘记写」失误里。fail-closed 把失误方向对准安全一侧——忘了写最多损失性能，不会损失安全性。'
  },
  {
    question: '`assembleToolPool` 里内置工具和 MCP 工具分别排序再 concat，而不是全局扁平排序，原因是？',
    options: [
      'uniqBy 只能对分段有序的数组做去重',
      '保持内置工具为连续前缀，MCP 变动只失效后段 cache',
      'localeCompare 对带双下划线的 MCP 名字有已知 bug',
      '让 deny 规则的过滤只作用于 MCP 分段以加速装配'
    ],
    correct: 1,
    explanation: 'API 的 prompt caching 按前缀匹配，工具 schema 顺序一变 cache 全灭。内置工具排成连续前缀、MCP 工具作为可变后缀，接入或断开 MCP server 时只有 MCP 段的 cache 失效。扁平排序会让 MCP 工具插进内置工具中间，任何 MCP 变动都移动后面所有工具的位置，下游 cache key 全部报废。'
  },
  {
    question: '`isBridgeSafeCommand` 对三种命令类型的判定，正确的是哪一项？',
    options: [
      'local-jsx 拒绝、prompt 默认放行、local 需在白名单',
      'local-jsx 默认放行、prompt 需白名单、local 拒绝',
      '三种类型都要逐个进白名单才能远程触发',
      'prompt 与 local-jsx 放行、只有 local 需要白名单'
    ],
    correct: 0,
    explanation: 'local-jsx 要渲染 Ink 组件，手机/网页端没有终端 UI，永远拒绝；prompt 展开成文本发给模型，默认安全；local 有本地副作用，必须进 BRIDGE_SAFE_COMMANDS 显式白名单（目前六个）。这套判定来自 PR #19134 的先全部禁止再逐个放开。'
  },
  {
    question: 'FileReadTool 把 `maxResultSizeChars` 设为 `Infinity`，落盘判定里这个值是怎么处理的？',
    options: [
      '参与阈值覆盖后由 GrowthBook 决定是否落盘',
      '作为硬 opt-out 直接跳过，任何覆盖都不生效',
      '等于 50k 全局默认值，因为 Math.min 会取小',
      '触发 64MB 截断逻辑，只落盘文件前半段'
    ],
    correct: 1,
    explanation: 'getPersistenceThreshold 开头先查 Number.isFinite——Infinity 直接原样返回，GrowthBook 的阈值覆盖拿它没办法。动机：落盘 Read 结果会形成「Read → 落盘文件 → 模型再 Read」的环，而 Read 自己有 maxTokens 截断策略。这个 opt-out 是硬的，注释明确写了 override flag 不能强制它重新开启。'
  },
  {
    question: 'Commander.js 在 Claude Code 命令体系里的实际角色是？',
    options: [
      '只解析 CLI 启动参数，REPL 斜杠命令走独立解析器',
      '统一解析 CLI 参数与 REPL 内的斜杠命令',
      '只负责斜杠命令的子命令与 flag 解析',
      '已被移除，全部命令解析都是手写的'
    ],
    correct: 0,
    explanation: 'Commander.js 在 main.tsx 里只处理 claude --print、--resume 这类启动 flags。REPL 内斜杠命令走 slashCommandParsing.ts 的 60 行极简解析：按空格切，第一词是命令名，剩下的是参数；参数语义由各命令自治解释，不为每个命令维护 flag schema。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
