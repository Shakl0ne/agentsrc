---
title: 工具系统与执行管线：模型怎么把一个工具请求变成真实行动
---

# 工具系统与执行管线：模型怎么把一个工具请求变成真实行动

> 本文基于 `dsh-v0.1.0-rc.7`（master @ 99f6f02，2026-08-17）。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前三篇里，agent 学会了"跑一回合"（agent-loop）、"看上下文"（session log）。但一个 agent 之所以能"干活"，关键在它能把一个模型请求变成**真实动作**：写文件、跑命令、查网页。这一层就是**工具系统**。

`dsh` 的工具系统不只是一张"名字 → 函数"的注册表。它完整地做成了四件事，对应这四节的抽象：

1. **注册**：工具怎么挂进注册表、在哪个作用域可见；
2. **进 prompt**：工具的 schema 怎么组合进系统提示，让模型"知道有这个工具"；
3. **被守卫执行**：一次工具调用怎么走完"允许/拒绝 → 超时/解耦 → 结果修正 → 落盘"的管线；
4. **作为能力缝的消费方**：工具是"能力缝三元组"里的 Consumer（下篇专讲）。

先把这一篇的核心判断放在最前：

> **`tools/*` 的生命周期事件（3 个可拦截 waterfall + 1 个只读结果通知），是 `dsh` 把"模型要动真格"这件事做成"可插值、可拦截、可观测"的关键。** 工具的注册是注册，能不能真的执行要交给一整条**分层管线**去裁决，而不是一个 if/else。


## 一、工具注册表：`ctx.tools` 与作用域

工具都注册到 `ctx.tools`（一个 `ToolRuntime` 服务）。`packages/core/tools/README.md` 说得很清楚：

> Tool plugins register their schemas and executors; the agent loop executes each call through … the pipeline.

注册一个工具就是调用 `ctx.tools.register(definition)`。一个 `ToolDefinition` 长什么样？它由"模型可见的 schema" + "执行函数" + "输出声明"组成。`defineTool()` 是给自家插件作者用的类型安全帮手，`docs/subsystems/tools.md` 里有一个最小例子：

```ts
// packages/core/tools/README.md — defineTool 最小示例
ctx.tools.register(defineTool({
  name: 'read_file',
  description: 'Read a file from disk.',
  parameters: {
    path: { type: 'string', required: true, description: 'Absolute file path' },
    offset: { type: 'number' },
    limit: { type: 'number' },
  },
  output: {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: value }],
  },
  async execute(args, exec) {
    return readFile(args.path, { encoding: 'utf8', signal: exec.signal })
  },
}))
```

拆开三个要点：

1. **`output` 是强制声明的**（mandatory canonical output declaration）。工具不仅声明"我会收什么参数"，还必须声明"我会返回什么 canonical JSON value"，以及怎么把它 `render` 成给模型的 content。这是第 4 篇"model-visible ⟺ logged"的具体延伸之一——工具输出也有一个可校验、可渲染的声明。
2. **`execute` 只返回"canonical JSON value"**，不含 UI。`output.render` 才是"把 value 变成模型/UI 能看的 content"的那个纯函数。执行与展示彻底分离。
3. **作用域**。`register` 在谁上调用很重要：一个普通 plugin 的 context 注册的是**全局**工具；在一个 agent 的 `agent.ctx` 上注册只对**那个 agent** 可见、且能遮蔽同名全局工具。

`ctx.tools` 还提供 `restrict(filter)` 做 per-agent 的 allow/deny 遮蔽。注意 README 反复强调："This is live visibility composition, not an authority boundary"——**作用域上的工具**可见性限制是"路由/组成"，不是安全边界（安全边界在 capability 缝 + 权限 policy，见第 6 篇）。

一句话记住注册层：

> **工具注册 = 声明"我能干什么、我要什么参数、我能返回什么 canonical 值"，并挂到全局或某个 agent 的作用域上；展示函数与执行函数彻底分离。**


## 二、schema 进 prompt：模型怎么"知道"有这些工具

工具注册了，下一步是让模型看到它们。`ctx.tools` 与 `system-prompt` 之间有一条自动约定：

> The registry automatically feeds its tool schemas into the system-prompt assembly via `ctx.systemPrompt.tools()`.

具体地，`ctx.tools.schemas(scope)` 投影出"该 scope 可见的所有工具"的**模型可见字段子集**——只含 `name/description/parameters`，**绝不含 `execute/finalizeContent/render` 等回调**。这些 schema 汇入 system prompt 组装，最终随 `agent/request` 的 waterfall 进入每个 step 的请求。

