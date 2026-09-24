---
title: 扩展的三个极致：自改、hooks 桥与跨产品委托
---

# 扩展的三个极致：自改、hooks 桥与跨产品委托

前五篇把 `dsh` 的骨架拆完了：插件树怎么装配（01）、靠什么原语共存（02）、默认驱动怎么跑回合（03）、会话日志怎么当唯一事实源（04）、能力怎么做成可换的缝（05）。终章看三件把"可扩展"推到头的事：agent 碰自己的运行时、翻译别家的 hook 协议、把活儿委托给另一个产品里的 agent。

这三件事放在一起，是因为它们共用同一个底层判断：**扩展永远落在插件树的扩展点上，引擎本身不为任何一家需求改形状。** 读完这一篇，你会带着三个问题的答案离开：

- agent 对自己运行时的权力边界画在哪里，为什么模型侧只剩"看"？
- 已经在 Claude Code 里养好的 hooks，凭什么原样搬进 `dsh` 就能跑？
- 委托给 Codex 或 Claude Code 的子任务，凭什么和本地孩子共用一个接口？

MCP 与 ACP 的接入最后带一段。这是全系列的收尾。

## 一、自改的边界：看、临时挂、持久装

"agent 改自己"是 `dsh` 最容易被浪漫化的部分，先把边界画准。它实际是一个三层结构，每层给不同的操作者：

**第一层，模型只拿只读。** `tool-cordis` 提供给模型两个工具：`cordis_inspect_list` 发现运行时可查的 provider，`cordis_inspect_query` 查某个 provider 的精确方法与类型。Host 侧的 Config provider 还能列出 Loader 树里活着的每个 entry：它的 id、patch 寻址用的树内 id、Config 状态（schema 校验通过 / 缺席 / 不支持 / 组与 include 承载行的 tree / 未激活），以及把某个 entry 的 Config 投影成一份自包含的 JSON Schema 文档。模型的全部权力是"看清自己在什么里面跑"。

**第二层，动态定义归程序与浏览器。** `cordis-host-runner` 维护一套进程内的动态包注册表，`define` / `run` / `stop` / `undefine` 四个动词归**程序化调用方**，浏览器面板只操作已存在的定义——README 的措辞很明确：no model tool creates dynamic definitions。

定义分两半：Host 半跑在 `node:vm` 隔离域里，同步段的执行时间受 `vmTimeoutMs` 约束；带浏览器半的包要先经过批准往返——服务发出 `cordis/request-run`，挂起的请求由应答页面走完 `runHostHalf` / `getClientCode` / `resolveRequestRun` 才提交激活。`stop` 收回活的效果但保留定义、可再 `run`；`undefine` 连定义一起忘掉。定义是**会话作用域、进程本地**的：别的会话看不见，重启即清空。

**第三层，持久变更归 Plugin Manager。** 要装一个真插件，走 `dsh plugin`——安装含插件代码或 MCP 配置的 bundle、启用、禁用，全部落进 profile。`dsh-base` 的装配链（01 篇）就是它的操作对象。

![自改的三层边界：只读 inspect、动态定义、持久安装](/images/deepseek/06-selfmod-boundary.svg)

三层的信任姿态一以贯之。vm 隔离的全局污染，但 README 写得直白：**The sandbox isolates globals but is not a security boundary**——动态包声明的服务能触达活运行时，"Treat a dynamic package like bash access"。

设计上还有两处值得记：注册表与沙箱是同一个服务（`DynamicCordisRunnerService` 独占定义注册、vm 沙箱、fiber 生命周期、invoke handler 表，一个定义的一生只有一个 owner）；版本是不可变的包（`define` 之后内容永不改，`run` 与 `update` 靠 `currentPackageId` / `nextPackageId` 指向运行版与目标版来区分）。

