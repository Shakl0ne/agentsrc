---
title: Claude Code 整体架构：51 万行代码怎么组织成一个终端 Agent
---

# Claude Code 整体架构：51 万行代码怎么组织成一个终端 Agent

在终端里敲下一句需求，几秒钟后它开始改文件、跑测试、修报错——Claude Code 给人的体验就像身边坐了个随叫随到的工程师。但拆开这个 51 万行 TypeScript 的庞然大物，你会发现一个反差：它的代码里没有一行模型推理。模型能力全部来自云端 API，本地这 51 万行，花的全是「怎么把模型用起来」的功夫。

一个不含推理逻辑的客户端，凭什么长到 51 万行？

答案要先从「编排」二字说起：它把自己组织成一个**纯客户端的编排器**，围绕一次模型调用的生命周期，把 UI、工具、权限、上下文管理层层装配上去。本文的任务，就是先搭起这张总地图，看看这 51 万行被切成了哪几块、怎么接起来。

看完这篇，你将理清三个问题：

- 第一，一个「纯客户端 Agent」的复杂度到底堆在哪几层；
- 第二，从进程启动到一次请求往返，主链路是怎么走的；
- 第三，没有沙箱的前提下，它靠什么把工具执行的风险管住。

主循环、工具、压缩、权限这些细部机制，放到后面的文章再往下拆。

![Claude Code 宏观架构：本地 Bun 进程编排云端模型与外围能力](/images/claudecode/01-macro-architecture.svg)

## 一、先把问题摊开：纯客户端的 51 万行花在哪

### 1.1 编排器，不是推理引擎

理解 Claude Code，先抓住一条：**模型在云端，本地只做编排**。本地代码承担的事情可以数得出来——组织上下文，也就是系统提示、历史消息、工具定义与记忆文件；向 Anthropic API 发起流式请求；解析返回的 `tool_use` 块并在本地执行；把工具结果回填后发起下一轮。

「请求—工具—回填—再请求」，这条链路构成整个 Agent 的心跳。后面读到主循环、工具系统、压缩机制，全是围着它转的配套。

有个容易误判的地方：「纯客户端」不等于「单进程」。Claude Code 内部照样会派生子进程跑 shell 命令、连接 MCP server、启动 LSP，但这些子进程只是工具执行的载体。什么时候调、调什么、结果怎么拼回去，决策权始终在本地编排逻辑手里。

### 1.2 三家终端 Agent 的横向坐标

把 Claude Code 放回同类工具里，规模感会更清楚：

| 维度 | Claude Code | OpenCode | Codex |
|------|-------------|----------|-------|
| 代码规模 | ~512k 行 TS / 1,884 文件 | ~440k 行 TS / 2,100 文件 | ~950k 行 Rust（含 vendored） |
| 运行时 | Bun | Bun / Node.js | 原生二进制（Rust 编译） |
| UI 框架 | React + 自研 Ink | SolidJS + `@opentui/solid` | ratatui（Rust TUI） |
| 沙箱 | 无 | 无 | 跨平台沙箱 |
| 源码状态 | 未公开 | 开源 | 开源 |

三家路径分化得很清楚：Codex 走 Rust 编译 + 沙箱的安全路线，OpenCode 选 SolidJS 生态，Claude Code 则把全部筹码押在「编排体验」上——React 渲染终端、权限分级管安全、feature flag 管灰度。沙箱与灰度这两笔账，第四节再展开细算。

## 二、四层骨架：51 万行的分层地图

### 2.1 按依赖方向切四层

把 `src/` 下 53 个顶层条目按职责归并，Claude Code 是一个清晰的四层结构：

| 层 | 组成 | 职责 |
|------|------|------|
| 表现层 | `screens/`、`components/`、`ink/` | React 组件 + 自研 Ink 渲染器，终端交互与可视化 |
| 业务逻辑层 | `QueryEngine.ts`、`query.ts`、`commands.ts`、`tools/` | 查询引擎、~90 个斜杠命令、~40 个工具 |
| 服务层 | `services/` 下 API、MCP、LSP、Analytics 等 | 无状态能力提供者，封装外部交互 |
| 基础设施层 | `utils/`、`state/`、`hooks/`、`memdir/` | 权限、状态、遥测、持久化记忆 |

