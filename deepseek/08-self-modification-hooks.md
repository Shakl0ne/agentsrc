---
title: 自改 / hooks 桥 / 生态：agent 学会改自己、也接得住别家的协议
---

# 自改 / hooks 桥 / 生态：agent 学会改自己、也接得住别家的协议

> 本文基于 `dsh-v0.1.0-rc.7`。项目处于 developer preview，迭代很快，文中机制以该基线为准。

前七篇我们把 `dsh` 从插件森林、Cordis、agent-loop、会话日志、工具管线、capability 缝，一路看到压缩 / 上下文注入 / 子代理这些"长会话扩展"。现在是收尾的第八篇，聊三件"往更大处收"的事：

1. **自改（self-modification）**：agent 能不能检查自己当前挂载了哪些插件、并且主动挂载 / 卸载新的临时插件？
2. **hooks 桥**：已经住在 Claude Code / Codex 生态里的人，他们的 `hooks.json` 能不能被 `dsh` 原样接住跑起来？
3. **生态**：把这八篇的机制串起来看，`dsh` 作为一个通用引擎，和 OpenCode / Codex / Claude Code 到底差在哪、又靠什么立住。

先放这一篇的核心判断：

> **`dsh` 的终局主张是：一切都可扩展，且扩展的接口统一。** 于是它把"agent 改自己"做成一组**自引用的临时组件工具**，把"接住别家 hooks"做成 **hooks 桥（一对应翻译器）**——而这两件事底层都复用一个东西：那套 typed-Decision 拦截点（`agent/pre-step`、`tools/pre-execute`、`tools/post-execute`……）和它背后的 Cordis 事件模型。

## 一、自改：agent 能不能"看自己"、并"卸了再装"

### 一个很尴尬的前提：runtime 里全是插件，agent 却看不见

整栏我们都习惯了一个前提——`dsh` 到处是 cordis 插件。但自引用的动机 Agent Note（`interception-extension-points` 的同门，`self-referential-cordis-toolset`）开篇就把那个别扭点破了：

> Everything in this harness is a cordis plugin, but the agent running inside that plugin runtime cannot see or touch it: it cannot enumerate the services and events around it, cannot extend itself with a new tool mid-session, and cannot compose capabilities it invents.

一个跑半天、背后有几十个插件的运行时，**里面的 agent 自己却看不见也没法触达这些插件**——没法枚举身边的服务与事件、没法在会话中途给自己加一个新工具、没法当场组装一个自己刚发明的新能力。这就是 `dsh` 要补的"自改"。

### 不是"让模型跑段 js"那么随便，有三个前置问题

把"让模型自己改运行时"交给模型，绝不是"让模型跑段 code"这么草率——设计上先承认三个正确性（correctness）问题并存：

1. **模型写的注册要在写的那一步就校验**：畸形 tool schema 必须在注册时就失败，而不是拖到下一次请求要把它拼进 prompt 时才炸。
2. **模型写的代码要调用它从没看过的 service API**：靠猜方法签名、甚至猜返回形状，会白白浪费很多次盲试。
3. **模型挂载的一切必须完全可卸**：model 能按需卸，宿主插件 reload 时也要能按正常插件生命周期跟着卸——否则长会话会慢慢堆出一批没人管的 listener 和 tool。

### 五个自引用的命令：inspect → define → run → stop → undefine

落法在 `packages/extensions/tool-cordis`（`@deepseek-ai/dsh-tool-cordis`）这个命名空间插件里。它给了模型五个针对**当前 DSH 进程实时运行时**的工具（不是 shell 脚本，是命令工具）：

- `cordis_inspect` —— 对当前进程的**只读报告**（`services`/`plugins`/`temporary`/`tools`/`api`/`events` 各一节，`what:` 可精确到一个目标）。**先看得见，才有资格动手。**
- `cordis_define` —— 校验并登记一个"动态包"（`name`、`purpose`、host half `code` 或/和 browser half `client`），对它做语法检查；**只登记、不运行**，取一个进程内的 `dyn-<n>` id。
- `cordis_run` —— 把该包的 host half 放进 `node:vm` 沙箱里真正评估，并把 browser half 推送给所有打开的 web 页面。**正在运行的包会重投交付（避免 reload 页面拿不到活体）。**
- `cordis_stop` —— 把宿主那一半卸到 quiescence（所拥有的 tool、listener、service、timer、effect 全回到可回构造状态）后返回，**定义本身在，可再 run**。
- `cordis_undefine` —— 先 stop、再 forget：把定义从注册表里忘掉，会话里那张卡片变成"未加载记录"，不能自动提升。

