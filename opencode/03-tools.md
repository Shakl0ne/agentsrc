---
title: OpenCode 工具系统：定义、执行与权限三道设计关口
---

# OpenCode 工具系统：定义、执行与权限三道设计关口

写自己的 AI Agent，最容易先烂掉的地方往往不是模型，而是工具系统。让 agent 调 shell 怕它执行危险命令，让 agent 写文件怕它越界，每次调外部 API 都弹窗又会烦到用户，而 LLM 自己填参数还总不按 schema 来。要是把这些防线摊到每个工具里各写一遍，系统会很快长成一片重复胶水。

那工具到底该怎么设计，才能既让模型敢调、又让引擎敢放行？OpenCode 把答案收敛成一条工具链，核心点只有一句：**同一个工具定义，要同时给 LLM 和运行时两边看懂**。前者靠 `description` 和 jsonSchema 判断“该不该调、怎么调”，后者靠运行时解码、权限检查和输出治理决定“能不能执行、执行到哪一步停”。这一篇就沿着这条双重接口往下拆。

先把四道关口钉住：

- **第一**，一个工具怎么定义，才能把 LLM 发来的 JSON 参数稳稳落成强类型对象；
- **第二**，参数校验和输出截断，为什么能做成所有工具共用的底座；
- **第三**，像 Edit 这种目标字符串常常不精确的工具，靠什么兜底；
- **第四**，越权和死循环两类风险，怎么被同一套权限机制拦住。

这一篇不按 18 个工具逐个点名，而是只抓定义、执行、权限三道设计关口；Edit 引擎和 Permission 系统会作为两块最重的证据单独展开。


## 一、工具是 LLM 的"手"，也是引擎的"门"

先给工具一个定位。对 LLM 而言，工具是它的"手"——它只能靠函数调用来动文件、跑命令、查网络，所以工具的描述与参数 schema 决定了模型怎么选、怎么填。对引擎而言，工具是它的"门"——每次调用都要过参数校验、越权检查、输出截断、死循环检测这一连串闸，否则一个不可控的工具会把整个 agent 拖垮。

这两套需求看似要拆成两套系统，OpenCode 用同一个 `Tool.Def` 接口扛了下来。往下走，正文会依次回答几个设计问题：

- 一个工具怎么定义，才能让 LLM 发来的 JSON 参数安全地变成强类型对象？
- 参数校验和输出截断，为什么能自动加到每个工具上，而不必每个工具自己写？
- 编辑文件这类"目标字符串往往不精确"的操作，靠什么策略兜底？
- 越权和死循环两类风险，怎么复用同一套权限机制来拦截？

## 二、定义层：一个工具怎么被同时"给 LLM"和"给引擎"理解

### 2.1 Tool.Def：五个字段锁定的"身份证"

OpenCode 的所有工具都对齐同一个 `Tool.Def` 接口（`src/tool/tool.ts:53`）：

```ts
export interface Def<
  Parameters extends Schema.Decoder<unknown>,
  M extends Metadata,
> {
  id: string
  description: string
  parameters: Parameters                  // Effect Schema 描述参数
  jsonSchema?: JSONSchema7
  execute(args: Schema.Schema.Type<Parameters>, ctx: Context): Effect.Effect<ExecuteResult<M>>
  formatValidationError?(error: unknown): string
}
```

六个字段里，`id` 是寻址标识，`description` 是给 LLM 看的说明，剩下的都是同一个抽象的几个侧面。`parameters` 是 **Effect Schema** 而非普通 TypeScript 类型，自带运行时解码能力；`execute` 返回 `Effect` 而非 `Promise`，这让执行过程能 `yield*` 去访问 `permission`、`truncate`、`agent` 这些服务；`formatValidationError` 则允许工具给参数错误定制一句更友好的提示。一个接口，同时决定 LLM 怎么理解工具、引擎怎么执行它。

### 2.2 Effect Schema 的双重身份

`parameters` 这一个对象要同时扮演两个角色。给 LLM 用时，它被序列化成 JSON Schema 拼成工具签名，模型据此构造参数。给引擎用时，同一个 Schema 被 `Schema.decodeUnknownEffect` 编译成运行时解码器，把 LLM 发来的字段解成带类型的对象再交给 `execute`。

