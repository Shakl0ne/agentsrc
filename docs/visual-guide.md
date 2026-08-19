# 配图指南

> 本站（agentsrc）**配图生成**的唯一权威。所有文章的配图均遵循统一的视觉风格体系，确保整体品牌一致性。以下为通用生成规范与 Prompt 模板。

本规范只管**图像的视觉生产**（风格、配色、模板、命名、存放、引用）。写作时如何决定"配几张、放哪节、传达什么"属于**写作侧**的职责，另有写作规范文档负责；本文档与之互补、互不引用。

---

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
1. **深色背景优先**：概念图以 `#0A1628` 为底
2. **中心聚焦**：主体居中或偏中，避免视觉分散
3. **等距视角（isometric）**：架构图、流程图优先用 2.5D 等距视角
4. **留白呼吸**：元素间保持充足间距，不塞满画面
5. **英文标注，严禁中文**：可带少量英文标注，但**绝对不能出现中文**。英文标注简洁、大写、无衬线字体。

> **文字使用原则**
> - 少量英文标签辅助理解（节点名、步骤序号）
> - 禁止中文：AI 对汉字渲染极差，出现即毁图
> - 核心说明用 HTML 承载，图片文字只是辅助定位
> - 无文字标注的图更通用：可复用于不同语境

---

## 二、场景类型与模板

| 类型 | 用途 |
|------|------|
| A：架构概览图 | 系统整体结构、模块关系 |
| B：流程循环图 | 主循环、Turn 系统、生命周期等循环/顺序流程 |
| C：对比/表格可视化 | 两个框架/方案差异对比 |
| D：安全/沙箱抽象图 | 隔离机制、安全边界 |
| E：压缩/分层堆叠图 | 分层架构、压缩机制、金字塔结构 |
| F：记忆/存储概念图 | 记忆系统、持久化、上下文存储 |
| G：多 Agent 协作/编排图 | Agent 树、并行执行、消息传递 |

### 类型 A：架构概览图
```
Dark futuristic vector illustration, system architecture diagram in isometric 2.5D perspective, navy blue (#0A1628) background, glowing cyan (#00D4FF) nodes and connections with subtle amber (#FFB347) highlights on key components, clean geometric shapes, depth layers showing hierarchy, minimal style, no text, 8k crisp lines
```

### 类型 B：流程循环图
```
Dark futuristic vector illustration, circular flow diagram showing sequential process steps, navy blue (#0A1628) background, cyan (#00D4FF) glowing arrows and step nodes, amber (#FFB347) accent on entry/exit points and decision diamonds, isometric perspective, clean line art, subtle gradient glows, no text, technical blueprint style
```

### 类型 C：对比/表格可视化
```
Dark futuristic vector illustration, side-by-side comparison layout, navy blue (#0A1628) background, left panel cyan (#00D4FF) dominant showing system A, right panel amber (#FFB347) dominant showing system B, subtle dividing line in center, floating geometric shapes representing features, isometric perspective, clean and organized, no text
```

### 类型 D：安全/沙箱抽象图
```
Dark futuristic vector illustration, abstract security boundary visualization, navy blue (#0A1628) background, glowing cyan (#00D4FF) shield structure, amber (#FFB347) warning/highlight outside boundary, layered depth, translucent glass-like barriers, clean vector style, no text, 8k
```

### 类型 E：压缩/分层堆叠图
```
Dark futuristic vector illustration, layered stack/pyramid showing hierarchical levels, navy blue (#0A1628) background, cyan (#00D4FF) glow from bottom to top, amber (#FFB347) top critical layer, floating layers with subtle shadow separation, isometric perspective, clean geometric, no text
```

### 类型 F：记忆/存储概念图
```
Dark futuristic vector illustration, abstract memory/storage visualization, navy blue (#0A1628) background, cyan (#00D4FF) glowing database cube or memory chip, amber (#FFB347) on active/transient data, layered concentric circles/stacked blocks, subtle particle effects, no text, clean technical aesthetic
```

### 类型 G：多 Agent 协作/编排图
```
Dark futuristic vector illustration, branching tree network of multiple agents, navy blue (#0A1628) background, cyan (#00D4FF) glowing parent node with branch lines to children, amber (#FFB347) on operative nodes, hierarchy via node size, subtle pulse glow, isometric perspective, no text
```

---

## 三、通用后缀（必加）

无论哪种类型，prompt 末尾统一附加：

```
, no text, no letters, no labels, no captions, no watermark, clean vector illustration, 8k resolution, dark background only
```

---

## 四、负面词（Negative Prompt）

生成时建议排除：

```
text, letters, words, labels, captions, watermark, signature, photo realistic, photographic, blurry, messy, cluttered, bright background, white background, cartoon, anime, sketch, hand-drawn
```

---

## 五、实际使用建议

1. **先文后图**：先写好文章，明确每图核心信息，再批量生成
2. **统一尺寸**：本站建议统一比例（16:9 或 1:1 视场景而定）
3. **文件命名**：`{type}-{scene}.png`，如 `opencode-01-hero.png`
4. **存放路径**：`public/images/{type}/`，如 `public/images/opencode/`
5. **引用方式**：`![描述](/images/{type}/{filename})`

---

## 六、示例：一张完整的生成 Prompt

**场景**：主循环 runLoop 七步流程概念图

```text
Dark futuristic vector illustration, circular 7-step process loop diagram with clean geometric nodes, navy blue (#0A1628) background, glowing cyan (#00D4FF) arrows connecting circular nodes arranged in a cycle, amber (#FFB347) accent on the start/entry node, isometric 2.5D, clean line art, subtle gradient glows, no text, no letters, no labels, no captions, clean vector illustration, 8k resolution, dark background only
```

---

> 新增文章配图请沿用此风格体系，确保视觉统一。