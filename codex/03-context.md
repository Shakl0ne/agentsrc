# 上下文组合与增量注入

## 〇、引言

上一篇我们看了 Codex 的主循环——`submission_loop` 事件分发 + `run_turn` 8 阶段生命周期。其中提到一个细节：在 Sampling 之前，Codex 会"记录上下文更新"。

但这个细节其实非常重要，值得单独写一篇。

Codex 的上下文管理有两个核心机制：

1. **10+ 个上下文段的组合**：每次构建 prompt 时，Codex 把 developer instructions、permissions、skills、plugins、environment 等十多个段组装成一条条消息
2. **Context diffing（增量注入）**：第一次发送完整上下文，之后只发送**变更的段**，最大化复用 prompt cache

这篇文章深入这两个机制，回答：

- Codex 把哪些信息塞进了模型上下文？
- 它怎么知道哪些段变了？
- 这个设计对性能和成本有什么影响？
- 和 CC 的 system prompt 构造对比？


![Codex 上下文组装流水线：13+ 段 → 3 个 PromptSlot → ResponseItems](/images/opencode/codex-03-hero.png)

## 一、build_initial_context：10+ 个段的组装

### 1.1 入口

入口在 `session/mod.rs:2725` 的 `build_initial_context`：

```rust
pub(crate) async fn build_initial_context(
    &self,
    turn_context: &TurnContext,
) -> Vec<ResponseItem> {
    let mut developer_sections = Vec::<String>::with_capacity(8);
    let mut contextual_user_sections = Vec::<String>::with_capacity(2);
    let mut separate_developer_sections = Vec::<String>::new();
    // ... 把 10+ 个段分别 push 到三个 Vec
}
```

它把上下文分成**三个槽位（PromptSlot）**：

| 槽位 | 合并方式 | 最终消息类型 |
|------|---------|-------------|
| `DeveloperPolicy` / `DeveloperCapabilities` | 全部合并为 1 条 | developer 消息 |
| `ContextualUser` | 全部合并为 1 条 | user 消息 |
| `SeparateDeveloper` | 每段独立成 1 条 | developer 消息（独立） |

### 1.2 13 个段

按 `build_initial_context` 的执行顺序，Codex 会注入这些段：

#### Developer sections（合并为 1 条 developer 消息）

| # | 段 | 来源 | 触发条件 |
|---|---|------|---------|
| 1 | Model switch message | `updates::build_model_instructions_update_item` | 模型切换时 |
| 2 | Permissions instructions | `PermissionsInstructions::from_permission_profile` | `include_permissions_instructions` |
| 3 | Developer instructions | `turn_context.developer_instructions` | 用户/Profile 指令非空 |
| 4 | Collaboration mode instructions | `CollaborationModeInstructions::from_collaboration_mode` | `include_collaboration_mode_instructions` |
| 5 | Realtime update | `updates::build_initial_realtime_item` | 实时对话激活 |
| 6 | Personality spec | `PersonalitySpecInstructions` | Feature::Personality + 未"baked" |
| 7 | Apps instructions | `AppsInstructions::from_connectors` | `include_apps_instructions` + apps_enabled |
| 8 | Available skills | `AvailableSkillsInstructions` | `include_skill_instructions` |
| 9 | Plugin instructions | `AvailablePluginsInstructions::from_plugins` | 总是检查 |
| 10 | Extension fragments | `context_contributors.contribute()` | 扩展注册了 contributor |

#### Contextual user sections（合并为 1 条 user 消息）

| # | 段 | 来源 |
|---|---|------|
| 11 | User instructions | `turn_context.user_instructions`（来自 AGENTS.md） |
| 12 | Environment context | `EnvironmentContext::from_turn_context`（shell / cwd / OS / subagents） |
| 13 | Extension fragments | context_contributors |

#### Separate developer sections（每段独立 1 条 developer 消息）

| # | 段 | 来源 |
|---|---|------|
| 14 | Extension fragments | context_contributors |
| 15 | Multi-agent v2 usage hint | `multi_agents::usage_hint_text` |
| 16 | Guardian policy prompt | `separate_guardian_developer_message` |

### 1.3 三个 Vec 怎么变成消息

函数末尾（`mod.rs:2912-2950`）：

```rust
let mut items = Vec::with_capacity(4);
// 1. 合并的 developer 消息
if let Some(developer_message) =
    crate::context_manager::updates::build_developer_update_item(developer_sections)
{
    items.push(developer_message);
}
// 2. 每个单独的 developer 段
for section in separate_developer_sections {
    if let Some(developer_message) =
        crate::context_manager::updates::build_developer_update_item(vec![section])
    {
        items.push(developer_message);
    }
}
// 3. multi-agent usage hint（如有）
// 4. 合并的 contextual user 消息
if let Some(contextual_user_message) =
    crate::context_manager::updates::build_contextual_user_message(contextual_user_sections)
{
    items.push(contextual_user_message);
}
// 5. guardian 单独 developer 消息（如有）
items
```

