---
layout: home

hero:
  text: AI Agent 源码精读
  tagline: 逐行拆解 AI Agent 源码 — Claude Code × OpenCode × Codex × DeepSeek Harness 架构设计、实现原理与工程哲学
---

## 导读目录

本博客覆盖 **Claude Code**、**OpenCode** 和 **Codex** 三款主流终端编程 Agent，外加 **DeepSeek Harness** 这一通用 agent harness。OpenCode（TypeScript）和 Codex（Rust）是当下最先进的两款开源终端编程 Agent，Claude Code 则是 Anthropic 官方闭源实现——四者的源码在本站均有深度解读。其中 dsh 以"一切皆插件"的架构思路提供了与前三者不同的引擎视角。

<div class="catalog-grid">

<div class="catalog-card">

### OpenCode 源码精读

> 开源版 Claude Code 终端编程 Agent 源码深度解析，基于 TypeScript + Effect-TS 函数式架构（`packages/opencode` ≈ 86K 行、`packages/core` ≈ 12K 行，配套 Drizzle ORM），每篇附 OpenCode vs Claude Code 架构对比

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [整体架构](/opencode/01-overview) | ~100K 行 TS 源码全景、Effect-TS 依赖注入、双运行时架构 | ~7K |
| 02 | [主循环 runLoop](/opencode/02-runloop) | while(true) 7 步循环、Doom Loop 检测、流式工具执行 | ~15K |
| 03 | [工具系统](/opencode/03-tools) | Tool.Def 接口、Edit 引擎 10 策略、Permission 三态权限 | ~15K |
| 04 | [上下文压缩](/opencode/04-compact) | 2 级压缩（Prune + Compact）、锚定摘要、消息重排 | ~14K |
| 05 | [Agent 系统](/opencode/05-agents) | SubAgent 隔离调度、tasks.pop() 串行模型、与 CC 对比 | ~13K |
| 06 | [上下文架构](/opencode/06-context) | 5 层上下文注入、指令文件 + Skill 系统、为什么不用 RAG | ~13K |
| 07 | [plan-execute-verify 编排](/opencode/07-plan-execute-verify) | 规划/执行/验证三阶段编排、oh-my-openagent 机制解析 | ~13K |

<p class="catalog-cta"><a href="opencode/01-overview" class="VPButton medium brand">开始阅读 →</a></p>

</div>

<div class="catalog-card">

### Codex 源码精读

> OpenAI 官方开源终端编程 Agent 源码深度解析，基于 Rust 的 ~100 crates 工程体系（核心 `codex-core`，配套 tokio async + ratatui + SQLite + MCP），每篇附 Codex vs Claude Code 架构对比

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [全景：架构与定位](/codex/01-overview) | 3 个二进制、~100 crate、TUI / exec / App Server 三模式 | ~16K |
| 02 | [主循环：Submission 驱动](/codex/02-mainloop) | 事件 Reactor、SessionTask、8 阶段 Turn 生命周期 | ~13K |
| 03 | [上下文组合与增量注入](/codex/03-context) | 13 个上下文段、context diffing、prompt cache | ~14K |
| 04 | [Compact 3 种压缩机制](/codex/04-compact) | Local / Remote v1 / v2、InitialContextInjection | ~19K |
| 05 | [工具系统与 MCP 双向集成](/codex/05-tools-mcp) | ToolExecutor、MCP 客户端/服务端双向集成 | ~12K |
| 06 | [安全架构：策略指令与 OS 沙箱](/codex/06-sandboxing) | ExecPolicy、Bubblewrap/Seccomp 进程隔离 | ~11K |
| 07 | [多 Agent 编排：V1/V2 路由机制](/codex/07-multi-agents) | Agent Path、V1/V2 协作与通信机制 | ~12K |
| 08 | [App Server：协议边界与解耦](/codex/08-app-server) | JSON-RPC 通信、IDE 解耦与后台守护进程 | ~12K |

<p class="catalog-cta"><a href="codex/01-overview" class="VPButton medium brand">开始阅读 →</a></p>

