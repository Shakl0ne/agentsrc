# AI 概念图生成规范

本站所有文章配图均遵循统一的视觉风格体系，确保整体品牌一致性。以下是通用生成规范与 Prompt 模板。

## 一、视觉风格定义

### 主基调
- **深色未来主义**（Dark Futurism）
- 矢量插画风格，干净利落的线条
- 避免照片写实，保持"技术感"与"可读性"的平衡

### 配色系统
| 用途 | 色值 | 说明 |
|------|------|------|
| 背景 | `#0A1628` | 深邃 Navy，近黑但带蓝调 |
| 主色 | `#00D4FF` | 亮 Cyan，高能量科技感 |
| 点缀 | `#FFB347` | 温暖 Amber，用于重点、警告、差异标注 |
| 辅色 | `#7B8794` | 中性灰蓝，用于次要元素、边框、文字 |
| 纯白 | `#FFFFFF` | 仅用于关键文字和高光 |

### 构图原则
1. **深色背景优先**：所有概念图以 `#0A1628` 为底
2. **中心聚焦**：主体元素居中或偏中，避免视觉分散
3. **等距视角（isometric）**：架构图、流程图优先使用 2.5D 等距视角
4. **留白呼吸**：元素间保持充足间距，不塞满画面
5. **英文标注，严禁中文**：图片可带少量英文文字标注（如节点标签、步骤序号），但**绝对不能出现中文字符**。AI 生成中文几乎 100% 是乱码。英文标注需简洁、大写、无衬线字体风格。

> **文字使用原则**
> - 少量英文标签辅助理解：如节点名（"AGENT"、"CONTEXT"）、步骤序号（"STEP 1"）等。
> - 禁止中文：AI 对汉字渲染能力极差，出现即毁图。
> - 核心说明仍由 HTML 承载：图片上的文字只是辅助定位，详细解释写在文章里。
> - 无文字标注的图更通用：一张纯视觉图可以复用于不同语境。

## 二、场景类型与模板

### 类型 A：架构概览图
用于展示系统整体结构、模块关系。

```
Dark futuristic vector illustration, system architecture diagram in isometric 2.5D perspective, navy blue (#0A1628) background, glowing cyan (#00D4FF) nodes and connections with subtle amber (#FFB347) highlights on key components, clean geometric shapes, depth layers showing hierarchy, minimal style, no text, 8k crisp lines
```

### 类型 B：流程循环图
用于展示主循环、Turn 系统、生命周期等循环/顺序流程。

```
Dark futuristic vector illustration, circular flow diagram showing sequential process steps, navy blue (#0A1628) background, cyan (#00D4FF) glowing arrows and step nodes, amber (#FFB347) accent on entry/exit points and decision diamonds, isometric perspective, clean line art, subtle gradient glows, no text, technical blueprint style
```

### 类型 C：对比/表格可视化
用于展示两个框架/方案的差异对比。

```
Dark futuristic vector illustration, side-by-side comparison layout, navy blue (#0A1628) background, left panel cyan (#00D4FF) dominant showing system A, right panel amber (#FFB347) dominant showing system B, subtle dividing line in center, floating geometric shapes representing features, isometric perspective, clean and organized, no text
```

### 类型 D：安全/沙箱抽象图
用于展示隔离机制、安全边界等抽象概念。

```
Dark futuristic vector illustration, abstract security boundary visualization, navy blue (#0A1628) background, glowing cyan (#00D4FF) shield or container structure, amber (#FFB347) warning/highlight elements outside the boundary, layered depth effect, translucent glass-like barriers, clean vector style, no text, 8k
```

### 类型 E：压缩/分层堆叠图
用于展示分层架构、压缩机制、金字塔结构。

```
Dark futuristic vector illustration, layered stack or pyramid structure showing hierarchical levels, navy blue (#0A1628) background, cyan (#00D4FF) glow increasing from bottom to top, amber (#FFB347) top layer for most critical/visible content, floating layers with subtle shadow separation, isometric perspective, clean geometric, no text
```