最终 `build_initial_context` 返回一个 `Vec<ResponseItem>`，可能是 0~6 条消息。


## 二、Context Diffing：增量注入

### 2.1 为什么需要 diffing

每次 turn 都重新发送 13+ 个段会浪费 token——大多数段在会话过程中不变。Codex 的优化策略：

- **第一次**：发送完整上下文（`build_initial_context`）
- **之后每次**：只发送**变更的段**（`build_settings_update_items`）

### 2.2 决策点：record_context_updates_and_set_reference_context_item

决策在 `mod.rs:2984` 的 `record_context_updates_and_set_reference_context_item`：

```rust
pub(crate) async fn record_context_updates_and_set_reference_context_item(
    &self,
    turn_context: &TurnContext,
) {
    let reference_context_item = {
        let state = self.state.lock().await;
        state.reference_context_item()
    };
    let should_inject_full_context = reference_context_item.is_none();
    let context_items = if should_inject_full_context {
        self.build_initial_context(turn_context).await
    } else {
        // Steady-state path: append only context diffs to minimize token overhead.
        self.build_settings_update_items(reference_context_item.as_ref(), turn_context)
            .await
    };
    // ... 记录到 history 并更新 reference_context_item
}
```

`reference_context_item` 是上一次 turn 的 `TurnContext` 快照。如果它是 `None`（首次 turn 或者被 compact 清空了），走 full injection；否则走 diff path。

### 2.3 Diff 实现：build_settings_update_items

Diff 逻辑在 `context_manager/updates.rs:209`：

```rust
pub(crate) fn build_settings_update_items(
    previous: Option<&TurnContextItem>,
    previous_turn_settings: Option<&PreviousTurnSettings>,
    next: &TurnContext,
    shell: &Shell,
    exec_policy: &Policy,
    personality_feature_enabled: bool,
) -> Vec<ResponseItem> {
    let contextual_user_message = build_environment_update_item(previous, next, shell);
    let developer_update_sections = [
        build_model_instructions_update_item(previous_turn_settings, next),
        build_permissions_update_item(previous, next, exec_policy),
        build_collaboration_mode_update_item(previous, next),
        build_realtime_update_item(previous, previous_turn_settings, next),
        build_personality_update_item(previous, next, personality_feature_enabled),
    ]
    .into_iter().flatten().collect();
    // ... 合并成 1~2 条消息
}
```

每个 `build_xxx_update_item` 函数会对比 `previous` 和 `next` 的对应字段，**只有变化时才返回 Some**，否则返回 None 被 `flatten()` 过滤掉。

被 diff 的 6 个字段：

1. **Environment**（contextual user 消息）：shell / cwd / OS 变化
2. **Model instructions**：模型切换
3. **Permissions**：权限 profile / approval policy 变化
4. **Collaboration mode**：协作模式切换
5. **Realtime**：实时对话开始/结束
6. **Personality**：个性设置变化

注意：**skills 和 plugins 不在 diff 列表里**。它们一旦在 turn 开始时注入就保持不变，只能通过重新发起 turn 改变。这是个权衡——减少 diff 计算的复杂度，代价是 skills/plugins 列表变化需要新 turn。

![6 个 Diff 字段：Environment / Model / Permissions / Collaboration / Realtime / Personality](/images/opencode/codex-03-fields.png)

### 2.4 Reference context 何时重置

`reference_context_item` 会在两种情况下重置为 None，触发下一次 full injection：

1. **Mid-turn compaction**：当 `run_auto_compact` 执行时，会重新构建 history，旧的 reference 失效
2. **新会话/fork**：新会话或 fork 出来的子会话第一次 turn

这意味着：**Compact 之后第一次 turn 总是发完整上下文**——这是合理的，因为压缩后历史已经重建，需要新的 baseline。

![Full vs Incremental Diff：8K vs 500 tokens](/images/opencode/codex-03-diffing.png)


## 三、Prompt Caching 的收益

OpenAI 的 Responses API 支持 prompt caching：相同前缀的 prompt 只需计算一次。Codex 的 context diffing 设计直接利用了这一点。

### 3.1 增量注入 vs 全量重发

假设一个 turn 的总上下文是 8000 tokens：

| 策略 | 每次发送 tokens | 命中 cache | 增量 |
|------|----------------|-----------|------|
| 全量重发 | 8000 | 部分（前缀稳定时） | +0 |
| Codex 增量 | ~500（只变更段） | 几乎全部 | -7500 |

对长会话尤其重要——一个 30 轮的会话如果每轮都重发 8000 tokens 上下文，总成本会是 240K tokens；用 diffing 后只有第一轮 8000 + 后面 29×500 = 22.5K，节省 90%+。

