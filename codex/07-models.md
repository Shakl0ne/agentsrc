# 模型管理与 Provider 抽象

## 〇、引言

前面几篇我们看了 Codex 的主循环、上下文、Compact、多 Agent、工具/沙箱。这一篇覆盖一个看似 boring 但非常重要的主题——**模型管理**。

为什么重要？因为 Codex 支持多个模型后端（OpenAI / ChatGPT / Ollama / LM Studio / Amazon Bedrock），每个后端的认证、API 形态、能力都不同。如何统一抽象？如何切换？切换时怎么处理上下文？这些都是工程难题。

读完这篇你能回答：

1. `ModelProviderInfo` 怎么抽象多种后端？
2. `SharedModelsManager` 怎么管理模型列表的缓存和刷新？
3. `ModelClientSession` 为什么是 turn-scoped？sticky routing 是什么？
4. 切换模型时为什么有时会自动 compact？
5. ChatGPT OAuth 认证怎么实现？
6. 实时语音对话（WebRTC）怎么集成？


![Codex 4 Provider 架构：OpenAI / Bedrock / Ollama / LM Studio](/images/codex/07-hero.png)

## 一、ModelProviderInfo：核心抽象

### 1.1 结构定义

`ModelProviderInfo` 在 `model-provider-info/src/lib.rs:85`：

```rust
pub struct ModelProviderInfo {
    pub name: String,
    pub base_url: Option<String>,
    pub env_key: Option<String>,
    pub env_key_instructions: Option<String>,
    pub experimental_bearer_token: Option<String>,
    pub auth: Option<ModelProviderAuthInfo>,
    pub aws: Option<ModelProviderAwsAuthInfo>,
    pub wire_api: WireApi,
    pub query_params: Option<HashMap<String, String>>,
    pub http_headers: Option<HashMap<String, String>>,
    pub env_http_headers: Option<HashMap<String, String>>,
    pub request_max_retries: Option<u64>,
    pub stream_max_retries: Option<u64>,
    pub stream_idle_timeout_ms: Option<u64>,
    pub websocket_connect_timeout_ms: Option<u64>,
    pub requires_openai_auth: bool,
    pub supports_websockets: bool,
}
```

15+ 个字段覆盖了一个模型后端的所有配置：

- **认证**：env_key / auth / aws / requires_openai_auth
- **网络**：base_url / wire_api / query_params / http_headers
- **重试**：request_max_retries / stream_max_retries / stream_idle_timeout_ms
- **WebSocket**：supports_websockets / websocket_connect_timeout_ms

### 1.2 关键方法

`supports_remote_compaction()` 在 `lib.rs:394`：

```rust
pub fn supports_remote_compaction(&self) -> bool {
    self.is_openai() || is_azure_responses_provider(&self.name, self.base_url.as_deref())
}
```

这决定了第四篇讲的 Compact 实现选择——OpenAI 和 Azure Responses provider 走 remote compaction，其他走 local。

`to_api_provider(auth_mode)` 在 `lib.rs:237`，根据 auth mode 选择 base URL：

- ChatGPT 订阅用户：`https://chatgpt.com/backend-api/codex`
- OpenAI API key 用户：`https://api.openai.com/v1`

`api_key()` 在 `lib.rs:273`，从环境变量读取 API key。

### 1.3 4 种内置 Provider

`built_in_model_providers` 在 `lib.rs:410`：

```rust
pub fn built_in_model_providers(
    openai_base_url: Option<String>,
) -> HashMap<String, ModelProviderInfo> {
    use ModelProviderInfo as P;
    let openai_provider = P::create_openai_provider(openai_base_url);
    let amazon_bedrock_provider = P::create_amazon_bedrock_provider(/*aws*/ None);
    [
        (OPENAI_PROVIDER_ID, openai_provider),          // "openai"
        (AMAZON_BEDROCK_PROVIDER_ID, amazon_bedrock_provider), // "amazon-bedrock"
        (OLLAMA_OSS_PROVIDER_ID, create_oss_provider(...)),     // "ollama"
        (LMSTUDIO_OSS_PROVIDER_ID, create_oss_provider(...)),   // "lmstudio"
    ]
    .into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}
```

Provider ID 常量（`lib.rs:35-48`）：

