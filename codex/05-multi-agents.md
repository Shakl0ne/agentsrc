# 多 Agent 编排架构

## 〇、引言

这是 Codex 系列中最有意思的一篇——因为我写这篇文章时发现 **Claude Code 实际上也有多 Agent 系统**，但和 Codex 的完全不同。

CC 有两套多 Agent 机制：

1. **AgentTool（同进程）**：通过 `agent` 工具 spawn 子 agent，本质上是同进程内的协程切换
2. **Swarm（跨进程）**：通过 `create_team` / `spawn_team` 工具创建 teammate，每个 teammate 是一个独立进程（tmux pane 或 iTerm2 split），通过 mailbox 通信

这两套各有局限，但 CC 确实有多 Agent 能力。Codex 不一样——它有从底层设计的完整的层次化多 Agent 系统，能：

- 形成 Agent Tree（父 → 子 → 孙）
- Agent 之间通过 `AgentPath` 互相寻址
- 父 Agent 可以 wait 子 Agent，也可以继续工作
- 支持 map-reduce 风格的 CSV 批处理并行
- 完整的生命周期管理（spawn / communicate / wait / close）

这篇文章深入 Codex 的多 Agent 系统，回答：

1. Agent Tree 怎么建模？父子关系怎么持久化？
2. V1 和 V2 两个版本有什么区别？
3. 6 种协作工具（spawn / send_message / followup_task / wait / close / list_agents）怎么工作？
4. CSV 批处理并行模式怎么实现？


![Codex Agent Tree 拓扑：Root → 子 Agent → Worker](/images/opencode/codex-05-hero.svg)

## 一、AgentControl：多 Agent 的核心入口

### 1.1 一个 root 一个 AgentControl

`AgentControl` 是多 Agent 操作的统一句柄。每个 root thread 持有一个 `AgentControl` 实例，所有子 Agent 共享这个实例——这样 Agent registry 的作用域就限定在单个 root thread 树内，而不是整个 `ThreadManager`。

定义在 `agent/control.rs:151`：

```rust
/// `AgentControl` is held by each session (via `SessionServices`). It provides capability to
/// spawn new agents and the inter-agent communication layer.
/// An `AgentControl` instance is intended to be created at most once per root thread/session
/// tree. That same `AgentControl` is then shared with every sub-agent spawned from that root,
/// which keeps the registry scoped to that root thread rather than the entire `ThreadManager`.
#[derive(Clone, Default)]
pub(crate) struct AgentControl {
    session_id: SessionId,
    manager: Weak<ThreadManagerState>,
    state: Arc<AgentRegistry>,
}
```

### 1.2 AgentRegistry：内存中的 Agent 树

`AgentRegistry` 维护所有活跃 agent 的元数据（`agent/registry.rs:22`）：

```rust
pub(crate) struct AgentRegistry {
    active_agents: Mutex<ActiveAgents>,
    total_count: AtomicUsize,
}

struct ActiveAgents {
    agent_tree: HashMap<String, AgentMetadata>,  // 按 AgentPath 字符串索引
    used_agent_nicknames: HashSet<String>,
    nickname_reset_count: usize,
}

pub(crate) struct AgentMetadata {
    pub(crate) agent_id: Option<ThreadId>,
    pub(crate) agent_path: Option<AgentPath>,
    pub(crate) agent_nickname: Option<String>,
    pub(crate) agent_role: Option<String>,
    pub(crate) last_task_message: Option<String>,
}
```

注意 `agent_tree` 用 `HashMap<String, AgentMetadata>` —— key 是 `AgentPath` 的字符串形式（如 `/root/researcher/worker`），value 是该 agent 的元数据。

### 1.3 AgentStatus：7 种状态

`AgentStatus` 枚举（`protocol.rs:1570`）：

```rust
pub enum AgentStatus {
    #[default]
    PendingInit,
    Running,
    Interrupted,
    Completed(Option<String>),  // 包含 final assistant message
    Errored(String),
    Shutdown,
    NotFound,
}
```

状态从事件推导（`agent/status.rs:5`）：

