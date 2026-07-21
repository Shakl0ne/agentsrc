# 验证报告：文章 4 Compact

## 一、为什么需要压缩？

### 1.2 溢出长什么样
- ✅ `src/session/overflow.ts` 文件存在（`packages/opencode/src/session/overflow.ts`）
- ✅ `isOverflow()` 函数存在，签名为 `isOverflow(input: { cfg: Config.Info; tokens: MessageV2.Assistant["tokens"]; model: Provider.Model; outputTokenMax?: number })`，与实际一致
- ✅ 代码块第 47-58 行与源码（overflow.ts:20-32）基本一致
  - 差异：源码 `isOverflow` 中 `count` 的 `tokens.total` 使用了 `||` 操作符，文章正确反映
  - ⚠️ 文章代码块第 48 行：`if (input.cfg.compaction?.auto === false)` — 源码第 26 行实际为 `if (input.cfg.compaction?.auto === false)`，完全一致
  - ⚠️ 文章代码块第 49 行：`if (input.model.limit.context === 0)` — 源码第 27 行完全一致
  - ⚠️ 文章代码块第 52-55 行的 `usize` 变量计算：`input.tokens.total || input.tokens.input + input.tokens.output + input.tokens.cache.read + input.tokens.cache.write` — 源码第 29-30 行完全一致
  - ✅ `usable()` 调用正确

### 1.3 可用额度怎么算
- ✅ `src/session/overflow.ts` 文件存在
- ✅ `usable()` 函数存在，签名为 `usable(input: { cfg: Config.Info; model: Provider.Model; outputTokenMax?: number })`，与实际一致
- ✅ 代码块第 65-79 行与源码（overflow.ts:6-18）基本一致
- ✅ `COMPACTION_BUFFER = 20_000` — 源码第 6 行 `const COMPACTION_BUFFER = 20_000`，完全一致
- ⚠️ 文章代码块第 68 行的函数签名：`export function usable(input)` 是简化版，源码为 `export function usable(input: { cfg: Config.Info; model: Provider.Model; outputTokenMax?: number })`。参数结构正确，签名省略了类型标注，属于可接受简化
- ✅ 第 76-78 行的 `input.model.limit.input` 分支逻辑与源码第 15-17 行完全一致
- ⚠️ 概念描述：「可用额度 = context - max_output_tokens」—— 源码是 `Math.max(0, context - ProviderTransform.maxOutputTokens(...))` 公式，文章省略了 `Math.max(0, ...)` 包裹，但核心逻辑正确

## 二、两条触发路径：proactive vs reactive

### 2.1 Proactive：上一轮结束后提前触发
- ✅ **位置**：`src/session/prompt.ts` 第 1322-1328 行 — 确认源码该文件存在
- ✅ 行号验证：prompt.ts:1322-1329 的实际内容：
  ```
  1322:           if (
  1323:             lastFinished &&
  1324:             lastFinished.summary !== true &&
  1325:             (yield* compaction.isOverflow({ tokens: lastFinished.tokens, model }))
  1326:           ) {
  1327:             yield* compaction.create({ sessionID, agent: lastUser.agent, model: lastUser.model, auto: true })
  1328:             continue
  1329:           }
  ```
  文章标注「第 1322-1328 行」，实际范围是 1322-1329（包括闭合大括号和 continue），但文章引用的 `if` 块结束于第 1328 行（缺少 `}`）。这属于简化截取，核心行号准确 ⚠️
- ✅ 代码块第 118-130 行与源码一致
- ⚠️ 文章注释 `// runLoop 中每一轮开头` — 源码中该代码在 `tasks.pop()` 处理之后而非最开头处，属于 conceptual 描述而非精确位置描述
- ✅ 三个精妙之处的描述准确：
  1. `lastFinished` 而不是 `lastAssistant` — 源码第 1323 行确实用 `lastFinished`，正确
  2. `summary !== true` — 源码第 1324 行，正确
  3. `create` 后立刻 `continue` — 源码第 1327-1328 行，正确
- ✅ 关于 `create` 后不直接调 `process` 的解释 — 与源码设计一致（create 只插入占位消息，process 在下一轮通过 tasks 调用）