```rust
OPENAI_PROVIDER_ID = "openai"
AMAZON_BEDROCK_PROVIDER_ID = "amazon-bedrock"
OLLAMA_OSS_PROVIDER_ID = "ollama"
LMSTUDIO_OSS_PROVIDER_ID = "lmstudio"
LEGACY_OLLAMA_CHAT_PROVIDER_ID = "ollama-chat"
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
```

### 1.4 OpenAI vs OSS Provider 差异

`create_openai_provider`（`lib.rs:319`）：

- `requires_openai_auth: true`
- `supports_websockets: true`
- 注入 `version` header 和 `OpenAI-Organization` / `OpenAI-Project` env headers

`create_oss_provider`（`lib.rs:476`）：

- 读取 `CODEX_OSS_PORT` / `CODEX_OSS_BASE_URL` 环境变量
- 默认 fallback 到 `localhost:{port}`
- `requires_openai_auth: false`
- `supports_websockets: false`

简言之：OpenAI 走 WebSocket + OAuth；OSS 走 HTTP + 无认证。

### 1.5 用户自定义 Provider

`merge_configured_model_providers`（`lib.rs:443`）让用户在 `config.toml` 里自定义 provider：

- 用户配置的 provider 扩展内置 set
- 只有 Amazon Bedrock 允许部分覆盖（aws profile/region）


## 二、SharedModelsManager：模型列表管理

### 2.1 Trait 定义

`ModelsManager` trait 在 `models-manager/src/manager.rs:78`：

```rust
#[async_trait]
pub trait ModelsManager: fmt::Debug + Send + Sync {
    async fn list_models(&self, refresh_strategy: RefreshStrategy) -> Vec<ModelPreset>;
    async fn raw_model_catalog(&self, refresh_strategy: RefreshStrategy) -> ModelsResponse;
    async fn get_remote_models(&self) -> Vec<ModelInfo>;
    fn try_get_remote_models(&self) -> Result<Vec<ModelInfo>, TryLockError>;
    fn auth_manager(&self) -> Option<&AuthManager>;
    async fn get_default_model(&self, model: &Option<String>, ...) -> String;
    async fn get_model_info(&self, model: &str, config: &ModelsManagerConfig) -> ModelInfo;
    async fn refresh_if_new_etag(&self, etag: String);
}
```

### 2.2 OpenAiModelsManager 实现

主要实现在 `manager.rs:182`：

```rust
pub struct OpenAiModelsManager {
    remote_models: RwLock<Vec<ModelInfo>>,       // 内存缓存
    etag: RwLock<Option<String>>,                 // 远程 ETag 用于条件刷新
    cache_manager: ModelsCacheManager,             // 磁盘缓存 (models_cache.json)
    endpoint_client: SharedModelsEndpointClient,   // 拉取远程 /models
    auth_manager: Option<Arc<AuthManager>>,
}
```

`SharedModelsManager`（`manager.rs:178`）就是 `Arc<dyn ModelsManager>`。

### 2.3 三种 Refresh 策略

`refresh_available_models` 在 `manager.rs:270`：

| 策略 | 行为 |
|------|------|
| `RefreshStrategy::Online` | 总是从网络拉取 |
| `RefreshStrategy::Offline` | 只从磁盘缓存加载 |
| `RefreshStrategy::OnlineIfUncached` | 优先缓存，fallback 到网络 |

### 2.4 磁盘缓存

`ModelsCacheManager` 在 `cache.rs:16`：

- **路径**：`$CODEX_HOME/models_cache.json`
- **TTL**：300 秒（5 分钟）
- **过期检查**：`fetched_at + TTL < now` 时认为过期
- **版本锁定**：`client_version` 不匹配时拒绝缓存

5 分钟 TTL 是个有意思的设计——既避免每次启动都拉网络，又不会让用户看到太旧的模型列表。


## 三、ModelClientSession：turn 级会话

### 3.1 结构定义

`ModelClientSession` 在 `core/src/client.rs:238`：

```rust
pub struct ModelClientSession {
    client: ModelClient,
    websocket_session: WebsocketSession,   // connection + last_request + last_response_rx
    turn_state: Arc<OnceLock<String>>,     // x-codex-turn-state sticky routing token
}
```

关键设计（doc comment，`client.rs:225`）：