这一整套的**执行宿主**在 `packages/extensions/cordis-host-runner`（`ctx.dynamicCordisRunner`）：定义注册表、`node:vm` 沙箱与 fiber 生命周期、invoke handler 表、以及浏览器页面承载 run 往返的那一半。`tool-cordis` 只是壳，没有 runner 就没有沙箱、工具派不上用场。

### 沙箱语义：隔离全局污染，但**不是**安全边界

动态包跑在 vm realm，但 `tool-cordis` README 与 Agent Note 反复强调同一个 trust stance：

> The sandbox isolates globals but is not a security boundary. …… Treat this toolset like bash access; do not rely on it as a product security boundary.

- vm 隔离**意外的全局污染**（Node 全局 `process`/`Buffer` 留 `undefined`，`require`/`fetch`/timer 是抛错指向 Cordis 替代品的问路口）；
- 但**已授权服务**（`ctx.fs`、`ctx.web`、`ctx.bash`、timer……）拿到的是 host 执行器的权限——这个临时组件真能触达线上运行时；
- 所以它**绝不是安全边界**，`dsh` 明说要像对待 `bash` 一样对待它、谨慎开启。

可以说：自改和 bash 是**同一档信任**——能用 bash 的地方就能用它，但它不给你"更安全"。

### "装"不难，"看得出能卸、真能卸干净"才是设计题

前几节讲了模型怎么写，真正让"自改"成立的是**管理容器**，不是写插件代码本身：`stop`/`undefine` 保证任何自挂的东西都能回到**没挂**状态；运行时全**进程内存**（storage stance：注册表是进程内唯一真源，`stop`/`undefine`/toolset 卸载/DSH restart 都会清空，且定义不会自动恢复）；`cordis_inspect` 先给模型看清"现在挂了什么"，再决定动不动。

把对齐起来，就是一句话：**自改 = inspect-able（先可看）→ disposable（必可卸）→ validate-at-define（写时即验）。** 模型被当成"被信任的插件作者"，而运行时被要求"可检验地接受、可回退地卸载"。

## 二、hooks 桥：把别人的 shell-hook 协议"接进"咱们的 typed Decision

如果说"自改"是从内部长出能力，那 hooks 桥就是**从外部接生态**——把 Claude Code / Codex 已经成规模的 `hooks` 配置，接进 `dsh` 自己的拦截点。

### 一次关键的重构："原生 hook"不是一个包，而是一套事件 API

拦截扩展点的 Agent Note（`interception-extension-points`）开头给出了整个 hooks 子系统的地基重构——它几乎把 hooks 定义成"给怎样一个 typed 事件 API 配了翻译器"：

> The key reframe driving this design is that **"native hooks" are not a package** — a native hook is just an ordinary cordis plugin subscribing to the canonical lifecycle events. So the real product is a *powerful, well-typed canonical event API*; the CC and Codex bridges are merely translators that map an external shell-hook protocol onto that same API.

也就是说，`dsh` 自己的 hook 面落在 **typed-Decision 拦截点** 上（`tools/pre-execute`、`tools/post-execute`、`agent/pre-step`、`agent/turn-stopping`……），"原生 hook" 就是**在那个面上订阅的普通 cordis 插件**。而 `dsh-hooks-claude-code` / `dsh-hooks-codex` 这两个包，**只是把外部 shell-hook 协议翻译到那套 API 的翻译器**——"anything a hook can do, a plain plugin can do directly"。

### 真正共享的一半，抽成 `hook-protocol` 库

两块独立桥各自要复用一大半；为避免各写各的，共享核心抽成 **`@deepseek-ai/dsh-hook-protocol`**（`packages/hooks/hook-protocol/`）。它**不是 cordis 插件，不注入任何东西**，是个 dialect-neutral 的库，提供：

