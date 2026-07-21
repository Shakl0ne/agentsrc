---
title: Codex 源码精读
---

# Codex 源码精读

> OpenAI 官方开源终端编程 Agent 源码深度解析

这里是「**Codex 源码精读**」专栏，逐行拆解 OpenAI 官方出品的本地编程 Agent。

不同于 Claude Code 和 OpenCode，Codex 选择 **Rust** 作为核心语言，拥有更激进的跨平台沙箱、完整的 Agent 树编排、以及独特的 event-driven reactor 主循环。本系列逐章拆解它的 100+ crates，讲清架构选型背后的工程哲学。

## 文章列表

| # | 文章 | 类型 | 字数 |
|---|---|------|------|
| 1 | [Codex 全景：架构与定位](/codex/01-overview) | 源码解析 | ~16,000 字 |
| 2 | [主循环：Submission 驱动的 Turn 系统](/codex/02-mainloop) | 源码解析 | ~13,000 字 |
| 3 | [上下文组合与增量注入](/codex/03-context) | 源码解析 | ~14,000 字 |
| 4 | [Compact 系统：3 种压缩机制](/codex/04-compact) | 源码解析 | ~19,000 字 |
| 5 | [多 Agent 编排架构](/codex/05-multi-agents) | 源码解析 | ~25,000 字 |
| 6 | [工具系统与安全沙箱](/codex/06-tools-sandbox) | 源码解析 | ~23,000 字 |
| 7 | [模型管理与 Provider 抽象](/codex/07-models) | 源码解析 | ~20,000 字 |
| 8 | [Codex vs CC 设计哲学对比](/codex/08-philosophy) | 源码解析 | ~14,000 字 |

## 关于系列

本系列基于 Codex 源码仓库（[GitHub](https://github.com/openai/codex)）编写，核心分析范围：

- **源码层**：`codex-rs/` ≈ **100+ 个 crate**，核心在 `codex-core`
- **构建系统**：Bazel + Cargo（工程规模相当但构建哲学不同）
- **技术栈**：Rust + tokio async + ratatui + SQLite + MCP + WebSocket
- **特色**：每篇文章都附带「Codex vs Claude Code」架构对比

## 整体架构速览

以下是 Codex 核心引擎 `codex-core` 的主循环流程预览：

```mermaid
flowchart TD
    A[用户输入 / 系统事件] -->|async_channel| B[submission_loop]
    B -->|Op::UserTurn| C[run_turn]
    B -->|Op::Compact| D[CompactTask]
    B -->|Op::Interrupt| E[Abort Task]
    C --> F[build_initial_context]
    C --> G[Token Check]
    G -->|超限| H[run_auto_compact]
    H -->|压缩完成| C
    G -->|正常| I[LLM 采样]
    I --> J[工具调用]
    J --> K[Tool Dispatch]
    K -->|pre-hook| L[ExecPolicy 审查]
    K -->|execute| M[SandboxManager]
    K -->|post-hook| N[结果写回]
    I -->|stop| O[响应返回]
```

## 三个终端 Agent 对比

本博客同时覆盖 Claude Code、OpenCode 和 Codex，三者的核心差异速览：

| 维度 | Claude Code | OpenCode | Codex |
|------|------------|----------|-------|
| 语言 | TypeScript | TypeScript | Rust |
| 主循环 | continuation-driven polling | while-true loop with Effect-TS | event-driven reactor (channel) |
| 沙箱 | 无 | 无 | 跨平台沙箱 |
| 多 Agent | 无原生支持 | task 工具 spawn | 原生 Agent Tree |
| 压缩 | 5 级（前 4 级纯数据结构） | 2 级 | 3 种实现（全部调 LLM） |
| 认证 | API Key | API Key | ChatGPT OAuth + API Key |
| IDE 集成 | 终端内使用 | 终端内使用 | App Server 守护进程 |

---

> 想了解 Claude Code 和 OpenCode？请移步 [OpenCode 源码精读](/opencode/)。
