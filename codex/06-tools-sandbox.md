# 工具系统与安全沙箱

## 〇、引言

前几篇我们看了 Codex 的主循环、上下文组合、Compact 系统、多 Agent 编排。这一篇覆盖两个相关的主题——**工具系统**和**安全沙箱**。

为什么放一起讲？因为 Codex 的工具执行链路天然包含沙箱——`shell` / `exec` 工具调用时，必须经过 ExecPolicy 检查和沙箱隔离才能执行。这两者是绑定的。

读完这篇你能回答：

1. Codex 怎么发现、注册、执行工具？
2. MCP 工具怎么集成进 Codex？
3. ExecPolicy 怎么决定一个命令能不能跑？
4. 跨平台沙箱怎么实现？macOS Seatbelt / Linux Landlock / Windows 分别做什么？
5. 和 CC 的工具系统对比，Codex 多做了什么？


## 一、ToolExecutor trait：所有工具的统一接口

### 1.1 核心定义

`ToolExecutor` trait 在 `codex-rs/tools/src/tool_executor.rs:44`：

```rust
pub trait ToolExecutor<Invocation>: Send + Sync {
    fn tool_name(&self) -> ToolName;
    fn spec(&self) -> ToolSpec;
    fn exposure(&self) -> ToolExposure { ToolExposure::Direct }
    fn search_info(&self) -> Option<ToolSearchInfo> { ... }
    fn supports_parallel_tool_calls(&self) -> bool { false }
    async fn handle(&self, invocation: Invocation) -> Result<Box<dyn ToolOutput>, FunctionCallError>;
}
```

每个工具实现这个 trait，提供：

- **`tool_name()`**：工具名（带 namespace，见下文）
- **`spec()`**：JSON Schema 描述，给模型看的
- **`exposure()`**：模型可见性
- **`handle()`**：实际执行逻辑

### 1.2 ToolExposure：4 种可见性

`ToolExposure` 枚举（`tool_executor.rs:8`）控制工具对模型的可见性：

```rust
pub enum ToolExposure {
    Direct,          // 初始模型可见 + nested code-mode
    Deferred,        // 初始隐藏；通过 tool_search 发现
    DirectModelOnly, // 仅初始列表；不进 code-mode
    Hidden,          // 注册只为 dispatch，永不模型可见
}
```

这个设计很有意思——**不是所有工具一开始就暴露给模型**。`Deferred` 让工具通过 `tool_search` 动态发现，避免初始 tool 列表过长污染 prompt。

![ToolExposure 4 种可见性级别：Direct / Deferred / DirectModelOnly / Hidden](/images/codex/06-exposure.png)

### 1.3 CoreToolRuntime：core 的扩展接口

Codex core 在 `ToolExecutor` 之上扩展了一个 `CoreToolRuntime` trait（`tools/registry.rs:48`）：

```rust
pub(crate) trait CoreToolRuntime: ToolExecutor<ToolInvocation> {
    fn matches_kind(&self, payload: &ToolPayload) -> bool { ... }
    fn waits_for_runtime_cancellation(&self) -> bool { false }
    fn telemetry_tags(...) -> BoxFuture<'a, ToolTelemetryTags> { ... }
    fn post_tool_use_payload(...) -> Option<PostToolUsePayload> { ... }
    fn pre_tool_use_payload(...) -> Option<PreToolUsePayload> { ... }
    fn with_updated_hook_input(...) -> Result<ToolInvocation, FunctionCallError> { ... }
    fn create_diff_consumer(&self) -> Option<Box<dyn ToolArgumentDiffConsumer>> { None }
}
```

这是 core 内部的扩展接口，增加了：

- **pre/post hooks**：每个工具可以注入 pre/post 钩子
- **telemetry**：自动收集工具调用遥测数据
- **diff consumer**：支持参数 diffing（如 edit 工具记录每次参数变化）

### 1.4 ToolName：带 namespace 的工具名

`ToolName` 在 `protocol/src/tool_name.rs:9`：

```rust
pub struct ToolName {
    pub name: String,
    pub namespace: Option<String>,
}
impl ToolName {
    pub fn plain(name: impl Into<String>) -> Self { ... }
    pub fn namespaced(namespace: impl Into<String>, name: impl Into<String>) -> Self { ... }
}
```

