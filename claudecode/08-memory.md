---
title: Claude Code 记忆系统与上下文注入
---

# Claude Code 记忆系统与上下文注入

第二天打开新会话，模型忘了昨天的约定；换了个 worktree，上周存的偏好也找不到了；团队里每个人都在教它同一件事，谁也没法让它记住。模型本身无状态，每一次 API 调用都是从零开始的推理——这些「记得住」的体验，全部依赖外部注入的上下文。

一套无状态的模型，怎么在单个会话、单个项目、单个组织这些不同尺度上保持记忆，还不在每一轮都把 prompt cache 打爆？

Claude Code 的做法是把记忆按生命周期拆开：会话级的系统上下文、项目级的 `CLAUDE.md`、跨会话的 memdir、单会话内的 SessionMemory、空闲时段的 AutoDream、文档自维护的 MagicDocs，每个子系统管好自己的存储位置、触发时机和失效策略，再统一收口到上下文注入的入口。

这一篇就拆这些子系统的分层、触发时机与安全边界，顺带看 prompt cache 这条隐含的约束如何反过来塑造注入的写法：

- `CLAUDE.md` 的六层来源怎么定优先级，注入时机怎么不破坏缓存？
- memdir 按什么键组织跨会话记忆，又怎么防止目录被指向敏感位置？
- 空闲时段的 AutoDream 用什么节奏和什么样的锁？

压缩消费侧（sessionMemoryCompact 的零 LLM 路径）第四篇已经拆过，本篇只讲记忆怎么被提取与注入。这也是系列的最后一篇，结尾会回到八篇的整体框架做个收束。

## 一、注入的入口：context.ts

整个上下文注入的入口是 `src/context.ts`（189 行），只导出两个核心函数，都通过 `lodash-es/memoize` 做了会话级缓存：