1. **每个 turn 创建新的 session**——`ModelClient::new_session()`（`client.rs:381`）
2. **首次 stream 请求时懒加载 WebSocket 连接**
3. **同一 turn 内复用 WebSocket 连接**（多次请求共享）
4. **Sticky routing**：第一次响应收到 `x-codex-turn-state` token，后续请求都带上
5. **Drop 时把 WebSocket session 缓存回 ModelClient**——为下一个 turn 预热

### 3.2 WebsocketSession

`WebsocketSession` 在 `client.rs:261`：

```rust
struct WebsocketSession {
    connection: Option<ApiWebSocketConnection>,
    last_request: Option<ResponsesApiRequest>,
    last_response_rx: Option<oneshot::Receiver<LastResponse>>,
    last_response_from_untraced_warmup: bool,
    connection_reused: StdMutex<bool>,
}
```

注意 `last_request` / `last_response_rx`——这些是为了在 turn 内重试时能拿到上次响应。

### 3.3 为什么 turn-scoped？

`ModelClientSession` 是 turn-scoped 的原因（doc 注释）：

> ModelClientSession is turn-scoped and caches WebSocket + sticky routing state, so we reuse one instance across retries within this turn.

一个 turn 内可能有多次 sampling（因为 mid-turn compact 后会重新 sampling）。重用同一个 session 可以：

- 复用 WebSocket 连接（避免每次 reconnect）
- 保持 sticky routing（让同一 turn 的请求路由到同一台后端机器，状态更一致）
- 减少认证开销（不需要重新认证）

### 3.4 Sticky Routing 的作用

`x-codex-turn-state` 这个 header 让 OpenAI 服务端能识别"这是同一个 turn 的后续请求"——可以让服务端复用某些状态（如 prefix cache）。

这是个很微妙的优化——**客户端通过显式标记让服务端知道"我还在这个 turn 里"**，从而获得更稳定的延迟和命中率。

![ModelClientSession 生命周期：Turn-scoped + Sticky Routing + Warm Pool](/images/codex/07-session.png)


## 四、Model Switch → Auto Compact

### 4.1 触发逻辑

`maybe_run_previous_model_inline_compact` 在 `turn.rs:810`：

```rust
async fn maybe_run_previous_model_inline_compact(
    sess: &Arc<Session>,
    turn_context: &Arc<TurnContext>,
    client_session: &mut ModelClientSession,
) -> CodexResult<()> {
    let Some(previous_turn_settings) = sess.previous_turn_settings().await else {
        return Ok(());
    };
    let previous_model_turn_context = Arc::new(
        turn_context.with_model(previous_turn_settings.model, &sess.services.models_manager).await,
    );
    let Some(old_context_window) = previous_model_turn_context.model_context_window() else { return Ok(()); };
    let Some(new_context_window) = turn_context.model_context_window() else { return Ok(()); };
    let active_context_tokens = sess.get_total_token_usage().await;

    let previous_model_limit_reached = match turn_context.config.model_auto_compact_token_limit_scope {
        AutoCompactTokenLimitScope::Total => {
            let new_auto_compact_limit = turn_context.model_info.auto_compact_token_limit()
                .unwrap_or(i64::MAX);
            active_context_tokens > new_auto_compact_limit || active_context_tokens >= new_context_window
        }
        AutoCompactTokenLimitScope::BodyAfterPrefix => active_context_tokens >= new_context_window,
    };
    let should_run = previous_model_limit_reached
        && previous_model_turn_context.model_info.slug != turn_context.model_info.slug
        && old_context_window > new_context_window;
    if should_run {
        run_auto_compact(sess, &previous_model_turn_context, client_session,
            InitialContextInjection::DoNotInject, CompactionReason::ModelDownshift,
            CompactionPhase::PreTurn).await?;
    }
    Ok(())
}
```

### 4.2 三个条件都满足才会触发

`should_run = previous_model_limit_reached && model_changed && window_shrunk`：

1. **`previous_model_limit_reached`**——当前 token 已经超过新模型的上下文窗口
2. **`previous_model_turn_context.model_info.slug != turn_context.model_info.slug`**——模型确实切换了
3. **`old_context_window > new_context_window`**——从大窗口模型切到小窗口模型

### 4.3 为什么用"前一个模型"做 compact？

注意这一行：

```rust
run_auto_compact(sess, &previous_model_turn_context, ...)
```

调用 `run_auto_compact` 时传的是 **previous_model_turn_context**——用旧模型的上下文做压缩！

