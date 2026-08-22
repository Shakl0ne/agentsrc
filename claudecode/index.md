---
title: Claude Code 源码精读
---

# Claude Code 源码精读

> Anthropic 官方终端编程 Agent 源码深度解析

这里是「**Claude Code 源码精读**」专栏，基于泄漏源码分析 Claude Code 的架构设计与核心机制。

Claude Code 是 Anthropic 官方出品的终端 AI 编程助手，用 TypeScript 编写、Bun 运行时驱动。其源码规模约 **51 万行**、~1,900 个文件，是当前最成熟的终端编程 Agent 之一。

本系列从源码组织结构出发，逐层拆解其核心模块的设计思路与实现细节。

## 文章列表

| # | 文章 | 主题 | 字数 |
|---|------|------|------|
| 1 | [整体架构与启动流程](/claudecode/01-overview) | 51 万行源码全景、Bun 运行时、React + Ink 终端 UI、分层架构 | |
| 2 | [主循环：QueryEngine 与 Continuation-Driven 架构](/claudecode/02-mainloop) | 流式 API 调用循环、工具调度、continuation-based turn 管理 | |
| 3 | [工具系统：40+ 内置工具](/claudecode/03-tools) | Tool 接口设计、20+ 工具目录、MCP 扩展 | |
| 4 | [对话压缩：5 级压缩机制](/claudecode/04-compact) | autoCompact / microCompact / apiMicrocompact / reactiveCompact / sessionMemoryCompact | |
| 5 | [Agent 系统：单 Agent 调度与 Swarm 协作](/claudecode/05-agents) | AgentTool、Coordinator、Teammate 模式、Agent Swarms | |
| 6 | [命令系统：70+ 斜杠命令与 CLI 参数](/claudecode/06-commands) | Commander.js 解析、commit/review/mcp/memory 等命令实现 | |
| 7 | [权限系统：7 种权限模式](/claudecode/07-permissions) | 5 外部 + 2 内部权限模式、AI 分类器、denial tracking | |
| 8 | [MCP 集成架构](/claudecode/08-mcp) | 4 种传输层、OAuth 认证、官方注册表、插件系统 | |
| 9 | [Bridge 桥接与远程模式](/claudecode/09-bridge) | IDE 扩展通信、JSON-RPC over WebSocket、Remote Session、SDK 适配层 | |
| 10 | [记忆系统与上下文注入](/claudecode/10-memory) | claude.md、memdir 持久化、SessionMemory、MagicDocs、AutoDream | |

## 关于本系列

本系列基于 Claude Code 泄漏源码分析，源码引用详见 `agent/claude-code-main/` 目录。

### 核心分析路径

| 层级 | 对应目录 | 规模 |
|------|---------|------|
| **入口与引导** | `src/main.tsx` | 4,683 行 |
| **主循环** | `src/QueryEngine.ts` + `src/query.ts` | ~3K 行 |
| **工具系统** | `src/tools/` (43 子目录) + `src/Tool.ts` | ~800 行接口 + ~40 工具 |
| **命令系统** | `src/commands/` (~40 目录) + `src/commands.ts` | ~750 行注册 + 50 命令 |
| **状态管理** | `src/state/` | 6 文件 |
| **服务层** | `src/services/` (36 目录) | 包括 API / MCP / LSP / Compact 等 |
| **Hook 层** | `src/hooks/` (85 文件) | React Hook 驱动的业务逻辑 |
| **UI 层** | `src/screens/` + `src/components/` | React + Ink 终端组件 |
| **IDE 桥接** | `src/bridge/` (31 文件) | JSON-RPC over WebSocket |
| **记忆系统** | `src/memdir/` | 跨会话持久化 |
| **任务系统** | `src/tasks/` (7 文件) | 4 种后台任务类型 |

### 特色

- 每篇文章附带与其他框架（OpenCode / Codex）的架构对比
- 基于真实源码的逐行走读
- 以架构图 + 流程图为辅助理解工具

## 架构速览

![Claude Code 主循环：QueryEngine 流式调用，分流渲染 / 工具执行 / 完成](/images/claudecode/article-index-architecture.svg)