### 类型 F：记忆/存储概念图
用于展示记忆系统、持久化、上下文存储等。

```
Dark futuristic vector illustration, abstract memory/storage visualization, navy blue (#0A1628) background, cyan (#00D4FF) glowing database cube or memory chip structure, amber (#FFB347) highlights on active/transient data, layered concentric circles or stacked blocks, subtle particle effects, no text, clean technical aesthetic
```

### 类型 G：多 Agent 协作/编排图
用于展示 Agent 树、并行执行、消息传递等。

```
Dark futuristic vector illustration, branching tree network showing multiple agents connected, navy blue (#0A1628) background, cyan (#00D4FF) glowing parent node with branching lines to child nodes, amber (#FFB347) on active/operative nodes, some nodes larger than others indicating hierarchy, subtle pulse/glow animation suggestion, isometric perspective, no text
```

## 三、通用后缀（必加）

无论哪种类型，Prompt 末尾统一附加：

```
, no text, no letters, no labels, no captions, no watermark, clean vector illustration, 8k resolution, dark background only
```

## 四、负面词（Negative Prompt）

生成时建议排除：

```
text, letters, words, labels, captions, watermark, signature, photo realistic, photographic, blurry, messy, cluttered, bright background, white background, cartoon, anime, sketch, hand-drawn
```

## 五、实际使用建议

1. **先文后图**：先写好文章，明确需要几张图、每张图的核心信息，再批量生成
2. **统一尺寸**：所有文章配图建议统一比例（本站使用 16:9 或 1:1 视场景而定）
3. **文件命名**：`{type}-{scene}.png`，如 `opencode-01-hero.png`、`codex-04-flow.png`
4. **存放路径**：`public/images/{type}/`，如 `public/images/opencode/`、`public/images/codex/`
5. **引用方式**：`![描述文字](/images/{type}/{filename})`

## 六、示例：一张完整的生成 Prompt

**场景**：OpenCode 主循环 runLoop 七步流程概念图

```
Dark futuristic vector illustration, circular 7-step process loop diagram with clean geometric nodes, navy blue (#0A1628) background, glowing cyan (#00D4FF) arrows connecting circular nodes arranged in a cycle, amber (#FFB347) accent on the start/entry node, isometric 2.5D perspective, clean line art, subtle gradient glows on each node, depth and hierarchy visible, no text, no letters, no labels, no captions, clean vector illustration, 8k resolution, dark background only
```

---

> 新增文章配图请沿用此风格体系，确保视觉统一。

## 七、本地开发

```bash
cd agentsrc
npm install        # 首次
npm run dev -- --port 4188
```

`npm run dev` 带热更新（HMR）：改 `.md`、`.css` 后浏览器自动刷新，**无需手动 build**。

## 八、写后验算（每章必做）

写后验算包含两层：**源码对齐**（事实正确）和**结构审校**（论述自洽），两轮缺一不可。

### 第一层：源码对齐（事实正确性）

人工判断结论是否正确，脚本仅做机械校验。

#### 验算清单

1. **文件路径存在**：正文中引用的文件名/路径是否在官方仓库实际存在？
2. **关键定义匹配**：提到的接口名、函数名、数据结构是否在源码中能找到对应定义？
3. **流程顺序对齐**：画的流程图/顺序图是否跟源码的执行路径完全一致？
4. **版本准确**：引用的源码版本是否与文章声称的一致？（Codex 和 OpenCode 都还在快速迭代）
5. **对比结论准确**：与 Claude Code 的对比是否有有据可依？不是主观感受，而是能从源码中找到对应机制的不同。

### 第二层：结构审校（论述自洽性）

完成源码对齐后，通读全文（或分段）做以下 4 个维度的检查：