把三层合起来看，`dsh` 对"自改"的态度清楚了：**看见，随时可以；临时挂载，程序可以、模型不行；持久安装，走正规装配。** 模型被当作运行时里最需要防的参与者——这与工具裁决链（05 篇）把参数进闸焊死是同一种哲学。

## 二、hooks 桥：翻译协议，不动引擎

已经住在 Claude Code 生态里的人，手里的 `hooks.json` 是现成资产。`dsh` 接它的方式不是实现一套 CC 的 hook 机制，而是做**翻译**：`dsh-hooks-claude-code` 读你的配置文件，把每个 hook 事件映射到 `dsh` 自己的拦截点上。

共享核心抽成了 `dsh-hook-protocol` 库——它不是插件、不注入任何东西，只定义"一个 hook 能做什么、跑了会发生什么"。处理管线是一条单职责函数链：校验 matcher 模式 → 经 `dsh-shell` 执行器跑命令 → 解码退出码与 stdout → 按 most-restrictive 合并（`deny > ask > allow`，`continue: false` 的停机请求有粘性，上下文按序累加）→ 落一对 `hook/invoked` / `hook/result` 日志事件。

两个方言唯一的分歧轴是 matcher 的解释方式：CC 把模式当字面量备选或正则，Codex 恒当非锚定正则——一个 `mode` 参数收掉了全部方言差异。

hook 的能力词汇就五种：exit 2 阻断（stderr 作为模型可见的理由）、请求确认（仅 CC 方言）、附加上下文（模型下次请求可见）、请求停机（记录但无运行级效果，拦截点还没有硬停原语）、以及非 2 退出码的非阻断失败。设计纪律只有一条，处处体现：**绝不让 hook 砸掉调用它的 turn**——非法正则算不匹配、执行器拒绝变成无退出码的输出、任何解析失败都降级为受控结果。

CC 桥支持七个事件，映射关系是这一篇的骨架：

| 你的 hook | 落点 | 能做什么 |
|---|---|---|
| `SessionStart` | `agent/created` 初始化 | 附加上下文，赶在第一个 turn 前 |
| `UserPromptSubmit` | `agent/pre-step` 瀑布 | 阻断提示，或附加上下文 |
| `PreToolUse` | `tools/pre-execute` 瀑布 | 阻断工具，或请求审批 |
| `PostToolUse` | `tools/post-execute` 瀑布 | 带反馈阻断结果，或附加上下文 |
| `Stop` | `agent/turn-stopping` serial | 阻断收尾、经 `steer()` 强制再走一步 |
| `SubagentStart` | `subagent/start` | 给活着的进程内子代理注入上下文 |
| `SubagentStop` | `subagent/end` | 只观察 |

![hooks 的翻译路径：外部协议到 typed 拦截点](/images/deepseek/06-hooks-bridge.svg)

两个细节体现翻译的诚实。其一，只附加上下文的 hook 必须先 `next()` 委托再把自己的消息折进下游决策——后面别的监听者仍然可以拒绝或改写，"加料"没有否决权。

其二，桥的 README 把不支持的部分列成了清单：CC 当前的 30 个 hook 事件里支持 7 个，`transcript_path` 恒为空串（持久化缝不暴露产物路径，zstd 压缩的会话日志 hook 脚本也读不了），`updatedInput` 解析但不生效。定位一句话说尽：**A compatibility adapter, not a power tool**——要定制行为，写原生 Cordis 插件，直接用全套 harness API，中间不再隔一层协议。

## 三、subagent 缝：一张按名共存的注册表

第五篇讲过 bash 缝的"同层互斥"：一个组合只能挂一个执行器。subagent 缝是反例，也是它最有信息量的地方——`ctx.subagents` 是一张**按名注册、多 provider 共存**的注册表，结构仿的是 LLM 适配器注册表。文档原话：subagent 与 bash 一样是 optional capability、不在 agent loop 里，但它因为**多个实现同时在场**而不同。

