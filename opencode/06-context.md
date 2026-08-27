---
title: OpenCode 上下文装配：持久知识、按需注入与窗口预算
---

# OpenCode 上下文装配：持久知识、按需注入与窗口预算

让 Agent 理解项目，最朴素的直觉是给它开一块"记忆"，把代码风格、用户偏好、往期对话一股脑记下来，下次要用直接查。可这句话刚说出口就卡住了——项目风格写在文件系统里、聊过的内容存在数据库里，可每次请求时能塞进窗口的那点 token，才是模型"能不能记住"的关键。

那 OpenCode 是怎么"既记得住项目、又不在每次调用里把窗口撑爆"的？它没有做一个抽象的记忆层，而是把"记住"拆成一条**装配链**：谁常驻磁盘随取随用、谁在读到相关文件时按需补进来、谁每次请求当场拼装，窗口实在装不下时又有哪一道预算闸来拦。这篇就把这条链一段一段拆开看。

下面重点解答三个核心问题：

第一，**分类与策略**：知识应该划分成哪些类型？哪些需要常驻，哪些应该按需加载进窗口？
第二，**动态加载**：读取文件时，相关上下文是如何"按需注入"并生长扩张的？
第三，**边界与持久化**：当上下文超限时，由什么机制进行裁切与截断？跨会话的信息又依靠什么在重新打开后得以复原？

压缩机制中的 prune / compact 细节已在第四章专门分析。本文将重点关注整体架构，仅在流程衔接需要时标注其触发位置，不做重复展开。

## 一、从"记忆"到"上下文装配"：系统由哪几部分组成

先抛个问题：你打开某个项目，Agent 凭什么知道这个项目的代码风格、构建约定、还有你上回聊到哪？有人会答"它有记忆"，有人会答"它把 AGENTS.md 读了"。这两种答案都只对了一半——因为它不是靠单一机制，而是靠一套多个部件拼起来的装配。

### 1.1 四类持久知识 + 一层装配 + 一个预算闸

把 OpenCode 的上下文拆开，是这么几块：

- **文件系统持久知识**——指令文件 `AGENTS.md`/`CLAUDE.md`、`Skill`、`Reference`，驻留磁盘、按需读入，不随每次请求耗 token。这三类住在磁盘上，给人的是"项目是什么样、该怎么查"，而不是每次调用都往 prompt 里倒全文。
- **请求级装配**：System Prompt。它每次调用都按当前模型、当前环境、当前可用 skill 重新拼一遍，是"每次都注入"的那部分。
- **运行时对话**：Messages。存在 SQLite，实现跨会话持久化，是"对话记忆"的落点。
- **预算闸**：overflow + compaction。它决定窗口塞满之后腾不腾空间、怎么腾，衔接本系列第四章做过的深入拆解。

### 1.2 谁是"每请求全量"，谁是"驻留磁盘、按需进窗"

这套装配最关键的分界线在"注入时机"。System Prompt 是**请求级、每次都全量**的——它基本稳定，因为越是稳定、越是基本不长的部分，越值得重复发送以便靠缓存省钱。指令文件/Skill/Reference 这些持久知识则相反，是**驻留磁盘、按需进窗**——除非 LLM 恰好走到那个目录、加载那个 skill、或引用那个 reference，它们的完整内容不会被拖进每次调用。

这条分界线不是概念上的把戏，而是成本结构上的必然：理论上每次都能全塞，但那样 token 就撑爆了。因此系统把"哪些必须每请求都有"（模型身份、工作区、工具清单、指令）和"哪些可以用到才取"（Skill 内容、Reference 源码、深层指令）分开调度。下文二、三、四节分别把这两头的装配讲透，第五节分析预算控制机制，第六节讨论跨会话的持久化。

## 二、每次都注入的固定头部：System Prompt 的装配

System Prompt 是每次 LLM 请求的固定头部。它由 `src/session/system.ts` 与 `src/session/prompt.ts` 联合拼装，内容在运行中几乎每次一样——这正是它适合被缓存复用、而非每次重算的价值所在。

### 2.1 四段拼装：provider 专属 / 环境 / 指令文件 / skill 清单

在 runLoop 把消息喂给 LLM 前，`prompt.ts` 会先并行取四种东西：