1. **层次划分合理**——如果文章把系统拆成若干层/模块，问自己：这个分法在源码中有对应边界吗？是按持久性、功能域、还是作者直觉分的？有没有把不同抽象层级的概念混在一层里？分层依据应统一且有源码依据。

2. **前后一致、无矛盾**——同一定义或机制在不同位置的说法是否一致？早期引用的数字、公式、逻辑是否在后文用到时仍成立？如果文章基于特定版本的分析，中间是否混入了更晚版本的描述而没有标注？

3. **覆盖无遗漏**——对于每个层级/模块，核心实现是否都覆盖了：有没有只说了"是什么"但没回答"怎么做到的"？有没有只说了一种触发路径，但源码里其实有两条？

4. **可读性检查**——长段落的代码引用是否可以用 2-3 行总结替代？流程图能否精简而不损失信息？有没有"读者看到这里会困惑"的非线性跳跃？

> **审校原型**：在审校 OpenCode 上下文架构一章时，发现：引用了不存在的 `request.ts` 文件（源码对齐问题 1）；Reference 的 `@refname` 语法在源码中未实现（源码对齐问题 2）；"2 轮对话"的定义未说清是"消息条数"还是"完整轮次"（结构审校问题 1）；compaction 的隔轮执行机制只讲了结果没讲原因（结构审校问题 3）。这五个问题分别属于两个层面的不同维度，说明双层校验缺一不可。

### 快速机械校验（可选）

如果已经把官方仓库 clone 到 `agent/` 目录，可以用 `grep` 快速验证正文中提到的符号名：

```bash
# 示例：检查 OpenCode 文章中提到的函数名是否在源码中找到
# grep -ri "runLoop" agent/opencode/src/
# grep -ri "SubAgent" agent/opencode/src/
```

> 注意：脚本只能告诉你「这个名词在源码里是否存在」，判断不了上下文和逻辑结论是否正确。最核心的"说得对不对"仍需人工走读源码。

### 审校流程建议

为减少逐段切换上下文的损耗，建议按角色分工分批完成：

1. **源码对齐轮**（针对性查证）：只查源码，不写文章。带着正文中提到的事实声明（文件名、函数名、常量值、流程步骤），逐条在源码中 grep/read 确认。发现差异即标记。这一轮结束后统一修正事实错误。

2. **结构审校轮**（通读检查）：确认事实无误后，从头到尾通读全文。不带源码，只关注论述是否自洽——层次划分合理吗？前后有无矛盾？哪里讲得不清楚？读完之后一条条改。

3. **最终一致性检查**：改完后扫一遍全文，确保早前标记的修改没有引入新的不一致（编号、引用、关键词）。

两轮分离的好处：**查证时不需要"写文章"的视角，通读时不需要"对齐源码"的负担**，每个任务只用一种认知模式。

### 修复原则

- 如果发现源码已更新导致文章内容过时，直接在文章中标注版本号和时间点，注明"截至 X 版本"。
- 不要硬套最新版本。文章的核心价值是分析特定版本的实现思路，而不是永远跟最新 commit 对齐。
- 如果差距过大，考虑 append 一段"版本更新说明"而不是全文重写。

## 九、作者署名规范

本站作者身份信息统一如下，所有文章、首页、配置文件均按此规范使用。

| 用途 | 名称 | 备注 |
|------|------|------|
| 文章署名/作者展示 | `shaco` | 全小写，无空格 |
| GitHub 用户名 | `Shakl0ne` | URL: `https://github.com/Shakl0ne` |
| 导航 GitHub 图标链接 | `https://github.com/Shakl0ne` | 指向个人主页 |

### 禁止出现

- ❌ "凌皓"、"小林" 等其他化名
- ❌ 大写 "Shaco" 或驼峰 "ShAco" 等变体
- ❌ 在文章正文里自称（如"我是小林"），文章保持中性叙述口吻

### 一致性检查

发布前 grep 全站确认：

```bash
grep -rn "凌皓\|小林" --include="*.md" .
# 期望输出为空
```