</div>

<div class="catalog-card">

### Claude Code 源码精读

> Anthropic 官方终端编程 Agent 源码深度解析，基于已泄露源码，TypeScript + Bun + React + Ink 技术栈，源码规模约 51 万行、~1,900 文件

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [整体架构与启动流程](/claudecode/01-overview) | 51 万行源码全景、Bun 运行时、React + Ink 终端 UI | ~15K |
| 02 | [主循环：QueryEngine](/claudecode/02-mainloop) | continuation-driven 轮询、流式 API 调用循环 | ~16K |
| 03 | [工具与命令系统](/claudecode/03-tools) | Tool 接口、装配线、斜杠命令三态执行 | ~15K |
| 04 | [对话压缩：5 级机制](/claudecode/04-compact) | auto/micro/apiMicro/reactive/sessionMemory 五级 | ~16K |
| 05 | [Agent 系统](/claudecode/05-agents) | AgentTool 与多级协作 | ~19K |
| 06 | [权限系统：7 种权限模式](/claudecode/06-permissions) | 5 外部 + 2 内部权限模式、AI 分类器 | ~21K |
| 07 | [MCP 集成架构与 Bridge](/claudecode/07-mcp) | 4 种传输层、OAuth 认证、Bridge 桥接 | ~18K |
| 08 | [记忆系统与上下文注入](/claudecode/08-memory) | CLAUDE.md 六层注入、memdir、AutoDream 空闲整合 | ~13K |

<p class="catalog-cta"><a href="claudecode/01-overview" class="VPButton medium brand">开始阅读 →</a></p>

</div>

<div class="catalog-card">

### DeepSeek Harness 源码精读

> DeepSeek 开源通用 agent harness 源码深度解析：一切皆插件、无特权核心，307 个包 / 约 75 万行 TypeScript 的 Cordis 插件树，只讲它独有的机制，每篇附跨框架架构对比

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [全景：一切皆插件](/deepseek/01-overview) | 无特权核心、profile/bundle 装配、一棵树长出五种产品形态 | ~8K |
| 02 | [Cordis 组合框架](/deepseek/02-cordis) | Context/effect、waterfall、Service 注入、scope、Loader+patch | ~11K |
| 03 | [agent-loop：可换的默认驱动](/deepseek/03-agent-loop) | turn/step 事件流、单一 inbox 三输入、waterfall vs serial | ~11K |
| 04 | [会话日志与上下文投影](/deepseek/04-session-log) | deriveMessages 投影、model-visible ⟺ logged、压缩的 surface replace | ~9K |
| 05 | [capability 缝与执行世界](/deepseek/05-capability-seams) | 三件套、执行世界、provider 整套迁移、工具裁决链 | ~8K |
| 06 | [扩展的三个极致](/deepseek/06-extensions) | 自改 toolset、CC/Codex hooks 翻译桥、跨产品 subagent 委托 | ~8K |

<p class="catalog-cta"><a href="deepseek/01-overview" class="VPButton medium brand">开始阅读 →</a></p>

</div>

<div class="catalog-card">

### Reading · 论文解读与随笔

> 走出源码逐行，站到更高一层——解读与 Agent 工程相关的论文，分享阅读笔记与工程随笔。

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [Skill 到底为什么有效，又在哪失效](/reading/01-skill-paper) | Demystifying Agent Skills 论文解读：12 种 skill-use 模式、何时该用 / 何时有害 | ~4K |
| 02 | [LLM 验证的「自动驾驶等级」](/reading/02-verification-autonomy) | Grading the Graders 论文解读：验证自主性等级 VAL L0-L5、完备性盲区 | ~4K |

<p class="catalog-cta"><a href="reading/01-skill-paper" class="VPButton medium brand">开始阅读 →</a></p>

</div>

</div>

> 本系列以**源码级精读**为核心目标，不是 "教你用"，而是 "告诉你为什么这么实现"。所有文章均基于真实源码逐行走读，配合架构流程图辅助理解。
