---
title: DeepSeek Harness 全景：一个"一切皆插件"的通用 agent harness
---

# DeepSeek Harness 全景：一个"一切皆插件"的通用 agent harness

> 本文基于 `dsh-v0.1.0-rc.7`（master @ 99f6f02，2026-08-17）。项目处于 developer preview，迭代很快，文中机制以该基线为准。

本站拆过的三个终端编程 Agent——OpenCode、Codex、Claude Code——都有同一个共同点：它们围绕"替用户写代码"这件事，各自实现了一个主循环。今天我们换一个物种：DeepSeek 开源的通用 agent harness，`dsh`。它不替用户写代码，也不打算被某一种产品独占，而是做一台可以被任意产品装配的通用 agent 引擎。为此，它选择了与前三个完全不同的架构路线：**everything is a plugin**。

先给一个决定全篇读法的起点判断：

> **`dsh` 的源码里没有"一个不可变的中央循环"可供逐步拆解——只有一棵可组合的插件树。** 所以本站这一栏的主线，从"拆主循环"改成了"理解如何用插件去拼出一个 agent loop"。

这篇文章先把全景铺开：它是什么、代码怎么组织、为什么这么组织，以及它和 OpenCode、Codex 到底是不是同一类东西。


## 一、同能不同构：它和你熟悉的终端 Agent 不是一回事

先设一个小问题：让一个 agent"跑一个回合"——读一条用户消息，决定该不该调工具，再生成回复。三个终端 Agent 各自用热衷的形态实现它：OpenCode 有一个 while(true) 的 7 步 runLoop，Codex 有一个事件 reactor，Claude Code 有 continuation 驱动的 QueryEngine。这三个循环都**硬编码进二进制**：你要扩展，得改核心循环，或在它上面再套钩子。

`dsh` 把这件事彻底拆开。它不把"循环"写死在哪一个文件里，而是拆成几十个互相独立的 package，每一个都以"插件"的身份存在，靠一个组合框架把它们**在运行期拼成一棵树**。agent loop 在这棵树里只是其中一条分支——它叫 `agent-loop`，并且官方明确标注它是 **swappable**（可替换）的。在 `docs/architecture.md` 的表里，你能看到"默认驱动"这个措辞：