namespace 用来区分不同来源的同名工具——比如 MCP server A 和 MCP server B 都有 `read_file` 工具，可以变成 `mcp_a::read_file` 和 `mcp_b::read_file`。


## 二、ToolRegistry：中央注册表

### 2.1 数据结构

`ToolRegistry` 在 `tools/registry.rs:326`：

```rust
pub struct ToolRegistry {
    tools: HashMap<ToolName, Arc<dyn CoreToolRuntime>>,
}

impl ToolRegistry {
    pub(crate) fn from_tools(tools: impl IntoIterator<Item = Arc<dyn CoreToolRuntime>>) -> Self {
        let mut tools_by_name = HashMap::new();
        for tool in tools {
            let name = tool.tool_name();
            // panic on duplicate name
            tools_by_name.insert(name, tool);
        }
        Self::new(tools_by_name)
    }
}
```

`HashMap<ToolName, Arc<dyn CoreToolRuntime>>`——按工具名索引，Arc 让工具可以在多线程共享。

### 2.2 ToolRouter：每个 turn 的包装

`ToolRouter` 在 `tools/router.rs:34`：

```rust
pub struct ToolRouter {
    registry: ToolRegistry,
    model_visible_specs: Vec<ToolSpec>,
}
```

每个 turn 创建一个 ToolRouter，包装 registry + 当前模型可见的 tool specs。

构建参数 `ToolRouterParams`（`router.rs:39`）：

```rust
pub(crate) struct ToolRouterParams<'a> {
    pub(crate) mcp_tools: Option<Vec<ToolInfo>>,
    pub(crate) deferred_mcp_tools: Option<Vec<ToolInfo>>,
    pub(crate) discoverable_tools: Option<Vec<DiscoverableTool>>,
    pub(crate) extension_tool_executors: Vec<Arc<dyn ToolExecutor<ExtensionToolCall>>>,
    pub(crate) dynamic_tools: &'a [DynamicToolSpec],
}
```

注意有 5 种工具来源：MCP / 延迟 MCP / 可发现工具 / 扩展工具 / 动态工具。

### 2.3 add_tool_sources：注册所有工具

`tools/spec_plan.rs:527` 的 `add_tool_sources` 是注册中心：

```rust
fn add_tool_sources(context: &CoreToolPlanContext<'_>, planned_tools: &mut PlannedTools) {
    add_shell_tools(context, planned_tools);        // shell / exec / write_stdin
    add_mcp_resource_tools(context, planned_tools); // list_mcp_resources / templates / read
    add_core_utility_tools(context, planned_tools); // plan, goals, request_*, apply_patch, view_image
    add_collaboration_tools(context, planned_tools); // multi-agent v1/v2, agent jobs
    add_mcp_runtime_tools(context, planned_tools);  // MCP 工具 handlers
    add_dynamic_tools(context, planned_tools);       // 动态工具 specs
    add_extension_tools(context, planned_tools);     // 扩展工具
}
```

6 类工具：

1. **Shell 工具**：`shell` / `exec` / `write_stdin`
2. **MCP 资源工具**：`list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource`
3. **核心工具**：`plan` / `get_goal` / `set_goal` / `request_permissions` / `apply_patch` / `view_image`
4. **协作工具**：多 Agent V1/V2 工具 + agent_jobs（CSV 批处理）
5. **MCP 运行时工具**：所有注册的 MCP server 提供的工具
6. **动态/扩展工具**：运行时注入的工具


## 三、Tool 执行流程

### 3.1 从模型响应到 ToolCall

`ToolRouter::build_tool_call` 在 `router.rs:96`：

```rust
pub fn build_tool_call(item: ResponseItem) -> Result<Option<ToolCall>, FunctionCallError> {
    match item {
        ResponseItem::FunctionCall { name, namespace, arguments, call_id, .. } => {
            let tool_name = ToolName::new(namespace, name);
            Ok(Some(ToolCall { tool_name, call_id, payload: ToolPayload::Function { arguments } }))
        }
        ResponseItem::CustomToolCall { name, input, call_id, .. } => {
            Ok(Some(ToolCall { tool_name: ToolName::plain(name), call_id, payload: ToolPayload::Custom { input } }))
        }
        // ...
    }
}
```

模型返回 `ResponseItem::FunctionCall` 或 `ResponseItem::CustomToolCall`，统一转换成 `ToolCall`。