这一步回答的是一个微妙问题：**工具定义里既要有"给机器执行的代码"，又要有"给模型看的 schema"，二者绝不能混**。`dsh` 的处理是"白名单投影"：`schemas()` 只把 `name/description/parameters` 投影到请求线，其余字段（执行函数、超时、展示回调、并发安全标记）**一律不让进 wire**。这样：

- 模型拿到的永远是干净的、最小化的函数签名；
- 实现细节、回调、执行代码绝不泄漏到模型请求里。

这里还能体会到 `isConcurrencySafe`、`timeoutMs` 这类**元数据**为什么必须"永不 model-visible"：它们属于执行/调度策略，不是模型调用工具所需的"了解接口"信息。


## 三、执行管线：一条带守卫的水瀑

这是工具系统的心脏。`ctx.tools.execute()` 把一次调用**走完整条管线**，`packages/core/tools/README.md` 给出了权威顺序：

> …run each call through `tools/pre-execute` (the extensible allow/deny gate) → monotonic registered guards → `tools/execute` (an around-dispatch wrapper for timeout/retry/metrics plugins) → `tools/post-execute` (inspect/replace the result, attach context) → the definition-owned `finalizeContent` boundary → the observe-only `tools/result` notification.

翻译成一段话：

`tools/pre-execute`（**允许/拒绝/询问**的可扩展闸门）→ **单调守卫**（谁也不能再放行）→ `tools/execute`（around-dispatch 包装：超时/重试/指标）→ `tools/post-execute`（检查/替换结果、附上下文）→ 工具自带 `finalizeContent` → 只读的 `tools/result`（落盘前最后通知）。

### 三种决策：allow / deny / ask

`tools/pre-execute` 是一个 waterfall，每个 listener 返回一个 `PreToolDecision`：

```ts
type PreToolDecision =
  | { kind: 'allow' }
  | { kind: 'deny'; reason: string }
  | { kind: 'ask'; reason?: string }
```

- `allow`：直接跑；
- `deny`：把这次调用变成一个错误结果；
- `ask`：**只有在某个审批服务返回 `allowed-once` 之后才跑**，否则也是 deny。没有审批服务时 ask 降级为 deny（`不允许默默放行`）。

注意 `ask` 的设计——`dsh` 把**审批（approval）**做成工具管线的一个环节：前端（交互层）可以挂 `ctx.approval`，`tools/pre-execute` 的 ask 就调用它；没挂就降级为拒绝，宁可保守不漏，也不静默放行。

### 单调守卫：不让已拒绝的再被批准

`ctx.tools.guard(guard)` 提供的是**单调**的执行守卫——即"只能更加严格，不能更松"。它的返回类型刻意**没有 allow 分支**（`(execution) => string | undefined`，返回字符串即拒绝）：因为只要有 deny 结果，"后续 listener 就无法把它改回 allow"；`undefined` 只说明"保持原样"。这是防"守卫之间互相推翻"的关键——`docs/subsystems/tools.md` 原话：

> Because guards have no allow result, listener ordering cannot turn a denial back into permission.

一个工具调用的权限裁决，是这层"让-拒绝-问 + 单调守卫"共同完成的：可扩展的 allow/deny/ask 提供**灵活性**，单调守卫保证"一旦拒绝就收不回来"。

### around-dispatch：超时/重试/指标挂在这里

`tools/execute` 是 around-dispatch 的 waterfall：wrapper 包住真正的 body，可做超时、重试、指标。它重要的约束是——wrapper 只允许替换 `exec.signal`，**不能去掉调用者取消信号**（registry 会在 body 前把原始的 caller signal 重新融合回去），这样 wrapper 的数据不会让调用者失去取消能力。

超时是怎么挂进去的？`docs/subsystems/tools.md` 里 `ToolDefinition.timeoutMs` 明确说是**声明性**的、**不是**执行期强制：

> `timeoutMs` on a definition is declarative only — the registry never enforces deadlines; enforcement requires the `@deepseek-ai/dsh-tool-call-timeout-policy` wrapper.

也就是说：工具可以被声明"我有一个 30s 的超时预算"，但真正执行超时的是**一个独立插件**（`dsh-tool-call-timeout-policy`），它作为一个 `tools/execute` 的 wrapper 注册。这是"插件哲学"在工具栈的又一次贯彻——超时不是写在核心注册表里，而是作为一个可独立挂载/卸载的 guard/wrapper 插上去。

### post-execute + result：结果修正与观察