```rust
pub(crate) fn agent_status_from_event(msg: &EventMsg) -> Option<AgentStatus> {
    match msg {
        EventMsg::TurnStarted(_) => Some(AgentStatus::Running),
        EventMsg::TurnComplete(ev) => Some(AgentStatus::Completed(ev.last_agent_message.clone())),
        EventMsg::TurnAborted(ev) => match ev.reason {
            TurnAbortReason::Interrupted | TurnAbortReason::BudgetLimited => AgentStatus::Interrupted,
            _ => AgentStatus::Errored(format!("{:?}", ev.reason)),
        },
        EventMsg::Error(ev) => Some(AgentStatus::Errored(ev.message.clone())),
        EventMsg::ShutdownComplete => Some(AgentStatus::Shutdown),
        _ => None,
    }
}
```

`is_final` 判断（`agent/status.rs:22`）：

```rust
pub(crate) fn is_final(status: &AgentStatus) -> bool {
    !matches!(status, AgentStatus::PendingInit | AgentStatus::Running | AgentStatus::Interrupted)
}
```

注意 `Interrupted` 不算 final——中断的 agent 可以恢复。

![AgentStatus 7 状态机：PendingInit → Running → Completed/Errored/Shutdown](/images/opencode/codex-05-states.svg)


## 二、AgentPath：树状寻址

### 2.1 路径结构

`AgentPath` 是 V2 的寻址原语（`protocol/src/agent_path.rs:9`）：

```rust
pub struct AgentPath(String);

impl AgentPath {
    pub const ROOT: &str = "/root";

    pub fn root() -> Self { Self(Self::ROOT.to_string()) }

    // 路径是绝对的：/root/<child>/<grandchild>
    pub fn join(&self, agent_name: &str) -> Result<Self, String> {
        validate_agent_name(agent_name)?;
        Self::from_string(format!("{self}/{agent_name}"))
    }

    // 解析相对/绝对引用: "worker" → /root/researcher/worker
    pub fn resolve(&self, reference: &str) -> Result<Self, String> {
        if reference == Self::ROOT { return Ok(Self::root()); }
        if reference.starts_with('/') { return Self::try_from(reference); }
        Self::from_string(format!("{self}/{reference}"))
    }
}
```

这是一个类似文件系统的路径设计：

- `/root` 是根 agent
- `/root/researcher` 是 root 的子 agent "researcher"
- `/root/researcher/worker` 是 researcher 的子 agent "worker"

### 2.2 引用解析

`AgentControl::resolve_agent_reference`（`agent/control.rs:904`）支持相对和绝对引用：

```rust
pub(crate) async fn resolve_agent_reference(
    &self,
    _current_thread_id: ThreadId,
    current_session_source: &SessionSource,
    agent_reference: &str,
) -> CodexResult<ThreadId> {
    let current_agent_path = current_session_source.get_agent_path().unwrap_or_else(AgentPath::root);
    let agent_path = current_agent_path.resolve(agent_reference)
        .map_err(CodexErr::UnsupportedOperation)?;
    if let Some(thread_id) = self.state.agent_id_for_path(&agent_path) {
        return Ok(thread_id);
    }
    Err(CodexErr::UnsupportedOperation(format!(
        "live agent path `{}` not found", agent_path.as_str()
    )))
}
```

这意味着 agent 可以用：

- **绝对路径**：`/root/researcher` （以 `/` 开头）
- **相对路径**：`worker` （相对于当前 agent 的路径）
- **特殊路径**：`/root` （根 agent）

这种设计让 agent 之间通信非常自然——子 agent 不知道父 agent 的全局路径，但可以用 `..` 或绝对路径回到上层。


## 三、MultiAgentVersion：V1 vs V2

### 3.1 三种模式

`MultiAgentVersion` 枚举（`protocol.rs:2760`）：

```rust
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, JsonSchema, TS)]
#[serde(rename_all = "snake_case")]
#[ts(rename_all = "snake_case")]
pub enum MultiAgentVersion {
    Disabled,
    V1,
    V2,
}
```

通过 feature flag 选择（`config/mod.rs:1281`）：

```rust
pub(crate) fn multi_agent_version_from_features(&self) -> MultiAgentVersion {
    if self.feature_set().contains("multi_agent_v2") { MultiAgentVersion::V2 }
    else if self.feature_set().contains("multi_agent_v1") { MultiAgentVersion::V1 }
    else { MultiAgentVersion::Disabled }
}
```

