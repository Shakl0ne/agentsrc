---
title: OpenCode 源码精读
---

# OpenCode 源码精读

> 开源版 Claude Code 源码深度解析

这里是「**OpenCode 源码精读**」专栏，逐行拆解这款最强开源终端编程 Agent 的架构设计与实现原理。

无论是面试被追问 Agent 系统的实现细节，还是日常想深入理解 AI 编程工具的源码，都能在这里找到答案。

## 文章列表

| # | 文章 | 类型 | 字数 |
|---|---|------|------|
| 1 | [整体架构：5 万行源码全景](/opencode/01-overview) | 源码解析 | ~7,000 字 |
| 2 | [主循环 runLoop](/opencode/02-runloop) | 源码解析 | ~15,000 字 |
| 3 | [工具系统：20+ 内置工具设计](/opencode/03-tools) | 源码解析 | ~15,000 字 |
| 4 | [上下文压缩：Compact 2 级机制](/opencode/04-compact) | 源码解析 | ~14,000 字 |
| 5 | [Agent 系统：SubAgent 与 Claude Code 对比](/opencode/05-agents) | 源码解析 | ~13,000 字 |
| 6 | [上下文架构：5 层上下文注入](/opencode/06-context) | 源码解析 | ~13,000 字 |
| 7 | [plan-execute-verify 编排机制](/opencode/07-plan-execute-verify) | 源码解析 | ~13,000 字 |

## 关于系列

本系列基于 OpenCode 源码仓库（[GitHub](https://github.com/opencode-ai/opencode)）编写，核心分析范围：

- **源码层**：`packages/opencode/src/` ≈ **86,715 行** TypeScript
- **抽象层**：`packages/core/src/` ≈ **12,405 行**
- **技术栈**：TypeScript + Effect-TS + AI SDK (Vercel) + Drizzle ORM
- **特色**：每篇文章都附带「OpenCode vs Claude Code」架构对比

## 整体架构速览

![OpenCode Monorepo 包拓扑：CLI 入口 → packages/opencode 实现枢纽 → packages/core 契约底座](/images/opencode/article-index-architecture.png)