- **matcher 校验与解释**（`matcherDiagnostic` / `matchesMatcher`，`claude` 模式 = 字面量或正则、`codex` 恒为正则）；
- **`runHook`**——真正用 `ctx.shell` 跑一个 hook（stdin payload、env、timeout），并解码退出码 / stdout；
- **结果合并**（`mergeHookOutputs`：权限 deny > ask > allow，halt 有粘性，block 原因拼接）；
- **两个 `hook/*` 会话事件**——`hook/invoked` 和 `hook/result`（log-only、非 surface，`appendHookInvoked`/`appendHookResult` 负责落盘和 decision 归属；stderr 摘要截断到 `stderrSummaryMaxChars`=500）；
- 以及那三处 detached 点（`SessionStart`/`SubagentStart`/`SubagentStop`）fire-and-forget 的一致性——**dispose 必须等所有在跑的 hook 进程中止后 resolve**。

每条 bridge 只保有自己方言那半边：CC 的 stdin payload + `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PROJECT_DIR}` 替换 + outcome→Decision 映射；Codex 类比它自己的子集。

### CC 那半边：逐 hook 映射到 typed Decision，most-restrictive

`dsh-hooks-claude-code` 把用户现有 `hooks.json`（或 settings 里的 `hooks` key）**只取可映射的命令子集**，映射到 typed Decision：

| CC hook | Harness point | 映射 |
|---|---|---|
| `SessionStart` | `agent/session-start`（emit，不能 block）| additionalContext → `agent.inject()`，不能 gate 启动 |
| `UserPromptSubmit` | `agent/pre-step`（waterfall）| `deny`→`PreStepDecision.reject`；否则走 `next()` 到下游 |
| `PreToolUse` | `tools/pre-execute`（waterfall）| `deny`→`PreToolDecision.deny`；`ask`→`ask` |
| `PostToolUse` | `tools/post-execute`（waterfall）| `deny`→ block + feedback；否则走 `next()` |
| `Stop` | `agent/turn-stopping`（serial）| 阻塞 Stop hook 的 reason 通过 `steer()` 逼出下一步 |
| `SubagentStart`/`SubagentStop` | `subagent/start`/`subagent/end`（emit）| additionalContext→`inject` 到 live 子代理 / observe-only |

多个文件 hook 挂同一 node 时**串行、按 config 顺序**跑，结果按 `hook-protocol` 的 **most-restrictive** 折叠。这就把 CC/Codex 那种"denied / block-with-reason"的 shell-hook，接进了 `dsh` 的 typed Decision 体系。

### 桥的价值与边界：兼容路径，不是最佳做法

为什么要"桥"而不直接支持 `hooks.json`？`hooks-claude-code` README 里有一句很诚实的话：

> A native cordis plugin could do everything this bridge does — more powerfully, with typed returns and no serialization boundary.

桥本身就是"兼容路径"——你有一段已经在 CC/Codex 里养好的 `hooks.json`，搬进 `dsh` 不用重写。**真正要定制的地方，应该写成普通 cordis 插件，而不是指望桥必须覆盖你所有的边缘需求。**

## 三、生态：八篇拼起来，`dsh` 的"引擎"立场

### 一张对照表（同能不同构）

把前七篇的机制对齐到三个既有生态，你会看到 `dsh` 的每一项几乎都用了同一招——"做成可换缝"：

| 主题 | OpenCode | Codex | Claude Code | **DeepSeek Harness** |
|---|---|---|---|---|
| 主结构 | 插件 / Layer | multi-Agent | claude.md + Task | **everything is a plugin（vendored Cordis）** |
| 长会话 | 压缩 2 级 | 压缩 3 级 | 压缩 5 级 | **compaction / request-context / subagent 均做成可变缝** |
| hooks | 无 shell-hook | 部分 shell-hook | `hooks.json` 原生 | **hook-protocol 共享库 + CC/Codex 翻译桥** |
| 自改 | — | — | — | **自引用 cordis toolset（inspect/define/run/stop/undefine）** |

**核心差异一句话**：

> 三栏的 hooks 是"产品自己的机制"（CC 原生用自己的 `hooks.json`，OpenCode 没这块，Codex 只做部分）；而 `dsh` 把它也拆成"一个 typed 事件 API + 外部协议翻译器"。与技术无关、与产品立场有关：`dsh` 想当一条能接住别家既有投资的筐，而不是再闭门造一套。

### `dsh` 的立场：可换不只用于 provider，还用于"接住别人的协议"

前七篇我们反复看到 `dsh`"把 X 做成可换缝"的习惯——capability seam 的每个（CompactionEngine / SubagentProvider / BashProvider……）。第八篇我们看到它其实收口成三个层次：