### 3.2 V1 vs V2 工具集对比

| 工具 | V1 | V2 |
|------|----|----|
| `spawn_agent` | ✅ | ✅ |
| `send_input` | ✅ | ❌ |
| `send_message` | ❌ | ✅ |
| `followup_task` | ❌ | ✅ |
| `wait_agent` | ✅ | ✅ |
| `close_agent` | ✅ | ✅ |
| `resume_agent` | ✅ | ❌ |
| `list_agents` | ❌ | ✅ |

V2 最大的改进：

1. **`send_message` vs `send_input`**：V2 的 send_message 默认不触发 turn（QueueOnly），让父 agent 可以批量发消息；V2 的 `followup_task` 才触发 turn
2. **`list_agents`**：V2 新增，让 agent 能列出所有活跃 agent
3. **路径寻址**：V2 通过 `AgentPath` 寻址，V1 通过 `ThreadId`

### 3.3 深度限制的差异

V1 限制递归深度（`tools/handlers/multi_agents/spawn.rs:70`）：

```rust
let child_depth = next_thread_spawn_depth(&session_source);
let max_depth = turn.config.agent_max_depth;
if exceeds_thread_spawn_depth_limit(child_depth, max_depth) {
    return Err(FunctionCallError::RespondToModel(
        "Agent depth limit reached. Solve the task yourself.".to_string(),
    ));
}
```

V2 **不限制深度**（`tools/spec_plan.rs:290`）：

```rust
fn collab_tools_enabled(turn_context: &TurnContext) -> bool {
    match turn_context.multi_agent_version {
        MultiAgentVersion::Disabled => false,
        MultiAgentVersion::V1 => !exceeds_thread_spawn_depth_limit(
            next_thread_spawn_depth(&turn_context.session_source),
            turn_context.config.agent_max_depth,
        ),
        MultiAgentVersion::V2 => true,   // V2: 总是启用，无深度检查
    }
}
```

这是个有趣的设计选择——V2 移除深度限制，依赖 `agent_max_threads`（并发数限制）和 agent 自己的判断来防止递归失控。

![V1 vs V2 协作工具矩阵对比](/images/opencode/codex-05-tools.svg)

`exceeds_thread_spawn_depth_limit` 函数本身很简单（`agent/registry.rs:63-77`）：

```rust
fn session_depth(session_source: &SessionSource) -> i32 {
    match session_source {
        SessionSource::SubAgent(SubAgentSource::ThreadSpawn { depth, .. }) => *depth,
        SessionSource::SubAgent(_) => 0,
        _ => 0,
    }
}

pub(crate) fn next_thread_spawn_depth(session_source: &SessionSource) -> i32 {
    session_depth(session_source).saturating_add(1)
}

pub(crate) fn exceeds_thread_spawn_depth_limit(depth: i32, max_depth: i32) -> bool {
    depth > max_depth
}
```

每次 spawn 子 agent，depth +1。


## 四、6 种 V2 工具详解

V2 工具的实现都在 `tools/handlers/multi_agents_v2/` 下：

```
multi_agents_v2.rs          # 模块入口
├── close_agent.rs          # close_agent 工具
├── followup_task.rs        # followup_task 工具
├── list_agents.rs          # list_agents 工具
├── message_tool.rs         # 共享的消息处理逻辑
├── send_message.rs         # send_message 工具
├── spawn.rs                # spawn_agent 工具
└── wait.rs                 # wait_agent 工具
```

### 4.1 spawn_agent

`spawn.rs` 是最复杂的工具之一。它的核心逻辑（`spawn.rs:112-150`）：