一套声明、两个用途，是这套定义层的核心收益。若给 LLM 用一套手写 JSON Schema，给引擎再维护一套 TS 校验，两套迟早漂移；如今它们来自同一个 `Schema`，不可能不一致。`jsonSchema` 因此保留成可选字段——多数场景引擎现编即可。

### 2.3 Context 与 ExecuteResult：运行时两条通道

工具执行时的环境由 `Context` 提供（`src/tool/tool.ts:34`）。`sessionID`/`messageID`/`callID` 定位调用，`abort` 传递中断信号，`messages` 让工具能读对话历史。里面最要紧的是 `ask`——它是 Permission 系统的入口，任何敏感操作执行前都要从这里过（第六章细讲）。

执行完返回 `ExecuteResult`：`output` 是纯文本，喂给 LLM；`title` 和 `metadata` 喂给 UI 展示。两个消费端看同一份结果，一个读语义驱动下一步，一个读元数据画界面。

## 三、包装层：两道保险怎么"自动"加上

### 3.1 define() 的惰性初始化

工具定义完只是个对象，暴露给 LLM 前要过 `Tool.define()`（`src/tool/tool.ts:97`）。它接收一个 `init` 的 `Effect`，把工具做成惰性初始化——工具真被用到时才去拉 `Truncate.Service` 和 `Agent.Service` 两个依赖。刻意推迟初始化，是希望启动阶段不碰那些可能还没就绪的服务。

### 3.2 wrap() 加的两道保险

承载这两道保险的是 `wrap()`，它把 `execute` 整个包一层，工具作者完全不必自己写。

**第一道：参数校验。** 每次调用先进 `Schema.decodeUnknownEffect` 解码参数，失败就包成一个 `InvalidArgumentsError`。这个错误的 `message` 是对 LLM 说的：

```txt
The ${this.tool} tool was called with invalid arguments: ${this.detail}.
Please rewrite the input so it satisfies the expected schema.
```

末尾那句"请改写后重试"是有意为之——工具系统对 LLM 的反馈闭环在此闭合。模型拿到报错就知道"参数写错了，改一改再调"，而不是原地重试。

**第二道：输出截断。** 每个工具的 `output` 都会过 `truncate.output()`。超限就截断，完整内容落盘，返回给 LLM 的是"截断提示 + 怎么取全文"的指引。模型不会面对一片空白，它有明确的下一步动作。

两道保险都落在包装层统一做好，意味着想给全部工具加一条校验或换一种截断策略，只需改 `wrap()` 一处就能同时生效——这是把横切能力收进一层的关键收益。截断的判定与落盘细节，见下面这张双限制的示意。

![包装层的两道保险：校验与截断](/images/opencode/03-wrapper.svg)


## 四、执行层（重点）：Edit 引擎的渐进式匹配

工具里最重的是 `edit`（711 行）。它解决的是编辑文件里最难的一类问题——LLM 给出的目标字符串几乎不可能精确命中磁盘上的原文。

### 4.1 为什么"找到 oldString"这么难

`oldString` 就是替换目标的锚。可 LLM 生成它时会踩中一连串坑：空白对不上（模型给空格、文件是 tab），行尾不一致（`\n` 对 `\r\n`），转义序列被当成字面量，缩进层级写错，以及最常见的——`oldString` 太短命中多个位置。若只用 `content.indexOf(oldString)`，模型会把整类不一致留给"编辑 → 报错 → 重试"的循环去消化，既难收敛又烦人。

### 4.2 渐进式匹配：九种策略按精度降序

OpenCode 把难度拆成策略栈，每种策略是一个生成器（`src/tool/edit.ts:213`）：

```ts
export type Replacer = (content: string, find: string) => Generator<string, void, unknown>
```

每个 Replacer 对 `find` 做不同精度的归一化后，`yield` 出候选串。九个 Replacer 按精度从高到低排列，逐个尝试：

| 策略 | 思路 |
|------|------|
| SimpleReplacer | 精确匹配 |
| LineTrimmedReplacer | 逐行去空白后比较 |
| BlockAnchorReplacer | 首尾行作锚点，中间行 Levenshtein |
| WhitespaceNormalizedReplacer | 连续空白压成一个 |
| IndentationFlexibleReplacer | 去掉最小公共缩进 |
| EscapeNormalizedReplacer | 展开 `\n`/`\t` 转义 |
| TrimmedBoundaryReplacer | 整体 trim 后比较 |
| ContextAwareReplacer | Block 的简化版，首尾作锚点 |
| MultiOccurrenceReplacer | 找出所有精确位置 |

