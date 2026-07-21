---
layout: home

hero:
  text: AI Agent 源码精读
  tagline: 逐行拆解最先进的开源终端编程 Agent — OpenCode × Codex 架构设计、实现原理与工程哲学
  actions:
    - theme: brand
      text: OpenCode
      link: /opencode/
    - theme: brand
      text: Codex
      link: /codex/
    - theme: alt
      text: GitHub
      link: https://github.com/Shakl0ne
---

## 导读目录

本博客覆盖 Claude Code、OpenCode 和 Codex 三款主流终端编程 Agent。OpenCode（TypeScript）和 Codex（Rust）是当下最先进的两款开源终端编程 Agent，本系列逐章对它们的源码进行深度解读。

<div class="catalog-grid">

### OpenCode 源码精读

> 开源版 Claude Code 终端编程 Agent 源码深度解析，基于 TypeScript + Effect-TS 函数式架构

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 01 | [整体架构源码精读](/opencode/01-overview) | ~100K 行 TS 源码全景、Effect-TS 依赖注入、双运行时架构 | ~10K |
| 02 | [主循环 runLoop 源码精读](/opencode/02-runloop) | while(true) 7 步循环、Doom Loop 检测、流式工具执行 | ~12K |
| 03 | [工具系统源码精读](/opencode/03-tools) | Tool.Def 接口、Edit 引擎 10 策略、Permission 三态权限 | ~12K |
| 04 | [上下文压缩源码精读](/opencode/04-compact) | 2 级压缩（Prune + Compact）、锚定摘要、消息重排 | ~12K |
| 05 | [Agent 系统源码精读](/opencode/05-agents) | SubAgent 隔离调度、tasks.pop() 串行模型、与 CC 对比 | ~12K |
| 06 | [上下文架构源码精读](/opencode/06-context) | 5 层上下文注入、指令文件 + Skill 系统、为什么不用 RAG | ~11K |

<p class="catalog-cta"><a href="/opencode/" class="VPButton medium brand">进入 OpenCode 专栏 →</a></p>

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

<p class="catalog-cta"><a href="/codex/" class="VPButton medium brand">进入 Codex 专栏 →</a></p>

</div>

### 推荐阅读路线

| 路线 | 适合谁 | 顺序 |
|------|--------|------|
| **新手路线** | 第一次接触终端编程 Agent 源码 | OpenCode 01 → 02 → 03 |
| **对比路线** | 想深入理解同主题不同实现 | [04 压缩对比](/opencode/04-compact) ↔ [04 Compact](/codex/04-compact)；[05 Agent](/opencode/05-agents) ↔ [05 多 Agent](/codex/05-multi-agents) |
| **面试路线** | 准备 AI Agent 岗位面试 | 每篇末尾的 "vs Claude Code" 对比章节 + [08 设计哲学](/codex/08-philosophy) |
| **全通路线** | 想把两个框架彻底吃透 | 按编号顺序读完 OpenCode 6 篇 → Codex 8 篇 |

---

## 三个终端编程 Agent 对比

| 维度 | Claude Code | OpenCode | Codex |
|------|------------|----------|-------|
| 语言 | TypeScript | TypeScript | Rust |
| 主循环 | continuation-driven polling | while-true loop with Effect-TS | event-driven reactor (channel) |
| 沙箱 | 无 | 无 | 跨平台沙箱 |
| 多 Agent | 无原生支持 | task 工具 spawn | 原生 Agent Tree |
| 压缩 | 5 级（前 4 级纯数据结构） | 2 级 | 3 种实现（全部调 LLM） |
| IDE 集成 | 终端内使用 | 终端内使用 | App Server 守护进程 |

> 本系列以**源码级精读**为核心目标，不是 "教你用"，而是 "告诉你为什么这么实现"。所有文章均基于真实源码逐行走读，配合架构流程图辅助理解。

---

## 关于作者

你好，我是 **shaco**，一名专注 AI 基础设施与系统工程的工程师。

本站内容是我对 OpenCode 和 Codex 两款开源终端编程 Agent 的源码逐行阅读笔记。写作初衷很简单：当时 Claude Code 刚出来不久，市面上没有系统讲解 Agent 源码实现的内容，而我又恰好在啃这两个代码库。既然已经读完了，不如整理成文，给后来的人撑把伞。

如果你在学习过程中发现问题，或者有改进建议，欢迎通过以下方式联系：

- GitHub: [Shakl0ne](https://github.com/Shakl0ne)
- 本站文章基于 OpenCode ([opencode-ai/opencode](https://github.com/opencode-ai/opencode)) 和 Codex ([openai/codex](https://github.com/openai/codex)) 官方仓库编写

> 本站所有内容均为个人学习笔记，如与官方实现有出入，以官方源码为准。