```ts
// src/context.ts:155-189（简化）
export const getUserContext = memoize(async (): Promise<{
  [k: string]: string
}> => {
  const shouldDisableClaudeMd =
    isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_CLAUDE_MDS) ||
    (isBareMode() && getAdditionalDirectoriesForClaudeMd().length === 0)
  const claudeMd = shouldDisableClaudeMd
    ? null
    : getClaudeMds(filterInjectedMemoryFiles(await getMemoryFiles()))
  setCachedClaudeMdContent(claudeMd || null)
  return {
    ...(claudeMd && { claudeMd }),
    currentDate: `Today's date is ${getLocalISODate()}.`,
  }
})
```

这两个函数返回字符串字典，`queryContext.ts` 的 `fetchSystemPromptParts()` 把它们与系统提示一起 `Promise.all` 并行取回，作为 cache-key 前缀的一部分。`memoize` 的语义是「整个会话只算一次」——压缩发生后，`postCompactCleanup.ts` 会主动调用 `getUserContext.cache.clear?.()` 和 `resetGetMemoryFilesCache('compact')` 清掉缓存，强制下一轮重新读取，否则压缩期间写入的新 `CLAUDE.md` 不会被模型看到。

`getSystemContext` 的内容相对静态，主要由 `getGitStatus()` 提供：分支名、默认分支、`git status --short`（截断到 2000 字符）、最近 5 条 commit、git user.name。所有 git 命令都带 `--no-optional-locks`，避免与其它进程争抢 `.git/index.lock`。CCR 远程模式下整段跳过——远程容器里没有 git 状态可读。`cacheBreaker` 字段只在内部调试时注入，作用是强制破坏 prompt cache 以便测试新提示词效果。

`getUserContext` 里有两处细节值得停一下。第一，`--bare` 模式（`CLAUDE_CODE_SIMPLE`）会跳过 `CLAUDE.md` 的自动发现，但用户显式通过 `--add-dir <path>` 指定的目录仍然会加载——跳过我没要的、保留我显式要的，语义边界划得很准。第二，`setCachedClaudeMdContent()` 把内容缓存到 `bootstrap/state.ts` 的 `STATE.cachedClaudeMdContent`，注释写明是给 `yoloClassifier.ts` 用的——权限分类器需要 `CLAUDE.md` 内容，但直接 import `claudemd.ts` 会形成 `yoloClassifier → claudemd → filesystem → permissions` 的循环依赖，所以走 state 中转。

## 二、CLAUDE.md：六层来源与覆盖语义

`CLAUDE.md` 是项目级指令文件，加载顺序定义在 `src/utils/claudemd.ts`（1479 行）顶部的注释里，按优先级从低到高：

| 类型 | 来源 | 说明 |
|------|------|------|
| Managed | `/etc/claude-code/CLAUDE.md` 与托管 `.claude/rules/*.md` | 全局策略，企业部署用 |
| User | `~/.claude/CLAUDE.md` 与 `~/.claude/rules/*.md` | 用户跨项目私有指令 |
| Project | 从 cwd 向上每一级的 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md` | 仓库内签入的指令 |
| Local | 从 cwd 向上每一级的 `CLAUDE.local.md` | 个人项目级私有指令（不签入） |
| AutoMem | `{memoryBase}/projects/{sanitized-git-root}/memory/MEMORY.md` | 自动记忆入口（memdir） |
| TeamMem | `{autoMemPath}/team/MEMORY.md` | 团队共享记忆 |

`getMemoryFiles()` 是这个体系的总入口，同样被 memoize。执行顺序是：先读 Managed 与 User 级的固定路径，再从 `getOriginalCwd()` 向上收集目录到根，每一级尝试读 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md`、`CLAUDE.local.md`；收集完后 `dirs.reverse()`，自根向 cwd 逐级处理，最后把 AutoMem 与 TeamMem 的 `MEMORY.md` 追加到末尾。靠近当前目录的文件最后拼接，而 `getClaudeMds()` 按数组顺序拼接、后出现的优先级更高——所以越靠近 cwd 的指令权重越大，子目录的项目指令覆盖父目录的通用指令。这个遍历方向与 git 从子目录向上找仓库根的习惯一致，用户不需要额外学习。

![六层记忆来源的注入顺序](/images/claudecode/08-memory-layers.svg)

两个特殊路径处理容易踩坑。第一是 **worktree 去重**：当 cwd 在 git worktree 内（`.claude/worktrees/<name>/`）且 worktree 又嵌在主仓库目录下时，向上走会同时穿过 worktree 根和主仓库根，两份 `CLAUDE.md` 会被加载两次。源码通过 `findCanonicalGitRoot()` 检测这种情况，跳过主仓库目录里、worktree 之外的 Project 类文件，只保留 `CLAUDE.local.md`（它被 gitignore，主仓库里那份才是有效的）。第二是 **`--add-dir` 路径**：只有 `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` 环境变量为真时才会扫描额外目录里的 `CLAUDE.md`——默认关闭，避免一个 `--add-dir /tmp/foo` 就把任意目录的指令塞进上下文。

`@include` 指令是另一处设计。`CLAUDE.md` 里可以写 `@./relative/path`、`@~/home/path`、`@/absolute/path` 来包含其它文件，被包含的文件作为独立条目追加在包含者之后——文件顶部的注释写的是「插入到包含者之前」，实现里 `processMemoryFile` 先 push 主文件再 push includes，注释过时了，以实现为准。扩展名白名单有 100 多个（`.md`、`.ts`、`.py`、`.go`、`.rs` 等几乎所有源码与配置格式），但拒绝二进制——避免把 PDF 或图片塞进上下文。循环引用通过 `processedPaths` Set 防住，不存在的文件静默忽略。

`getClaudeMds()` 把所有文件拼成最终字符串，每个文件前加 `Contents of {path} ({type} instructions):` 前缀。type 描述区分 Project（"checked into the codebase"）、Local（"user's private project instructions, not checked in"）、AutoMem（"persists across conversations"）、TeamMem（"synced across the organization"）。显式标注让模型能区分哪些指令是团队共识、哪些是个体偏好。TeamMem 内容还会被包进 `<team-memory-content source="shared">` 标签，进一步强化「跨组织同步」的语义。

文件列表会被 `MEMORY_INSTRUCTION_PROMPT` 前缀约束：「These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.」这句声明让 `CLAUDE.md` 在模型决策中拥有高于默认行为的优先级。单文件内容上限是 `MAX_MEMORY_CHARACTER_COUNT = 40000` 字符，超限文件被 `getLargeMemoryFiles()` 标记，在状态栏与 `/context` 可视化中提示用户拆分。`getMemoryFilesForNestedDirectory()` 还支持按目标文件路径做条件规则匹配——frontmatter 里的 `paths` 字段可以是 glob 模式，只有当用户正在编辑的文件匹配该模式时，对应的 `.claude/rules/*.md` 才被加载。按需注入避免了把所有规则一次性塞进上下文。

`filterInjectedMemoryFiles()` 是 `tengu_moth_copse` feature flag 控制的开关：开启时，AutoMem 与 TeamMem 不再注入到系统提示，改由 `findRelevantMemories()` 按 query 召回、作为 attachment 注入。这是从「全量塞」到「按需召回」的策略切换，目的是砍掉无关记忆占用的上下文。另一条独立的 flag `tengu_paper_halyard` 在 `getClaudeMds()` 内被读取——开启时跳过 Project 与 Local 类型文件，让项目级指令也走 attachment 路径。两个 flag 组合出四种注入策略，对应不同的实验分组。

`resetGetMemoryFilesCache()` 与 `clearMemoryFileCaches()` 是两个容易混淆的失效接口。前者带 `InstructionsLoadReason` 参数（`'session_start'` / `'compact'`），会重新武装 `InstructionsLoaded` hook，让下一次 `getMemoryFiles()` miss 时触发钩子通知——压缩后需要这种通知让 UI 知道指令被重新加载了。后者只清缓存不触发 hook，用于 worktree 切换、设置同步、`/memory` 对话框等「纯正确性失效、不需要通知」的场景。区分这两个接口，避免了 UI 在每次缓存失效时都被无意义地刷新。

## 三、memdir：跨会话的记忆目录

`memdir/` 是 CC 跨会话持久化记忆的核心，目录结构如下：

```
{memoryBase}/projects/{sanitized-git-root}/memory/
├── MEMORY.md          # 入口索引（自动加载到系统提示）
├── user_role.md       # 单条记忆，带 frontmatter
├── feedback_testing.md
├── project_auth.md
├── reference_docs.md
├── logs/2026/07/2026-07-23.md   # KAIROS 模式下的 append-only 日志
└── team/              # TeamMem 子目录（feature TEAMMEM）
    └── MEMORY.md
```

`memoryBase` 由 `getMemoryBaseDir()` 决定：`CLAUDE_CODE_REMOTE_MEMORY_DIR` 环境变量（CCR 用）优先，否则是 `~/.claude`。项目段用 `sanitizePath(getAutoMemBase())` 做路径清洗，`getAutoMemBase()` 走 `findCanonicalGitRoot()`——同一个 git 仓库的所有 worktree 共享同一个 memory 目录，不会因为 worktree 路径不同而记忆分裂。`getAutoMemPath()` 被 memoize，key 是 `getProjectRoot()`。

`isAutoMemoryEnabled()` 的判定链体现了对关闭路径的细致处理：

```ts
// src/memdir/paths.ts:30-55
export function isAutoMemoryEnabled(): boolean {
  if (isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY)) return false
  if (isEnvDefinedFalsy(envVal)) return true
  if (isEnvTruthy(process.env.CLAUDE_CODE_SIMPLE)) return false   // --bare
  if (isEnvTruthy(process.env.CLAUDE_CODE_REMOTE) &&
      !process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR) return false    // CCR 无持久存储
  if (settings.autoMemoryEnabled !== undefined) return settings.autoMemoryEnabled
  return true  // 默认开
}
```

路径解析还有一道安全关卡。`validateMemoryPath()` 拒绝相对路径、根路径、Windows 驱动器根（`C:`）、UNC 路径（`\\server\share`）、包含 null 字节的路径——这些都能让 memory 目录被指到 `~/.ssh` 等敏感位置。settings.json 的 `autoMemoryDirectory` 字段支持 `~/` 展开，但 `projectSettings`（仓库内签入的 `.claude/settings.json`）被显式排除——恶意仓库不能通过签入 settings 把 memory 目录指向敏感位置，因为那会让 `filesystem.ts` 的写权限 carve-out（`isAutoMemPath` 匹配时跳过 `DANGEROUS_DIRECTORIES` 检查）变成攻击面。`CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 环境变量是另一条覆盖路径，但同样不享受写权限 carve-out——`hasAutoMemPathOverride()` 返回 true 时，`filesystem.ts` 不会跳过危险目录检查。

记忆类型被约束在一个闭集四元组里（`memoryTypes.ts`）：`user`、`feedback`、`project`、`reference`。每种类型都声明 `<when_to_save>`、`<how_to_use>`、`<examples>`，feedback 与 project 另有 `<body_structure>`，喂给模型后让它知道何时该写哪种记忆。`WHAT_NOT_TO_SAVE_SECTION` 显式禁止把代码模式、git 历史、debugging 解决方案、`CLAUDE.md` 已有内容、临时任务状态写成记忆——这些都能从当前项目状态派生，存进 memdir 是冗余。注释里还有一条强化条款：「These exclusions apply even when the user explicitly asks you to save.」用户说「把这周的 PR 列表存下来」，模型也应该追问「什么是 surprising 或 non-obvious 的部分」，而非机械地把活动日志写进去。

`TRUSTING_RECALL_SECTION` 是 eval 验证过的关键段落：模型读到记忆里写的「函数 X 存在」时，必须先 `grep` 确认才推荐，因为记忆是写入时刻的快照，函数可能已被重命名或删除。eval 数据显示，把这段话放在独立 section 下（标题用「Before recommending from memory」而非「Trusting what you recall」）能把准确率从 0/3 提升到 3/3——同样的正文，标题的语义触发点不同，效果天差地别。这段经验对任何做 prompt 工程的人都有参考价值。

`MEMORY.md` 入口有双重截断保护：`MAX_ENTRYPOINT_LINES = 200` 行、`MAX_ENTRYPOINT_BYTES = 25_000` 字节。`truncateEntrypointContent()` 先按行截（自然边界），再按字节截到上一个换行符，最后附上一段警告说明哪条限制被触发。这是 p97/p100 长尾防护：实测 p97 在 200 行内，但 p100 出现过 197KB 的失控索引。

`loadMemoryPrompt()` 是把 memdir 内容拼进系统提示的入口（被 `systemPromptSection('memory', ...)` 缓存）。它的派发逻辑反映了几种并存的记忆形态：

```ts
// src/memdir/memdir.ts:419-506（简化）
export async function loadMemoryPrompt(): Promise<string | null> {
  if (feature('KAIROS') && autoEnabled && getKairosActive()) {
    return buildAssistantDailyLogPrompt(skipIndex)  // 助手模式：append-only 日志
  }
  if (feature('TEAMMEM') && teamMemPaths!.isTeamMemoryEnabled()) {
    await ensureMemoryDirExists(teamDir)
    return teamMemPrompts!.buildCombinedMemoryPrompt(...)  // 个人+团队合并
  }
  if (autoEnabled) {
    await ensureMemoryDirExists(autoDir)
    return buildMemoryLines('auto memory', autoDir, ...).join('\n')
  }
  return null
}
```

KAIROS（助手模式）走 append-only daily log：记忆写到 `logs/YYYY/MM/YYYY-MM-DD.md`，每晚 `/dream` skill 把日志蒸馏成 topic 文件与 `MEMORY.md`。注释解释了为什么 prompt 里写的是 `YYYY-MM-DD` 模式而非具体日期：prompt 被 `systemPromptSection('memory', ...)` 缓存，跨午夜不失效，模型从上下文里的日期信号（午夜翻转时追加的 date_change 附件）推断今天，user-context 的 `currentDate` 反而故意保持 stale 以守住缓存前缀。cache 感知在这里落到了 prompt 的措辞层面。

`ensureMemoryDirExists()` 是「harness 保证目录存在」的承诺——prompt 里明确写「This directory already exists — write to it directly with the Write tool (do not run mkdir or check for its existence)」，避免模型浪费一轮工具调用去 `ls`/`mkdir -p`。

召回侧由 `findRelevantMemories.ts` 实现（`tengu_moth_copse` 开启时生效）：`scanMemoryFiles()` 扫描目录里所有 `.md`（排除 `MEMORY.md`），读取每个文件前 `FRONTMATTER_MAX_LINES = 30` 行的 frontmatter，按 mtime 倒序保留 `MAX_MEMORY_FILES = 200` 个。然后构造 manifest 交给 Sonnet 通过 `sideQuery` 选最多 5 个最相关的，`querySource: 'memdir_relevance'`。`recentTools` 参数让 selector 跳过正在使用的工具的 reference 文档——模型已经在用这个工具了，再注入它的用法文档是噪音。`alreadySurfaced` 集合在 selector 调用前就过滤掉前几轮已经展示过的文件，避免 selector 把 5 个 slot 浪费在重复选择上。manifest 格式是 `[type] filename (timestamp): description` 的一行式列表。即使 selector 返回空列表也会触发 `logMemoryRecallShape()` 遥测——`selection-rate` 需要分母来区分「跑了但没选」与「压根没跑」。

另一条记忆注入路径是 `extractMemories.ts`（在 stop hook 中触发）。它与主对话的内存写入是互补关系：当主对话在某轮里已经写了 memory 文件，`hasMemoryWritesSince()` 检测到这一情况后跳过该区间，避免重复提取。源码注释把这个关系写得很直白：「主 agent 的 prompt 总是包含完整的保存指令，无论 extractMemories 是否开启；当主 agent 写了记忆，后台 agent 跳过那段；当它没写，后台 agent 补上漏掉的。」这种主加补的双轨设计让记忆提取既不依赖主 agent 的主动性，也不会在主 agent 已经做了的情况下重复劳动。`createAutoMemCanUseTool()` 在 extractMemories 与 autoDream 中都被复用——`Read`/`Grep`/`Glob` 不限路径放行，`Edit`/`Write` 限定在 memory 目录内，Bash 只放行只读命令，其它一律 deny。

## 四、SessionMemory：会话内的实时提取

`src/services/SessionMemory/` 与 memdir 的定位不同：memdir 跨会话持久化，写一次下次会话还看得到；SessionMemory 是当前会话内的草稿，产物服务于 Level 5 的 `sessionMemoryCompact`。压缩怎么消费这份产物，第四篇拆过了；这里看产物怎么来的。

`sessionMemory.ts` 的核心是 `extractSessionMemory` 这个 post-sampling hook：

```ts
// src/services/SessionMemory/sessionMemory.ts:272-350（简化）
const extractSessionMemory = sequential(async function (context: REPLHookContext) {
  const { messages, querySource } = context
  if (querySource !== 'repl_main_thread') return  // 只在主线程跑
  if (!isSessionMemoryGateEnabled()) return       // GB: tengu_session_memory
  initSessionMemoryConfigIfNeeded()                // 懒加载配置
  if (!shouldExtractMemory(messages)) return
  const setupContext = createSubagentContext(toolUseContext)
  const { memoryPath, currentMemory } = await setupSessionMemoryFile(setupContext)
  const userPrompt = await buildSessionMemoryUpdatePrompt(currentMemory, memoryPath)
  await runForkedAgent({
    promptMessages: [createUserMessage({ content: userPrompt })],
    cacheSafeParams: createCacheSafeParams(context),
    canUseTool: createMemoryFileCanUseTool(memoryPath),  // 只允许 Edit memoryPath
    querySource: 'session_memory',
    forkLabel: 'session_memory',
    overrides: { readFileState: setupContext.readFileState },
  })
  updateLastSummarizedMessageIdIfSafe(messages)
})
```

触发节奏由配置控制：`minimumMessageTokensToInit: 10000`（对话达 10K tokens 才初始化）、`minimumTokensBetweenUpdate: 5000`（每增长 5K tokens 更新一次）、`toolCallsBetweenUpdates: 3`。token 计数走 `tokenCountWithEstimation()`——和 autoCompact 用同一个口径，确保两边的阈值判断一致。触发条件是「tokens 增长 且 工具调用达标」或「tokens 增长 且 上一轮无工具调用」，后者在自然对话断点（模型纯文本回复）时强制触发，保证关键转折点能被捕获。

SessionMemory 的模板是一份 10 段固定结构（`prompts.ts`）：Session Title、Current State、Task specification、Files and Functions、Workflow、Errors & Corrections、Codebase and System Documentation、Learnings、Key results、Worklog。prompt 严格要求模型「不能改 section header、不能改斜体描述、只能更新描述下方的内容」。`MAX_SECTION_LENGTH = 2000` 限制单段长度，`MAX_TOTAL_SESSION_MEMORY_TOKENS = 12000` 限制总量，超预算时 prompt 会追加一段 CRITICAL 指令要求压缩。模板可被 `~/.claude/session-memory/config/template.md` 覆盖。

`createMemoryFileCanUseTool()` 只允许 `FileEditTool` 修改 SessionMemory 文件本身，其它所有工具一律 deny——摘要把对话上下文写歪的风险被工具白名单挡住了：模型不能借摘要之机执行任意代码、读任意文件、改任意文件。`runForkedAgent` 复用主对话的 prompt cache（通过 `createCacheSafeParams`），让提取本身的 token 成本大幅降低。`createSubagentContext()` 克隆一份 `toolUseContext`，forked agent 对 `readFileState` 的修改通过 `overrides` 显式传入克隆副本，不污染父上下文。

`updateLastSummarizedMessageIdIfSafe()` 只在「最后一条 assistant 消息没有 tool_use 块」时更新 `lastSummarizedMessageId`——避免压缩切分点落在 tool_use/tool_result 配对中间，产生孤儿 `tool_result`。`waitForSessionMemoryExtraction()` 提供 15 秒超时等待，超过 1 分钟视为 stale 直接放弃，保证压缩时不会因为 SessionMemory 卡死而无限阻塞。`initSessionMemory()` 在 setup 阶段同步注册 hook，但 gate 检查与配置加载都延迟到 hook 运行时才做——避免启动阶段被 GrowthBook 网络请求阻塞，代价是首轮对话可能用 stale 的配置值。还有一条前置：`isAutoCompactEnabled()` 也是 SessionMemory 启动的条件——用户关了 autoCompact，SessionMemory 也不会初始化，因为它存在的唯一目的就是服务于压缩。

恢复会话（`--resume`）时 `lastSummarizedMessageId` 因进程重启丢失的兜底处理，第四篇 §7.4 已经拆过，这里不重复。

## 五、AutoDream：空闲时段的记忆整合

`src/services/autoDream/` 把「睡觉时整理记忆」这个比喻落到了代码里。`autoDream.ts` 在每次 stop hook 后被 `executeAutoDream()` 调用，实际触发要过三道闸门：

![三道闸门按成本递增排列](/images/claudecode/08-autodream-gates.svg)

默认配置是 `minHours: 24`、`minSessions: 5`，由 GrowthBook `tengu_onyx_plover` 远程下发。三道闸门按成本递增排列：时间闸门只读一个 stat，扫描闸门要遍历整个 transcript 目录，锁闸门要写文件并验证 PID。大多数 stop hook 调用在时间闸门就 return，成本一次 stat。`SESSION_SCAN_INTERVAL_MS = 10 * 60 * 1000` 是扫描节流——时间闸门一旦通过，每轮都会通过，但目录扫描不能每轮都做，所以加一道 10 分钟节流。

锁机制（`consolidationLock.ts`）的设计把两个语义压进了一个文件。锁文件 `.consolidate-lock` 存在 memory 目录里，它的 mtime 就是 `lastConsolidatedAt`——一次 stat 同时回答「上次整合是什么时候」与「现在有没有人在整合」。锁文件正文是持有者的 PID。`HOLDER_STALE_MS = 60 * 60 * 1000` 是 PID 复用防护：即使 PID 还在运行，锁超过 1 小时也算 stale——Unix PID 会被回收复用，长时间后 PID 仍存活不代表还是原来那个进程。失败时 `rollbackConsolidationLock(priorMtime)` 把 mtime 回滚到获取前的值，让下一轮能重新尝试。

两个 reclaim 同时写入时，最后读到的 PID 赢，输家在 re-read 时发现自己的 PID 不在文件里就 bail。这种「write → re-read 验证」的模式是无锁文件协调的经典手法，比 flock 更跨平台。

整合 prompt（`consolidationPrompt.ts`）是 4 阶段结构：Orient（`ls` memory 目录、读 `MEMORY.md`、看现有 topic 文件）→ Gather（查日志、查漂移的记忆、必要时 grep transcript）→ Consolidate（合并新信号到现有文件，不新建）→ Prune（更新 `MEMORY.md` 索引，删过期的、降级过长的）。Bash 在这一阶段被 `createAutoMemCanUseTool()` 限制为只读命令（`ls`/`find`/`grep`/`cat`/`stat`/`wc`/`head`/`tail`），整合过程不会意外修改文件。

`DreamTask` 是 CC 四种后台任务类型之一。`registerDreamTask()` 让原本不可见的 forked agent 出现在终端底部的 task pill 与 `Shift+Down` 对话框里，`makeDreamProgressWatcher()` 把每一轮 assistant 消息折叠成 `{ text, toolUseCount }`，并收集 Edit/Write 的 `file_path`——注释明确说这是「至少这些被改了」，bash 写的文件抓不到。完成后通过 `appendSystemMessage` 把「Improved N memory files」塞进主 transcript，与 `extractMemories` 的「Saved N memories」消息保持同一 surface。`DreamTaskState` 的 `phase` 只有 `'starting'` 与 `'updating'` 两态，在第一次 Edit/Write 工具调用落地时翻转；`turns` 数组保留最近 30 轮（`MAX_TURNS = 30`）。`abortController` 让用户可以从后台任务对话框 kill 一个正在跑的 dream：abort 与置 killed 在同一次状态更新里完成，之后再回滚锁的 mtime——`priorMtime` 被 stash 在 task state 里就是为了这条 kill 路径。

## 六、MagicDocs：文档自维护

`src/services/MagicDocs/` 是一个更轻量的服务：自动维护带特殊头部的 markdown 文档。任何 `.md` 文件第一行写 `# MAGIC DOC: [title]`，下一行可选地写一段斜体说明（`*instructions*`），CC 读取这个文件后就会把它登记进 `trackedMagicDocs`，之后每次会话空闲时跑一次更新：

```ts
// src/services/MagicDocs/magicDocs.ts:45-70（节选）
export function detectMagicDocHeader(
  content: string,
): { title: string; instructions?: string } | null {
  const match = content.match(MAGIC_DOC_HEADER_PATTERN)  // /^#\s*MAGIC\s+DOC:\s*(.+)$/im
  if (!match || !match[1]) return null
  const title = match[1].trim()
  const afterHeader = content.slice(match.index! + match[0].length)
  // 头部后一行（允许一个空行）若为斜体，则作为自定义更新指令
  const nextLineMatch = afterHeader.match(/^\s*\n(?:\s*\n)?(.+?)(?:\n|$)/)
  const italicsMatch = nextLineMatch?.[1].match(ITALICS_PATTERN)
  return italicsMatch ? { title, instructions: italicsMatch[1].trim() } : { title }
}
```

`initMagicDocs()` 通过 `registerFileReadListener()` 订阅文件读取事件——每次 `FileReadTool` 读一个文件，listener 都会被调用一次，检测内容是否匹配 Magic Doc 头部。匹配则 `registerMagicDoc(filePath)` 把路径登记下来。`updateMagicDocs` 是 post-sampling hook，只在 `querySource === 'repl_main_thread'` 且上一轮无工具调用时跑——避免打断用户工作流。

更新走 `runAgent`（不走 `runForkedAgent`，MagicDocs 不需要复用主对话的 cache 前缀），`canUseTool` 同样被限制为只允许 Edit 该文档本身。prompt 的核心理念写在开头：「BE TERSE. High signal only」——文档反映当前状态，不记历史变化，过期信息就地替换，不追加「Updated to...」。文档作者可以通过斜体指令提供自定义更新规则，这些规则优先于通用 prompt。

MagicDocs 与 SessionMemory 在 hook 注册上的差异反映了定位差异：MagicDocs 通过文件读取被动发现——模型读了 Magic Doc 文件才登记、才更新，用户主动声明要维护的文档必须有显式读取动作才生效；SessionMemory 是系统默认开启的摘要服务，满足阈值就跑。MagicDocs 整体是 ant-only 的（`initMagicDocs()` 开头有 `if (process.env.USER_TYPE === 'ant')`），自定义模板放在 `~/.claude/magic-docs/prompt.md`。`substituteVariables()` 用单次正则替换避免两个 bug：`$` 反引用损坏与双重替换（用户内容里恰好包含 `{{varName}}` 会被后续变量匹配）。`cloneFileStateCache()` 与 `delete(docInfo.path)` 也是个细节：克隆 `readFileState` 后删掉当前文档的条目，确保 `FileReadTool` 在 MagicDocs agent 里不会因为 dedup 返回 `file_unchanged` stub——必须读到真实内容才能重新检测 header 与 instructions。

## 七、注入的另一半约束：prompt cache

前文反复出现「cache 感知」，这一节看它具体怎么落地。`src/services/api/promptCacheBreakDetection.ts`（727 行）是一个两阶段的缓存破坏检测器。

**Phase 1（调用前）**：`recordPromptState()` 记录本次请求的「指纹」——`systemHash`（系统提示 JSON 的 hash）、`toolsHash`（工具定义 hash）、`cacheControlHash`（带 `cache_control` 字段的 hash，捕捉 scope/TTL 翻转）、`perToolHashes`（每个工具 schema 的 hash，定位是哪个工具描述变了）、`model`、`fastMode`、`betas`、`effortValue` 等约 15 个维度。同时计算 `pendingChanges`：与上一次的指纹逐项 diff，记录哪些字段变了。

**Phase 2（响应后）**：`checkResponseForCacheBreak()` 拿到响应的 `cache_read_tokens` 与上一次比较。`cacheReadTokens >= prevCacheRead * 0.95` 或 `tokenDrop < MIN_CACHE_MISS_TOKENS`（2000）就不算破坏；否则根据 `pendingChanges` 解释原因——客户端可解释的（model/system/tools/betas 变化）直接列出，剩下的按时序猜测：

```ts
// src/services/api/promptCacheBreakDetection.ts:577-588（简化）
let reason: string
if (parts.length > 0) {
  reason = parts.join(', ')                       // 客户端可解释
} else if (lastAssistantMsgOver1hAgo) {
  reason = 'possible 1h TTL expiry (prompt unchanged)'
} else if (lastAssistantMsgOver5minAgo) {
  reason = 'possible 5min TTL expiry (prompt unchanged)'
} else {
  reason = 'likely server-side (prompt unchanged, <5min gap)'
}
```

BQ 分析显示约 90% 的「所有客户端 flag 都 false 且间隔小于 TTL」的破坏是服务端引起的，不应误导成客户端 bug。`cacheDeletionsPending` 是个特殊标志：当 cached microcompact 通过 `cache_edits` API 删除服务端缓存内容时，cache_read 必然下降——这是预期的。检测器看到这个标志后跳过本次比较，把基线重置为新的 `cacheReadTokens`，避免下一轮误报。

![稳定前缀与易变段的取舍](/images/claudecode/08-cache-prefix.svg)

`systemPromptSections.ts` 提供两种段类型，选择本身就传递了设计意图：`systemPromptSection()` 是缓存的（计算一次，存到 `STATE.systemPromptSectionCache`，`/clear` 与 `/compact` 时清空）；`DANGEROUS_uncachedSystemPromptSection()` 每轮重算，会破坏缓存，函数名要求调用者传 reason 解释为什么必须。MCP 指令段用后者——MCP server 可能在会话中途连接/断开，必须每轮重算；memory 段用前者——memdir 内容在压缩前是稳定的，缓存住就能守住前缀。`getTrackingKey()` 还揭示了一个细节：`compact` 这个 querySource 复用 `repl_main_thread` 的 tracking state——压缩是 fork 主对话做摘要，共享同一份 cache 前缀，共享 tracking state 才能正确检测破坏。

## 八、一次完整循环的时序

把所有子系统串起来，一次完整的 query 循环里，关键时序点有五个。

1. `getSystemContext` 与 `getUserContext` 在 query 开始时被 `fetchSystemPromptParts()` 并行取回，memoize 保证只在首次或缓存被清后才计算。

2. postSamplingHooks 在每轮模型采样后执行，SessionMemory 与 MagicDocs 都注册在这里，`sequential()` 包装保证它们不并发执行；SessionMemory 内部有 token 阈值守卫，多数轮次直接 return。

3. stopHooks 在 `message_stop` 且无工具调用时执行，`extractMemories` 与 `executeAutoDream` 在这里 fire-and-forget，不阻塞主循环返回——`--bare` 模式下整段被跳过，注释写着「Scripted -p calls don't want auto-memory or forked agents contending for resources during shutdown」。

4. AutoDream 的 stop hook 路径只是入口，实际触发要看三道闸门，大多数调用在时间闸门就 return，成本一次 stat。

5. CacheBreakDetection 的 Phase 1 在请求构造时记录、Phase 2 在响应回来后比较，只做遥测，不影响请求本身。

## 九、横向对比

| 维度 | Claude Code | OpenCode | Codex |
|------|-------------|----------|-------|
| 项目级指令文件 | `CLAUDE.md` 多层级（Managed/User/Project/Local） | `INSTRUCTIONS.md` / `AGENTS.md` | `.claude.md` |
| 自动发现机制 | 从 cwd 向上遍历到根 + `--add-dir` 显式指定 | 单一项目根 | 单一项目根 |
| 跨会话记忆目录 | memdir/（按 git root 分桶，含 team 子目录） | 有（`memory/` 目录） | 无 |
| 记忆类型分类 | 四元闭集（user/feedback/project/reference） | 自由格式 | 自由格式 |
| 会话内摘要 | SessionMemory（后台 forked agent，10 段模板） | 无独立摘要服务 | 无独立摘要服务 |
| 摘要服务于压缩 | 是（Level 5 sessionMemoryCompact 零 LLM 压缩） | 否 | 否 |
| 空闲时段整合 | AutoDream（三闸门 + PID 锁 + 4 阶段 prompt） | 无 | 无 |
| 文档自维护 | MagicDocs（ant-only，`# MAGIC DOC:` 头部触发） | 无 | 无 |
| Prompt cache 感知 | 是（约 15 维度指纹 + 两阶段检测 + cache_edits API） | 否 | 是（基础） |
| 段级缓存 | `systemPromptSection()` + `DANGEROUS_uncached` | 无段级缓存 | 无段级缓存 |

CC 在记忆系统上的投入远超另外两者，核心差异有三点。第一，**记忆分层与压缩的耦合**：SessionMemory 的产物直接被 `sessionMemoryCompact` 消费，让压缩走零 LLM 调用路径——摘要成本从压缩关键路径前置到了对话进行中的后台。OpenCode 和 Codex 的摘要服务与压缩解耦，每次压缩都在关键路径上同步调 LLM。

第二，**AutoDream 的睡眠整合**：CC 把跨会话的记忆整合做成一个独立的、低频的（24 小时 / 5 会话）、有锁的后台任务，让 memdir 不会无限膨胀——4 阶段 prompt 显式要求合并现有文件、删除过期条目。OpenCode 与 Codex 的记忆目录都只增不减，依赖用户或模型主动整理，长期使用会积累大量重复或过时条目。

第三，**cache 可观测性的粒度**：CC 把每一个可能破坏 cache 的维度（system prompt、tool schemas、betas、model、effort、cache_control scope/TTL）都做成了可追踪的指纹，两阶段比对给出可解释的破坏原因。OpenCode 与 Codex 的 cache 行为对用户和开发者都是黑盒。

代价是复杂度：`context.ts` 只有 189 行，但围绕它的 `claudemd.ts`、`memdir/`、`SessionMemory/`、`autoDream/`、`MagicDocs/`、`promptCacheBreakDetection.ts` 加起来超过 5400 行。每个子系统都有自己的 feature flag、阈值配置、缓存策略与失效路径。这种复杂度是 CC 作为 Anthropic 官方产品长期演进的产物——很多分支（KAIROS daily log、TEAMMEM、`tengu_moth_copse` 召回模式）都是 A/B 实验中的并行策略，最终哪条留下、哪条裁掉取决于线上数据。

## 十、全系列收束

这是系列的最后一篇。八篇文章走下来，Claude Code 的骨架已经完整摊开：

1. **第一篇**建立了四层地图：基础设施层、Agent 层、会话层、交互层，以及「模型在云端、本地只做编排」的总纲。
2. **第二篇**下潜主循环：七条显式 continue 撑起的 queryLoop，工具调度与流式响应在 continuation 里推进。
3. **第三篇**拆工具系统：工具池排序、八层门控、斜杠命令的三种执行模型与六路来源。
4. **第四篇**看压缩：从 snip 到 autocompact 的六级管线，阈值设计，以及零 LLM 的 session memory 压缩路径。
5. **第五篇**进 Agent 协作：AgentTool 的派发、子 agent 生命周期、Coordinator 的压缩与 backgrounding。
6. **第六篇**聚焦权限：规则匹配、AI 分类器两阶段、以及那场「消除误拦」的失败成本权衡。
7. **第七篇**讲 MCP 与 Bridge：四种传输层、鉴权链，以及把本地会话桥接到远程控制的完整链路。
8. **本篇**收束于记忆：六个生命周期各异的子系统，如何在「让模型看到什么」与「不破坏 cache」之间各显其能。

回头看，八篇串起来只在回答一个问题：一个无状态的模型，怎么被一套本地编排系统喂成一个「看起来有连续性」的工程助手。主循环提供推进的骨架，工具系统延伸手脚，压缩守住上下文的预算，Agent 并行扩展吞吐，权限决定什么能碰什么不能碰，MCP 与 Bridge 把边界推到进程外，记忆让这一切在时间上延续。每一层都在做同一类权衡——能力、成本、安全，只是各自站在不同的边界上。把这套源码读下来，你带走的不只是 Claude Code 的实现，更是一个工业级 coding agent 的完整决策地图。

## 源码索引

| 模块 | 路径 | 行数 |
|------|------|------|
| 上下文入口 | `src/context.ts` | 189 |
| CLAUDE.md 系统 | `src/utils/claudemd.ts` | 1479 |
| memdir 核心 | `src/memdir/memdir.ts` | 507 |
| memdir 路径解析 | `src/memdir/paths.ts` | 278 |
| 记忆扫描 | `src/memdir/memoryScan.ts` | 94 |
| 记忆召回 | `src/memdir/findRelevantMemories.ts` | 141 |
| 记忆类型 | `src/memdir/memoryTypes.ts` | 271 |
| SessionMemory 核心 | `src/services/SessionMemory/sessionMemory.ts` | 495 |
| SessionMemory 工具 | `src/services/SessionMemory/sessionMemoryUtils.ts` | 207 |
| SessionMemory Prompt | `src/services/SessionMemory/prompts.ts` | 324 |
| AutoDream 核心 | `src/services/autoDream/autoDream.ts` | 324 |
| 整合锁 | `src/services/autoDream/consolidationLock.ts` | 140 |
| 整合 Prompt | `src/services/autoDream/consolidationPrompt.ts` | 65 |
| MagicDocs 核心 | `src/services/MagicDocs/magicDocs.ts` | 254 |
| Cache 破坏检测 | `src/services/api/promptCacheBreakDetection.ts` | 727 |
| 系统提示段缓存 | `src/constants/systemPromptSections.ts` | 68 |
| Stop Hook 集成 | `src/query/stopHooks.ts` | 473 |
| DreamTask 注册 | `src/tasks/DreamTask/DreamTask.ts` | 157 |
| 记忆提取服务 | `src/services/extractMemories/extractMemories.ts` | 615 |

## 章节小测

<script setup>
const q = [
  {
    question: 'CLAUDE.md 的加载顺序为什么从 cwd 向上遍历到根目录？',
    options: [
      '为兼容 git 从子目录向上遍历仓库根目录的工作方式',
      '减少文件系统随机读取次数以提升加载性能',
      '子目录指令后出现优先级更高可覆盖父目录通用指令',
      'git worktree 场景要求从当前目录向上遍历至根'
    ],
    correct: 2,
    explanation: '收集时从 cwd 向上走到根，处理时反转为自根向 cwd——越靠近当前目录的文件越晚拼接，后出现的优先级更高，子目录指令得以覆盖父目录。CC 还会对 worktree 场景做去重处理，避免同一份 CLAUDE.md 被加载两次。'
  },
  {
    question: 'memdir 按 git root 而非按绝对路径分桶，这一设计解决了什么问题？',
    options: [
      '同一仓库所有 worktree 共享记忆不因路径分裂',
      '简化底层路径解析复杂度并降低错误概率',
      '按 git root 分桶后各分支获得独立记忆空间',
      '避免以长绝对路径为键降低文件系统开销'
    ],
    correct: 0,
    explanation: 'getAutoMemBase() 走 findCanonicalGitRoot()，同一 git 仓库的所有 worktree 共享同一个 memory 目录。即使用户在不同 worktree 路径下工作，跨会话记忆仍然一致，不会因为 worktree 路径不同而导致记忆分裂。'
  },
  {
    question: 'extractMemories 与主对话的内存写入是什么协作关系？',
    options: [
      '两者各自独立提取互不感知可能重复写入',
      '主对话写入时 extractMemories 强制接管合并',
      'extractMemories 定期清空主对话写过的文件',
      '主对话写了就跳过该区间没写则后台补上'
    ],
    correct: 3,
    explanation: 'hasMemoryWritesSince() 检测主对话在某轮是否写过 memory 文件：写了，后台 agent 跳过那段；没写，后台 agent 补上漏掉的。主加补的双轨设计让提取既不依赖主 agent 主动性，也不重复劳动。'
  },
  {
    question: 'AutoDream 的三道闸门（时间/扫描/会话）为什么按这个顺序排列？',
    options: [
      '闸门顺序由每次 stop hook 的随机种子决定',
      '时间闸门一次 stat 会话闸门要扫目录按成本递增',
      '三道闸门在每次触发时被同时并行地检查',
      '先执行最贵的目录扫描再执行便宜的时间检查'
    ],
    correct: 1,
    explanation: '时间闸门只读取一个文件 stat（毫秒级），扫描闸门需要遍历整个 transcript 目录（秒级），锁闸门需要写文件并验证 PID（含 IO）。按成本递增排列，大多数 stop hook 调用在时间闸门就 return。'
  },
  {
    question: 'MCP 指令段为什么用 DANGEROUS_uncachedSystemPromptSection() 每轮重算？',
    options: [
      '段内容含敏感凭证每轮重新生成更安全',
      '该段计算开销很小缓存收益可以忽略',
      'MCP server 可能在会话中途连接或断开',
      '缓存系统对提示段的大小有硬性上限'
    ],
    correct: 2,
    explanation: 'MCP server 可能在会话中途连接/断开，指令段必须每轮重算，所以走 DANGEROUS_uncachedSystemPromptSection()，函数名还要求调用方传 reason 说明为什么必须破坏缓存。对照 memory 段：memdir 内容在压缩前稳定，用 systemPromptSection() 缓存一次就能守住 cache 前缀。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
