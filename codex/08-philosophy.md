# Codex vs Claude Code：设计哲学对比

## 〇、引言

这是 Codex 系列的最后一篇。前 7 篇我们逐个子系统看了 Codex 的实现——主循环、上下文、Compact、多 Agent、工具/沙箱、模型管理。每一篇都做了和 CC 的对比，但都是局部的。

这一篇做全局对比，但**不堆砌表格**。我想从设计哲学的角度切入，回答一个核心问题：

> **两个同样优秀的工程团队，做出来的东西为什么差异这么大？**

我的核心观点是：**这些差异不是随机的，它们源于两个团队对"AI Agent 是什么"的不同假设**。一旦你理解了这些假设，所有差异都有了统一的解释。

![两种架构支柱：CC queryLoop vs Codex submission_loop](/images/codex/08-hero.png)


## 一、哲学的差异：3 个核心假设

### 1.1 假设一：Agent 是"工具调用循环"还是"任务编排系统"？

**CC 的假设：Agent = 工具调用循环**

CC 的核心是 `queryLoop()`（`query.ts:200-1677`，~1,477 行）——一个 async generator，while-true 拉模型响应、执行工具、循环。所有功能都嵌在这个循环里。

这种设计的优势：

- 简单直接，状态隐式保持在 generator 的栈帧里
- 单进程单线程，没有并发问题
- 易于理解和调试（除了 1,477 行的长度）

劣势：

- 想加多 Agent 需要重构基础架构
- 想加并行执行需要改变核心循环
- 长会话下性能优化受限

**Codex 的假设：Agent = 任务编排系统**

Codex 的核心是 `submission_loop`（`handlers.rs:738`）——一个事件驱动的 reactor，接收 `Op` 枚举的消息分派到不同 handler。每个 Op 可能 spawn 一个独立的 `SessionTask`，task 内部又有自己的循环。

这种设计的优势：

- 天然支持多 Agent（每个 Agent 是独立的 thread + 自己的 submission_loop）
- 多协程并发执行（tokio runtime）
- 子系统解耦（Compact / Tool / Multi-agent 通过 Op 解耦）

劣势：

- 代码更复杂（理解 submission_loop + SessionTask + run_turn 三层架构需要时间）
- 调试更难（异步协程堆栈）
- 状态管理需要显式锁（`Arc<Mutex<SessionState>>`）

### 1.2 假设二：Agent 在"可信环境"还是"不可信环境"运行？

**CC 的假设：可信环境**

CC 没有沙箱、没有 ExecPolicy、没有进程加固。用户对自己的命令负责。

这反映了 CC 的目标场景：**开发者本地使用**。你在自己的笔记本上跑 CC，要 `rm -rf` 是你的事。

**Codex 的假设：不可信环境**

Codex 有：

- 跨平台沙箱（macOS Seatbelt / Linux Landlock+seccomp / Windows Restricted Token）
- ExecPolicy Starlark DSL 规则引擎
- 进程加固（pre_main_hardening）
- 网络规则限制出站连接
- `host_executable` 限定可执行路径

这反映了 Codex 的目标场景：**可能部署到 CI / 共享环境 / 远程服务器**。OpenAI 想让 Codex 能被企业部署到生产环境，必须默认安全。

### 1.3 假设三：复杂任务怎么解决？

**CC 的假设：单 Agent + 长上下文**

CC 的策略是"一个 agent 解决所有问题"，通过：

- 200K context window（Claude 3.5+）
- 5 级压缩让上下文"虚拟"无限大
- 强大的工具调用循环

如果任务复杂，让 agent 跑更多轮，调用更多工具。

**Codex 的假设：多 Agent 分而治之**

Codex 的策略是"分解任务"，通过：

- 多 Agent 系统（V1 + V2 两套实现）
- AgentPath 树状寻址
- map-reduce 风格的 CSV 批处理（`spawn_agents_on_csv`）
- Agent 之间通过 `InterAgentCommunication` 通信

如果任务复杂，spawn 专门的子 agent 处理子任务，父 agent 协调。

![3 个核心假设：Tool Loop vs Orchestration / Trusted vs Default Security / Single vs Multi Agent](/images/codex/08-assumptions.png)


## 二、哲学差异的工程体现

### 2.1 语言选择：TypeScript vs Rust

这看似是技术选型，其实是哲学选择。

**CC 选 TypeScript** 因为：