```rust
let spawn_source = thread_spawn_source(
    session.thread_id, &turn.session_source,
    child_depth, role_name, Some(args.task_name),
)?;

let result = Box::pin(
    session.services.agent_control.spawn_agent_with_metadata(
        config,
        match (spawn_source.get_agent_path(), initial_operation) {
            // 如果初始 op 是 text，包装为 InterAgentCommunication
            (Some(recipient), Op::UserInput { items, .. })
                if items.iter().all(|item| matches!(item, UserInput::Text { .. })) =>
            {
                Op::InterAgentCommunication {
                    communication: InterAgentCommunication::new(
                        turn.session_source.get_agent_path().unwrap_or_else(AgentPath::root),
                        recipient,
                        Vec::new(),
                        prompt.clone(),
                        /*trigger_turn*/ true,
                    ),
                }
            }
            (_, initial_operation) => initial_operation,
        },
        Some(spawn_source),
        SpawnAgentOptions { fork_parent_spawn_call_id, fork_mode, parent_thread_id, environments },
    ),
).await...
```

关键设计：

1. **task_name → agent_path**：spawn 时传入的 `task_name` 直接变成子 agent 的 path（如 `worker` → `/root/researcher/worker`）
2. **初始消息包装**：如果初始操作是纯文本，自动包装成 `InterAgentCommunication`，标记 trigger_turn=true
3. **SpawnAgentOptions**：包含 fork 模式、parent_thread_id、environments

### 4.2 send_message vs followup_task

这两个工具共用 `handle_message_string_tool`，差异只在 `MessageDeliveryMode`：

`send_message` 用 `QueueOnly`（`send_message.rs`）——**只入队，不触发 turn**：

```rust
pub(crate) struct Handler;
async fn handle(&self, invocation) -> Result<..., FunctionCallError> {
    let args: SendMessageArgs = parse_arguments(&arguments)?;
    handle_message_string_tool(invocation, MessageDeliveryMode::QueueOnly, args.target, args.message).await
}
```

`followup_task` 用 `TriggerTurn`（`followup_task.rs`）——**入队并触发 turn**：

```rust
pub(crate) struct Handler;
async fn handle(&self, invocation) -> Result<..., FunctionCallError> {
    let args: FollowupTaskArgs = parse_arguments(&arguments)?;
    handle_message_string_tool(invocation, MessageDeliveryMode::TriggerTurn, args.target, args.message).await
}
```

`MessageDeliveryMode::apply` 修改 `trigger_turn` 字段（`message_tool.rs:11-31`）：

```rust
pub(crate) enum MessageDeliveryMode {
    QueueOnly,    // send_message
    TriggerTurn,  // followup_task
}
impl MessageDeliveryMode {
    fn apply(self, communication: InterAgentCommunication) -> InterAgentCommunication {
        match self {
            Self::QueueOnly => InterAgentCommunication { trigger_turn: false, ..communication },
            Self::TriggerTurn => InterAgentCommunication { trigger_turn: true, ..communication },
        }
    }
}
```

**为什么这个区分重要？**

- `send_message` 让父 agent 可以**批量发消息给多个子 agent**，再统一用 `followup_task` 唤醒它们
- 这是 map-reduce 模式的核心：父 agent 把任务分发给 N 个 worker（send_message），然后让它们并行开始（followup_task），最后 wait_agent 收集结果

### 4.3 wait_agent

`wait.rs:63` 实现等待逻辑：

```rust
let mut mailbox_rx = session.input_queue.subscribe_mailbox().await;
// ... emits CollabWaitingBeginEvent ...
let deadline = Instant::now() + Duration::from_millis(timeout_ms as u64);
let timed_out = !wait_for_mailbox_change(&mut mailbox_rx, deadline).await;
let result = WaitAgentResult::from_timed_out(timed_out);
// ... emits CollabWaitingEndEvent ...
```

注意：`wait_agent` 等的是**当前 agent 自己 mailbox 的变化**——也就是说，agent A 调用 `wait_agent` 时，它在等**别人给 A 发消息**，而不是等 A 的子 agent 完成。

如果想等子 agent 完成，需要：

1. 子 agent 完成后通过 completion watcher 自动给父 agent 发 `InterAgentCommunication`
2. 父 agent 的 mailbox 收到消息
3. 父 agent 的 `wait_agent` 被唤醒

### 4.4 close_agent

`close_agent` 标记 edge 为 Closed 并递归关闭整个子树（`agent/control.rs:800`）：