### 3.2 为什么 diff 顺序很重要

注意 `build_settings_update_items` 里 developer 段的顺序：

```rust
let developer_update_sections = [
    build_model_instructions_update_item(...),  // 1. 模型指令
    build_permissions_update_item(...),         // 2. 权限
    build_collaboration_mode_update_item(...),  // 3. 协作模式
    build_realtime_update_item(...),            // 4. 实时
    build_personality_update_item(...),         // 5. 个性
]
```

这个顺序和 `build_initial_context` 中段的顺序一致——保证新增的 diff 段 append 到原有 cache 的尾部，最大化 cache 命中率。

如果某个段在中间变了（比如 permissions），前面的段仍然能命中 cache，只有变更段和它之后的段需要重新计算。但只要 permissions 不变，后面的 realtime/personality 仍然命中。


## 四、和 CC 的 system prompt 构造对比

### 4.1 CC 的方式

CC 的 system prompt 构造在 `src/utils/messages.ts`（具体路径因版本而异）。它的策略是：

- **每次 turn 都重建完整 system prompt**
- 通过 Anthropic API 的 prompt caching 标记（`cache_control`）让 API 自动处理缓存

CC 没有显式的 diff 逻辑——它依赖 API 服务端的 cache 机制。优点是简单，缺点是即使只有一个字符变了，cache 也可能失效（取决于 API 实现）。

### 4.2 Codex 的方式

Codex 的策略是**显式 diff**——只发送变更段，让 API 自然命中前缀 cache。

优点：
- 主动控制 cache 行为，不依赖 API 实现细节
- 网络传输量减少（只发增量）
- 可以 diff 一些 API cache 看不到的东西（如 shell info）

缺点：
- 代码复杂度高（要维护 reference_context_item 的对比）
- 某些字段（如 skills）变化需要新 turn
- 注释中提到（`mod.rs:1615`）："TODO: Make context updates a pure diff of persisted previous/current TurnContextItem state so replay/backtracking is deterministic"——目前 diff 不是完全 deterministic 的

### 4.3 对比表

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| **注入策略** | 显式 diff，只发变更段 | 每次重发完整 system prompt |
| **Cache 依赖** | 客户端控制 | API 服务端控制 |
| **diff 实现位置** | `context_manager/updates.rs:209` | 无 |
| **diff 字段数** | 6 个（env/model/perm/collab/realtime/personality） | N/A |
| **网络传输** | 增量 | 全量 |
| **代码复杂度** | 高 | 低 |
| **回滚确定性** | 部分（有 TODO） | N/A |

![Codex vs Claude Code：上下文策略对比](/images/opencode/codex-03-vs.png)


## 五、为什么这个设计有意思

### 5.1 体现了 Rust 的工程思维

Codex 的 diff 机制本质上是把"什么变了"作为显式状态管理。这和 Rust 的所有权模型很契合——每个字段的变化都要 tracked，编译器帮你发现遗漏。

TypeScript 的 CC 没有这种约束，所以选择更简单的"全量重发 + 依赖 API cache"。

### 5.2 体现了 Codex 的多 Agent 假设

Diff 机制在多 Agent 场景下尤其有用——子 Agent 继承父 Agent 的部分上下文，但只关心自己关心的段。Codex 可以通过 `SeparateDeveloper` 槽位给特定子 Agent 注入额外指令，不影响其他 Agent 的 cache。

CC 有多 Agent 系统（AgentTool + Swarm 跨进程），但它的上下文组合方式和 Codex 不同——CC 每次 turn 重建完整 system prompt，没有 diff 机制，不支持给特定子 agent 独立注入上下文段。因此这个优化场景对它意义不大。

### 5.3 一个小细节：环境上下文

`EnvironmentContext` 段包含 shell / cwd / OS / subagents——subagents 是当前 Agent 已知的所有子 Agent 列表。

这意味着：**当 spawn 一个新子 Agent 后，下一次 turn 的环境上下文会变化**，自动触发 environment update item。

这是一个很巧妙的设计——子 Agent 状态变化通过 diff 机制自然地通知给父 Agent，不需要额外的同步代码。


## 六、小结

| 你学到什么 | 对应源码 |
|-----------|---------|
| 13+ 个上下文段组装 | `session/mod.rs:2725-2951` |
| 3 个 PromptSlot（DeveloperPolicy/Capabilities/ContextualUser/SeparateDeveloper） | `session/mod.rs:2872-2883` |
| Full injection vs Diff 路径选择 | `session/mod.rs:2984-2999` |
| Diff 6 个字段 | `context_manager/updates.rs:209-243` |
| Reference context 重置时机 | mid-turn compaction 或新会话 |
| 13 个段顺序与 cache 命中 | `updates.rs:222-233` |
| Environment context 的 subagents 字段 | `session/mod.rs:2895-2907` |