分层的依据是**依赖方向**而非功能域：上层依赖下层，下层不反向依赖。表现层的组件调用业务层的 QueryEngine，业务层调用服务层的外部能力，服务层落在基础设施上。51 万行规模下架构不散架，这条单向依赖是根因。

![Claude Code 四层架构：依赖单向向下，hooks 跨层横切](/images/claudecode/01-layers.svg)

### 2.2 三个值得停留的目录

四层之外，有几个目录的「体型」透露了设计取向。

`hooks/` 有 **104 个文件**——大量业务逻辑以 React Hook 形式实现，挂在组件树里随生命周期运行。它物理上归基础设施层，运行期却横跨表现层与业务逻辑层，是事实上的跨层胶水。读懂任何一个功能，都得在 `hooks/` 和对应组件之间来回跳，这是这套架构的阅读成本。

`tools/` 下 43 个条目，**一个工具一个子目录**——`tools/BashTool/`、`tools/FileEditTool/` 各自成家，内部装着该工具的提示词、Schema、实现与权限描述。工具从几个涨到四十几个，目录结构依然扁平，靠的就是这个隔离布局。

`state/` 只有 **6 个文件**。没有 Redux、没有 Zustand，一个基于 React Context 的轻量自研 store 加上 `selectors.ts` 就撑住了全局状态。状态管理刻意保持轻量——共享状态规模可控，主要矛盾在流程编排，而不在状态。

### 2.3 技术栈：Bun 与编译期裁剪

运行时选了 **Bun**。启动快、原生跑 TypeScript 是表层原因，深层原因是 `bun:bundle` 的 `feature()` 函数支持编译期死代码消除：

```ts
// src/main.tsx:74-76
// Dead code elimination: conditional import for COORDINATOR_MODE
const coordinatorModeModule = feature('COORDINATOR_MODE')
  ? require('./coordinator/coordinatorMode.js') as typeof import('./coordinator/coordinatorMode.js')
  : null;
```

flag 关着时，对应模块根本不会进最终产物——`feature()` 在编译期就把没启用的代码裁掉了。仓库里能 grep 到的这类 flag 有近百个——Coordinator 协调模式、Kairos 助手模式、SSH 远程、内置浏览器工具等都靠它门控。同一个代码库，打包时按 flag 裁出不同产物，这是它源码未公开还能持续快速迭代的技术底座。

UI 侧是 **React + Ink**，但 Ink 是 `src/ink/` 下自研分叉的版本——对 ANSI 控制序列、焦点管理、双向文本有更精细的要求。最大的 UI 文件 `screens/REPL.tsx` 单文件 5,005 行，主界面几乎所有可见元素都编排在这里。

## 三、主链路：从进程启动到一次请求往返

### 3.1 启动：并行预热 + 串行装配

启动流程不是线性的「加载—渲染」。`src/main.tsx` 开头的源码注释直接点明了设计意图：三个副作用必须在所有 import 之前执行——性能打点、MDM 配置读取子进程、macOS 钥匙串预取。后者把两次钥匙串读取并行化，否则配置加载阶段会同步串行执行，每次启动多耗约 65ms。

```ts
profileCheckpoint('main_tsx_entry');      // src/main.tsx:12
startMdmRawRead();                        // src/main.tsx:16
startKeychainPrefetch();                  // src/main.tsx:20
```

这段代码的算盘：模块导入约 135ms、是 CPU 密集且不可压缩的，而钥匙串与 MDM 读取是 IO 等待、天然可并行。把 IO 塞进 CPU 导入的间隙，等导入完成时数据大概率已就绪。代价是顶层副作用违反常规代码规范，源码专门用 eslint 逐行豁免并配注释说明。