```rust
pub(crate) async fn close_agent(&self, agent_id: ThreadId) -> CodexResult<String> {
    // persists DirectionalThreadSpawnEdgeStatus::Closed
    Box::pin(self.shutdown_agent_tree(agent_id)).await
}

async fn shutdown_agent_tree(&self, agent_id: ThreadId) -> CodexResult<String> {
    let descendant_ids = self.live_thread_spawn_descendants(agent_id).await?;
    let result = self.shutdown_live_agent(agent_id).await;
    for descendant_id in descendant_ids {
        self.shutdown_live_agent(descendant_id).await?;
    }
    result
}
```

关闭一个 agent 时会递归关闭它所有的后代——这是树形结构的标准做法。

### 4.5 list_agents

V2 独有，列出当前 agent 树中所有活跃 agent。让 agent 能"看看周围有谁"，决定下一步和谁通信。


## 五、InterAgentCommunication：消息格式

`InterAgentCommunication` 是 agent 之间通信的 wire format（`protocol.rs:686`）：

```rust
pub struct InterAgentCommunication {
    pub author: AgentPath,           // 发送者
    pub recipient: AgentPath,        // 主接收者
    #[serde(default)]
    pub other_recipients: Vec<AgentPath>,  // 抄送
    pub content: String,             // 消息内容
    pub trigger_turn: bool,          // 是否触发接收者的 turn
}
```

`send_inter_agent_communication`（`agent/control.rs:734`）是底层发送函数：

```rust
pub(crate) async fn send_inter_agent_communication(
    &self,
    agent_id: ThreadId,
    communication: InterAgentCommunication,
) -> CodexResult<String> {
    let last_task_message = communication.content.clone();
    let state = self.upgrade()?;
    let result = self.handle_thread_request_result(
        agent_id, &state,
        state.send_op(agent_id, Op::InterAgentCommunication { communication }).await,
    ).await;
    if result.is_ok() {
        self.state.update_last_task_message(agent_id, last_task_message);
    }
    result
}
```

底层走的是 `Op::InterAgentCommunication`——还记得第二篇的 `submission_loop` 吗？这个 Op 就是被那个 loop 接收并处理的。


## 六、CSV 批处理并行模式

### 6.1 spawn_agents_on_csv 工具

定义在 `tools/handlers/agent_jobs/spawn_agents_on_csv.rs:15`：

```rust
pub struct SpawnAgentsOnCsvHandler;
// 工具名: "spawn_agents_on_csv"
// 参数: csv_path, instruction, id_column, output_csv_path,
//       max_concurrency, max_workers, max_runtime_seconds
```

这个工具让 agent 能对 CSV 每行 spawn 一个工作 agent，实现 map-reduce 风格的批处理。

### 6.2 并发控制

核心循环在 `agent_jobs.rs:156`：

```rust
async fn run_agent_job_loop(session, turn, db, job_id, options) -> anyhow::Result<()> {
    const DEFAULT_AGENT_JOB_CONCURRENCY: usize = 16;
    const MAX_AGENT_JOB_CONCURRENCY: usize = 64;
    loop {
        // 用并发槽位填充 pending CSV 行
        if !cancel_requested && active_items.len() < options.max_concurrency {
            let slots = options.max_concurrency - active_items.len();
            let pending_items = db.list_agent_job_items(...).await?;
            for item in pending_items {
                let prompt = build_worker_prompt(&job, &item)?;
                let thread_id = session.services.agent_control
                    .spawn_agent_with_metadata(
                        options.spawn_config.clone(),
                        items.into(),
                        Some(SessionSource::SubAgent(SubAgentSource::Other(format!("agent_job:{job_id}")))),
                        SpawnAgentOptions { parent_thread_id: Some(session.thread_id), ... },
                    ).await?
                    .thread_id;
                active_items.insert(thread_id, ActiveJobItem { item_id, started_at, status_rx });
            }
        }
        // 收割完成的 worker，导出 CSV
        let finished = find_finished_threads(session.clone(), &active_items).await;
        for (thread_id, item_id) in finished {
            finalize_finished_item(session, db, job_id, item_id, thread_id).await?;
            active_items.remove(&thread_id);
        }
    }
}
```

设计要点：

| 配置项 | 默认值 | 上限 |
|--------|--------|------|
| `max_concurrency` | 16 | 64 |
| `max_workers` | 配置决定 | - |
| `max_runtime_seconds` | 配置决定 | - |