### 3.2 Dispatch pipeline

`ToolRegistry::dispatch_any_with_terminal_outcome` 在 `registry.rs:408-690`，dispatch 流程：

1. **Telemetry + sandbox tags**：收集追踪标签
2. **Name lookup**：按 `ToolName` 查找，找不到返回 `RespondToModel` 错误
3. **`notify_tool_start()`**：发开始事件
4. **`run_pre_tool_use_hooks()`**：可以阻塞或重写 invocation
5. **`handle_any_tool(tool.handle(invocation))`**：实际执行
6. **`run_post_tool_use_hooks()`**：可以替换输出
7. **`notify_tool_finish()`**：发完成事件
8. **返回 `AnyToolResult`**

这是个标准的 pipeline——pre hook → execute → post hook。pre hook 可以阻止工具执行，post hook 可以修改输出。

![Tool Dispatch Pipeline 5 阶段：ToolCall → Registry → PreHooks → Execute → PostHooks](/images/codex/06-hero.png)


## 四、MCP 集成

### 4.1 McpConnectionManager

`McpConnectionManager` 在 `codex-mcp/src/connection_manager.rs:105`：

```rust
pub struct McpConnectionManager {
    clients: HashMap<String, AsyncManagedClient>,
    server_metadata: HashMap<String, McpServerMetadata>,
    tool_plugin_provenance: Arc<ToolPluginProvenance>,
    host_owned_codex_apps_enabled: bool,
    prefix_mcp_tool_names: bool,
    elicitation_requests: ElicitationRequestManager,
    startup_cancellation_token: CancellationToken,
}
```

它持有所有 MCP server 的 client 连接，每个 server 是一个 `AsyncManagedClient`。

### 4.2 list_all_tools：聚合所有 MCP 工具

`connection_manager.rs:416`：

```rust
pub async fn list_all_tools(&self) -> Vec<ToolInfo> {
    let mut tools = Vec::new();
    for (server_name, managed_client) in &self.clients {
        if let Some(server_tools) = managed_client.listed_tools().await {
            tools.extend(server_tools.into_iter().map(|tool| self.with_server_metadata(tool)));
        }
    }
    normalize_tools_for_model_with_prefix(tools, self.prefix_mcp_tool_names)
}
```

`normalize_tools_for_model_with_prefix` 是关键——如果 `prefix_mcp_tool_names=true`，会给所有 MCP 工具名加上 server name 前缀，避免名字冲突。

### 4.3 ToolInfo：MCP 工具的元数据

`ToolInfo` 在 `codex-mcp/src/tools.rs:30`：

```rust
pub struct ToolInfo {
    pub server_name: String,         // MCP server 名（路由用）
    pub supports_parallel_tool_calls: bool,
    pub server_origin: Option<String>,
    pub callable_name: String,        // 模型可见的工具名
    pub callable_namespace: String,   // 模型可见的 namespace
    pub namespace_description: Option<String>,
    pub tool: Tool,                   // 原始 rmcp::model::Tool
    pub connector_id: Option<String>,
    pub connector_name: Option<String>,
    pub plugin_display_names: Vec<String>,
}
```

注意区分 `server_name`（路由用）和 `callable_name`（模型可见）——这两者可以不一样，因为前缀规则可能改写。

### 4.4 McpHandler：MCP 工具的 core 包装

`spec_plan.rs:765` 的 `add_mcp_runtime_tools` 把每个 `ToolInfo` 包装成 `McpHandler`：

```rust
fn add_mcp_runtime_tools(context: &CoreToolPlanContext<'_>, planned_tools: &mut PlannedTools) {
    if let Some(mcp_tools) = context.mcp_tools {
        for tool in mcp_tools {
            match McpHandler::new(tool.clone()) {
                Ok(handler) => planned_tools.add(handler),
                Err(err) => warn!("Skipping MCP tool ..."),
            }
        }
    }
    // plus deferred MCP tools
}
```

`McpHandler` 实现 `CoreToolRuntime`，`handle()` 时路由到 `McpConnectionManager::call_tool()`（`connection_manager.rs:685`）：