- 开发速度快（动态类型 + 巨大生态）
- 与 npm / Node.js 生态无缝集成
- async generator 是天然的 agent 循环抽象
- 团队熟悉（Anthropic 内部很多 TS）

**Codex 选 Rust** 因为：

- 系统级能力（沙箱 / 进程加固 / WebSocket 都需要）
- 内存安全（避免 buffer overflow 等安全漏洞）
- 零成本抽象（性能可预测，对长会话重要）
- 多 crate 工程组织（100+ crate 强制模块化）

Rust 的选择反映了 Codex 的"系统级软件"定位——它不是"工具脚本"，而是"基础设施"。

### 2.2 主循环：polling vs reactor

CC 的 `queryLoop` 是 polling 模式：

```typescript
async function* queryLoop() {
  while (true) {
    const result = await callModel();
    yield result;
    if (result.stop) break;
  }
}
```

简单，但所有逻辑都耦合在这个函数里。

Codex 的 `submission_loop` 是 reactor 模式：

```rust
while let Ok(sub) = rx_sub.recv().await {
    match sub.op {
        Op::UserInput { .. } => user_input_or_turn(...).await,
        Op::Compact => compact(...).await,
        // ...
    }
}
```

复杂，但子系统解耦——加新功能只需要加新 Op 枚举值。

### 2.3 上下文管理：全量重发 vs 增量注入

CC 每次都重建完整 system prompt，依赖 API 服务端的 prompt cache。简单，但传输量大。

Codex 显式 diff（`build_settings_update_items`），只发送变更段。复杂，但网络传输小，cache 命中率高。

这反映了 Rust 团队对"显式优于隐式"的偏好——Codex 不愿意把 cache 行为交给 API 实现，而是自己控制。

### 2.4 Compact：渐进式 vs LLM-only

CC 的 5 级压缩策略：

- 前 4 级纯数据结构操作（截断 / Snip / Microcompact / Context Collapse）
- 第 5 级才调 LLM
- **大多数会话走不到第 5 级**

这是个"成本优先"的设计——能用便宜的操作省空间就绝不调 LLM。

Codex 的 3 种压缩实现：

- Local / Remote v1 / Remote v2 都调 LLM
- 选择哪种取决于 provider 能力
- 通过 `InitialContextInjection` 控制是否重注入初始上下文

这是个"质量优先"的设计——压缩必须可靠，宁可多调 LLM。

### 2.5 多 Agent：无 vs 有

CC 的 Task 工具：同进程协程，单层，无并行。

Codex 的多 Agent：独立 thread，递归 spawn，并行执行，路径寻址，消息传递，CSV 批处理。

这个差异**直接源于哲学假设**——CC 认为"单 agent 够用"，Codex 认为"复杂任务需要分解"。


## 三、哲学差异的连锁反应

一个核心假设的差异，会引发一连串工程决策的连锁反应。

### 3.1 "多 Agent" 假设的连锁反应

Codex 假设需要多 Agent → 必须有 Agent 通信 → 必须有 Agent 寻址（AgentPath）→ 必须有 Agent 拓扑持久化（AgentGraphStore）→ 必须有 Agent 状态机（7 种 AgentStatus）→ 必须有 completion watcher → ...

CC 没有这个假设，所以这些都不需要。简单。

### 3.2 "默认安全" 假设的连锁反应

Codex 假设需要默认安全 → 必须有沙箱 → 必须有跨平台沙箱抽象 → 必须有 ExecPolicy → 必须有 Starlark DSL → 必须有进程加固 → 必须有网络规则 → ...

CC 没有这个假设，所以这些都不需要。用户自己负责。

### 3.3 "多后端" 假设的连锁反应

Codex 假设需要支持多后端 → 必须有 Provider 抽象 → 必须有 ModelProviderInfo → 必须有 SharedModelsManager → 必须有 Model downshift compact → 必须有 OAuth flow → ...

CC 没有这个假设，所以这些都不需要。只支持 Anthropic。

![连锁反应：Default Security → Sandbox → ExecPolicy → Starlark → Process Hardening](/images/codex/08-chain.png)


## 四、哲学没有绝对优劣

我不想给"哪个哲学更好"下结论——因为**没有绝对优劣**，只有**适用场景**。

### 4.1 CC 的哲学适合什么场景

- **个人开发者本地使用**：不需要多 Agent，单进程够用
- **快速迭代新功能**：单后端、单进程、动态语言让开发速度快
- **长会话编程**：5 级压缩让上下文"虚拟"无限
- **Claude 模型用户**：单一后端深度优化