并发槽位控制让大量 CSV 行可以分批 spawn——避免一次性 spawn 上千个 agent 把系统搞挂。

![CSV 批处理 Map-Reduce：CSV → Slot Pool → Workers → Output](/images/opencode/codex-05-csv.svg)


## 七、AgentGraphStore：持久化父子拓扑

### 7.1 trait 定义

agent 之间的父子关系不只是内存中的，还要持久化（为了 resume/fork）。`AgentGraphStore` trait（`agent-graph-store/src/store.rs:12`）：

```rust
#[async_trait]
pub trait AgentGraphStore: Send + Sync {
    async fn upsert_thread_spawn_edge(
        &self, parent_thread_id: ThreadId, child_thread_id: ThreadId,
        status: ThreadSpawnEdgeStatus,
    ) -> AgentGraphStoreResult<()>;
    
    async fn set_thread_spawn_edge_status(
        &self, child_thread_id: ThreadId,
        status: ThreadSpawnEdgeStatus,
    ) -> AgentGraphStoreResult<()>;
    
    async fn list_thread_spawn_children(
        &self, parent_thread_id: ThreadId,
        status_filter: Option<ThreadSpawnEdgeStatus>,
    ) -> AgentGraphStoreResult<Vec<ThreadId>>;
    
    async fn list_thread_spawn_descendants(
        &self, root_thread_id: ThreadId,
        status_filter: Option<ThreadSpawnEdgeStatus>,
    ) -> AgentGraphStoreResult<Vec<ThreadId>>;
}
```

### 7.2 Edge 状态

`ThreadSpawnEdgeStatus`（`agent-graph-store/src/types.rs:4`）：

```rust
pub enum ThreadSpawnEdgeStatus {
    Open,    // 子 agent 还活着或可恢复
    Closed,  // 子 agent 已被关闭
}
```

只有两种状态——简单。一个 agent close 后，它的 edge 变成 Closed，但**关系记录保留**——这让我们可以查询历史拓扑（"这个 root 一共 spawn 过哪些 agent？"）。

### 7.3 SQLite 实现

`LocalAgentGraphStore`（`agent-graph-store/src/local.rs:13`）是默认实现：

```rust
pub struct LocalAgentGraphStore {
    state_db: Arc<StateRuntime>,
}

impl LocalAgentGraphStore {
    pub fn new(state_db: Arc<StateRuntime>) -> Self { Self { state_db } }
}

#[async_trait]
impl AgentGraphStore for LocalAgentGraphStore {
    // 委托给 state_db.upsert_thread_spawn_edge / set_thread_spawn_edge_status /
    //      list_thread_spawn_children / list_thread_spawn_descendants
}
```

底层用 SQLite 持久化——这意味着即使进程重启，agent 拓扑关系也能恢复。


## 八、Completion Watcher：自动通知父 agent

### 8.1 V1 独有机制

V1 用一个 detached tokio 任务监控子 agent 状态，子 agent 完成时自动通知父 agent（`agent/control.rs:1036`）：

```rust
fn maybe_start_completion_watcher(
    &self,
    child_thread_id: ThreadId,
    session_source: SessionSource,
    child_reference: String,
    child_agent_path: Option<AgentPath>,
) {
    tokio::spawn(async move {
        let status = ...;  // subscribe_status → wait for is_final
        if child_uses_multi_agent_v2 {
            // V2: 发 InterAgentCommunication 回父 agent
            let communication = InterAgentCommunication::new(
                child_agent_path, parent_agent_path, Vec::new(), message,
                /*trigger_turn*/ false,  // 不触发父 turn
            );
            control.send_inter_agent_communication(parent_thread_id, communication).await;
        } else {
            // V1: 直接注入用户消息（不触发 turn）
            parent_thread.inject_user_message_without_turn(message).await;
        }
    });
}
```

### 8.2 V2 的差异

V2 没有显式的 completion watcher——它依赖 `trigger_turn` 字段控制是否唤醒。`send_message` 默认 trigger_turn=false，所以子 agent 完成后即使发消息给父 agent，也不会打断父 agent 的工作。父 agent 主动调用 `wait_agent` 时才会被唤醒。