```rust
pub async fn call_tool(&self, server: &str, tool: &str, arguments: Option<serde_json::Value>, ...) 
    -> Result<CallToolResult> 
{
    let client = self.client_by_name(server).await?;
    let result = client.client.call_tool(tool.to_string(), arguments, meta, client.tool_timeout).await?;
    Ok(CallToolResult { content, structured_content, is_error, meta })
}
```

底层是 RMCP（Rust MCP client）调用 MCP server 的 `tools/call` 方法。


## 五、ExecPolicy：执行策略引擎

### 5.1 Decision：3 种决策

`Decision` 枚举在 `execpolicy/src/decision.rs:7`：

```rust
pub enum Decision {
    Allow,     // 可以直接执行
    Prompt,    // 需要用户审批
    Forbidden, // 不可执行
}
```

### 5.2 Policy：规则集合

`Policy` 在 `execpolicy/src/policy.rs:28`：

```rust
pub struct Policy {
    rules_by_program: MultiMap<String, RuleRef>,  // first-token → rules
    network_rules: Vec<NetworkRule>,              // host/protocol rules
    host_executables_by_name: HashMap<String, Arc<[AbsolutePathBuf]>>, // basename → 允许的路径
}
```

规则按"命令的第一个 token"索引——比如 `git push` 的规则挂在 `git` 下，查找时先按第一个 token 快速过滤。

### 5.3 Rule trait + PrefixRule

`Rule` trait 在 `execpolicy/src/rule.rs:214`：

```rust
pub trait Rule: Any + Debug + Send + Sync {
    fn program(&self) -> &str;
    fn matches(&self, cmd: &[String]) -> Option<RuleMatch>;
    fn as_any(&self) -> &dyn Any;
}
```

最常用的实现是 `PrefixRule`（`rule.rs:110`）：

```rust
pub struct PrefixRule {
    pub pattern: PrefixPattern,  // (first: Arc<str>, rest: Arc<[PatternToken]>)
    pub decision: Decision,
    pub justification: Option<String>,
}

impl Rule for PrefixRule {
    fn matches(&self, cmd: &[String]) -> Option<RuleMatch> {
        self.pattern.matches_prefix(cmd).map(|matched_prefix| RuleMatch::PrefixRuleMatch { ... })
    }
}
```

### 5.4 Starlark DSL：规则定义

`PolicyParser` 在 `execpolicy/src/parser.rs:38`，使用 Starlark 语言定义规则：

```python
prefix_rule(["git", "push"], decision="allow")
prefix_rule(["rm", "-rf"], decision="prompt")
prefix_rule(["dd"], decision="forbidden")
network_rule("api.github.com", protocol="https", decision="allow")
host_executable("python3", ["/usr/local/bin/python3", "/opt/homebrew/bin/python3"])
```

Starlark 是 Python 的子集，由 Bazel 推广。Codex 用它来定义规则——这是个聪明的选择：

- 比 JSON/YAML 表达力强（支持变量、条件）
- 比 DSL 通用（开发者已熟悉）
- 沙箱化（Starlark 本身就是设计成嵌入式语言）

### 5.5 ExecPolicyManager：core 集成

`core/src/exec_policy.rs:245`：

```rust
pub(crate) struct ExecPolicyManager {
    policy: ArcSwap<Policy>,
    update_lock: Semaphore,
}
```

`ArcSwap` 让 policy 可以原子热替换——用户改了规则后，新 turn 立刻用新规则。

### 5.6 命令评估流程

`create_exec_approval_requirement_for_command` 在 `exec_policy.rs:272`：

1. **解析命令**：可能 unwrap shell wrapper（`bash -lc "git push"` → `git push`）
2. **逐条评估**：通过 `check_multiple_with_options()` 评估每个命令
3. **未匹配规则**：通过 `render_decision_for_unmatched_command()` 用 safe/dangerous 启发式判断
4. **返回 `ExecApprovalRequirement`**：
   - `NeedsApproval`：需要用户审批
   - `Forbidden`：直接拒绝
   - `Skip`：跳过沙箱

### 5.7 ReviewDecision：用户响应

`ReviewDecision` 枚举在 `protocol.rs:3676`：

```rust
pub enum ReviewDecision {
    Approved,                                          // 执行
    ApprovedExecpolicyAmendment { proposed_execpolicy_amendment }, // 执行 + 持久化规则
    ApprovedForSession,                                 // 本会话内自动批准
    NetworkPolicyAmendment { network_policy_amendment }, // 持久化网络 allow/deny
    #[default] Denied,                                  // 跳过，继续会话
    TimedOut,                                           // 自动审批超时
    Abort,                                              // 拒绝 + 停止 agent
}
```