从精确到宽松的排序，立场是"能精确就不引入相似度的误伤，宽松策略只在精确失效时介入"。

### 4.3 replace() 决策循环：单匹配替换，多匹配跳下一个

拿主意的是 `replace()`（`src/tool/edit.ts:674`）：

```ts
for (const replacer of replacers) {
  for (const candidate of replacer(content, oldString)) {
    const index = content.indexOf(candidate)
    if (index === -1) continue
    if (replaceAll) return content.replaceAll(candidate, newString)
    const lastIndex = content.lastIndexOf(candidate)
    if (index !== lastIndex) continue   // 单个策略多匹配，交给下一个
    return content.substring(0, index) + newString + content.substring(index + candidate.length)
  }
}
```

外层按精度遍历，内层对每个候选定位。若 `index !== lastIndex`，说明该候选匹配多处，此刻继续替换会有歧义，于是 `continue` 交给下一个更宽松的 Replacer 去尝试。这保住了一个确定性前提：只要某个策略给出唯一命中，立刻返回；多匹配意味着 `oldString` 太短，是直觉上该让下一个策略接力去碰的信号。全部失败时，抛一个对 LLM 友好的提示："找到多个匹配，请提供更多上下文"。


### 4.4 BlockAnchorReplacer：首尾锚点 + Levenshtein

九个策略里最复杂的是 `BlockAnchorReplacer`（134 行）。它要求 `oldString` 至少 3 行，以首行和末行当边界，中间行用 Levenshtein 距离算相似度（`src/tool/edit.ts`）：

```ts
const SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.0
const MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.3
```

单一候选时相似度 0 也接受，多个候选时才要求 30% 以上。这么宽松的单一候选基准，来自一个工程判断：首尾锚点已经唯一锁住替换范围，中间行只要不严重破坏结构就值得替换。而多候选场景就要求相似度比拼，避免在低精度里错改一个。这个策略正是"edit 这类模糊目标"里最典型的兜底路径。

### 4.5 换完之后的 LSP 反馈闭环

替换成功后还多做一步（`src/tool/edit.ts`）：`yield* lsp.touchFile(filePath)` 通知 LSP 文件变更，再拉一次诊断，有错就用 `LSP.Diagnostic.report` 追加到工具输出末尾：

```txt
LSP errors detected in this file, please fix: ...
```

于是 LLM 拿到的不止是"edit 成功"，还有"这个文件现在有 3 个语法错误，去修"。编辑动作和语法反馈被绑进一个输出里，把"改完再自检"这一轮循环提前到了工具内部。

### 4.6 文件锁防并发

同一文件可能被 LLM 连续两次 edit。`src/tool/edit.ts:35` 用一个 `Map<path, Semaphore>` 给每个文件一把锁，`Semaphore.makeUnsafe(1)` 保证对同一文件的替换串行。若不加锁，第二次 edit 会基于第一次完成前的旧内容替换，结果不可靠。

## 五、注册/组装层：工具怎么被组织、过滤、路由、扩展

### 5.1 Registry.tools()：一次请求，一套注册表

工具定义好之后要打包给 LLM，这一步不只是简单合并。`ToolRegistry.tools()`（`src/tool/registry.ts`）会做三件事：

- **过滤**：`WebSearchTool` 按 provider/flags 判断是否启用；`usePatch` 按模型路由——GPT 非-oss 非-4 系用 `apply_patch`，其它模型用 `edit`/`write`。
- **plugin hook**：`tool.definition` 钩子让插件能改任意工具的 `description`、`parameters`、`jsonSchema`。
- **动态 description**：`task`/`skill` 工具的 description 末尾按当前 agent 注入可用的 agent/skill 列表（task 用 `describeTask`、skill 用 `describeSkill`）。

这三步合起来，就是"每次请求都对这个 agent 重新算一遍工具注册表"。路由这一点值得单独拎出来：同一类"改文件"操作，为何给不同模型配不同工具？因为 GPT 系对 unified patch 格式理解更好，Claude 系对 `oldString + newString` 的精确替换更在行。正常让工具适配模型，而非反过来要求模型迁就工具——这是多模型框架必须面对的差距。