```ts
const [skills, env, instructions, modelMsgs] = yield* Effect.all([
  sys.skills(agent),
  sys.environment(model),
  instruction.system().pipe(Effect.orDie),
  MessageV2.toModelMessagesEffect(msgs, model),
])
const system = [...env, ...instructions, ...(skills ? [skills] : [])]
```

`sys.environment(model)` 给 `<env>` 环境块，`instruction.system()` 注入指令文件，`sys.skills(agent)` 注入可用的 skill 描述清单。等实际交给 LLM 时，`src/session/llm/request.ts` 还会在最前面补上 provider 专属指令、在最后补用户自定义 system：

```ts
const system = [
  [
    ...(input.agent.prompt ? [input.agent.prompt] : SystemPrompt.provider(input.model)),
    ...input.system,
    ...(input.user.system ? [input.user.system] : []),
  ].filter((x) => x).join("\n"),
]
```

于是最终 System Prompt 的四段顺序是：**agent/provider 指令 → 环境块 → 指令文件 → skill 描述**。四段各自承担一种稳定角色：指令告诉模型"你是谁"，环境告诉它"你在哪"——这支撑着相对路径解析与 git 命令选择——指令文件告诉它"这个项目的约定"，skill 清单告诉它"有哪些工具能按需取来"。

### 2.2 Provider 专属 prompt：一套内核跑多家模型

在上述四段里，第一段并非固定文案，而是按模型路由的动态指令。`src/session/system.ts` 的 `provider(model)` 用 `model.api.id` 做字符串匹配落到不同 prompt 文件：

```ts
export function provider(model) {
  if (model.api.id.includes("gpt-4") || model.api.id.includes("o1") || model.api.id.includes("o3")) return [PROMPT_BEAST]
  if (model.api.id.includes("gpt")) {
    if (model.api.id.includes("codex")) return [PROMPT_CODEX]
    return [PROMPT_GPT]
  }
  if (model.api.id.includes("gemini-")) return [PROMPT_GEMINI]
  if (model.api.id.includes("claude")) return [PROMPT_ANTHROPIC]
  if (model.api.id.toLowerCase().includes("trinity")) return [PROMPT_TRINITY]
  if (model.api.id.toLowerCase().includes("kimi")) return [PROMPT_KIMI]
  return [PROMPT_DEFAULT]
}
```

跨厂商的适配取舍最终收敛为**给每家模型配置一份独立的 Prompt 文件**——共预置 anthropic、gpt、beast、gemini、codex、trinity、kimi 和 default 八份。

这项设计的核心逻辑在于：同一个 Agent 内核需要适配多家模型，而不同模型对指令风格、工具调用及上下文组织的偏好差异巨大。与其用一套通用 Prompt 导致各模型效果互相折损，不如针对每家模型提供专属指令。

该机制通过无外部依赖的纯字符串匹配完成路由，实现了"更换模型仅需切换 Prompt"，使得 Agent 内核无需为此分叉；此外，诸如 `explore`、`compaction` 这类特定 Agent，还可配置自身的 `agent.prompt` 来覆盖默认的 Provider 文案。

### 2.3 环境注入 `<env>`：位置与状态的快照

`sys.environment(model)` 把当前运行环境翻译成一段给 LLM 的可读文本（`src/session/system.ts:48`）：

```ts
environment: Effect.fn("SystemPrompt.environment")(function* (model) {
  const ctx = yield* InstanceState.context
  return [
    [
      `You are powered by the model named ${model.api.id}. The exact model ID is ${model.providerID}/${model.api.id}`,
      `Here is some useful information about the environment you are running in:`,
      `<env>`,
      `  Working directory: ${ctx.directory}`,
      `  Workspace root folder: ${ctx.worktree}`,
      `  Is directory a git repo: ${ctx.project.vcs === "git" ? "yes" : "no"}`,
      `  Platform: ${process.platform}`,
      `  Today's date: ${new Date().toDateString()}`,
      `</env>`,
    ].join("\n"),
  ]
})
```

五个字段每个都支撑一项具体行为：`Working directory` 让 LLM 知道相对路径怎么解析；`Workspace root` 划出工作区边界（和 external_directory 权限、文件操作范围相关）；`Is a git repo` 决定要不要用 git 工具；`Platform` 影响 shell 命令选用；`Today's date` 帮助避免把"未来"当已发生。这一小块不是装饰——它是"每次请求都有、但内容几乎不变"的典型：它承载了让模型在不重新加载文件系统的情况下就能做相对判断所需的稳定上下文。