这种设计让 V2 更灵活——父 agent 可以选择"我现在就要等子 agent"还是"我让子 agent 在后台跑，我继续干活"。


## 九、Codex vs Claude Code：多 Agent 对比

### 9.1 CC 有什么？两套多 Agent 系统

CC 共有两套多 Agent 机制：

**AgentTool / Task 工具（同进程）**：通过 `agent` 工具 spawn 子 agent。本质上是同进程内的协程切换，支持递归 spawn、fork、resume、agent 内存快照。

**Swarm（跨进程）**：通过 `create_team` / `spawn_team` 工具创建 teammate。每个 teammate 是一个独立的 Node.js 进程（tmux pane、iTerm2 split 或独立窗口），通过 mailbox 通信、权限桥同步。配套有 `send_message`、`delete_team`、`rename` 等协作工具。

Swarm 与 AgentTool 的对比：

| 维度 | AgentTool（同进程） | Swarm（跨进程） |
|------|--------------------|----------------|
| **进程隔离** | ❌ 同进程 | ✅ 独立进程 |
| **并行执行** | ❌ 阻塞主 agent | ✅ 独立运行 |
| **消息传递** | ❌ 只有 final message | ✅ send_message + mailbox |
| **递归 spawn** | ✅ 支持 fork/resume | ❌ 单层（leader→teammate） |
| **状态持久化** | ✅ agent memory snapshot | ✅ teammate 文件 + reconnection |
| **通信延迟** | 低（内存） | 中（跨进程 mailbox） |

但总的来说，CC 的多 Agent 系统是**后来添加上去的**（swarm 相关工具命名甚至带 `TeamCreateTool` 等外部工具风格），不像 Codex 是从底层设计的原生能力。

### 9.2 Codex 多 Agent 的优势

| 能力 | Codex | CC（AgentTool） | CC（Swarm） |
|------|-------|-----------------|-------------|
| **真正的 Agent Tree** | ✅ 递归 spawn | ⚠️ 有限递归 | ❌ 单层 |
| **独立进程/线程** | ✅ 每个 agent 独立 thread | ❌ 同进程 | ✅ 独立进程 |
| **并行执行** | ✅ N 个子 agent 同时跑 | ❌ 阻塞 | ✅ 独立运行 |
| **路径寻址** | ✅ AgentPath | ❌ 无 | ❌ 无 |
| **消息传递** | ✅ send_message + followup_task | ❌ 只有 final message | ✅ send_message + mailbox |
| **持久化拓扑** | ✅ SQLite AgentGraphStore | ⚠️ agent memory snapshot | ⚠️ teammate 文件 |
| **CSV 批处理** | ✅ spawn_agents_on_csv | ❌ 无 | ❌ 无 |
| **深度限制** | V1 有限制，V2 无限制 | N/A | N/A |
| **Resume/Fork** | ✅ 底层支持 | ✅ fork/resume 工具 | ✅ reconnection |

### 9.3 设计哲学差异

CC 的 Swarm 系统反映了一个假设：**多 Agent 应该通过外部进程协作**（tmux / iTerm2）。每个人是一个自包含的 Claude Code 实例，通过 mailbox 通信。这套设计的好处是天然隔离，坏处是 spawn 成本高、通信延迟大。

Codex 的多 Agent 系统反映了一个不同假设：**多 Agent 应该从底层集成**，是框架的核心能力。通过共享的 AgentRegistry、AgentPath 寻址、InterAgentCommunication，agent 之间的协作可以非常高效。

两种哲学各有道理，落实到具体场景：

| 场景 | CC Swarm 更优 | Codex 更优 |
|------|--------------|------------|
| 跨文件批处理（CSV 100 行） | ❌ spawn 成本高 | ✅ spawn_agents_on_csv |
| 隔离性（一个 agent 崩不影响其他） | ✅ 独立进程 | ⚠️ 同一进程 |
| 细粒度 agent 通信 | ❌ mailbox 延迟 | ✅ 进程内直接送达 |
| 轻量级子任务（快速查询） | ⚠️ spawn 一个进程太重 | ✅ spawn thread |


## 十、小结

