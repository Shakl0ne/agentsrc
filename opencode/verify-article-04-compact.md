# 验证报告：04-compact.md（OpenCode 上下文压缩）

> 双层验证：Layer 1 源码对齐（OpenCode 侧 + CC 侧）+ Layer 2 结构审校
> 验证日期：2026-07-22
> 验证人：exp-1（OpenCode 源码）、exp-2（CC 源码）、orchestrator（结构审校）

## 重要发现

本次验证发现 **ch1 review 中的两项修正有误**，已回滚：
1. **`cache_edits` 是真实存在的**：exp-2 在 CC 源码 `microCompact.ts`（line 108, 335-338, 369, 410）和 `claude.ts`（line 3053, 3115-3183）中确认 `cache_edits` 是 Anthropic 专有 API content block（`type: 'cache_edits'`），用于服务端缓存删除。ch1 错误地认为此词不存在并改为 `cache_control`——已回滚。
2. **"前 4 级都不调用 LLM"是正确的**：exp-2 确认 CC 的 5 级压缩中，Level 1-4（Tool Result Budget / Snip / Microcompact / Context Collapse）均为纯数据操作，只有 Level 5（Auto-compact）调用 LLM。ch1 错误地改为"前 2 级零 LLM"——已回滚。

---

## 第一层：OpenCode 侧源码对齐（28 ✅ / 2 ⚠️ / 0 ❌）

### A. overflow.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| A1 | `isOverflow()` 函数逻辑 | ✅ 完全匹配（auto===false, context===0, token 累加, count>=usable） | ✅ |
| A2 | `COMPACTION_BUFFER = 20_000` | ✅ line 6 | ✅ |
| A3 | `usable()` 函数逻辑 | ✅ 完全匹配 | ✅ |
| A4 | "32 行（overflow.ts）" | ✅ wc -l 确认 32 行 | ✅ |

### B. compaction.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| B1 | "639 行" | ✅ wc -l 确认 639 行 | ✅ |
| B2 | `create()` at line 584-614 | ✅ line 584-618 完全匹配 | ✅ |
| B3 | `processCompaction` at line 344, ~240 行 | ⚠️ 实际 344-618 = **275 行**（差 35） | ⚠️ |
| B4 | `prune()` at line 296, 扫描 + 保护 | ✅ line 298-340 | ✅ |
| B5 | `select()` at line 245-294, 默认 2 轮 | ✅ 完全匹配 | ✅ |
| B6 | `preserveRecentBudget()` at 136-141, clamp(2000, 8000, 25%) | ✅ MIN=2000, MAX=8000, DEFAULT_TAIL_TURNS=2 | ✅ |
| B7 | `buildPrompt()` at 123-134, 两模式 | ✅ 完全匹配 | ✅ |
| B8 | `SUMMARY_TEMPLATE` at 42-77, 9 段 | ✅ 完全匹配 | ✅ |
| B9 | `PRUNE_MINIMUM=20000`, `PRUNE_PROTECT=40000`, `PRUNE_PROTECTED_TOOLS=["skill"]` | ✅ line 35-38 | ✅ |
| B10 | prune 标记 `compacted = Date.now()` | ✅ line 336 | ✅ |
| B11 | `pruned > PRUNE_MINIMUM` 才执行 | ✅ line 333 | ✅ |
| B12 | overflow 场景 replay vs 合成 continue | ✅ line 478-540 | ✅ |

### C. prompt.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| C1 | proactive at 1322-1328 | ✅ line 1322-1329 | ✅ |
| C2 | reactive at 1477 | ✅ line 1476-1483 | ✅ |

### D. processor.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| D1 | `ContextOverflowError.isInstance` at 754-756 | ✅ line 754-758 | ✅ |
| D2 | `if (ctx.needsCompaction) return "compact"` | ✅ line 845 | ✅ |

### E. agent.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| E1 | compaction agent at 235-249, hidden+deny all | ✅ 完全匹配 | ✅ |

### F. compaction.txt

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| F1 | 文件存在 | ✅ | ✅ |
| F2 | prompt 内容匹配 | ✅ 完全匹配 | ✅ |

### G. message-v2.ts

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| G1 | `filterCompacted()` at 1014-1065 | ⚠️ 函数本体 1014-1037，重排逻辑 1039-1065 是独立块 | ⚠️ |
| G2 | `tail_start_id` 重排 | ✅ | ✅ |

### H. 其他

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| H1 | `TOOL_OUTPUT_MAX_CHARS = 2_000` | ✅ line 37 | ✅ |
| H2 | `stripMedia: true` | ✅ line 407 | ✅ |
| H3 | `"[Old tool result content cleared]"` | ✅ message-v2.ts:792 | ✅ |
| H4 | prune `Effect.forkIn(scope)` + `Effect.ignore` at 1495 | ✅ | ✅ |
| H5 | `splitTurn()` 存在 | ✅ line 161 | ✅ |