## 三、驻留磁盘、按需进窗：指令文件 / Skill / Reference

System Prompt 负责每次都有。但描述"这个项目"的持久知识大多不该每次都发——它们驻留在磁盘，只有当 LLM 实际需要用时才被拖进窗口。指令文件、Skill、Reference 这三类共享同一个模式：**只给轻量元数据/描述，完整内容按需再取**。

### 3.1 指令文件（instruction.ts）：从磁盘到 system prompt

指令文件驻留磁盘，`Instruction.system()` 把它们读进来拼进 system prompt。它找到多少、从哪里找，由 `systemPaths()`（`src/session/instruction.ts:109`）决定：

```ts
// The first project-level match wins so we don't stack AGENTS.md/CLAUDE.md from every ancestor.
if (!Flag.OPENCODE_DISABLE_PROJECT_CONFIG) {
  for (const file of instructionFiles) {
    const matches = yield* fs.findUp(file, ctx.directory, ctx.worktree).pipe(Effect.catch(() => Effect.succeed([])))
    if (matches.length > 0) {
      matches.forEach((item) => paths.add(path.resolve(item)))
      break
    }
  }
}
```

`instructionFiles` 是 `["AGENTS.md", ...(disableClaudeCodePrompt ? [] : ["CLAUDE.md"]), "CONTEXT.md"]`——CLAUDE.md 仅在未禁用时加载，用于兼容 Claude Code；CONTEXT.md 标为 deprecated。查找分两路：全局（`~/.config/opencode/AGENTS.md` 与 `~/.claude/CLAUDE.md`）和项目——从 CWD 向上 `findUp`。`systemPaths` 的注释点明了关键取舍：**取首个匹配就停，不叠加各级父目录里的 AGENTS.md**——不把工作目录到根之间所有 AGENTS.md 全摞起来，避免多层互相覆盖时的不可预期。这是"简单可预测 vs 多层叠加"之间的明确选择：宁可丢失一些父级目录里的约定，也要保证行为确定。内容拼成 `Instructions from: {filepath}\n{content}` 注入，`config.instructions` 里还能挂本地/远程指令 URL。

### 3.2 Skill（skill/index.ts）：描述轻、内容贵、按需取

Skill 是另一类持久知识。它的 `Info` 只有四项：`name`、`description`、`location`、`content`（`src/skill/index.ts:36`）。发现时扫描多渠道——全局 `~/.claude/skills`、`~/.agents/skills`、项目 `.claude/skills` 与 `.agents/skills`、配置的 `skills.paths`、远程 `skills.urls`，外加一个内置的 `customize-opencode`。

关键在它**两阶段进窗**。`SystemPrompt.skills`（`src/session/system.ts:65`）只把名称与描述放进 system prompt：

```ts
skills: Effect.fn("SystemPrompt.skills")(function* (agent) {
  if (Permission.disabled(["skill"], agent.permission).has("skill")) return
  const list = yield* skill.available(agent)
  return [
    "Skills provide specialized instructions and workflows for specific tasks.",
    "Use the skill tool to load a skill when a task matches its description.",
    Skill.fmt(list, { verbose: true }),
  ].join("\n")
})
```

`Skill.fmt(list, { verbose: true })` 用于输出包含 `name`、`description` 和 `location` 的 `<available_skills>` 清单。源码注释解释了其设计初衷：在 System Prompt 中提供详尽的技能清单，能让 Agent 更准确地识别技能。

**技能的具体实现与完整指令 `content` 并不随 System Prompt 预加载**。System Prompt 仅注入轻量级的"技能名片"；只有当 LLM 匹配到对应需求时，才会通过 `skill` 工具把具体的 `content` 动态拉取并注入到 Messages 对话流中。

这种**System 存高密度摘要，工具按需载入全文**的分级加载策略，避免了将所有 Skill 全文一次性塞入上下文的 Token 浪费，是典型的"描述轻、内容贵、按需调"。

### 3.3 Reference（reference.ts）：元数据注入，正文靠工具