### 2.2 Reactive：LLM 调用过程中被截断
- ✅ **位置**：`src/session/processor.ts` 第 754-756 行 — 确认行号准确：
  ```
  754:         if (MessageV2.ContextOverflowError.isInstance(error)) {
  755:           ctx.needsCompaction = true
  756:           yield* bus.publish(Session.Event.Error, { sessionID: ctx.sessionID, error })
  757:           return
  758:         }
  ```
  完全一致 ✅
- ✅ 代码块第 147-151 行与源码第 754-758 行完全一致
- ✅ 文章称「step-finish 检查」在 processor.ts 大约 845 行 — 实际 line 845 为：
  ```
  845:           if (ctx.needsCompaction) return "compact"
  ```
  确认正确 ✅
- ✅ 代码块第 158 行：`if (ctx.needsCompaction) return "compact"` — 与源码 line 845 完全一致
- ✅ 位置：`prompt.ts:1477` — 实际源码：
  ```
  1477:             if (result === "compact") {
  1478:               yield* compaction.create({
  1479:                 sessionID,
  1480:                 agent: lastUser.agent,
  1481:                 model: lastUser.model,
  1482:                 auto: true,
  1483:                 overflow: !handle.message.finish,
  1484:               })
  1485:             }
  ```
  文章标注 1477 行，完全一致 ✅
- ⚠️ 文章代码块第 165-174 行与源码第 1477-1485 行一致，但文章注释写 `// prompt.ts:1477`，准确
- ✅ `overflow: true` 描述准确 — 源码第 1483 行 `overflow: !handle.message.finish`

## 三、Compact 流程全景

### 3.1 create：插一条占位消息
- ✅ **位置**：compaction.ts:584-614 — 实际：
  ```
  584:     const create = Effect.fn("SessionCompaction.create")(function* (input: {
  ...
  614:     })
  ```
  行号范围 584-614 准确 ✅
- ✅ 代码块第 219-248 行中关键内容与源码 585-614 行一致
- ✅ `create` 函数签名：`(input: { sessionID: SessionID; agent: string; model: { ... }; auto: boolean; overflow?: boolean })` — 与源码第 585-589 行一致
- ✅ 三步逻辑（建立 user 消息、挂 compaction part、发布事件）与实际一致
- ✅ `auto` 和 `overflow` 字段描述正确

### 3.2 process：真正执行压缩的核心
- ✅ **位置**：compaction.ts:344 — `const processCompaction = Effect.fn("SessionCompaction.process")` 确实在 line 344
- ✅ 5 件事的描述与源码 344-582 行的结构一致
- ⚠️ 文章说 240 多行 — processCompaction 从 line 344 到 line 582，共约 238 行，基本准确

### 3.3 prune：异步标记旧工具输出
- ✅ **位置**：compaction.ts:296 — `const prune = Effect.fn("SessionCompaction.prune")` 确实在 line 296
- ✅ 代码块第 275-282 行与源码第 298-306 行一致
- ✅ 第 277 行：`if (!cfg.compaction?.prune) return` — 源码 line 300 为 `if (!cfg.compaction?.prune) return`，完全一致 ✅
- ✅ **触发时机**（prompt.ts:1495）：
  ```
  1495:         yield* compaction.prune({ sessionID }).pipe(Effect.ignore, Effect.forkIn(scope))
  1496:         return yield* lastAssistant(sessionID)
  ```
  完全一致 ✅
- ✅ `Effect.forkIn(scope)` 和 `Effect.ignore` 描述准确
- ⚠️ 文章说「runLoop 整个 while 循环退出后才 fork」— 从上下文看，line 1495 在 while 循环外部（return 之前），确实在循环退出后才执行，描述准确

## 四、select：计算保留的尾巴轮次

### 4.1 默认保留 2 轮，可配置
- ✅ **位置**：compaction.ts:245-294 — 实际 `const select = Effect.fn("SessionCompaction.select")` 在 line 245，函数结束于 line 294，完全一致
- ✅ 代码块第 312-323 行的关键行与源码一致：
  - 第 313 行：`const limit = input.cfg.compaction?.tail_turns ?? DEFAULT_TAIL_TURNS` — 源码 line 250，完全一致
  - 第 317 行：`const all = turns(input.messages)` — 源码 line 253，完全一致
  - 第 320 行：`const recent = all.slice(-limit)` — 源码 line 255，完全一致