### 4.2 Codex 的哲学适合什么场景

- **企业部署**：默认安全，沙箱隔离
- **多模型环境**：OpenAI / Ollama / LM Studio / Bedrock 都能用
- **复杂任务编排**：多 Agent 分而治之
- **生产环境集成**：App Server 守护进程 + IDE 集成
- **批处理场景**：CSV 并行 spawn 100 个 agent

### 4.3 一个有意思的现象

**两个产品都成功了**。CC 是开发者最爱的 AI 编程工具之一；Codex 是 OpenAI 官方出品，企业用户众多。

这说明：**没有"正确"的哲学，只有"适合场景"的哲学**。如果你的用户是个人开发者，CC 的简单性是优势；如果你的用户是企业，Codex 的复杂性是必要的。


## 五、对未来 Agent 框架的启示

### 5.1 假设决定架构

最重要的启示：**架构差异源于假设差异**。

如果你想做一个新的 Agent 框架，先问自己 3 个问题：

1. Agent 是工具调用循环还是任务编排系统？
2. 在可信环境还是不可信环境运行？
3. 复杂任务用单 agent 还是多 agent？

这 3 个问题的答案决定了 80% 的架构。

### 5.2 显式 vs 隐式的权衡

Codex 倾向显式（diff 上下文、显式 Op 枚举、显式 AgentPath），CC 倾向隐式（依赖 API cache、隐式 control flow、隐式 Task 工具）。

显式的好处是可控，劣势是复杂。隐式的好处是简单，劣势是不可控。

工程上，**先隐式快速验证，再根据需要显式化**——这是 CC 现在做的事（很多原本隐式的功能在重构为显式）。

### 5.3 多 Agent 是必然趋势

CC 现在的 Task 工具是"伪多 Agent"——同进程协程。但社区强烈要求真多 Agent。CC 团队正在重构中。

Codex 的多 Agent 系统已经成熟，V2 还在迭代。

**未来的 Agent 框架大概率都会支持真多 Agent**——这是 Codex 走在前面的一个领域。

### 5.4 安全将成为默认

随着 Agent 被部署到越来越多场景（CI / 服务器 / 共享环境），**默认安全将成为必需**。

CC 现在的不沙箱策略在企业场景下是劣势。未来可能需要补上。

Codex 的沙箱 + ExecPolicy 是一个值得参考的设计。


## 六、总结：两张全景图

### 6.1 CC 的全景

```
queryLoop (async generator)
  ├── 等待用户输入
  ├── 调模型
  ├── 执行工具（嵌在循环里）
  ├── 5 级压缩（前 4 级纯数据结构，第 5 级 LLM）
  ├── Stop Hooks
  └── 循环

辅助系统：
  ├── Skills（指令注入）
  ├── Hooks（外部钩子）
  ├── MCP（外部工具）
  └── Task（伪多 Agent）

哲学：单进程、单线程、单后端、可信环境
```

### 6.2 Codex 的全景

```
submission_loop (event reactor)
  ├── Op::UserInput → spawn RegularTask
  ├── Op::Compact → spawn CompactTask
  ├── Op::Interrupt → abort current task
  └── 20+ 种 Op 类型

SessionTask (4 种实现)
  └── run_turn (8 阶段生命周期)
       ├── build_initial_context (13 段) / build_settings_update_items (diff 6 段)
       ├── run_pre_sampling_compact (3 种压缩实现)
       ├── run_sampling_request (WebSocket + sticky routing)
       ├── mid-turn compact (token_limit_reached && needs_follow_up)
       ├── 工具执行 pipeline (pre-hook → execute → post-hook)
       │    └── ExecPolicy check + Sandbox wrap
       ├── run_turn_stop_hooks
       └── loop

辅助系统：
  ├── AgentControl + AgentPath (多 Agent V1/V2)
  ├── AgentGraphStore (SQLite 拓扑持久化)
  ├── McpConnectionManager (MCP 集成)
  ├── SharedModelsManager (4 种 Provider)
  ├── AuthManager (4 种认证)
  ├── SandboxManager (跨平台沙箱)
  ├── ExecPolicyManager (Starlark 规则)
  └── Process Hardening (进程加固)

哲学：多进程、多协程、多后端、默认安全、多 Agent
```

![CC vs Codex 两套全景系统对比](/images/codex/08-panorama.png)


## 七、致谢与告别