| 你学到什么 | 对应源码 |
|-----------|---------|
| `AgentControl` 共享句柄 | `agent/control.rs:151` |
| `AgentRegistry` 内存树 | `agent/registry.rs:22` |
| `AgentStatus` 7 种状态 | `protocol.rs:1570` |
| `AgentPath` 寻址 | `protocol/src/agent_path.rs:9` |
| `MultiAgentVersion` V1/V2 | `protocol.rs:2760` |
| 6 种 V2 工具 | `tools/handlers/multi_agents_v2/` |
| `MessageDeliveryMode` 区分 | `message_tool.rs:11-31` |
| `InterAgentCommunication` wire format | `protocol.rs:686` |
| `spawn_agents_on_csv` 批处理 | `agent_jobs/spawn_agents_on_csv.rs:15` + `agent_jobs.rs:156` |
| 并发上限 16 默认 / 64 最大 | `agent_jobs.rs` 中的常量 |
| `AgentGraphStore` trait | `agent-graph-store/src/store.rs:12` |
| V1 completion watcher | `agent/control.rs:1036` |
| 深度限制 V1 vs V2 | `tools/spec_plan.rs:290` |

## 章节小测

<script setup>
const q = [
  {
    question: 'Codex 多 Agent 系统的 AgentPath 为什么采用类似文件系统的路径设计（如 /root/researcher/worker）？',
    options: ['统一用短标识符作为路径可使序列化传输占用更少网络带宽', '支持绝对路径相对路径和特殊路径使寻址方式灵活且自然', '直接将文件系统的挂载路径映射为子 Agent 全局标识符', '为满足 JSON Schema 标准序列化格式对路径字段的规范约束'],
    correct: 1,
    explanation: 'AgentPath 设计为类似文件系统的树状路径：绝对路径（/root/researcher 以 / 开头）、相对路径（worker 相对当前 agent）、特殊路径（/root 根 agent）。子 agent 不需要知道父 agent 的全局路径就能寻址，通信非常自然。'
  },
  {
    question: 'V2 中 send_message 和 followup_task 的核心区别是什么？',
    options: ['send_message 仅允许由父 Agent 向子 Agent 方向发送消息', 'send_message 使用 QueueOnly 模式 followup_task 使用 TriggerTurn', 'send_message 对单条消息内容的长度施加了严格的字符限制', '两者在底层实现上功能完全一致仅对外暴露的工具名不同'],
    correct: 1,
    explanation: '这个区分是实现 map-reduce 模式的关键：父 agent 先用 send_message 批量发消息给多个子 agent（只入队不触发 turn），再用 followup_task 统一唤醒它们并行执行，最后 wait_agent 收集结果。'
  },
  {
    question: '为什么 V2 移除了深度限制（V1 有），改为依赖 agent_max_threads（并发数限制）？',
    options: ['V2 架构设计限制子 Agent 不能再递归调用 spawn_agent 工具', 'V2 更信任 Agent 自身判断能力用并发上限替代深度限制', 'V2 尚未实现深度限制检查功能将在后续版本中补全', '深度限制逻辑在 V2 中改由 OpenAI 服务端 API 统一控制'],
    correct: 1,
    explanation: 'V2 中 collab_tools_enabled 总是返回 true，不检查深度。依赖 agent 自己的判断和 agent_max_threads（并发数上限）来防止递归失控，体现了 V2 对 agent 能力的信任。'
  },
  {
    question: 'Codex 的 CSV 批处理模式（spawn_agents_on_csv）中，默认并发上限和最大并发分别是多少？为什么？',
    options: ['默认 4 并行执行上限而最大并发配置上限设为 16', '默认 16 并发且最大 64 通过槽位控制防止系统资源耗尽', '默认 1 串行执行且不允许配置超过 10 的并发上限', '不设置任何并发限制各 CSV 行同时全量并行 spawn 子 Agent'],
    correct: 1,
    explanation: '默认 16 并发（DEFAULT_AGENT_JOB_CONCURRENCY），最大 64（MAX_AGENT_JOB_CONCURRENCY）。并发槽位控制让大量 CSV 行分批 spawn worker agent，避免一次性创建上千个 agent 导致资源耗尽。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