- ✅ `DEFAULT_TAIL_TURNS = 2` — 源码第 39 行，完全一致
- ✅ 代码块第 328-336 行 `preserveRecentBudget` 与源码 136-141 行一致

### 4.3 splitTurn：在一轮内部切分
- ✅ `splitTurn` 函数存在，位置 compaction.ts:161-184，与文章描述一致
- ✅ 代码块第 362-369 行的调用方式与源码 277-283 行一致

## 五、Prune 截断：PRUNE_PROTECT 40K tokens 保护

### 5.1 prune 的扫描逻辑
- ✅ **位置**：compaction.ts:296-342 — 准确，prune 函数从 line 296 到 line 342
- ✅ 代码块第 384-421 行中的关键逻辑与源码 line 298-341 一致
- ✅ 常量值：
  - `PRUNE_MINIMUM = 20_000` — 源码 line 35，完全一致 ✅
  - `PRUNE_PROTECT = 40_000` — 源码 line 36，完全一致 ✅
  - `PRUNE_PROTECTED_TOOLS = ["skill"]` — 源码 line 38，完全一致 ✅

### 5.2 三个关键保护
- ✅ **保护 1**：`if (turns < 2) continue` — 源码 line 316，完全一致
- ✅ **保护 2**：`if (total <= PRUNE_PROTECT) continue` — 源码 line 326，完全一致
- ✅ **保护 3**：`if (PRUNE_PROTECTED_TOOLS.includes(part.tool)) continue` — 源码 line 322，完全一致

### 5.3 「标记」而不是「删除」
- ✅ 代码块第 457-459 行：`part.state.time.compacted = Date.now(); yield* session.updatePart(part)` — 源码 line 336-337，完全一致
- ✅ 描述「给 part 的 state.time 字段加了一个 compacted 时间戳」— 源码中 `compacted` 定义在 message-v2.ts:275：`compacted: Schema.optional(NonNegativeInt)`，正确
- ✅ 代码块第 466-470 行的描述— 实际的序列化逻辑在 message-v2.ts:791-794：
  ```ts
  const outputText = part.state.time.compacted ? "[Old tool result content cleared]" : ...
  const attachments = part.state.time.compacted || options?.stripMedia ? [] : ...
  ```
  文章描述「工具输出正文变为 [Old tool result content cleared]」和「附件也会清空」正确 ✅

### 5.4 最低门槛保护
- ✅ 代码块第 477 行：`if (pruned > PRUNE_MINIMUM)` — 源码 line 333，完全一致

## 六、LLM 摘要生成：9 段摘要模板 + compaction Agent

### 6.1 compaction Agent 的配置
- ✅ `src/agent/agent.ts` 文件存在
- ⚠️ 文章标注「src/agent/agent.ts:235-249」— 实际源码中 compaction agent 配置为：
  ```
  235:           compaction: {
  236:             name: "compaction",
  ...
  249:           },
  ```
  第 235 行是对象 key，文章说 235-249，实际范围 235-249（包含闭合大括号），准确 ✅
- ✅ `hidden: true` — 源码 line 239，完全一致
- ✅ `permission: { "*": "deny" }` — 源码 line 244，`Permission.fromConfig({ "*": "deny" })`，完全一致
- ✅ `native: true` — 源码 line 238，完全一致
- ✅ `prompt: PROMPT_COMPACTION` — 源码 line 240，完全一致

### 6.2 compaction Agent 的 prompt
- ✅ `src/agent/prompt/compaction.txt` 文件存在（9 行）
- ✅ 代码块第 526-542 行与实际内容完全一致，包括：
  - 「anchored context summarization assistant for coding sessions」 ✅
  - 「<previous-summary> block」✅
  - 「Keep every section, preserve exact file paths」✅
  - 「Do not answer the conversation itself. Do not mention that you are summarizing」✅