在场的是六个 provider：`spawn-in-process`（进程内新孩子）、`fork-in-process`（用父会话已完成的历史做种子的进程内孩子）、`acp`（走 Agent Client Protocol 的进程外孩子）、`codex`（官方 app-server 协议驱动的真 Codex）、`claude-code`（官方 Agent SDK 驱动的真 Claude Code）、`dsh-sdk`（进程外 Harness 孩子经 TypeScript SDK）。模型面的 Consumer 是 `tool-subagent`（按 provider 委托）加 `tool-subagent-control`（`send_message`、`interrupt_agent`、`list_agents`）。

![ctx.subagents 上按名共存的六个 provider](/images/deepseek/06-subagent-providers.svg)

能力的声明分两条路，这个区分很讲究。**一次性 start 的能力**标在 provider 的静态描述符上，服务在 start 之前检查：请求里用到一个 provider 缺的能力，直接以 `SubagentError('UNSUPPORTED_CAPABILITY')` 拒绝——fail loud，绝不先接受再忽略。**可续跑（continuable）孩子的能力**则用类型系统表达：provider 实现了可选方法 `prepareContinuable`，这个方法的存在本身就是能力，TS 的类型窄化就是发现机制。

continuable 是这套设计里最完整的一条线：`startContinuable` 预留稳定的子 id、快照版本化的 `subagent/descriptor`、向 provider 要一份**纯数据**的创建规格（只携带区分"新孩子"与"带种子孩子"的东西——比如父日志已完成回合的前缀），再经私有 activation-owner scope 创建子 Agent、投递首条 prompt。

冷恢复根本不经过 provider：续跑管理器折叠描述符、直接调 `ctx.agents.resume()`。fork 的种子语义与 04 篇的会话 fork 同构——子会话从父日志的一段闭合前缀起步，provider 只负责"怎么读父历史"这一小段。

跨产品的意义在这里收口：委托给 Codex 或 Claude Code 的一次任务，与本地 spawn 的孩子，对模型来说是同一个 `tool-subagent` 接口下的不同 provider 名。第五篇的"换 provider 不动 Consumer"，在这里直接跨出了 `dsh` 自家的进程边界。

## 四、MCP 与 ACP：接入生态也是插件

顺着"接别人的协议"再补两块拼图。`mcp-client` 连接一个 MCP 服务器，把它的工具与指令暴露进 `ctx.tools`、资源操作经 `mcp-resources` 以显式选服务器的共享工具提供；`acp` 组让外部程序经 ACP 管理持久 agent、挂 MCP 服务器、选模型、下发与取消任务。两件事都没有专门的一章，因为机制上前文全部覆盖：MCP 服务器就是一组注册进来的工具，ACP 是一个进程外的驱动方——它们出现在 `dsh` 里是插件，离开时 `dsh` 不记得它们来过。

## 五、终局：两根柱子

六篇走完，`dsh` 的骨架可以收进两根柱子。**一根是 Cordis 插件树**：一切皆插件、注册即副作用、无特权核心——从循环本身到沙箱后端到 hooks 桥，全部是树上的挂件。**一根是会话日志**：追加式事件流是唯一事实源，模型上下文、回放、持久化、fork、压缩的表面替换，全是它的投影，invariant 逐请求断言"模型可见即已落日志"。

三件极致的事放回这个坐标系：自改是插件树对"自己"的最后一次自我应用——连"看和改自己"都拆成了三层插件；hooks 桥是外部生态到扩展点的翻译器——引擎保持中立，方言差异被压进一个 `mode` 参数；跨产品委托是缝机制的越境——provider 注册表跨出了自家进程。它们没有一件需要引擎开口子，这就是全系列反复验证的那个判断的最终形态。

与三个终端 Agent 的最后一照：

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness |
|------|------|------|
| 定位 | 产品 | 被装配的引擎 |
| 扩展 | 产品内的钩子与模块 | 一切皆插件，含循环与日志的消费者 |
| 兼容生态 | 各自的原生协议 | 桥翻译 CC/Codex hooks、MCP、ACP |
| 委托 | 各自的子 agent | 同一注册表下本地与跨产品共存 |