### OpenCode 侧小结

**0 个事实错误**，2 项 ⚠️ 均为行号/行数微偏差。文章对 OpenCode 压缩机制的拆解极其精确。

---

## 第一层：CC 侧源码对齐（15 ✅ / 5 ⚠️ / 1 ❌）

### A. 5 级压缩结构

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| A1 | 5 级：Tool Result Budget / Snip / Microcompact / Context Collapse / Auto-compact | ✅ 5 个机制全部确认 | ✅ |
| A3 | "前 4 级都不调用 LLM" | ✅ **确认正确**：L1-L4 均为纯数据操作，仅 L5 调 LLM | ✅ |
| A4 | L1: >50K 字符写磁盘 + 2KB 预览 | ✅ `DEFAULT_MAX_RESULT_SIZE_CHARS=50000`, `PREVIEW_SIZE_BYTES=2000` | ✅ |
| A5 | L2: token 超（阈值 + 13K） | ⚠️ `AUTOCOMPACT_BUFFER_TOKENS=13000` 是 autoCompact 的 buffer，被文章归到 Snip 名下 | ⚠️ 归属略有混淆 |
| A6 | L3: 每次 API 调用前 | ✅ microCompact + apiMicrocompact 在 API 调用前运行 | ✅ |
| A7 | L4: ~90% 利用率 | ⚠️ 90% 是 warning 阈值（200K-20K），auto-compact 触发在 93.5%（200K-13K） | ⚠️ |
| A8 | L5: 前 4 级不足时 | ✅ `compactConversation()` 在 `shouldAutoCompact()` 为 true 时调用 | ✅ |

### B. cache_edits（**重要纠正**）

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| B1 | "cache_edits" 出现 6 次 | ✅ **cache_edits 确实存在**！在 `microCompact.ts:108,335-338,369,410` 和 `claude.ts:3053,3115-3183`。是 Anthropic 专有 API content block（`type: 'cache_edits'`），用于服务端缓存删除。与标准 `cache_control` 不同 | ✅ 文章正确 |
| B2 | "Anthropic 内部 API" | ✅ cache_edits 是专有/内部 API | ✅ |
| B3 | "深度 Prompt Cache 集成（双路径）" | ✅ CC 同时有 cache_control（标准）+ cache_edits（专有删除块），构成双路径 | ✅ |
| B4 | OpenCode "无专门缓存优化" | ❌ **已知错误**：OpenCode 有 `packages/llm/src/cache-policy.ts`，默认 `cache: "auto"` | ❌ 需修正 |

### C. CC 源码规模

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| C1 | "~3,960 行（5 个核心文件）" | ⚠️ 行数 **3960 精确**，但实际是 **11 个文件**，非 5 个 | ⚠️ 文件数错误 |

### D. 保护窗口

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| D1 | "最后 3-5 个 tool results + 40K tokens" | ✅ `keepRecent: 5`, `DEFAULT_TARGET_INPUT_TOKENS=40000` | ✅ |

### E. Reactive 路径

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| E1 | "413 错误后保留最后 4 条消息重试" | ⚠️ 413 错误确认（`query.ts:1070`），但"4 条消息"无法从源码验证（reactiveCompact.ts 是 lazy-loaded 无 .ts） | ⚠️ |

### F. 阻塞与熔断

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| F1 | "~88.5% 时主动阻塞" | ❌ **无源码支持**。实际阻塞限制 = `effectiveWindow - 3000`（200K → 98.5%）。88.5% 不匹配任何阈值 | ❌ |
| F2 | "3 次连续失败后停止" | ✅ `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` | ✅ |

### G. 摘要结构

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| G1 | "9 段 XML + `<analysis>` 草稿（后剥离）" | ✅ `prompt.ts:68-77` 确认 9 段；`<analysis>` 在 line 82，由 `formatCompactSummary()` 剥离 | ✅ |

### H. 压缩后行为

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| H1 | "重读最近 5 个文件" | ⚠️ continuation message 确认；re-read files 逻辑确认；但"5"这个数字未找到 | ⚠️ |
| H2 | "自动重读文件 + skills + plan 状态" | ✅ `postCompactCleanup.ts` + skills preservation + plan preservation | ✅ |

### I. 手动触发

| ID | 声明 | 实际 | 结论 |
|----|------|------|------|
| I1 | "/compact [instructions]" | ✅ `commands/compact/compact.ts:52` 读取 `customInstructions` | ✅ |

### CC 侧小结

**1 个事实错误**：88.5% 阻塞限制无源码支持
**5 个 ⚠️**：13K 归属、90% vs 93.5%、4 条消息不可验证、5 文件 vs 11 文件、5 files 不可验证
**关键纠正**：cache_edits 是真实的、前 4 级零 LLM 是正确的