为什么？因为旧模型有更大的上下文窗口，能处理更长的历史。如果用新模型（小窗口）做压缩，可能历史都装不下，压缩自己就失败了。

这是一个**防御性的设计**——先让大窗口模型压缩历史，再切到小窗口模型继续工作。压缩理由 `CompactionReason::ModelDownshift` 准确描述了这个场景。

### 4.4 调用时机

这个函数在 `run_pre_sampling_compact`（`turn.rs:784`）里调用——**在普通 auto-compact 之前**。这意味着：

1. 先检查是否需要 model downshift compact（用旧模型）
2. 再检查是否需要普通 auto-compact（用新模型）

如果 model downshift 触发了，普通 auto-compact 大概率就不用了——因为压缩后 token 已经低于阈值。

![Model Downshift Compact 3 条件触发](/images/codex/07-downshift.png)


## 五、ChatGPT OAuth 认证

### 5.1 CodexAuth 枚举

`CodexAuth` 在 `login/src/auth/manager.rs:51`：

```rust
pub enum CodexAuth {
    ApiKey(ApiKeyAuth),
    Chatgpt(ChatgptAuth),             // OAuth with refresh tokens
    ChatgptAuthTokens(ChatgptAuthTokens), // external tokens (ephemeral)
    AgentIdentity(AgentIdentityAuth),
}
```

4 种认证方式：

1. **ApiKey**：传统 API key（环境变量或 auth.json）
2. **Chatgpt**：完整 OAuth flow + refresh token
3. **ChatgptAuthTokens**：外部传入的 ephemeral tokens（如 ChatGPT 桌面应用集成）
4. **AgentIdentity**：JWT 认证（用于 Codex agent 身份）

### 5.2 AuthManager：认证管理器

`AuthManager` 在 `manager.rs:1254`：

```rust
pub struct AuthManager {
    codex_home: PathBuf,
    inner: RwLock<CachedAuth>,                   // 缓存的 CodexAuth + 永久 refresh 失败
    auth_change_tx: watch::Sender<u64>,          // 认证变化时通知消费者
    enable_codex_api_key_env: bool,
    auth_credentials_store_mode: AuthCredentialsStoreMode,
    forced_chatgpt_workspace_id: RwLock<Option<Vec<String>>>,
    chatgpt_base_url: Option<String>,
    refresh_lock: Semaphore,                      // 串行化 token refresh
    external_auth: RwLock<Option<Arc<dyn ExternalAuth>>>, // 可插拔的外部 auth provider
}
```

### 5.3 认证加载优先级

`load_auth` 在 `manager.rs:733`：

1. **`CODEX_API_KEY`** 环境变量（最高优先级）
2. **Ephemeral store**（外部 ChatGPT auth tokens）
3. **`CODEX_ACCESS_TOKEN`** 环境变量（agent identity）
4. **Persistent store**（文件/keyring）

这个顺序很合理——环境变量优先（开发者显式覆盖），然后外部 ephemeral（IDE 集成场景），最后才是持久化存储。

### 5.4 OAuth Token Refresh

`request_chatgpt_token_refresh` 在 `manager.rs:817`：

POST 到 `https://auth.openai.com/oauth/token`，使用 `refresh_token` grant type。

失败分类：

- **`Expired`**：refresh token 过期，需要重新 OAuth
- **`Exhausted`**：用量耗尽
- **`Revoked`**：token 被吊销
- **`Other`**：其他错误

### 5.5 Token 存储后端

`storage.rs` 提供多种存储后端：

- **`FileAuthStorage`**：`auth.json` 文件，权限 0600
- **`KeyringAuthStorage`**：系统 keyring（macOS Keychain / Windows Credential Manager / Linux Secret Service）
- **`AutoAuthStorage`**：keyring + 文件 fallback
- **`EphemeralAuthStorage`**：全局内存 HashMap

### 5.6 Device Code OAuth Flow

`device_code_auth.rs` 实现设备代码 OAuth：

1. `request_device_code()`——请求设备码
2. 用户访问 URL + 输入码
3. `poll_for_token()`——轮询 token
4. PKCE code exchange
5. `persist_tokens_async()`——持久化

这是经典的 device code flow——TV / CLI 应用的标准 OAuth 模式。

![Auth 加载优先级栈：API_KEY > Ephemeral > ACCESS_TOKEN > Persistent](/images/codex/07-auth.png)