`ApprovedExecpolicyAmendment` 很有意思——用户不仅批准这一次，还可以选择"以后类似命令自动批准"。这是 Codex 把用户决策沉淀成规则的方式。

![ExecPolicy 决策流：Command → PrefixRule → Allow/Prompt/Forbidden](/images/codex/06-execpolicy.png)


## 六、Sandboxing：跨平台沙箱

### 6.1 SandboxType：4 种选择

`sandboxing/src/manager.rs:22`：

```rust
pub enum SandboxType {
    None,
    MacosSeatbelt,
    LinuxSeccomp,
    WindowsRestrictedToken,
}
```

平台选择 `get_platform_sandbox`（`manager.rs:48`）：

```rust
pub fn get_platform_sandbox(windows_sandbox_enabled: bool) -> Option<SandboxType> {
    if cfg!(target_os = "macos") { Some(SandboxType::MacosSeatbelt) }
    else if cfg!(target_os = "linux") { Some(SandboxType::LinuxSeccomp) }
    else if cfg!(target_os = "windows") && windows_sandbox_enabled { Some(SandboxType::WindowsRestrictedToken) }
    else { None }
}
```

### 6.2 SandboxManager：核心转换器

`SandboxManager` 在 `sandboxing/src/manager.rs:152`：

```rust
pub struct SandboxManager;

impl SandboxManager {
    pub fn select_initial(&self, ...) -> SandboxType { ... }
    
    pub fn transform(&self, request: SandboxTransformRequest<'_>) 
        -> Result<SandboxExecRequest, SandboxTransformError> 
    {
        // 1. 计算有效权限 profile（base + additional + MITM CA）
        // 2. 根据 SandboxType 分派：
        //    - None: 透传
        //    - MacosSeatbelt: create_seatbelt_command_args() + /usr/bin/sandbox-exec
        //    - LinuxSeccomp: create_linux_sandbox_command_args() + codex-linux-sandbox binary
        //    - WindowsRestrictedToken: 透传（进程级处理）
    }
}
```

`transform()` 是核心——它把用户的原始命令转换成沙箱包装后的命令。

### 6.3 macOS Seatbelt 实现细节

`sandboxing/src/seatbelt.rs:20-29` 定义常量：

```rust
const MACOS_SEATBELT_BASE_POLICY: &str = include_str!("seatbelt_base_policy.sbpl");
const MACOS_SEATBELT_NETWORK_POLICY: &str = include_str!("seatbelt_network_policy.sbpl");
```

`create_seatbelt_command_args` 在 `seatbelt.rs:602`：

```rust
pub fn create_seatbelt_command_args(args: CreateSeatbeltCommandArgsParams<'_>) -> Vec<String> {
    // 构建 file_read*, file_write*, unreadable_glob deny, network access policies
    // 作为 Seatbelt Scheme (.sbpl) 规则，最终产出：
    //   /usr/bin/sandbox-exec -p "<full_policy>" -DKEY=value ... -- <original_command>
}
```

生成 5 类策略：

1. Base restrictions
2. File read allow rules
3. File write allow rules
4. Deny-read glob rules
5. Network rules

最终调用 macOS 自带的 `/usr/bin/sandbox-exec` 二进制执行沙箱化命令。

### 6.4 Linux 沙箱

`sandboxing/src/landlock.rs:23`：

```rust
pub fn create_linux_sandbox_command_args_for_permission_profile(
    command, command_cwd, permission_profile, sandbox_policy_cwd, ...
) -> Vec<String> {
    // 把 PermissionProfile 序列化为 JSON，构建：
    //   codex-linux-sandbox --sandbox-policy-cwd <dir> --command-cwd <dir>
    //     --permission-profile <json> [--use-legacy-landlock] [--allow-network-for-proxy] -- <command>
}
```

Linux 用 `codex-linux-sandbox` 二进制实现，组合三种机制：

- **Landlock**：Linux 5.13+ 的内核级文件系统访问控制
- **seccomp**：系统调用过滤
- **Bubblewrap**：可选的容器级隔离

### 6.5 进程级加固

