---
layout: home

hero:
  text: AI Agent 源码精读
  tagline: 逐行拆解最先进的终端编程 Agent — Claude Code × OpenCode × Codex 架构设计、实现原理与工程哲学
---

## 导读目录

本博客覆盖 **Claude Code**、**OpenCode** 和 **Codex** 三款主流终端编程 Agent，外加 **DeepSeek Harness** 这一通用 agent harness。OpenCode（TypeScript）和 Codex（Rust）是当下最先进的两款开源终端编程 Agent，Claude Code 则是 Anthropic 官方闭源实现——四者的源码在本站均有深度解读。其中 dsh 以"一切皆插件"的架构思路提供了与前三者不同的引擎视角。

<div class="catalog-grid">

### OpenCode 源码精读

> 开源版 Claude Code 终端编程 Agent 源码深度解析，基于 TypeScript + Effect-TS 函数式架构

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [整体架构](/opencode/01-overview) | ~100K 行 TS 源码全景、Effect-TS 依赖注入、双运行时架构 | ~7K |
| 02 | [主循环 runLoop](/opencode/02-runloop) | while(true) 7 步循环、Doom Loop 检测、流式工具执行 | ~15K |
| 03 | [工具系统](/opencode/03-tools) | Tool.Def 接口、Edit 引擎 10 策略、Permission 三态权限 | ~15K |
| 04 | [上下文压缩](/opencode/04-compact) | 2 级压缩（Prune + Compact）、锚定摘要、消息重排 | ~14K |
| 05 | [Agent 系统](/opencode/05-agents) | SubAgent 隔离调度、tasks.pop() 串行模型、与 CC 对比 | ~13K |
| 06 | [上下文架构](/opencode/06-context) | 5 层上下文注入、指令文件 + Skill 系统、为什么不用 RAG | ~13K |
| 07 | [plan-execute-verify 编排](/opencode/07-plan-execute-verify) | 规划/执行/验证三阶段编排、oh-my-openagent 机制解析 | ~13K |

<p class="catalog-cta"><a href="opencode/" class="VPButton medium brand">进入 OpenCode 专栏 →</a></p>

### Codex 源码精读

> OpenAI 官方开源终端编程 Agent 源码深度解析，基于 Rust 的 ~100 crates 工程体系

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [全景：架构与定位](/codex/01-overview) | 3 个二进制、~100 crate、TUI / exec / App Server 三模式 | ~16K |
| 02 | [主循环：Submission 驱动](/codex/02-mainloop) | 事件 Reactor、SessionTask、8 阶段 Turn 生命周期 | ~13K |
| 03 | [上下文组合与增量注入](/codex/03-context) | 13 个上下文段、context diffing、prompt cache | ~14K |
| 04 | [Compact 3 种压缩机制](/codex/04-compact) | Local / Remote v1 / v2、InitialContextInjection | ~19K |
| 05 | [多 Agent 编排架构](/codex/05-multi-agents) | Agent Path、V1/V2 协作、CSV 批处理 Map-Reduce | ~25K |
| 06 | [工具系统与安全沙箱](/codex/06-tools-sandbox) | ToolExecutor、MCP、ExecPolicy、跨平台沙箱 | ~23K |
| 07 | [模型管理与 Provider 抽象](/codex/07-models) | 4 种 Provider、AuthManager、WebRTC 语音对话 | ~20K |
| 08 | [Codex vs CC 设计哲学对比](/codex/08-philosophy) | 3 个核心假设、连锁反应、未来启示 | ~14K |

<p class="catalog-cta"><a href="codex/" class="VPButton medium brand">进入 Codex 专栏 →</a></p>

### Claude Code 源码精读