### 6.3 锚定摘要：增量更新而不是重新生成
- ✅ `buildPrompt` 函数存在，位置 compaction.ts:123-134
- ✅ 代码块第 549-561 行与源码 line 123-134 完全一致，包括 `Create a new anchored summary from the conversation history above.` 和 `Update the anchored summary below...` 两种模式
- ✅ 概念描述「增量更新」与实际代码行为一致

### 6.4 9 段摘要模板
- ✅ `SUMMARY_TEMPLATE` 常量在 compaction.ts:42-77
- ✅ 文章代码块第 581-606 行中 7 个节（Goal / Constraints & Preferences / Progress 含 3 子段 / Key Decisions / Next Steps / Critical Context / Relevant Files）与实际常量内容一致
- ✅ Rules 部分：「Keep every section, even when empty.」「Use terse bullets, not prose paragraphs.」「Preserve exact file paths, commands, error strings, and identifiers when known.」「Do not mention the summary process or that context was compacted.」— 与源码 line 73-77 完全一致
- ⚠️ 文章说「9 段摘要模板」— 从结构上看是 7 个顶级 section（其中 Progress 含 3 个子 section，如果按 section 标题计数是 7+2=9 个可填写段），描述为「9 段」合理

### 6.5 调用时的额外参数
- ✅ 代码块第 637-640 行：
  ```ts
  const modelMessages = yield* MessageV2.toModelMessagesEffect(msgs, model, {
    stripMedia: true,
    toolOutputMaxChars: TOOL_OUTPUT_MAX_CHARS,
  })
  ```
  与源码 compaction.ts:406-409 完全一致 ✅
- ✅ `TOOL_OUTPUT_MAX_CHARS` 在 compaction.ts:37：`const TOOL_OUTPUT_MAX_CHARS = 2_000`，文章说「= 2_000」完全一致 ✅
- ✅ `stripMedia: true` 描述正确

## 七、filterCompacted：消息重排的艺术

### 7.1 重排逻辑
- ✅ `filterCompacted` 函数存在，位置 message-v2.ts:1014-1065
- ⚠️ 文章标注「message-v2.ts:1014-1065」— 实际 line 1014 到 line 1065（含空行），完全一致 ✅
- ✅ 代码块第 674-704 行与源码结构一致，关键行：
  - 第 676 行：`const completed = new Set<string>()` — 源码 1016 行，一致
  - 第 677 行：`let retain: MessageID | undefined` — 源码 1017 行，一致
  - 第 687 行：`if (msg.info.role === "user" && completed.has(msg.info.id))` — 源码 1024 行，一致
  - 第 690 行：`if (!part.tail_start_id) break` — 源码 1027 行，一致
- ⚠️ 文章第 703 行注释 `// ...具体重排代码省略...` — 实际的后续逻辑（line 1037-1064）确实是重排的核心，文章合理省略
- ✅ 核心思路描述正确：找到最近一次完成的 compaction → 拿到 tail_start_id → 重排

### 7.2 overflow 场景的特殊处理：重放用户消息
- ✅ 代码块第 731-740 行与源码中 processCompaction 的 overflow 处理逻辑一致
- ✅ 源码 compaction.ts:477-558（`if (result === "continue" && input.auto)` 块）确实包含 replay 和非 replay 两条路径
- ⚠️ 文章代码块第 732 行条件：`if (result === "continue" && input.auto)` — 源码 line 477 完全一致 ✅
- ✅ `replay` 场景描述准确：复制原 user message（去除媒体附件）
- ✅ 非 overflow 场景：发合成 user 消息「Continue if you have next steps...」— 源码 line 540 包含 `"Continue if you have next steps..."` 字符串 ✅

## 八、为什么 CC 用 4 级压缩，OpenCode 只用 2 级？

- ⚠️ 第八节和第九节引用的是 Claude Code 的源码信息，非 OpenCode 仓库内容，标注「需验证」的项目无法在 OpenCode 源码中验证
- ✅ OpenCode 相关描述均可验证：
  - 「2 级梯度」— compaction.ts 中只有 prune（line 298）和 process（line 344）两个核心操作，与文章一致
  - 「Prune 用时间戳标记」— 已验证 ✅
  - 「Compact 一定调 LLM」— processCompaction 调用 `processor.process` 确实调 LLM ✅
  - 「compaction.ts 639 行」— 实际 639 行，完全一致 ✅