`process-hardening/src/lib.rs:12`：

```rust
pub fn pre_main_hardening() {
    // Linux: prctl(PR_SET_DUMPABLE, 0) + RLIMIT_CORE=0 + clear LD_* env vars
    // macOS: ptrace(PT_DENY_ATTACH) + RLIMIT_CORE=0 + clear DYLD_* env vars
    // BSD:   RLIMIT_CORE=0 + clear LD_* env vars
    // Windows: TODO
}
```

通过 `#[ctor::ctor]` 在 main 之前自动执行，做防御性加固：

- 禁止 core dump（防止敏感信息泄露）
- 拒绝 ptrace attach（防止调试器注入）
- 清理危险的 `LD_*` / `DYLD_*` 环境变量

![跨平台沙箱架构：macOS Seatbelt / Linux Landlock+seccomp / Windows RestrictedToken](/images/codex/06-sandbox.png)


## 七、Codex vs Claude Code：工具与安全对比

### 7.1 工具系统对比

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| **工具数量** | ~20+ 内置 + MCP + 扩展 + 动态 | ~30+ 内置 + MCP |
| **工具定义** | Rust trait + JSON Schema | Zod Schema |
| **可见性控制** | 4 种 ToolExposure | 无（全暴露） |
| **延迟工具** | ✅ tool_search 发现 | ❌ 无 |
| **MCP 集成** | ✅ McpConnectionManager | ✅ 类似机制 |
| **工具命名** | ToolName + namespace | 普通字符串 |
| **Pre/Post hooks** | ✅ CoreToolRuntime 内置 | ✅ 外部 hooks |
| **CSV 批处理** | ✅ spawn_agents_on_csv | ❌ 无 |

### 7.2 安全模型对比

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| **沙箱** | ✅ 跨平台（Seatbelt/Landlock/Windows） | ❌ 无沙箱 |
| **进程加固** | ✅ pre_main_hardening | ❌ 无 |
| **ExecPolicy** | ✅ Starlark DSL 规则 | ❌ 只有 PreToolUse hooks |
| **审批类型** | 7 种 ReviewDecision | Approve / Deny / 2 种 |
| **规则持久化** | ✅ ExecPolicyAmendment | ❌ 无 |
| **网络规则** | ✅ network_rule | ❌ 无 |
| **文件系统权限** | ✅ PermissionProfile | ❌ 无 |
| **host_executable** | ✅ 限定可执行路径 | ❌ 无 |

### 7.3 设计哲学差异

**CC 的"信任用户"哲学**：
- 不沙箱，依赖用户自己保证安全
- 通过 PreToolUse hooks 让用户自定义拦截
- 简单灵活，但需要用户自己负责

**Codex 的"默认安全"哲学**：
- 默认沙箱，需要用户主动放开
- ExecPolicy 用规则化的方式管理命令白名单/黑名单
- 进程级加固防御深度攻击
- 网络规则限制出站连接

这两个哲学的差异源于使用场景：

- **CC 假设**：开发者本地使用，自己对自己负责
- **Codex 假设**：可能被部署到 CI / 共享环境 / 远程服务器，必须默认安全

### 7.4 一个小细节：ApprovedExecpolicyAmendment

`ReviewDecision::ApprovedExecpolicyAmendment` 反映了一个有意思的设计——**用户的审批决策可以变成规则**。

用户每次批准 `rm -rf /tmp/*`，可以选择"以后类似命令自动批准"。这等于把用户的判断"沉淀"成 ExecPolicy 规则——一次决策，长期生效。

CC 没有这个机制——每次 `rm -rf /tmp/*` 都需要重新审批（除非用户写 PreToolUse hook 自己实现类似逻辑）。


## 八、小结