1. **内部一致**：所有东西（request、tool、hook、compaction、subagent）都回写到 session log + 都挂 typed 拦截点——可换 Provider。
2. **外部兼容**：用「桥翻译既有 shell-hook 协议」接住 CC/Codex 的既有投资，不必从零重建。
3. **自反扩展**：用「自引用 cordis toolset + vm 沙箱」给模型"看自己 + 卸自己"——但 trust 是 bash-equivalent，所以在生产上只做 opt-in。

这三点叠起来，`dsh` 的成熟画像是：**运行时用统一的 typed 事件表达一切可换能力；对外要么用 bridge 翻译既有 shell-hook，要么用 self-mod 从内长出能力。安全是" sandbox 本身不是边界，只防意外"，而完整的产品化由 opt-in 决定。**

## 结语

八篇走完，`dsh` 的骨架其实就是两根柱子——**"一切皆插件"（Cordis）** 和 **"一切变化都能写进 session surface"（typed events）**。最后一篇我们看到的是它两端用力：

- **向内**：agent 能 inspect / mount / unmount 自己的临时插件（自引用 cordis toolset），但 trust = bash、不设安全边界；
- **向外**：能原样接住 Claude Code / Codex 的 `hooks.json`（typed hooks 桥），并诚实地说"桥只是兼容路径，真要定制请写原生插件"；
- **成生态**：能对齐三栏，是因为核心在生产一个"typed event + surface + translation"的开放平面，而不是另一套闭门的产品。

一句话收束整栏：

> **`dsh` 不是"又一个 agent"，它是一套由「插件 runtime + 可变缝 + 自改 + 桥接生态」组成、且每样都能可换可卸可追溯的引擎。** 你能看得见自己、改得动自己、卸得下自己——也能把别家的壳接进自己的平面。

至此，`DeepSeek Harness 源码精读` 八篇完结，感谢读完。

## 章节小测

<script setup>
const q = [
  {
    question: '`dsh` 的"native hook"到底是什么？',
    options: ['一个 TypeScript 接口', '一个订阅拦截点的普通插件', '恰好一个 shell 脚本文件', '一个 TypeScript 类库'],
    correct: 1,
    explanation: '核心重构：native hook 不是包，而是订阅 typed-Decision 拦截点的普通 cordis 插件；CC/Codex bridge 只是一层翻译。'
  },
  {
    question: '`dsh-tool-cordis` 这套自引用工具的信任姿态是什么？',
    options: ['作为完整的安全边界', '作为强力隔离即安全', 'sandbox，但不作安全边界', 'sandbox 能完全阻止宿主访问'],
    correct: 2,
    explanation: 'vm sandbox 隔离全局污染、但不是安全边界；临时库能触到 ctx.fs/bash 的真实权限，所以 trust = bash access、需 opt-in。'
  },
  {
    question: '`dsh-hook-protocol` 属于什么？',
    options: ['一个 cordis 插件', '一个库', '一个 CDN 资源', '一个 NPM 脚本'],
    correct: 1,
    explanation: 'hook-protocol 是 CC/Codex 两个 bridge 共享的 dialect-neutral 库，不注入任何东西，只提供 matcher、run hook、merge 与 hook/* 事件。'
  },
  {
    question: '为什么 hooks 桥存在？',
    options: ['它比原生插件能力更强', '它走原生插件走不了的路', '兼容已有 hooks 配置', '它负责填补安全边界空洞、并守住每一处'],
    correct: 2,
    explanation: '桥只是兼容路径：把已在 CC/Codex 里养好的 hooks.json 平移进来；真要定制要走原生插件，桥并非最佳做法。'
  },
  {
    question: '下面哪个最能概括 `dsh` 全篇的"引擎"立场？',
    options: ['把全部能力都装进单个大文件里，一次打包完毕且以后也不再改动', 'typed events + 可变缝 + bridge', '闭门自研，并拒绝与任何外部生态做协议互通', '把每个任务都拆成一段完全固定的流水线逐一跑完'],
    correct: 1,
    explanation: '综合全文：统一的 typed event + surface，一切可换 provider、可 bridge 既有协议、可自反扩展，正是 dsh 作为通用引擎的立场。'
  }
]
</script>

<Quiz :questions="q"></Quiz>