这个系列从 OpenCode 开始，到 Codex 结束。三套 Agent 框架（OpenCode / Claude Code / Codex）从不同角度展示了"如何构建一个 AI 编程代理"。

每个框架都有自己的哲学，每个哲学都有自己的适用场景。理解这些差异，比记住某个 API 重要得多——因为 API 会变，哲学会传承。

希望这个系列对你有帮助。


## 八、系列文章索引

| # | 文章 | 主题 |
|---|------|------|
| 1 | [全景：架构与定位](/codex/01-overview) | 整体架构、3 个二进制、~100 个 crate |
| 2 | [主循环：Submission 驱动的 Turn 系统](/codex/02-mainloop) | 事件 reactor、SessionTask、8 阶段 turn |
| 3 | [上下文组合与增量注入](/codex/03-context) | 13 个上下文段、context diffing、prompt cache |
| 4 | [Compact 3 种压缩机制](/codex/04-compact) | Local / Remote v1 / v2、InitialContextInjection |
| 5 | [多 Agent 编排架构](/codex/05-multi-agents) | AgentPath、V1/V2、CSV 批处理 |
| 6 | [工具系统与安全沙箱](/codex/06-tools-sandbox) | ToolExecutor、MCP、ExecPolicy、沙箱 |
| 7 | [模型管理与 Provider 抽象](/codex/07-models) | 4 种 Provider、AuthManager、WebRTC |
| 8 | [Codex vs CC 设计哲学对比](/codex/08-philosophy) | 3 个核心假设、连锁反应、未来启示 |

## 章节小测

<script setup>
const q = [
  {
    question: '文章提出的 3 个核心假设差异中，哪一个最能解释 Codex 和 CC 在架构上的根本分歧？',
    options: ['两种编程语言在性能与开发效率之间的根本性选型分歧', '对 Agent 定位环境假设及任务拆分策略的差异决定 80% 架构', '开源社区协作与闭源商业产品在开发模式上的天然差异', '两个团队在社区贡献度和代码审查标准上存在不同传统'],
    correct: 1,
    explanation: '这三个假设回答了"AI Agent 是什么"这个核心问题。CC 认为 Agent=工具调用循环、可信环境、单 Agent；Codex 认为 Agent=任务编排系统、不可信环境、多 Agent。文章核心观点是这些假设差异不是随机的，而是源于团队对 Agent 本质的不同理解。'
  },
  {
    question: 'Codex 的"多 Agent"假设引发了怎样的连锁工程反应？',
    options: ['在单 Agent 架构基础上增加 spawn 工具即可完成集成', '多 Agent 假设引发寻址持久化状态机等连锁子系统', '该假设的影响范围仅限于 V2 版本功能且 V1 不支持', '多 Agent 假设仅改变通信方式对整体架构影响比较有限'],
    correct: 1,
    explanation: '核心假设会引发连锁反应：假设需要多 Agent，就必须有 Agent 间通信机制，因此设计了 AgentPath 寻址；有寻址就需要持久化拓扑（AgentGraphStore）；有通信就需要状态机（7 种 AgentStatus）；有子 Agent 就需要完成通知（completion watcher）。CC 没有这个假设所以这些都不需要。'
  },
  {
    question: '文章认为 CC 和 Codex 哪种设计哲学更好？',
    options: ['Codex 架构更安全因此整体上设计更优更好', 'CC 架构更简洁因此整体上设计更优更好', '没有绝对优劣本地开发选 CC 企业部署选 Codex', '两者均不够完善需在未来版本中持续改进'],
    correct: 2,
    explanation: '文章核心观点是"没有正确的哲学，只有适合场景的哲学"：CC 的简单性对个人开发者是优势，Codex 的复杂性对企业部署是必要的。两个产品都成功了，说明不同哲学适应不同场景。'
  },
  {
    question: '文章认为未来 Agent 框架的两个必然趋势是什么？',
    options: ['使用 Rust 和 TypeScript 混合架构统一两大技术栈优势', '多 Agent 为默认配置且安全策略引擎为必备基础设施', '全量上下文发送与 LLM-only 压缩成为行业新标准', 'WebSocket 与 HTTP 混合传输成为 Agent 通信新架构'],
    correct: 1,
    explanation: '文章指出两个趋势：真多 Agent 是必然（CC 社区强烈要求，CC 团队正在重构中；Codex 已成熟）；默认安全将成为必需（随着 Agent 部署到 CI/共享环境，CC 的不沙箱策略在企业场景下是劣势）。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
