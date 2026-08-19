---
title: DeepSeek Harness 源码精读
---

# DeepSeek Harness 源码精读

> DeepSeek 开源通用 agent harness 源码深度解析

这里是「**DeepSeek Harness 源码精读**」专栏，逐层拆解 DeepSeek 这套「一切皆插件」的通用 agent 引擎——从插件框架、主循环、会话日志，到能力缝体系、长会话扩展与生态桥接。

无论你是想理解「插件式 agent 运行时」该如何设计，还是想看看一个能把模型适配、上下文、压缩、子代理全部做成可换缝的引擎长什么样，都能在这里找到答案。

## 关于本系列

本系列基于 **`dsh-v0.1.0-rc.7`**（master @ `99f6f02`，2026-08-17）编写。项目处于 developer preview，迭代很快，文中机制以该基线为准。

- **源码层**：`packages/` 约 **40+ 万行** TypeScript、**220+ 个 package**（按组分布：core / api / llm / shell / subprocess / fs / lsp / web / compaction / context / subagent / hooks / session / workflow …）
- **技术底座**：vendored Cordis（事件 / 插件 / 生命周期）+ TypeScript
- **设计主线**：capability 缝（Service Definition / Provider / Consumer）+ 会话日志（"模型可见 ⟺ 可重建"）+ 一切皆插件
- **特色**：每篇都附「OpenCode / Codex / Claude Code 对比」，以及「可变缝」视角的统一分析

## 文章列表

| # | 文章 | 类型 | 字数（约） |
|---|---|---|---|
| 1 | [DeepSeek Harness 全景：一个"一切皆插件"的通用 agent harness](/deepseek/01-overview) | 源码解析 | ~8,000 |
| 2 | [Cordis 组合框架：几十个 package 靠什么拼成一个 agent](/deepseek/02-cordis) | 源码解析 | ~12,000 |
| 3 | [Agent 接口与默认 loop：一个 agent 到底怎么跑一个回合](/deepseek/03-agent-loop) | 源码解析 | ~11,000 |
| 4 | [会话日志与上下文投影：模型看到的上下文从哪来](/deepseek/04-session-log) | 源码解析 | ~9,000 |
| 5 | [工具系统与执行管线：模型怎么把一个工具请求变成真实行动](/deepseek/05-tools-pipeline) | 源码解析 | ~10,000 |
| 6 | [Capability 缝体系：为什么换一个实现能牵一发动全身](/deepseek/06-capability-seams) | 源码解析 | ~10,000 |
| 7 | [压缩 / 上下文注入 / 子代理：装配式的长会话扩展](/deepseek/07-compaction-context-subagent) | 源码解析 | ~10,000 |
| 8 | [自改 / hooks 桥 / 生态：agent 学会改自己、也接得住别家的协议](/deepseek/08-self-modification-hooks) | 源码解析 | ~10,000 |

## 文章顺序（建议按此阅读）

1. **全景** —— 篇开场把整座 harness 的地图画出来：几十个 package 怎么被 `dsh --profile` 组装成一个跑得起来的 agent。
2. **Cordis 组合框架** —— 核心的中枢、组合、全生命周期模型；理解它，才能读后面所有机制的"骨架"。
3. **Agent 接口与默认 loop** —— 一个回合（turn）怎么被拆成 message batch、推导、工具调用、结束判定。
4. **会话日志与上下文投影** —— 模型看到的上下文从哪来；日志如何做持久化与重建。
5. **工具系统与执行管线** —— 工具 request 怎么穿过 pre-execute / execute / post-execute 变成真实行动。
6. **Capability 缝体系** —— 换一个 Provider 为什么能牵一发动全身；Definition / Provider / Consumer 三件套。
7. **压缩 / 上下文注入 / 子代理** —— 长会话怎么活下来：可换缝 + `agent.inject()` 队列 + Provider 注册表。
8. **自改 / hooks 接口 / 生态** —— 我能改自己、也能原样接住 Claude Code / Codex 的 `hooks.json`；最后对比三栏收尾。