Reference 把外部知识（依赖仓库、文档目录）映射成一个名字，在 prompt 里通过 `@name` 引用。`resolveAll`（`src/reference/reference.ts:161`）把配置里的引用解析成 `local` / `git` / `invalid` 三种；git 引用会解析出 clone 缓存路径，`materializeAll` 在需要时把仓库拉到本地（受 `experimentalScout` gate）。关键的是 `referenceTextPart`（`src/session/prompt/reference.ts:30`）往 user message 注入的**不是全文**，而是元数据：

```ts
text: [
  `Referenced configured reference ${label}.`,
  ...(metadata.kind === "local" ? ["Kind: local directory"] : []),
  ...(metadata.kind === "git" ? ["Kind: git repository"] : []),
  ...(metadata.repository ? [`Repository: ${metadata.repository}`] : []),
  ...(metadata.path ? [`Reference root: ${metadata.path}`] : []),
  ...(metadata.problem
    ? [`Problem: ${metadata.problem}`]
    : ["For targeted context, inspect the reference path directly with Read, Glob, and Grep."]),
  // …metadata.branch / targetPath 等可选行与 scout 提示从略
].join("\n")
```

它告诉 LLM "这里有个 reference，根在这、是什么类型"，然后明确丢给 Read/Glob/Grep（或调 scout 子 agent）去访问正文。于是 LLM 拿到的是一份**线索而非全文**——Reference 的源码永远在磁盘，需要时由模型自己用工具去读。指令文件/Skill/Reference 这三类持久知识在这里对齐到同一条原则：**驻留磁盘，只给可用于定位的元数据/描述，正文按需再取**。

## 四、读文件时的上下文相关注入：instruction.resolve

前三节的注入都是"装配时决定"。还有一种更精准的注入发生在"LLM 真的去读文件时"——`instruction.resolve`。它把"内容该不该进来"的判定，从"预先加载"挪到"恰好用到"。

### 4.1 Read 读文件 → 从该文件目录向上找 nearby 指令

每当 LLM 使用 `Read` 工具读取文件时，`Instruction.resolve`（`src/session/instruction.ts:178`）会以该文件的当前目录为起点向上递归查找至工作区根目录。在此过程中，一旦发现附近（Nearby）存在指令文件，就会将其动态注入。

值得注意的是，这些指令并非注入到全局 System Prompt 中，而是拼接到工具返回结果（Tool Result）的 `<system-reminder>` 节点内——这意味着该指令仅在当前文件读取操作对应的上下文消息中生效：

```ts
let current = path.dirname(target)
while (current.startsWith(root) && current !== root) {
  const found = yield* find(current)
  if (!found || found === target || sys.has(found) || already.has(found)) { current = path.dirname(current); continue }
  let set = s.claims.get(messageID)
  if (!set) { set = new Set(); s.claims.set(messageID, set) }
  if (set.has(found)) { current = path.dirname(current); continue }
  set.add(found)
  const content = yield* read(found)
  if (content) results.push({ filepath: found, content: `Instructions from: ${found}\n${content}` })
  current = path.dirname(current)
}
```

`find(dir)` 只在当前目录找 `AGENTS.md`/`CLAUDE.md`/`CONTEXT.md` 单个文件是否存在，命中就返回。`sys.has(found)` 跳过已在 system prompt 出现过的路径（不重复），`already.has(found)` 跳过已由 `read` 工具实际读过、`state.metadata.loaded` 记录过的路径。

### 4.2 为什么比"全塞 system"精准：按需 + 上下文相关

这套机制替代了"把所有指令全塞进 system prompt"——那样 token 会撑。它只在 LLM 真读了某目录下的文件时，才把那个目录的约定给到 LLM，并用 `claims` 这个 `Map<MessageID, Set<string>>` 去重，保证同一 assistant 消息只注入一次。读 `src/auth/` 下的文件，就注入 `src/auth/AGENTS.md`（如果存在），而不是每次都把全项目指令倒进来。

这是"预检索"的反面：不预先猜哪些 context 相关，而是让 LLM 的行动（去读某处）本身带来相关的 context。下一节会看到，这条原则和"不用 RAG"是同一枚硬币的两面——与其先 chunk 索引再相似度召回，不如把指令留在它会出现的路径上，让读取动作把它们带进来。

## 五、装不下的：窗口预算与压缩

持久知识和装配保证"该进来的都进来了"，但窗口有限。总有一刻需要问：预算还有多少、装不下时腾不腾。这是预算闸 `overflow` + `compaction` 的范畴，本系列第四章已把 prune/compact、9 段摘要都讲透，这里不再展开，只把"它在装配链里站哪一个位置"说清。