---

## 第二层：结构审校

### 1. Prompt Cache 一致性（跨章节）

| # | 问题 | 位置 | 结论 |
|---|------|------|------|
| I-1 | "无专门缓存优化" | line 840 | ❌ OpenCode 有 cache-policy.ts |
| I-2 | "放弃了 Prompt Cache 优化和 cache_edits API" | line 872 | ❌ OpenCode 有 Prompt Cache（cache_control），但没有 cache_edits |
| I-3 | ch1 已回滚 cache_edits→cache_control 的错误修正 | ch1 line 378 | ✅ 已回滚 |
| I-4 | ch1 已回滚"前2级零LLM"→"前4级零LLM"的错误修正 | ch1 line 379 | ✅ 已回滚 |

### 2. CC 对比准确性

| # | 问题 | 位置 | 结论 |
|---|------|------|------|
| I-5 | "88.5% 阻塞限制" | line 841 | ❌ 无源码支持，实际 ~98.5% |
| I-6 | "5 个核心文件" | line 845 | ⚠️ 实际 11 个文件 |

### 3. 覆盖完整性

| # | 检查 | 结论 |
|---|------|------|
| COV-1 | create→process→select→prune 全流程？ | ✅ |
| COV-2 | proactive + reactive 两条路径？ | ✅ |
| COV-3 | 9 段摘要模板？ | ✅ |
| COV-4 | 3 个保护机制（2 轮 + 40K + skill）？ | ✅ |

---

## 修正清单

### ✅ 已修正（事实错误）

1. ✅ **§9 表**（line 840）：`缓存感知: 无专门缓存优化` → `有 prompt cache（cache: "auto"），无 cache_edits（Anthropic 专有）`
2. ✅ **末尾**（line 872）：`放弃了 Prompt Cache 优化和 cache_edits API` → `放弃了 cache_edits API（Anthropic 专有的服务端缓存删除机制）`
3. ✅ **§9 表**（line 841）：`~88.5% 时主动阻塞 API 请求` → `~98.5% 时主动阻塞 API 请求（effectiveWindow - 3K）`

### ✅ 已修正（精度）

1. ✅ **§9 表**（line 845）：`~3,960 行（5 个核心文件）` → `~3,960 行（11 个文件）`
2. ✅ **§3.2**（line 261）：`240 多行` → `275 行`
3. ✅ **§7.1**（line 679）：`filterCompacted() at 1014-1065` → `1014-1037`（函数本体）

### ch1 回滚（已完成）

1. ✅ ch1 line 378：`cache_control 等 Anthropic 公开 API` → `cache_edits 等 Anthropic 内部 API`（回滚）
2. ✅ ch1 line 379：`microCompact + apiMicrocompact 前 2 级零 LLM` → `5 级 Compact 策略（前 4 级零 LLM 调用）`（回滚）
3. ch1 verify 报告 CC-F11 需更正：cache_edits 确实存在

---

## 验证结论（终态）

- **OpenCode 侧**：0 事实错误，2 项行号微偏差已修正
- **CC 侧**：1 事实错误已修正（88.5%→98.5%）+ 3 项 ⚠️ 已修正（文件数、process 行数、filterCompacted 行号）+ 2 项 ⚠️ 保留（13K 归属、90% vs 93.5%——均为阈值定义差异，不影响理解）
- **跨章节**：ch1 两项错误修正已回滚（cache_edits 真实存在、前 4 级零 LLM 正确）
- **整体**：文章对 OpenCode 2 级压缩机制的拆解极其精确（28/30 完全匹配）；CC 5 级压缩描述也基本准确（cache_edits、前 4 级零 LLM、9 段 XML 均确认正确）；主要问题集中在 OpenCode Prompt Cache 描述错误和 88.5% 数字无据，已全部修正

### 重大发现：ch1 review 误判已纠正

本次 ch4 验证发现 ch1 review 有两项**误判**（exp-2 的 CC 源码深查推翻了 ch1 的结论）：
1. **cache_edits**：ch1 错误地认为"源码中无此词"，实际在 `microCompact.ts` 和 `claude.ts` 中存在多处。是 Anthropic 专有 API content block，与标准 `cache_control` 不同。ch1 错误修正已回滚。
2. **"前 4 级零 LLM"**：ch1 错误地改为"前 2 级零 LLM"，实际 CC 的 5 级压缩中 Level 1-4 均为纯数据操作，只有 Level 5 调 LLM。ch1 错误修正已回滚。

这说明深查源码（而非快速 grep）对 CC 专有机制至关重要。`cache_edits` 作为非公开 Anthropic API，只在特定文件中出现，浅层 grep 容易漏判。