跑完之后，`tools/post-execute` listener 可以**修正结果**：接受、替换 content 或 value、或 block 成带反馈的错误；随后 `finalizeContent`（工具自带）做**最后一公里的 content-only** 修正；最后由 `tools/result` **只读通知**观测最终的冻结结果。

这里有一个必须在意的设计取舍——**"参数不可改"**与"结果可改"：

- `tools/pre-execute` **故意不允许改写 `exec.arguments`**，因为参数已经被 logged 和 presented，改写会造成历史/审计/UI/执行彼此脱钩；
- 但 `tools/post-execute` 允许改 content（展示）而不改 canonical value，或反过来改 value 重算 content——这是策略层面的"结果修正"，与"进线前的约定"不同。

**结论**：执行的"进"（参数）和"出"（结果）在抽象上有非常不对等的自由度——**进闸不可改（保真），出闸可改（策略）**。这确保了历史一致（参数与所执行一致）、策略灵活（结果可被包装/遮蔽）。


## 四、守卫、超时与重复提醒：插件在守什么

即便有 allow/ask/post 三层水面，`dsh` 还留了两个"卫生守卫"——它们是独立插件，不是硬编码在工具系统里：

- **超时**：`dsh-tool-call-timeout-policy`（tools/execute 的 wrapper），遵守通知 `timeoutMs` 给某个工具设的 deadline；超了就把结果变 `TOOL_TIMEOUT`，并把错误交给模型自纠。
- **重复提醒**：guard 家族里的 loop-hygiene 插件 `@deepseek-ai/dsh-repeat-tool-reminder`——在一个工具被反复以相同参数调用时，注入"重复了吗/卡死了吗"的提示。注意它**不是** `ctx.tools.guard()` 那种单调守卫，而是挂在 `tools/post-execute` + `agent/pre-step` 上做 **advisory（建议式）提醒**：它只提示、不 veto，因为"重复调用"既可能是合法轮询、也可能真死循环，这个判断留给模型/用户去定。

它们为什么是**插件**而非硬编码？因为：

1. `timeoutMs` 是**声明式**的，真正的 deadline 由 `@deepseek-ai/dsh-tool-call-timeout-policy` 作为一个可独立安装/卸载的 `tools/execute` wrapper 去强制执行——它不在注册表内，而是插在管线上；
2. "是否/多久算超时""重复多少次该提醒"是**部署时可变**的策略——项目能按产品/配置项，而不是改核心代码。

这和第 2 篇的"注册即副作用、可逆卸载"一脉相承：**凡是"行为性"的东西都尽量做成插件，核心目录只保留机械的执行管线骨架。**


## 五、工具 = capability 缝的 Consumer

这是把工具接到第 6 篇的桥。前几节一直在讲"工具怎么注册、怎么执行"，但必须补一句定位：**在 `dsh` 里，一个工具通常是某个 capability 缝的「Consumer（消费方）」**，而不是"夹在代码里的一个东西"。

看第 2 篇就埋过伏笔的 `docs/capability-seams.md`：一个 seam 是"Service Definition（接口）+ Provider（实现）+ Consumer（消费方）"的三元组，而 Consumer **通常是一个 model-facing 工具**——它一端接进"agent / 模型怎么看到这个能力"，另一端接进"实际执行这个能力的 provider"。

举个例子，`dsh-tool-fs`（见包 README）就是 fs 这个 capability 的 Consumer：**它向模型暴露"读写文件"的工具 schema，实际干活时通过 `ctx.fs` 服务（缝）去调用本地 provider。** 所以：

> 工具"层"与"能力缝"层不是一个概念。**工具是能力的 Consumer，能力缝是"接口 + 实现 + 消费"三件套。** 理解工具，最终要理解它吃哪一种缝、替换哪种 provider 时它跟着一起走。

这句话把 5、6 两篇锁在一起：工具（第 5 篇）是消费方，Capability 缝（资源第 6 篇）是"三件套"。

当然，也有**纯工具**不属于某个 seam（比如 `todo_write` 这种纯粹的工具，它不需要一个可替换的 provider）——`todo` 在 `packages/todo/` 单独成 package 而没有缝。所以"工具不一定都要吃 seam"，但"能力缝的 Consumer 通常是工具"。这层区分在第 6 篇会讲得更细。


## 六、与三栏对比：工具的"守卫"放哪

| 维度 | OpenCode | Codex | DeepSeek Harness（`dsh`） |
|------|----------|-------|--------------------------|
| 工具注册 | Tool.Def 接口 | tool executor | **ctx.tools.register（typed schema DSL）** |
| 工具进 prompt | 手动拼入 context | executor 反射 | **自动经 `ctx.systemPrompt.tools()`** |
| 执行管线 | 一层 executor | 一层 event | **pre-execute → guard → execute → post-execute → result** |
| 是否可碰 | 专业里改 | 改动大 | 单功能都可写插件挂 |