| 你学到什么 | 对应源码 |
|-----------|---------|
| `ToolExecutor` trait | `codex-rs/tools/src/tool_executor.rs:44` |
| `ToolExposure` 4 种可见性 | `tool_executor.rs:8` |
| `CoreToolRuntime` core 扩展 | `tools/registry.rs:48` |
| `ToolRegistry` HashMap | `tools/registry.rs:326` |
| `ToolRouter` 每 turn 包装 | `tools/router.rs:34` |
| `add_tool_sources` 6 类工具 | `tools/spec_plan.rs:527` |
| Tool dispatch pipeline | `tools/registry.rs:408-690` |
| `McpConnectionManager` | `codex-mcp/src/connection_manager.rs:105` |
| `list_all_tools` 聚合 | `connection_manager.rs:416` |
| `McpHandler` core 包装 | `tools/spec_plan.rs:765` |
| `Decision` 3 种 | `execpolicy/src/decision.rs:7` |
| `Policy` 规则集合 | `execpolicy/src/policy.rs:28` |
| `PrefixRule` 命令前缀匹配 | `execpolicy/src/rule.rs:110` |
| Starlark 规则 DSL | `execpolicy/src/parser.rs:38` |
| `ExecPolicyManager` 热替换 | `core/src/exec_policy.rs:245` |
| `ReviewDecision` 7 种 | `protocol.rs:3676` |
| `SandboxType` 4 种 | `sandboxing/src/manager.rs:22` |
| `SandboxManager::transform` | `sandboxing/src/manager.rs:152` |
| macOS Seatbelt 策略生成 | `sandboxing/src/seatbelt.rs:602` |
| Linux 沙箱命令 | `sandboxing/src/landlock.rs:23` |
| 进程加固 | `process-hardening/src/lib.rs:12` |

## 章节小测

<script setup>
const q = [
  {
    question: 'ToolExposure 的 4 种可见性级别设计的核心目的是什么？',
    options: ['将工具按内部实现和第三方扩展两种来源划分可见性层级', '控制工具对模型的初始暴露时机避免过长工具列表污染 prompt', '根据工具敏感级别对用户授予差异化的调用权限控制', '兼容不同底层模型 API 对工具可见性语义的差异化要求'],
    correct: 1,
    explanation: 'Direct 初始可见，Deferred 初始隐藏通过 tool_search 发现，DirectModelOnly 仅初始列表不进 code-mode，Hidden 只用于 dispatch。Deferred 的设计灵感是避免初始 tool 列表过长、污染模型的 prompt 空间。'
  },
  {
    question: 'Codex 的 ExecPolicy 使用 Starlark 语言定义规则（而非 JSON/YAML），这个设计的优势是什么？',
    options: ['Starlark 是 OpenAI 内部工程团队统一使用的标准配置语言', '表达力强且开发者熟悉且支持沙箱化执行比 JSON 更灵活', 'Starlark 的编译执行速度显著快于 JSON 和 YAML 解析器', 'JSON 和 YAML 格式无法表达网络规则类型的策略配置'],
    correct: 1,
    explanation: 'Starlark 是 Python 的子集，由 Bazel 推广。Codex 用它定义规则：比 JSON/YAML 表达力强（支持变量、条件），比自创 DSL 通用，且 Starlark 本身就是设计成嵌入式语言，天然沙箱化。'
  },
  {
    question: 'Claude Code 没有沙箱而 Codex 有完整跨平台沙箱，这个差异反映了两者什么不同的安全假设？',
    options: ['CC 团队判断沙箱功能在当前版本中优先级较低暂不实现', 'CC 假设可信环境本地使用而 Codex 假设不可信环境默认安全', 'CC 选择降低开发复杂度刻意省略非核心安全基础设施', 'CC 的 TypeScript 技术栈在底层技术上无法实现沙箱隔离'],
    correct: 1,
    explanation: 'CC 假设读者在本地开发，自己对自己负责，通过 PreToolUse hooks 让用户自定义拦截。Codex 假设代理可能被部署到 CI/共享环境/远程服务器，必须有默认安全（沙箱/ExecPolicy/进程加固/网络规则）。'
  },
  {
    question: 'ReviewDecision::ApprovedExecpolicyAmendment 这个设计体现了什么思想？',
    options: ['每次用户审批操作后必须以弹窗形式再次确认审批结果', '用户的一次审批决策可以沉淀为可持久化的 ExecPolicy 规则', '该选项表示用户无权修改已设定的 ExecPolicy 规则配置', '仅作为用户在误操作后恢复会话的紧急兜底恢复方案'],
    correct: 1,
    explanation: '用户批准 rm -rf /tmp/* 时，可以选择"以后类似命令自动批准"。这意味着把用户的单个决策"沉淀"成可持久化的 ExecPolicy 规则，一次决策长期生效。CC 没有这个机制，每次都要重新审批。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