模块就绪后进入装配阶段，主干的推进大体串行：配置加载 → `getTools()` 装载约 40 个工具 → 认证与 setup 界面 → GrowthBook 拉取 feature flag → 内置插件与技能注册 → 启动 Ink 渲染器挂载 REPL。工具与插件必须赶在主循环前就绪，否则用户首条输入就可能触发未注册工具；MCP 则是并行预取、不等完成——源码注释明确 MCP 不阻塞 REPL 渲染与第一轮请求，慢 server 提供的工具要到第 2 轮之后才可用。

最后在 `launchRepl`（`src/main.tsx:3134`）处，`main.tsx` 把控制权完全交出。此后主循环由挂载完成的 REPL 调用 `query()` 驱动，入口只负责「把环境准备好、把 REPL 挂上去」。

### 3.2 请求往返：continuation-driven 主循环

一次用户请求的流转可以压缩成七步：用户输入 → 命令分流与预处理 → 上下文准备 → QueryEngine 流式调用 → 工具执行与权限检查 → 结果回填 → 循环或终止。

其中两步值得放大看。**命令分流**：输入以 `/` 开头就直接路由到 `commands.ts` 注册的处理器，不进模型路径；只有普通输入才走后续流程。**循环终止**：循环体是 `query.ts:307` 的一个 `while(true)`，要不要继续，取决于这一轮有没有出现工具调用——源码注释直接说明 `stop_reason` 不可靠，退出信号是 `tool_use` 块到达时置位的 `needsFollowUp` 标志（`src/query.ts:553-558`）。没有待执行工具，控制权交还 REPL；有则回填结果、构造新的 `State` 再 `continue` 回顶，循环体内没有固定步骤序列。

这种 continuation-driven 风格的好处是没有忙等：每轮都 `await` 流式响应，API 间隙进程完全空闲。它与 OpenCode 的显式循环、Codex 的 Tokio event loop 形成根本差异，是第 2 篇的主角。

## 四、外围能力：权限、灰度与扩展

### 4.1 权限：无沙箱的安全底座

Codex 内置跨平台沙箱兜底，Claude Code 和 OpenCode 都没有沙箱——工具直接以用户权限运行命令。安全责任由此全部上移到权限决策点，Claude Code 的答案是全三者中最精细的权限系统：**7 种模式**，5 个外部模式加 2 个内部模式（`src/types/permissions.ts:16-28`）。

用户按 Shift+Tab 循环的实际路径是 `default → acceptEdits → plan → bypassPermissions`。其中 `plan` 模式把 Agent 限制为只读规划；`auto` 模式由 AI 转录分类器实时评估每个工具调用的风险、自动放行低风险操作，被 `TRANSCRIPT_CLASSIFIER` feature flag 门控。权限不是某一「层」的东西，而是一条贯穿各层的横切关注点——业务层执行工具前查、服务层连 MCP 前查、表现层渲染弹层时也要读当前模式。

### 4.2 双层开关：编译期裁剪 + 运行期灰度

前面看到的 `feature()` 决定代码**是否被打包**，GrowthBook 决定已打包代码**是否启用**——后者按用户/组织维度做 AB 实验与渐进发布。两层开关配合，Agent 协作这类较新的能力（Coordinator、Teammate）可以先灰度验证再逐步默认启用。

### 4.3 扩展面：工具之外的能力外挂

内置工具之外，外部能力靠三条路接入：MCP 协议接云端工具与资源、插件与打包技能在启动阶段注册、`memdir/` 与 `tasks/` 提供跨会话记忆与后台任务。加上 `bridge/` 通过 WebSocket 把本地会话桥接到 claude.ai/code 的远程控制，这些外围能力都栓在主链路旁，不动四层骨架。

## 五、这张地图上，后面往哪走

### 5.1 三条阅读坐标

回头看，51 万行可以浓缩成三个设计结论，它们是后续每一篇的坐标：