### 5.2 自定义工具的两种加载

扩展方式同样收敛到一处。`registry.ts` 先扫 `tool/` 或 `tools/` 目录下的 `.js/.ts` 文件，动态 `import`；再遍历插件，把插件 `tool` 字段里的定义拉进来，共用同一条 `fromPlugin` 通道并统一过 `Tool.Def` 校验。命名上，默认导出用文件名，命名导出用 `文件名_导出名`。这样一个单文件丢进 `tool/` 目录就能注册一个工具，配置零改动。

## 六、安全层（重点）：ask/allow/deny 三态与两道防线

安全层的设计初衷：越权和死循环这两类"不可控"，要能被拦在产生副作用之前，且避免持续打扰用户。它的落点是一个可复用的规则引擎，统一收敛两类风险的判断。

### 6.1 三态，而不是二态

权限状态由 `Action = Schema.Literals(["allow", "deny", "ask"])` 定义（`packages/core/src/permission.ts:6`）。二态只能放行或拒绝，但敏感操作的多数恰好处于中间地带——直接放行有风险，直接拒绝会误伤。`ask` 把决定权交回用户：

- `allow`：静默通过；
- `deny`：拒绝并返回错误；
- `ask`：弹窗询问用户。

多维护一个中间态，就需要额外的申诉机制。落上去的补丁是 evaluate 与 ask 之间那套规则。

![Permission 的三态异步模型](/images/opencode/03-permission-states.svg)

### 6.2 evaluate()：最后匹配胜出

规则怎么被匹配呢？`evaluate()`（`packages/core/src/permission.ts:21`）遍历 ruleset，命中某条规则就让其成为兜底：

```ts
rulesets
  .flat()
  .findLast((rule) =>
    Wildcard.match(permission, rule.permission) && Wildcard.match(pattern, rule.pattern),
  )
  ?? { action: "ask", permission, pattern: "*" }
```

两处设计值得记住：`findLast` 让后写的规则压过先写的，用户配置天然覆盖框架默认；`Wildcard.match` 让它 `permission` 和 `pattern` 都支持 `*` 通配。两者共同保证"最靠近用户最近声明的那条规则会赢"这个可预期判断。

### 6.3 ask() 的 Deferred 异步等待

`ask()` 把"问用户"实现成异步等待（`src/permission/index.ts`）。它遍历 `patterns`，任何一条命中 `deny` 就抛 `DeniedError`；全 `allow` 就静默通过；有 `ask` 则扛一个 `Deferred` 挂到 pending，同时 publish 事件，然后 `Deferred.await` 阻塞在用户回复上：

```ts
const deferred = yield* Deferred.make<void, RejectedError | CorrectedError>()
pending.set(id, { info, deferred })
yield* bus.publish(Event.Asked, info)
yield* Deferred.await(deferred)
```

`Deferred` 是 Effect 的等待原语，不占用线程却能把"等回复"和"继续跑"解耦。用户一回复，`succeed`/`fail` 解开等待，`ask()` 随之返回或抛错。代价是 pending 需要有人维护，于是有了下面的"always 自动解锁"。

### 6.4 once / always / reject：回复的三种含义

用户对一次询问可以给三种回复：

- `once`：只放行当前请求；
- `always`：写入 `approved` 列表，永久 `allow`；
- `reject`：拒绝当前，并级联拒绝同 session 的其它 pending。

`always` 之后藏着一处复用：用户批准一次 `always`，系统回头扫一遍 pending，凡是被 `approved` 能放行的 `Deferred` 全部 `succeed` 自动解锁。这样"同一次会话要问 10 次同样的权限"的烦人场景被压缩成一次确认，打扰降到最低。

### 6.5 external_directory：工作区边界

最常用的约束是防止工具改工作目录之外的文件。`assertExternalDirectoryEffect`（`src/tool/external-directory.ts`）先判断目标是否落进 `containsPath(workspace, target)`。在内直接放行；不在就以目录通配 `path.join(dir, "*")` 作为 pattern 走 `ctx.ask`，权限用 `external_directory`。

这个"默认 ask"的策略里，agent 第一次碰工作区外文件会弹窗，用户选 `always` 后该目录不再询问。白名单目录在 `Permission.fromConfig` 里默认写进 `approved`，其余 `"*": "ask"`。边界可配置，不是硬编码死线。