### 5.1 窗口预算：usable / isOverflow

预算这样算（`src/session/overflow.ts`，见第四章）：`usable()` 先算出"敢用额度"，一般是 `context − 输出预留`；`isOverflow()` 用"累计 token ≥ usable"判定是否溢出。这两行比较听着简单，但它取代的是"印象里觉得上下文变大了"——它把"要不要压"变成一个可计算、可复用的开关，被主动检查与被动承接共用。

### 5.2 触发 compaction：一句话引到第四章

溢出判定之后，OpenCode 走 proactive（上一轮 assistant 结束后查 `isOverflow`）与 reactive（流中 LLM 返回 `ContextOverflowError`）两条路径，经 `compaction.create` 立占位、下一轮 `runLoop` 从任务队列派给 `compaction.process`。压缩的产物是"不删数据、只标记 + 重排 + 锚定摘要"的两级机制。

这一段的完整实现（`PRUNE_MINIMUM`/`PRUNE_PROTECT`/辅助干摘要/`filterCompacted` 重排）就在前面讲过的 [OpenCode 会话压缩：Compact 2 级机制](/opencode/04-compact)。这里只需记住它在装配链里的位置：System Prompt + 持久知识 + 对话构成"能装下的"，溢出与压缩决定"装不下时怎么办"。

### 5.3 Prompt Caching 的架构收益：不是独立 cache 层

值得单独点一句的是：OpenCode 没有专门做一个"cache 层"，但它的装配结构自带省钱效果，集中在三点：

- **system prompt 稳定**：每次请求 system 几乎不变，例如 provider 指令、环境、指令与 skill 清单，重复发送的头部天然命中 provider 侧的 prompt cache。
- **tail 保留重叠**：压缩保留最近若干轮，相邻两次请求间消息末端高度重叠，新增的只是当前轮的工具调用与响应。
- **prune 截断**：旧工具输出被折叠成 `[Old tool result content cleared]` 这样的占位，让后续每次请求 body 更小、也能让后续压缩传给 compact agent 的输入更轻。

这三点的共同点是：**它们都不是独立 cache 层，而是装配结构自带的省钱结果**。稳定头部、保留尾巴、截断负载，每一条都是系统为了让"下一次请求和上一次更像"而做的选择——这比事后加一个缓存服务更省心，因为缓存命中率是装配结构直接决定的。

## 六、跨会话持久化：Messages 的持久化与重排

装配与预算决定"这一轮进窗口的有什么"。但对话还要跨会话存活——上次聊到哪、哪个文件被改到了、某个决定是为什么。这层由 messages 的持久化负责，存在 SQLite，靠的是实时写 + 重启能续 + 压缩后不删只重排。

### 6.1 SQLite：三张表加级联

`src/session/session.sql.ts` 用 Drizzle 定义多张表。核心三张是 `SessionTable`、`MessageTable`、`PartTable`，逐层 `references(..., { onDelete: "cascade" })`——删 session 自动删 message，删 message 自动删 part。每条 message/part 的实体数据压成一个 JSON blob 存进 `data` 列，外层只留 `id`、`session_id`、时间戳与索引；这样查询/分页走结构化字段，内容本身保持灵活。级联删除保证了会话生命周期闭合：一个会话没了，它的全部历史随之清干净。

### 6.2 实时增量写：进程被杀不丢

对话不是一次性写盘，而是每段文本、每个工具状态都实时落库。`Session.updatePart()` 在消息生成过程中反复被调用，把 `text`、`tool`、`file` 等各类型的 part 增量写进 `PartTable`。这正是"实时增量写"的意义：**process 中途被 kill，已发生的内容已经留在磁盘**，重启后能从上次中断点继续。相比"整轮跑完再保存"，实时写牺牲了一些批量效率，换来的是可以承受中断的可靠性——对话记忆不依赖进程活到结束。

### 6.3 跨会话恢复 + filterCompacted 重排

持久化让跨会话成为可能：下次启动读 SQLite 就能恢复历史。但压缩之后，数据库里消息的物理顺序、与压缩时要保留的两轮内容，跟 LLM 该看到的自洽顺序未必一致。`filterCompacted`（详见第四章）在序列化给 LLM 前重排，使输出成为 `[compaction-user][summary-assistant][tail][后续]` 的自洽序列。