- ⚠️ CC（Claude Code）的 5 级压缩数据不属于本仓库，无法直接验证。文章标注「基于两份源码同时分析」，建议加脚注说明数据来源

## 九、OpenCode vs Claude Code 对比表

- ⚠️ 对比表中的 OpenCode 侧均可验证：
  - ✅ 「2 级压缩」— 正确
  - ✅ 「Level 2 必调 LLM」— 正确（process 走 LLM）
  - ✅ 「Prune: 时间戳标记」— 已验证
  - ✅ 「最后 40K tokens + 最后 2 轮」— PRUNE_PROTECT = 40_000 ✅，`turns < 2` ✅
  - ✅ 「PRUNE_PROTECTED_TOOLS = ["skill"]」— 正确
  - ✅ 「9 段 Markdown（Goal/Progress/...）」— 正确
  - ✅ 「锚定摘要（增量更新）」— 正确
  - ✅ 「专用 compaction agent（hidden + deny all）」— 正确
  - ✅ 「重放最后一条用户消息（reactive）/ 合成 continue（proactive）」— 正确
  - ✅ 「ContextOverflowError → 重放用户消息」— 正确
  - ✅ 「无专门缓存优化」— 源码中无 cache_edits 相关代码，正确
  - ✅ 「summarize HTTP API + auto: false」— compaction.ts create 支持 `auto: boolean` ✅
  - ✅ 「timestamp-based hiding，数据在 DB」— 已验证 ✅
  - ✅ 「~639 行（compaction.ts）+ 32 行（overflow.ts）」— 639 + 32 = 671 行，准确
  - ✅ 「不依赖厂商特定 API」— 正确
- ❌ 表格有 1 个小错误：OpenCode 的「压缩层级数」表内标注「**2 级**」，而第八节的标题写「为什么 CC 用 4 级压缩」但正文第一句写「CC 实际上有 5 个层级的压缩」— 正文正确（5 级），但标题说「4 级压缩」不一致。⚠️ 这属于同一篇文章内部不一致，非源码验证问题，但值得指出

## 最后（结尾总结）

- ✅ 「两条触发路径（proactive + reactive）」— 已验证
- ✅ 「2 级梯度（Prune + Compact）」— 已验证
- ✅ 「锚定摘要」— 已验证
- ✅ 「时间戳标记而不是物理删除」— 已验证
- ✅ 「专用 compaction agent 完全禁工具」— 已验证
- ✅ 「filterCompacted 消息重排」— 已验证
- ✅ 「reactive 路径重放用户消息」— 已验证
- ✅ 「639 行 TypeScript」— compaction.ts 确实 639 行
- ⚠️ 「Claude Code 3960 行」— 无法在 OpenCode 仓库验证

## 汇总

### ✅ 全部通过的验证项

| 类别 | 数量 |
|------|------|
| 文件存在性 | 7/7 |
| 行号标注 | 7/7 |
| 函数名/签名 | 9/9 |
| 常量值 | 5/5 |
| 代码块关键行 | ~30 处 |

### ⚠️ 需要注意的偏差

1. **第一节 usable 计算描述**：「扣完剩 180K」— 实际逻辑分两条路径（`input.limit` 存在 vs 不存在），文章简化到了只展示结果，建议可加注
2. **第一节代码块签名简化**：`function usable(input)` — 省略了类型参数，可接受
3. **第二节行号范围**：prompt.ts:1322-1328 实际 if 块在 1329 行有闭合大括号，文章少了行号
4. **第八节标题 vs 正文**：标题写「4 级压缩」，正文第一句写「5 个层级的压缩」— 标题不一致

### ❌ 错误

- **未发现** 严重错误（行号错误、函数名错误、逻辑描述与实际完全不符的情况）

### 跨仓库引用说明

第八节（Claude Code 对比）、第九节（对比表）中所有关于 **Claude Code** 的陈述（5 级压缩、cache_edits、3960 行等）在本仓库（OpenCode）中无法验证，因为它们引用的是 Claude Code 源码。文中标注「基于两份源码分析」但未提供 Claude Code 源码路径，属于外部引用。