### 6.6 Doom Loop 检测：复用同一套规则机

死循环检测没有独立的服务，而是复用权限系统。`src/session/processor.ts:425` 拿最近 3 个 part，若全来自同一个工具，且 `JSON.stringify(input)` 完全相同，就视为死循环，走 `permission.ask({ permission: "doom_loop", patterns: [name] })`：

```ts
if (
  recentParts.length !== DOOM_LOOP_THRESHOLD ||
  !recentParts.every((part) =>
    part.type === "tool" && part.tool === value.name &&
    part.state.status !== "pending" &&
    JSON.stringify(part.state.input) === JSON.stringify(input),
  )
) return
yield* permission.ask({ permission: "doom_loop", patterns: [value.name], ... })
```

"连续 3 次同工具同参数"作为一个判定信号，再把它塞回 `ask/allow/deny` 这一条管道。用户给 `allow` 放行这次，给 `reject` 就打断循环。它并没有新开服务，而是完整复用已经把 Deferred、always 解锁、通配匹配做好的权限机制，默认 `doom_loop: "ask"`。这是把"复用"当设计优先级的印证。

默认配置（`src/agent/agent.ts:106`）里，通配 `"*"` 是 `allow`，但几个敏感工具是显式收紧的：`doom_loop` 与 `external_directory` 默认 `ask`，`question`、`plan_enter`/`plan_exit`、`repo_clone`、`repo_overview` 默认 `deny`，`read` 对 `*.env` 类也是 `ask`。默认放开是出于体验——全 `ask` 会烦死用户——再靠死循环与目录边界兜住最容易失控的两个风险，流畅与安全两不误。


## 七、横向对比：OpenCode vs Claude Code 的工具取舍

通观整条链之后，放回生态对比一次取舍。下表是两框架在工具系统上几个横切面的差异：

| 维度 | Claude Code | OpenCode |
|------|-------------|----------|
| 工具定义 | Zod Schema | Effect Schema（一处双用） |
| 参数校验 | Zod parse | `decodeUnknownEffect`（编译缓存复用） |
| Edit 策略 | 精确 old_string → new_string | 9 种 Replacer 渐进决策树 |
| Doom Loop | 无（社区呼吁） | 连续 3 次同参数触发 ask |
| 权限模型 | 内置 allow/deny + PreToolUse hooks | 三态 + 通配符匹配 + 复用 |
| 自定义工具 | 主要靠 MCP | 文件加载 + Plugin 注册 |
| 模型路由 | 无 | GPT 系用 apply_patch，其他 edit/write |
| Plugin Hook | PreToolUse / PostToolUse | `tool.definition` / execute 前后钩子 |

四条关键差异值得占住：

1. **Doom Loop**：OpenCode 用权限机制复用实现，CC 暂无这类内置，社区不断要求。
2. **Edit 策略层级丰富度截然不同**：OpenCode 用 9 种 Replacer 消化空白/缩进/转义/多匹配等不一致；CC 更简单，靠精确替换 + 引号归一化。
3. **多模型路由**：OpenCode 原生支持跨厂商模型，按模型切换 edit/apply_patch；CC 只服务 Claude，路由无需求。
4. **权限集成度**：OpenCode 把 Doom Loop、External Directory、Deferred 全归进一个三态系统；CC 用内置规则 + hooks 各管一摊，hooks 灵活但一致性略散。

OpenCode 的取舍，是牺牲一部分 hooks 自由，换来统一、可复用的权限模型；CC 的 hooks 是外部扩展入口，灵活，但规则分散。你要在"边界统一性"与"扩展自由度"之间权衡。

## 八、设计要点回收

把"定义 → 包装 → 执行 → 注册 → 安全"这条链走完，每类设计点对应到机制，可以收拢成几条线：

- **一处定义、多处消费**：`Tool.Def` + Effect Schema，LLM 和引擎读同一份。
- **横切能力靠包装层自动加成**：校验与截断都在 `wrap()` 统一做好，工具作者不重复。
- **模糊目标靠策略递进**：9 种 Replacer + 决策树，把"匹配不精确"系统化消化掉。
- **不可控风险靠统一规则机**：ask/allow/deny + findLast + 复用，Doom Loop、External Directory 都搭在同一条管道上。
- **体验靠默认放开 + 风险兜底**：默认通配 `allow`，靠 `doom_loop`/目录边界与少数敏感工具的显式限制兜底。