它的重点在于"**重排而不是删**"：压缩阶段只打标记、更新 `tail_start_id`，数据库里的旧消息仍在；LLM 看到的是摘要 + 保留尾巴 + 后续，顺序对得上，仿佛上下文没断。这一层和第三章对照着看：**会话历史走数据库，持久知识走文件系统——两条持久化轨道**。指令/skill/reference 这些知识住在文件的字面里，"说了什么、改了什么"这些历史住在数据库的行里，二者在装配时汇合进窗口。

## 七、取舍收束：不建记忆层、不用 RAG

最后往回看一遍整条链，能回答一个常被问到的问题：OpenCode 为什么不用向量库、也不给"记忆"单开一层抽象？答案不是"做不出来"，而是"装配已经把这些需求覆盖了"。

### 7.1 为什么不用向量库：四个点对应已讲的机制

传统 RAG 的标准做法是 chunk 化 → embedding → 向量库 → 相似度召回。OpenCode 完全不跑这套，理由可以落到四个具体痛点，每个都有已讲的机制作证：

- **实时性差 → Grep 实时搜索**：向量库要预先 embedding，代码变更后索引滞后；Grep 直接搜文件系统，永远是最新内容。
- **上下文丢失 → Read 读全文**：chunk 把一段代码从它的类、调用链里切出来，上下文残缺；模型找到行号后 Read 读完整文件，上下文完整。
- **成本高 → 本地零成本**：几万个 chunk 的 embedding 要花钱；Grep/Read 完全本地。
- **Agent 要的是"特定"不是"相似" → 精确匹配**：找函数定义、error 字符串、一个 import，需要的是精确命中；相似度召回在这种场景反而不如 grep 精确。

这四条的共同点：Agent 的检索需求大多是"我要找某样特定的东西"，而不是"猜我可能想要什么"。把 LLM 当推理引擎、把 Grep/Read 当它的手，比先建索引再召回更贴近真实使用方式。第四节的 `instruction.resolve` 也沿同一条路——指令跟着读取动作进窗，而不是预先向量化后按相似度喂给它。

### 7.2 为什么不叠加独立记忆层

OpenCode 也没有单开"记忆抽象层"（如 CC 的 memdir）。理由同样现实：那层记忆想覆盖的功能，已经被三件现有的东西覆盖——项目知识交由**指令文件按需注入**承载，会话历史交由 **SQLite 持久化**承载，上下文自洽交由 **filterCompacted 重排**保证。再叠一个"自动读/写记忆文件 + LLM 检索记忆"的抽象，等于再造一套与装配链平行的、重复功用的子系统，复杂度上升而新增收益有限。

这里的取舍是：**功能完备性 vs 架构简洁**。文件系统 + 数据库 + 装配已经回答了"它靠什么记住"，记忆层是可选优化而非必需；而少一个层，也就少一个"何时该同步记忆、何时该失效它"的维护面。

### 7.3 OpenCode vs Claude Code：一张对比表

把整条链放回生态对照一次，OpenCode 与 CC 的核心差异在"有没有独立记忆层 + 服务多少模型"：

| 维度 | OpenCode | Claude Code |
|------|----------|-------------|
| 记忆抽象层 | 无（用装配替代） | 有（memdir，4 种类型 + LLM 检索） |
| 供应商 | 跨厂商（provider 路由多份 prompt） | 单 Anthropic |
| 指令文件 | AGENTS.md/CLAUDE.md（首个匹配，不叠加各级父目录） | CLAUDE.md（多层 + @include + rules） |
| Skill | `**/SKILL.md` 多渠道 + 两阶段（描述/工具加载） | 有 skills |
| 引用 | Reference（local/git，元数据注入 + 工具访问） | 无对应层 |
| 会话持久 | SQLite（级联、分页、实时写） | JSONL |
| 压缩 | 2 级（prune + compact，锚定摘要） | 多级（microcompact/collapse/autocompact） |

收束一句：OpenCode 用"装配"替代了"记忆"——把跨会话要留的留在文件与数据库，把每次要用的装进一个稳定、按需、预算受控的窗口。这一整套的价值不在堆了多少缓存服务，而在**用最少的抽象层把"记住 + 取出 + 淘汰"讲成了一个可预测的装配流程**：持久知识定义它是什么，System Prompt 定义它每次长什么样，窗口预算定义装不下时怎么办，SQLite 定义它聊到哪。多 Agent 也好、上下文也好，取舍的价值都在"站得稳、不叠层、可逆"，而不是把每个能力都武装到最重。