参照点：dsh 最大的不同是把工具从"一个函数"抬成了"一条**可插值、可拦截、可观察**的管线"。工具并不"天生安全"，需要靠"受调的授权语义 + 单调守卫 + 可观测事件"去约束它既敢放它去执行，又能在它上面挂审批、超时、重复提醒等一堆策略插件。**它把"给模型一个工具"从"一次性决定"变成了"每一步都可以裁决"。**


## 七、总结：工具系统让你做 plugin 的三个平面

工具系统把一件事展开成三个平面，你不必混：

1. **声明平面**：`defineTool` 注册 schema + 强制 `output`；模型只看到 `name/description/parameters`。
2. **执行平面**：`pre-execute`（allow/deny/ask）+ guard（单调）→ `execute`（可改 signal）→ `post-execute`（可改结果）→ `finalizeContent` → 只读 `tools/result`。**成败靠这条管线裁决：进闸的参数不可改（保真），出闸的结果可被策略修正（灵活）。**
3. **projector/观察平面**：`schemas()` 按 scope 投影模型可见字段；`presentCall/presentResult` 让工具自己决定 UI **怎么展示**；一切结果都沿线沉淀为 `tool/call`/`tool/result` 会话事件（回到第 4 篇的"什么都被记录"）。

这三个平面的分割，是 `dsh` 把"模型在用工具"从一个"无从下手"的黑盒，变成一个**每一环可观测、可拦截、可审计的流程**。它把第 2 篇"一切皆插件"从"结构层"落实到了"单个动作层"：连一次工具调用都能被你在它上面挂审批、挂超时、挂 ask。

### 下一篇

工具系统解释了"怎么执行"。那"这些工具到底在调用谁""为什么换 provider 天下动？"——这才是 `dsh` 真正厉害的一层：**capability 缝（capability seam），三元组与 provider 互换。**走到第 6 篇。


## 章节小测

<script setup>
const q = [
  {
    question: '工具定义必须声明且单独一个 `output`（canonical 输出），最重要的作用是？',
    options: ['约定"返回什么 JSON"，并独立 render 去呈现，让 JSON 与展示分离', '让 TypeScript 类型推断更精确', '使工具储存空间更小', '为了在模型面前隐藏参数'],
    correct: 0,
    explanation: 'output + render 让"规范值"与"展示"分离、可回放，也强化了 session "能记录但不可改"精神。输出规定属于职责分离，而非只在类型/空间向。'
  },
  {
    question: '为什么 `tools/schemas(scope)` 只把 name/description/parameters 投影给模型（而非整个定义）？',
    options: ['避免把 execute/回调/元数据泄漏到 wire，也让模型只见最小签名', '为了减少一次文件读取', '因为模型只接受某种固定数据类型', '为了不让工具注册到任何 scope'],
    correct: 0,
    explanation: '白名单只发 name/description/parameters；execute 与回调属于执行层，不能入 wire。这是"不给模型看实现"的决定。'
  },
  {
    question: '哪个描述 100% 正确地说明单调守卫（guard）？',
    options: ['只能返回拒绝理由，不能给出允许，防止被后续放行', '能被之后的绕过', '允许无论 guard 顺序都反转', '可以随意替换参数'],
    correct: 0,
    explanation: '单调 guard 返回值是 string|undefined 且无 allow 分支，即"只拒绝、不授权"，因此后来 listener 不能把已拒绝的放回。'
  },
  {
    question: '`tools/pre-execute` 的 `ask` 决策代表什么？',
    options: ['调用需审批，只有被返回 allowed-once 才执行，否则 deny', '无需任何许可直接允许', '强制异步重试一次', '把调用静默丢弃'],
    correct: 0,
    explanation: 'ask 只有在宿主审批（ctx.approval）给了 allowed-once 才跑；没有审批则降级为 deny。体现"宁可少放行不放错"。'
  },
  {
    question: '为什么 `dsh-tool-call-timeout-policy` 是一个插件而不是写死在工具注册表？',
    options: ['超时是部署侧策略，可由插件按需装卸，不动核心目录', '因为 timeout 不好实现', '因为工具注册表不能写代码', '因为模型不能理解 timeout'],
    correct: 0,
    explanation: '`timeoutMs` 是声明式，真正的 deadline 由 `tools/execute` wrapper 插件强制；把行为性策略插件化正是"可单卸单装"的哲学，核心目录只保留机械骨架。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
