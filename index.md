---
layout: home

hero:
  text: AI Agent 源码精读
  tagline: 逐行拆解最先进的终端编程 Agent — Claude Code × OpenCode × Codex 架构设计、实现原理与工程哲学
---

## 导读目录

本博客覆盖 **Claude Code**、**OpenCode** 和 **Codex** 三款主流终端编程 Agent。OpenCode（TypeScript）和 Codex（Rust）是当下最先进的两款开源终端编程 Agent，Claude Code 则是 Anthropic 官方闭源实现——三者的源码在本站均有深度解读。

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

</div>

> 本系列以**源码级精读**为核心目标，不是 "教你用"，而是 "告诉你为什么这么实现"。所有文章均基于真实源码逐行走读，配合架构流程图辅助理解。