## 六、Realtime WebRTC：实时语音对话

### 6.1 入口

`RealtimeWebrtcSession::start()` 在 `realtime-webrtc/src/lib.rs:72`：

```rust
pub fn start() -> Result<StartedRealtimeWebrtcSession> {
    // macOS only (libwebrtc native)
    let started = native::start()?;
    Ok(StartedRealtimeWebrtcSession {
        offer_sdp: started.offer_sdp,     // SDP offer 发给服务端
        handle: RealtimeWebrtcSessionHandle { inner: started.handle, ... },
        events: started.events,           // mpsc channel for Connected/LocalAudioLevel/Closed/Failed
    })
}
```

### 6.2 Native 实现

`worker_main` 在 `native.rs:78`：

- 创建独立的 tokio runtime + thread（`codex-realtime-webrtc`）
- 创建 `PeerConnection` + 音频 transceiver（`SendRecv`）
- 生成 offer SDP
- 监听命令：
  - `ApplyAnswer`（`native.rs:112`）：设置 remote SDP，触发 `Connected` 事件，开始音频电平轮询
  - `Close`（`native.rs:124`）：关闭 peer connection，触发 `Closed` 事件

### 6.3 音频电平轮询

`local_audio_level` 在 `native.rs:215`：

每 200ms 调用 `peer_connection.get_stats()`，从 `MediaSource` stats 拿 `audio_level`（0.0–1.0），转成 peak `u16`。

这个音频电平事件用来在 UI 上显示"用户正在说话"——典型的语音对话 UX。

### 6.4 事件类型

`RealtimeWebrtcEvent` 在 `lib.rs:18`：

```rust
pub enum RealtimeWebrtcEvent {
    Connected,
    LocalAudioLevel(u16),
    Closed,
    Failed(String),
}
```

4 种事件覆盖了 WebRTC 会话的所有状态。

### 6.5 平台限制

**macOS only**（`#[cfg(target_os = "macos")]`），其他平台返回 `UnsupportedPlatform` 错误。

这是因为 libwebrtc 的 native 绑定目前只在 macOS 上构建。Linux/Windows 用户暂时不能用语音对话。


## 七、Codex vs Claude Code：模型管理对比

### 7.1 完整对比

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| **支持后端** | 4 个内置（OpenAI / Bedrock / Ollama / LM Studio）+ 自定义 | 1 个（Anthropic） |
| **认证方式** | 4 种（API key / OAuth / External / JWT） | 1 种（API key） |
| **模型列表缓存** | ✅ 5 分钟 TTL 磁盘缓存 | ❌ 无（硬编码） |
| **WebSocket 支持** | ✅ Responses API WebSocket | ❌ HTTP only |
| **Sticky routing** | ✅ x-codex-turn-state | ❌ 无 |
| **Model switch auto-compact** | ✅ ModelDownshift 触发 | ❌ 不支持切换 |
| **语音对话** | ✅ WebRTC（macOS only） | ❌ 无 |
| **Provider 抽象** | ✅ ModelProviderInfo | ❌ 无（单一后端） |
| **OAuth flow** | ✅ Device Code + PKCE | ❌ 无 |

### 7.2 设计哲学差异

**CC 的"单一后端"哲学**：

- 只支持 Anthropic，深度优化
- 简单，不需要 provider 抽象
- 认证只用 API key

**Codex 的"多后端"哲学**：

- 支持多家 provider，包括本地模型
- 需要 provider 抽象层
- 认证方式多样（OAuth / API key / External）
- 处理模型切换的边界情况（如 downshift compact）

这两种哲学各有道理：

- CC 的简单性让它能更快迭代新功能（不用考虑多后端兼容）
- Codex 的多后端让它能服务更多场景（如本地模型用户、ChatGPT 订阅用户）

### 7.3 一个独特点：Model Downshift Compact

`ModelDownshift` 是 Codex 独有的——CC 不支持模型切换，所以不会有这个问题。

但这个设计揭示了一个通用原则：**模型切换不是 free 的**。从大窗口切到小窗口，可能需要先压缩。这个发现对未来的多模型 agent 系统都有借鉴意义。


## 八、小结