```
| core/agent       | 接口 + live registry + `agent/*` 事件 | ctx.agents |
| core/agent-loop  | 默认驱动，实现上面的接口            | ctx.agentLoop |
```

`agent` 定义接口，`agent-loop` 只提供一个默认实现。于是模型适配器、工具注册表、会话日志、agent 循环本身——全部都是插件，"一切都可从配置换掉"。

这就是第一句判断的来源。理解 `dsh`，不是去找那个中央 while，而是理解：**一层层插件怎么声明、怎么互相注入、最后怎么被一棵树编排起来**。

先给一个粗略的方向图：

- 三个终端 Agent = 一个产品 + 一个专用主循环
- `dsh` = 一个组合框架 + 一堆能力插件 + 一个可选的默认装配（profile）


## 二、仓库怎么组织：约 220 个 package 的 monorepo

`dsh` 是一个规模惊人的 pnpm monorepo。先给两组实测的真实数字（截至 rc.7）：

- `packages/` 下的 TypeScript 源码约 **43 万行**，全仓库（含 apps、scripts、tests）合计约 **50 万行** `.ts`。
- 有**约 220 个** `@deepseek-ai/dsh-*` 的 npm package，按 `packages/<group>/<pkg>` 两级分组，另有 vendored 框架包、native addon、`apps/*` 与 `website`。

这组数字本身就回答了"为什么这么组织"的一半：单仓库 + pnpm workspaces，让"依赖必须通过包边界显式声明、按包可独立发布、依赖关系可被机器检查"成为可能。根 `package.json` 用 workspaces 字段把它们都编进同一个 workspace：

```jsonc
"workspaces": [
  "vendor/*",
  "packages/*/*",
  "native/landlock-run",
  "native/landlock-run/packages/*",
  "apps/*",
  "website"
],
```

入口只有两个壳子：`apps/cli`（提供 `dsh` 这个 CLI）与 `apps/web`（提供浏览器前端构建）。**一个包不等于一个产品**——CLI 与 Web 是同一个 `packages` 池上的两种装配。

### package 分组表：带着地图走读

`packages/README.md` 有一张分组表，每行写着这个组的角色（core = 产品 API 脊柱、某个能力缝 = 定义/实现/消费方三件套、bundle = profile 补丁层）。这张表是走读地图，你记不住每个包没关系，但要抓住三条规律：

- **`core/` 组是产品 API 脊柱**，`session`、`system-prompt`、`tools`、`agent`、`agent-loop` 这些不讲"能力"、只讲"产品内部数据结构与循环"的包都在这里。
- **其它组几乎都是"能力缝"的堆叠**：`fs`、`shell`、`subprocess`、`sandbox`、`compaction`、`subagent`……每个能力 = "接口声明 + 实现 + 消费方"的三件套（这是后文要展开的专项）。
- **`bundle/` 组是 profile 的补丁层**，是最后装配的关键。

支撑这套分工的依赖规则很硬："Extension plugins depend on Service Definitions, never concrete providers"——扩展插件只依赖服务定义，绝不依赖具体实现；依赖图又是机器生成的。所以包的边界是约束，不是摆设。


## 三、既然"一切皆插件"，为什么没有特权核心？

你多半会问：如果什么都是插件，那总得有个东西来编排？谁定插件的先后？谁提供最底层？

`dsh` 的回答非常彻底——**没有特权核心可被钉住**。`docs/architecture.md` 原话几乎是这个意思：

> There is no privileged core to patch: you extend `dsh` by mounting a plugin beside the others, and registrations are effects that unwind when the plugin unloads.

把这段话反过来读，就得到它三处与终端 Agent 的本质差异：

1. **扩展位置不同**。终端 Agent 是把动作写进产品循环；`dsh` 是在树旁边新挂一个插件，不碰别人。
2. **注册即副作用、可逆卸载**。每个贡献都走 `ctx.effect()` 或 `ctx.on()`，插件卸载时它的注册也会反向回滚。热插拔因此自洽——不是你挂了一个再也收不回来的状态。
3. **其所以能拆成插件，是因为底层有框架在管树/作用域/生命周期**。这个框架，就是后面要成专篇的 Cordis。

"没有特权核心"不是口号，而是工程上的必须。既然目标是任一样产品都能把 `dsh` 当引擎，那循环、工具、权限、沙箱都必须可换。`dsh` 连 agent-loop 本身都标了 swappable，正是把"必须"贯彻到每一环节的结果。

### 它要"大而全"，所以必须"无中心"

这里可补一个设计取舍：终端 Agent 的循环必须固定，因为职责专一（替用户在终端干活），不可变循环反而好维护。而 `dsh` 的目标是"能被任意产品装配"——不同产品要不同循环、工具集、沙箱策略。**循环一旦写死，通用就不是天然的。** 既然要通用，就必须把"循环"降级成一个可替换的实现。


## 四、profile、bundle 与 patch：一套代码、任意组装

前几节讲 package 怎么堆，这一节讲**怎么把它们堆成一个可跑的 `dsh`**。一个 `dsh` 跑起来，其实不是一个"单一程序"，而是一棵**按 ordered layers 装配的插件树**。

关键概念在 `docs/architecture.md`：

> A **profile** is a named composition stored in the harness home. A **bundle** is a distribution format for the config rows and code they mount.

翻译成人话：

- **Profile（档）** = 一个"叫什么名字、装哪些 bundle"的命名组合。`web`、`headless` 都作为模板随仓库发出去。
- **Bundle（包袱）** = 一个"Cordis 配置行 + 加载代码"的分发格式。它最重要的作用是：让 bundle 里插入的每一行配置，**都能被更上层的 patch 覆盖**。

再者两者都在自己 `package.json` 的 `dsh` 字段里自我声明。`dsh-base` 的声明尤其直白：

```jsonc
// packages/bundle/base/package.json（节选）
"dsh": {
  "bundle": {
    "patch": "./cordis.patch.yml"
  }
}
```

一个 profile 的装配顺序，在 `profile-boot.ts` 里写得很清楚——**先 bundle 层（按 profile 列出的顺序），再 profile 自己的 `cordis.patch.yml`，再 home 一份、最后任何 `--patch`**。示意：

```text
[空 root 配置]                      ← 每个 profile 都是白纸
   └─[bundle 层] dsh-base
        └─[bundle 层] dsh-web-app | dsh-headless   ← 浏览器 / 一次性 runner
             └─[profile 的 cordis.patch.yml]        ← 用户 profile 覆盖
                  └─[home 的 cordis.patch.yml]       ← 用户机器级覆盖（跨 profile）
                       └─[--patch overlays]          ← 命令行覆盖（最上）