## 章节小测

<script setup>
const q = [
  {
    question: 'OpenCode 指令文件用「findUp 取首个匹配、不叠加各级父目录」，而不是把从工作目录到根的所有 AGENTS.md 都注入。这样设计的主要取舍是什么？',
    options: ['首匹配丢弃父级约定换实现成本更低但牺牲可嵌套', '取全部父级目录可获更全约束但不叠加会丢深层指令', '首匹配保证行为确定可预测代价是丢弃部分父级约定', '只加载项目根单个文件以简化权限与缓存逻辑'],
    correct: 2,
    explanation: '源码注释明确「first project-level match wins so we don\'t stack...」。取首个匹配保证行为可预期，代价是父级目录里可能更有用的约定被丢弃。轮换成「全部叠加」则行为不可预测、覆盖规则复杂——这是简单可预测 vs 多层叠加的取舍。',
  },
  {
    question: 'Skill 系统把完整内容留在磁盘，system prompt 里只放 name+description。这个「两阶段加载」对上下文装配的意义是什么？',
    options: ['仅在输出阶段把 skill 名称重复一遍以提高可见性', '只发描述省 token，要用时再通过 skill 工具取正文', '两阶段本质相同只是分开写让代码更易维护', 'system 与工具各自加载一半保证内容不重复'],
    correct: 1,
    explanation: '第一阶段 system 只放可用的 name+description（轻量），第二阶段 LLM 判断匹配后调 skill 工具加载 content（按需）。这避免了「所有 skill 全文都塞进 prompt」的 token 浪费，是「描述轻、内容贵、按需取」的典型。',
  },
  {
    question: 'instruction.resolve 在 Read 工具读文件时把 nearby 指令注入到工具结果的 system-reminder，而不全塞进 system prompt。这套「按需 + 上下文相关」的核心收益是什么？',
    options: ['把指令转移到消息里从而绕开 provider 对 system 的长度限制', '只在读某目录时才给它该目录约定省 token 且精准', '让全部指令都能在每一条消息里重复可见以提高准确性', '必要时可跳过指令文件的磁盘读取直接复用缓存'],
    correct: 1,
    explanation: '全塞 system 会撑 token 且给无关上下文。resolve 让 LLM 真读 src/auth 时才注入 src/auth/AGENTS.md，并通过 claims 去重（同一 assistant 消息只注入一次），兼顾省 token 与精准，是「预检索」的反面。',
  },
  {
    question: 'OpenCode 为什么用 Grep/Read 这类工具让 LLM 自己搜，而不是用向量库 + 相似度召回？',
    options: ['embedding 无法在本地运行且成本不可控', 'Agent 检索多是找特定符号而非猜相似需要精确匹配', '向量库无法索引被指令文件和 Skill 覆盖的项目知识', 'SQLite 分页加载天然比 embedding 更省实时内存'],
    correct: 1,
    explanation: 'Agent 常要找函数定义、error 字符串、import 这类「特定东西」，相似度召回不如精确匹配。加上向量库实时性差（代码更新索引滞后）、chunk 丢上下文、embedding 有成本——四条对应 Grep 实时 / Read 读全文 / 本地零成本 / 精确匹配。',
  },
  {
    question: 'OpenCode 不用独立记忆层（如 CC 的 memdir）。作者为什么认为这方面的覆盖已经够用？',
    options: ['指令按需注入 + SQLite 持久化 + filterCompacted 已覆盖三块', '文件系统足够表达记忆故数据库只存会话不必另建层', '因为跨厂商环境不支持本地记忆文件所以放弃抽象层', '记忆尚未覆盖但未来版本会补 memdir 因此暂不叠层'],
    correct: 0,
    explanation: '记忆层想覆盖的三种能力——项目知识、会话历史、上下文自洽——分别已被指令文件按需注入、SQLite 持久化、filterCompacted 重排接管，再加 memdir 等于建平行重复子系统，复杂度上升收益有限。这是「功能完备性 vs 架构简洁」的取舍。',
  },
]
</script>

<Quiz :questions="q"></Quiz>