1. **纯客户端编排器**：所有复杂度集中在「怎么组织上下文、怎么调度工具、怎么管控权限」三件事上；
2. **并行预热 + 分阶段装配**：IO 藏进导入时间，工具与插件赶在主循环前就位，MCP 并行预取、不阻塞首屏，`profileCheckpoint` 打点让每个阶段可观测；
3. **continuation-driven**：没有忙等循环，是否进入下一轮由本轮的工具调用决定。

### 5.2 阅读路线

从这张地图往下走：第 2 篇剖析主循环的 continuation 机制与流式工具调度，第 3 篇展开工具系统与斜杠命令的接口与生命周期，第 4 篇拆解压缩机制，第 5 篇进入 Agent 协作，第 6 篇聚焦权限系统的 AI 分类器，第 7 篇讲 MCP 集成与 Bridge 桥接，第 8 篇收束于跨会话记忆。每一篇都会回到本文建立的四层与主链路框架，从宏观走向微观。

## 章节小测

<script setup>
const q = [
  {
    question: 'Claude Code 本地代码与模型能力的关系，准确的说法是？',
    options: [
      '本地含轻量推理逻辑，小任务端上直接出结果',
      '本地只做编排，模型能力全部来自云端 API',
      '本地跑一个量化小模型，云端只做复杂任务',
      '本地与云端各推理一半，结果在本地合并'
    ],
    correct: 1,
    explanation: 'Claude Code 是纯客户端编排器：组织上下文、发流式请求、本地执行工具、回填结果再请求。51 万行全部花在编排上，没有自研模型代码。A/C/D 的端侧推理均不存在。'
  },
  {
    question: '`hooks/` 目录 104 个文件在四层架构中的位置，正确的是？',
    options: [
      '属于表现层，只负责组件渲染与事件绑定',
      '属于业务逻辑层，负责注册命令与工具',
      '物理归基础设施层，运行期横跨表现与业务层',
      '属于服务层，封装 API 与 MCP 外部交互'
    ],
    correct: 2,
    explanation: 'hooks 物理上在基础设施层，运行期绑定在组件树上，既调用业务层的 QueryEngine 又调用服务层能力，是跨层胶水。这也意味着理解功能需要在 hooks 与组件之间来回跳转。'
  },
  {
    question: '启动时把钥匙串预取放在所有 import 之前，目的是什么？',
    options: [
      '规避模块循环依赖导致的求值死锁',
      '让 IO 等待与 CPU 密集的模块导入并行',
      '确保身份认证先于任何业务代码执行',
      '提前校验配置文件格式避免启动失败'
    ],
    correct: 1,
    explanation: '模块导入约 135ms 且 CPU 密集，钥匙串读取是 IO 等待、可并行。提前发射可以把 IO 塞进导入间隙，省下约 65ms 的串行等待，等导入完成时数据大概率已就绪。'
  },
  {
    question: 'continuation-driven 主循环的「继续或终止」由什么决定？',
    options: [
      '外部循环变量计数到达上限后退出',
      '定时器到期后强制进入下一轮请求',
      '本轮流式响应里是否出现工具调用',
      '用户每轮手动确认后才继续执行'
    ],
    correct: 2,
    explanation: '循环体是 while(true)，退出信号是 tool_use 块到达时置位的 needsFollowUp——源码注释明确 stop_reason 字段不可靠，不能作为依据。没有工具调用就终止并把控制权交还 REPL；好处是没有忙等，每轮 await 流式响应。'
  },
  {
    question: '无沙箱前提下，Claude Code 管控工具执行风险的核心机制是？',
    options: [
      '所有命令都在一次性容器里执行后销毁',
      '七种模式分级的权限系统做放行决策',
      '靠网络隔离切断工具的外部访问路径',
      '靠只读文件系统限制工具的写入范围'
    ],
    correct: 1,
    explanation: 'Codex 用沙箱兜底，Claude Code 把安全责任上移到权限决策点：5 个外部模式加 2 个内部模式，auto 模式由 AI 分类器实时评估风险。权限是贯穿各层的横切关注点，容器与网络隔离在三者中都不存在。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