```

运行时怎么验证？官方给了一个好用命令：

```bash
dsh --profile web --dump-config
```

它把"这台机器实际会起来的树"无条件打出来，打印的每一行都能被你的 patch 替换。

### headless：一个反向证明

最有说服力的装配例子是 `dsh-headless`——一次性、干干净净的 runner。它的 `package.json` description 写得很直白："over `dsh-base` with no Host, HTTP, or browser layer"，README 则说它"mounts no Host, HTTP server, Web runtime, or browser plugin"。它只是往 `dsh-base` 之上**不装** host / web / browser 这个面，就变成一个纯后台模式的 agent。这印证了"装配即壳子"：同一个 `dsh`，加不加一个面，就成了完全不同的产品形态。

这也就是它"为什么是 harness、而不是又一个终端 agent loop"的原因——它做的是"中枢可换" + "装配即壳子"。于是 dsh 对 terminal agent 的对比口径也清晰了：它是一个通用 harness，不是另一个终端循环；`agent-loop` 只是这个 harness 的默认实现，可被替换。


## 五、与三栏的同能不同构

参考本站三栏的公共三方对比表，给一个"同能不同构"的对照——**同能**（都能让 agent 干活）、**不同构**（内里架构根本不同）：

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness（`dsh`） |
|------|------|------|
| 定位 | 终端编程 Agent（面向产品） | 通用 agent harness（被产品装配） |
| 核心抽象 | 一个主循环（runLoop / reactor / QueryEngine） | **一切皆插件 + Cordis 插件树** |
| 核心循环可否换 | 写死在产品中心 | `agent-loop` 默认实现，swappable |
| 底层 | 原生 Effect-TS / Rust 事件 / Bun | vendored Cordis 组合框架 |
| 规模 | 单包 5 万～51 万行 | 约 50 万行 TS / 约 220 包 monorepo |
| 面向 | 替用户写代码 | 被任意产品装配成任意形态 |

要点是**不是"哪个更好"，而是"为不同目标长了不同样"**。终端 Agent 追求"开箱即用"，因此牺牲可塑性；`dsh` 追求"任产品用它当引擎"，因此必须把"中心循环"让位给"可组合树、可换实现"。


## 六、下一步走哪条线？

前三栏各有主循环可拆，而 `dsh` 既然没有中央循环，读者最大的问题就变成：**它以什么为骨架让我精读？**

答案有两条，恰好是后两篇的起点：

1. 既然是插件树，第一步必须读**拼树的框架**——vendor 下来的 Cordis。不理解 `ctx`（作用域）、"注册即副作用"、waterfall（串行委托）、Service 声明注入、scope（per-agent 私有注册），后面的 agent 树、会话日志、capability 缝全都看不懂。这就带出 **02-cordis**。
2. 真正驱动 agent 干活的是默认分支的 `agent-loop`。你想懂"一个 agent 到底怎么跑一个回合"，就顺着树走进去——是 **03-agent-loop**。

一句话收束：

> **`dsh` 不是一个固定的 agent loop，而是一套让"任意 agent loop 都能被拼出来"的组合框架。** 它把一切变插件、变可换、变可退，然后交给装配者决定"你要一个 CLI、一个浏览器还是一个后台一次性 agent"。

## 章节小测

<script setup>
const q = [
  {
    question: '下列哪一项最符合 `dsh` 的架构定位？',
    options: ['所有逻辑，包括 agent 循环，都是无特权中心的可替换插件', '只有一个中央主循环，其它模块围绕它注册', 'agent 循环是核心，改代码才能扩展', '与 OpenCode 相同，把 runLoop 写死在中心'],
    correct: 0,
    explanation: 'dsh 用 "everything is a plugin" + 无特权核心：`agent-loop` 只是默认实现、swappable。后三项都指向需要特权中心或把循环写死的旧模式。'
  },
  {
    question: '关于仓库规模，哪一项正确？',
    options: ['约 50 个包、10 万行', '约 220 个包、43 万行', '约 500 个包、80 万行', '约 20 个包、5 万行'],
    correct: 1,
    explanation: '实测（截至 rc.7）monorepo 约 220 个 `dsh-*` 包、`packages/` 内 TS 约 43 万行、全仓库约 50 万行 `.ts`；其余偏离这两项数值。'
  },
  {
    question: '一个可跑的 `dsh` 是怎么被组装出来的？',
    options: ['按 bundle 在 profile 列表的顺序，在空根上逐层钉 patch', '每个 bundle 都是独立启动程序，profile 负责调用它们', 'profile 是纯空清单，bundle 往里写整棵树', 'profile 与 bundle 完全无关，各自独立运行'],
    correct: 0,
    explanation: '正确项即"空根 → bundle 层序 → 用户 patch → home → overlay"的叠层模型，与 `profile-boot.ts` 的 `allPatches` 一致；bundle 是"配置行 + 加载代码"的分发格式（`dsh.bundle.patch`）。'
  },
  {
    question: '用 `dsh --profile web --dump-config` 最合理的用途？',
    options: ['把这台机器实际会起来的插件树打印出来审', '查看 Web 前端的构建产物', '新建一个叫 dump-config 的 profile', '从 profile 卸载某个插件'],
    correct: 0,
    explanation: '`--dump-config` 不启动、不依赖 key，只为打印"已叠好"的配置树以供审查；它既不是构建产物、不建 profile、也不卸插件。'
  },
  {
    question: '`dsh-headless` 这类 bundle 最说明什么设计思想？',
    options: ['同一套核心可加 web 面，也可不加独立成后台', 'headless 是 web 的后台子集，必须依赖 web', '每个 bundle 自带独立的核心循环', 'bundle 之间强依赖，不能单独装配'],
    correct: 0,
    explanation: '`dsh-headless` 基于 base 不装 host/HTTP/web，证明一套核心可被不同 bundle 任意外壳，正是"profile/bundle 装配"的意图；它不另立循环、也不是 web 子集。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