OpenCode 用 656 行 tool.ts + registry.ts、711 行 edit.ts、312 行 permission 实现了很多 CC 需要更多 hooks 与独立工具才做得到的事。简化的代价是放弃 PreToolUse hooks 的一部分自由度，换来的是统一的权限模型和内建的 Doom Loop 检测。取舍清楚之后，无论是调试上下文还是自己扩工具，都可以沿这条抽象链对号入座。下篇我们进 agent 系统。

## 章节小测

<script setup>
const q = [
  {
    question: 'Edit 引擎的 9 种 Replacer 按精度从高到低尝试，当某个 Replacer 找到多个匹配时，Edit 引擎会怎么做？',
    options: ['在精确匹配找到多候选时回退到编辑距离最接近的匹配', '在精确匹配找到多候选时将全部候选逐一替换并返回合并结果', '在精确匹配找到多候选时跳过当前策略让下个策略尝试', '在精确匹配找到多候选时要求用户从候选列表中手动选择'],
    correct: 2,
    explanation: '这是 Edit 引擎最聪明的设计——精确匹配找到多个说明 oldString 太短不唯一，更宽松的匹配也找到多个就继续尝试下一个策略，直到有一个策略给出唯一匹配。如果全部多匹配，最后抛出「请提供更多上下文」的错误。'
  },
  {
    question: 'Permission 系统为什么设计成 ask/allow/deny 三态而不是二态？',
    options: ['ask 性能较差实现简单仅用于调试场景', 'ask 为灰色地带提供用户决策而非简单放行拒绝', 'deny 与 ask 行为等价可合并为单一拒绝态', 'allow 与 deny 覆盖全部 ask 增加维护成本'],
    correct: 1,
    explanation: 'ask 的存在是因为很多敏感操作处于「不一定安全也不一定危险」的灰色地带。直接 deny 太粗暴，allow 太危险，「问用户」是最稳妥的中间态。'
  },
  {
    question: 'BlockAnchorReplacer 要求 oldString 至少 3 行，用首尾行作锚点。为什么单一候选时相似度 0% 也接受？',
    options: ['首尾锚点仅作定位标识中间内容相似度仍需达到最低阈值', '首尾锚点已唯一锁定替换范围中间内容差异不影响匹配安全', '首尾锚点匹配后完全依赖 Levenshtein 距离而非相似度阈值', '首尾锚点与中间内容的相似度阈值采用独立的两阶段判据'],
    correct: 1,
    explanation: '如果首尾锚点都唯一匹配，说明 LLM 已精确指出了要替换的块边界。中间行即便有空白或微调差异，只要首尾锚点对得上，替换就是安全的。「锚点确认边界后对中间内容宽容」是个实用主义取舍。'
  },
  {
    question: '为什么 OpenCode 按模型路由工具（GPT 系用 apply_patch，其他用 edit/write），而 Claude Code 没有这种设计？',
    options: ['OpenCode 因历史包袱按模型切换而 CC 无此负担', 'CC 仅服务单一模型无需路由而 OpenCode 需适配多模型', 'GPT 架构不支持 edit 所以只能用 apply_patch', 'CC 内部已有同款路由机制未对外暴露'],
    correct: 1,
    explanation: '这是跨厂商框架必须面对的复杂性。GPT 系模型对 unified patch 格式理解更好，Claude 系对 oldString + newString 的精确替换更在行。CC 只支持 Claude，不需要这种路由。'
  },
  {
    question: 'Permission 系统的 evaluate() 用 findLast（最后匹配胜出），而不是 findFirst（最先匹配）。为什么？',
    options: ['使用 findFirst 让默认配置优先级最高且用户配置难以覆盖', '使用 findLast 让后写入规则的优先级高于先写入的规则', '用 findFirst 与 findLast 的组合实现配置优先级动态切换', '使用统一优先级排序而非 findFirst 或 findLast 来执行规则'],
    correct: 1,
    explanation: 'findLast 让后添加的规则覆盖前面的规则。用户配置的优先级高，天然压过 OpenCode 的默认配置。这是配置覆盖机制的通用做法——最近的声明最有发言权。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