> Anthropic 官方终端编程 Agent 源码深度解析，基于已泄露源码，TypeScript + Bun + React + Ink 技术栈

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [整体架构与启动流程](/claudecode/01-overview) | 51 万行源码全景、Bun 运行时、React + Ink 终端 UI | ~15K |
| 02 | [主循环：QueryEngine](/claudecode/02-mainloop) | continuation-driven 轮询、流式 API 调用循环 | ~16K |
| 03 | [工具系统：40+ 内置工具](/claudecode/03-tools) | Tool 接口设计、20+ 工具目录、MCP 扩展 | ~14K |
| 04 | [对话压缩：5 级机制](/claudecode/04-compact) | auto/micro/apiMicro/reactive/sessionMemory 五级 | ~16K |
| 05 | [Agent 系统](/claudecode/05-agents) | AgentTool / Coordinator / Swarm 多级协作 | ~19K |
| 06 | [命令系统：70+ 斜杠命令](/claudecode/06-commands) | Commander.js 解析、commit/review/mcp/memory 等 | ~15K |
| 07 | [权限系统：7 种权限模式](/claudecode/07-permissions) | 5 外部 + 2 内部权限模式、AI 分类器 | ~21K |
| 08 | [MCP 集成架构](/claudecode/08-mcp) | 4 种传输层、OAuth 认证、官方注册表 | ~18K |
| 09 | [Bridge 桥接与远程模式](/claudecode/09-bridge) | IDE 扩展通信、JSON-RPC、Remote Session | ~10K |
| 10 | [记忆系统与上下文注入](/claudecode/10-memory) | claude.md、memdir 持久化、SessionMemory | ~13K |

<p class="catalog-cta"><a href="claudecode/" class="VPButton medium brand">进入 Claude Code 专栏 →</a></p>

### DeepSeek Harness 源码精读

> DeepSeek 开源通用 agent harness 源码深度解析，基于 vendored Cordis 插件框架，四十余万行 TypeScript、两百多个 package

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [全景：一切皆插件](/deepseek/01-overview) | 无特权核心、profile/bundle 装配、vendored Cordis、规模实测 | ~8K |
| 02 | [Cordis 组合框架](/deepseek/02-cordis) | Context/effect/waterfall、Service 声明注入、per-agent scope、Loader+patch | ~12K |
| 03 | [Agent 接口与默认 loop](/deepseek/03-agent-loop) | step vs turn、turn 生命周期事件、inbox、waterfall vs serial、swappable | ~11K |
| 04 | [会话日志与上下文投影](/deepseek/04-session-log) | SessionEvent 追加日志、deriveMessages 投影、model-visible ⟺ logged、fork/resume | ~9K |
| 05 | [工具系统与执行管线](/deepseek/05-tools-pipeline) | 注册表与 scope、schema+prompt 组合、pre→execute→post、guard 守卫 | ~10K |
| 06 | [Capability 缝体系](/deepseek/06-capability-seams) | Seam 三元组、Provider 互换=整套移动、沙箱后端、approval 挂缝 | ~10K |
| 07 | [压缩/上下文注入/子代理](/deepseek/07-compaction-context-subagent) | 压缩可换缝、agent.inject 排队、ctx.subagents provider 注册表 | ~10K |
| 08 | [自改/hooks 桥/生态](/deepseek/08-self-modification-hooks) | self-referential cordis toolset、CC/Codex hooks 翻译桥、typed-Decision 拦截 | ~10K |

<p class="catalog-cta"><a href="deepseek/" class="VPButton medium brand">进入 DeepSeek Harness 专栏 →</a></p>

### Reading · 论文解读与随笔

> 走出源码逐行，站到更高一层——解读与 Agent 工程相关的论文，分享阅读笔记与工程随笔。首篇解读的是"Agent Skill 为何有效、又为何失效"。

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [Skill 到底为什么有效，又在哪失效](/reading/01-skill-paper) | Demystifying Agent Skills 论文解读：12 种 skill-use 模式、何时该用 / 何时有害 | ~4K |

<p class="catalog-cta"><a href="reading/" class="VPButton medium brand">进入 Reading 专栏 →</a></p>

</div>

> 本系列以**源码级精读**为核心目标，不是 "教你用"，而是 "告诉你为什么这么实现"。所有文章均基于真实源码逐行走读，配合架构流程图辅助理解。