| 你学到什么 | 对应源码 |
|-----------|---------|
| `ModelProviderInfo` 15+ 字段 | `model-provider-info/src/lib.rs:85` |
| `supports_remote_compaction()` | `lib.rs:394` |
| 4 种内置 Provider | `lib.rs:410` (`built_in_model_providers`) |
| Provider ID 常量 | `lib.rs:35-48` |
| `ModelsManager` trait | `models-manager/src/manager.rs:78` |
| `OpenAiModelsManager` | `manager.rs:182` |
| 3 种 Refresh 策略 | `manager.rs:270` |
| 磁盘缓存 5 分钟 TTL | `cache.rs:16` (`ModelsCacheManager`) |
| `ModelClientSession` turn-scoped | `core/src/client.rs:238` |
| Sticky routing `x-codex-turn-state` | `client.rs:225` (doc) |
| `WebsocketSession` | `client.rs:261` |
| Model downshift compact | `turn.rs:810` (`maybe_run_previous_model_inline_compact`) |
| 3 个触发条件 | `turn.rs:850-854` |
| `CodexAuth` 4 种 | `login/src/auth/manager.rs:51` |
| `AuthManager` | `manager.rs:1254` |
| 认证加载优先级 | `manager.rs:733` |
| 4 种 token 存储后端 | `login/src/auth/storage.rs` |
| Device Code OAuth | `login/src/auth/device_code_auth.rs` |
| WebRTC 入口 | `realtime-webrtc/src/lib.rs:72` |
| macOS only 限制 | `#[cfg(target_os = "macos")]` |
| 音频电平 200ms 轮询 | `realtime-webrtc/src/native.rs:215` |

## 章节小测

<script setup>
const q = [
  {
    question: 'ModelClientSession 为什么是 turn-scoped（每个 turn 创建新 session）的？',
    options: ['简化 session 的创建与销毁生命周期减少状态管理负担', '复用 WebSocket 连接并保持 sticky routing 且减少认证开销', '底层模型 API 强制要求每次新 turn 携带全新认证凭据', '为在负载均衡场景下实现请求分发提供更细粒度的控制'],
    correct: 1,
    explanation: '每个 turn 创建新 session，但同一 turn 内复用 WebSocket 连接和 sticky routing。这样 mid-turn compact 后重新 sampling 时可以复用连接、保持请求路由到同一台后端机器以获得更稳定的延迟和 cache 命中率。'
  },
  {
    question: 'Sticky routing 中 x-codex-turn-state header 的作用是什么？',
    options: ['作为请求来源用户的身份凭据实现认证与鉴权', '标记同一 turn 内后续请求以复用服务端 prefix cache', '用于后端 API 对客户端请求实施速率限制与配额计算', '跟踪不同版本的 Responses API 以保证请求接口兼容性'],
    correct: 1,
    explanation: '客户端通过显式标记 x-codex-turn-state 告诉服务端"我还在这个 turn 里"。这是个微妙的优化——让服务端知道请求属于同一 turn，从而复用某些服务端状态如 prefix cache。'
  },
  {
    question: '模型切换触发自动 compact 的三个条件分别是什么？为什么用旧模型的上下文做压缩？',
    options: ['任何模型切换均无条件触发 compact 且优先用新模型做压缩', 'token 超新窗口模型变更窗口缩小旧模型保障压缩成功', '仅在切到本地 OSS 模型时触发且由新模型执行压缩更准确', '仅在用户手动切换模型时触发两模型效果无实质差别'],
    correct: 1,
    explanation: '三个条件：当前 token 已超新模型窗口、模型确实切换了、从大窗口切到小窗口。用旧模型（大窗口）做压缩是防御性设计——旧模型能处理更长的历史，如果用新模型（小窗口）做压缩可能连历史都装不下。'
  },
  {
    question: 'Codex 支持 4 种模型 Provider（OpenAI/Bedrock/Ollama/LM Studio）而 Claude Code 只支持 Anthropic，这个差异带来的工程复杂度体现在哪里？',
    options: ['各 Provider 仅配置项不同整体工程复杂度差异并不大', '多后端带来 Provider 抽象模型缓存及认证多样性等复杂度', '支持多后端因可复用同一套代码设计反而更加简单', 'CC 实际也支持多后端只是官方文档未对外公开说明'],
    correct: 1,
    explanation: '多后端带来一系列连锁工程难题：统一的 Provider 抽象、模型缓存与刷新、多种认证方式、模型切换时的上下文窗口适配（downshift compact）。CC 单一后端深度优化，不需要这些抽象层，迭代更快。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