三个终端 Agent 选择了把一件事做到开箱即用；`dsh` 选择了把"被任意产品装配"贯彻到底，代价是每个使用者都要先理解一棵树的语法。哪种都对——前者服务今天的产品，后者赌的是明天会有很多个产品。

至此，DeepSeek Harness 源码精读六篇完结，感谢读完。

## 源码索引

- `packages/extensions/tool-cordis/README.md` — 只读 inspect 工具
- `packages/extensions/cordis-host-runner/README.md` — 动态定义注册表、vm 沙箱、信任姿态
- `packages/boot/plugin-manager/` — 持久 bundle 安装
- `packages/hooks/hook-protocol/README.md` — 共享 hook 规则、合并语义、`hook/*` 事件
- `packages/hooks/hooks-claude-code/README.md` — 七事件映射表与支持边界
- `packages/subagent/README.md` — 委托缝包族
- `docs/subsystems/subagent.md` — provider 契约、能力发现、continuable
- `packages/mcp/README.md`、`packages/acp/README.md` — 生态接入
- `docs/subsystems/extensions.md` — 生成的 `ctx.cordisInspect` / `ctx.dynamicCordisRunner` API

## 章节小测

<script setup>
const q = [
  {
    question: '在当前的 `dsh` 里，模型对自己运行时的最大权力是？',
    options: ['写并挂载临时插件', '改写任一插件的配置行', '只读地查看服务与注册树', '卸载并重装任意 bundle'],
    correct: 2,
    explanation: 'tool-cordis 只给模型 cordis_inspect_list / cordis_inspect_query 两个只读工具；动态定义归程序与浏览器，持久安装归 Plugin Manager——no model tool creates dynamic definitions。A/D 是动态层的事，B 是 Plugin Manager 的事，都不归模型。'
  },
  {
    question: '一个 hook 以退出码 2 结束，意味着什么？',
    options: ['阻断动作，stderr 作为理由', 'hook 崩溃，运行中断', '请求确认后放行', '结果被静默丢弃'],
    correct: 0,
    explanation: 'exit 2 是阻断信号：动作不发生，错误输出作为模型可见的理由。非 2 退出码才是非阻断失败（B 说反了），C 是 ask 语义，D 不存在。'
  },
  {
    question: 'subagent 缝与 bash 缝的关键差异在于？',
    options: ['subagent 不走 capability 缝', '允许多 provider 按名共存', 'subagent 只支持进程内实现', 'bash 的 provider 也按名注册'],
    correct: 1,
    explanation: 'ctx.subagents 仿 LLM 适配器注册表：spawn/fork/acp/codex/claude-code/dsh-sdk 六个 provider 同时在场、按名选择；bash 是单一服务只挂一个执行器。A/C 与事实相反，D 张冠李戴。'
  },
  {
    question: '想要一套 hooks.json 覆盖不了的定制行为，`dsh` 的官方答案是？',
    options: ['给桥提交扩展补丁', '写原生 Cordis 插件', '把逻辑拆成多个 hook 组合', '用 MCP 服务器包装'],
    correct: 1,
    explanation: '桥自我定位是 compatibility adapter, not a power tool：原生插件直接用全套 harness API，挂在同一批扩展点上，中间不再隔协议。A 违背定位，C/D 都绕不开协议的表达力上限。'
  },
  {
    question: '`fork-in-process` 起子代理时，"种子"是什么？',
    options: ['父会话的全部原始事件', '一份系统提示的快照', '父日志已完成回合的前缀', '父 agent 的工具注册表'],
    correct: 2,
    explanation: 'ContinuableCreateSpec 只携带区分新孩子与带种子孩子的东西：父日志的闭合前缀，与 04 篇会话 fork 同构。A 不要求闭合，C/D 不是 provider 侧的种子内容。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
