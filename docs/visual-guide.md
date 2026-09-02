# 配图指南

> 本站（agentsrc）**配图生成**的唯一权威，适用于所有专栏（Claude Code / OpenCode / Codex / 新增专栏）。
> 技术配图一律使用 **Fireworks Tech Graph** skill 脚本化生成，确保几何正确、风格统一、可导出多种格式。

本规范只管**图像的技术生产**（风格选型、生成方式、命名、存放、引用）。写作时如何决定"配几张、放哪节、传达什么"属于**写作侧**的职责，另有写作规范文档负责；本文档与之互补、**互不引用**。

---

## 一、生成方式

- 技术配图（架构、流程、时序、部署、数据流等技术概念图）优先采用**纯手工编写 SVG 代码**（Hand-coded SVG），以达到最高质量的定制化 UI 卡片级设计。
- 产出物为**几何校验过的 SVG**，文章中**只使用 SVG 格式**，不再导出和使用 PNG。
- 图内**英文标注、禁止中文**；SVG 直接放入站点资源目录并在文章中引用。
- 不使用图像生成模型（如 DALL·E / Midjourney / Stable Diffusion 等）烧"插画式"配图。

> 若使用自动化工具兜底，可使用 `~/.config/opencode/skills/fireworks-tech-graph/`，但产出必须经过手工 CSS 注入与排版微调，使其达到手写级质量。

---

## 二、主力 Style 与使用场景

全站统一采用以下 5 种内置 style，按**图的语义类型**挑选。其余 style（UML、时序、ER、事件流等特殊图型）按 skills 自带 `references/style-diagram-matrix.md` 临时挑选。

| Style | 图类型 / 场合 | 风格特征 |
|-------|--------------|---------|
| **GitHub Dark Hand-Coded**（默认主力） | 架构概览、分层、概念骨架图、第一章总览、面向读者的产品化技术呈现 | 纯手工 SVG，深色渐变背景（`#0d1117` 到 `#161b22`），半透明玻璃卡片（`rgba(255,255,255,0.03)`），发光边框，标题使用彩色渐变（`url(#titleGrad)`），极具现代展示感 |
| **Blueprint** | 源码精读正文里的工程关系图、模块关系、需要更强工程图纸感的结构图 | 深蓝底 + 青色网格，工程图纸感，锐角 |
| **Dark Luxury** | 封面 / 开门图、设计哲思 / 总结页的立牌视觉 | 纯黑底 + 香槟金作辅，编辑级高端质感 |
| **Cloud Fabric** | 云部署 / 多区域 / 网络 / 容器集群归属类拓扑 | 网格底，清晰标注"在哪运行、谁属谁" |
| **Ops Pulse** | 可靠性 / 事故 / 延迟 / SRE / 黄金信号追踪类 | 运维观测类视图，强调状态与追踪 |

> 默认主力为 **GitHub Dark Hand-Coded**；有明确更适配的语义（工程关系拆解→Blueprint、部署→Cloud Fabric、运维→Ops Pulse、封面→Dark Luxury 等）时换用对应 style。

---

## 三、GitHub Dark 手写 SVG 模板骨架

当需要生成默认主力的 GitHub Dark 风格配图时，**不要使用脚本**，直接由 Agent 生成如下结构的纯手工 SVG 代码。这能确保卡片、渐变、网格等 UI 细节达到像素级完美：

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 600" width="1000" height="600" role="img" aria-label="Diagram Title">
  <style>
    text { font-family: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .title { font-size: 32px; font-weight: 700; fill: url(#titleGrad); }
    .subtitle { font-size: 18px; font-weight: 500; fill: #b7c3d2; }
    .card-title { font-size: 20px; font-weight: 700; fill: #f0f6fc; text-anchor: middle; }
    .inner-text { font-size: 16px; font-weight: 600; fill: #d2dbe5; text-anchor: middle; }
    .inner-sub { font-size: 13px; font-weight: 500; fill: #8b9eb3; text-anchor: middle; }
    .arrow-text { font-size: 14px; font-weight: 600; fill: #9fb1c8; text-anchor: middle; }
    
    .card { fill: rgba(255,255,255,0.03); stroke: rgba(255,255,255,0.1); stroke-width: 2; }
    .inner-card { fill: rgba(255,255,255,0.06); stroke: rgba(255,255,255,0.15); stroke-width: 1.5; }
    .card-highlight { fill: rgba(88,166,255,0.08); stroke: rgba(88,166,255,0.3); stroke-width: 1.5; }
    .card-async { fill: rgba(63,185,80,0.08); stroke: rgba(63,185,80,0.3); stroke-width: 1.5; stroke-dasharray: 4 4; }
    .card-warn { fill: rgba(239,68,68,0.08); stroke: rgba(239,68,68,0.3); stroke-width: 1.5; }
    .card-core { fill: rgba(168,85,247,0.08); stroke: rgba(168,85,247,0.4); stroke-width: 2; }
    
    .spine { fill: none; stroke: rgba(125,196,255,0.5); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
    .spine-async { fill: none; stroke: rgba(63,185,80,0.6); stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; stroke-dasharray: 6 6; }
    .spine-warn { fill: none; stroke: rgba(239,68,68,0.6); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
  </style>

  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0d1117"/>
      <stop offset="50%" stop-color="#161b22"/>
      <stop offset="100%" stop-color="#0d1117"/>
    </linearGradient>
    <linearGradient id="titleGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#58a6ff"/>
      <stop offset="100%" stop-color="#bc8cff"/>
    </linearGradient>
    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M40 0 L0 0 0 40" fill="none" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
    </pattern>
    <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(125,196,255,0.6)"/>
    </marker>
    <marker id="arrow-async" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(63,185,80,0.8)"/>
    </marker>
    <marker id="arrow-warn" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(239,68,68,0.8)"/>
    </marker>
  </defs>

  <!-- 基础背景与网格 -->
  <rect width="100%" height="100%" fill="url(#bg)"/>
  <rect width="100%" height="100%" fill="url(#grid)"/>

  <!-- 标题区 -->
  <text x="500" y="60" text-anchor="middle" class="title">DIAGRAM TITLE</text>
  <text x="500" y="90" text-anchor="middle" class="subtitle">Brief explanation of the diagram's core concept</text>

  <!-- 节点与连线区 (Agent 自由发挥排版) -->
  <!-- ... -->
</svg>
```

---

## 四、命名、存放与引用

| 项 | 规范 |
|----|------|
| 文件格式 | 统一使用 `.svg`，文章内直接引用 SVG（不再使用 PNG） |
| 命名 | `{type}-{scene}.svg`，如 `architecture-01.svg`、`deploy-01.svg` |
| 存放 | 站点 `public/images/{专栏名}/`，按专栏归类存放（如 `public/images/opencode/`） |
| 引用 | 相对路径：`![描述](/images/{专栏名}/{filename})`，避免绝对路径 |
| 品牌标注 | 图内英文标签保持简洁、大写、无衬线；核心解释靠正文承载不依赖图内文字 |

---

## 五、使用建议

1. **先文后图**：先写文章、明确每张图传达的核心设计点，再批量生成，避免装饰性配图
2. **一张图一个点**：每张图只聚焦一个设计取舍，直接服务正文的抽象论证
3. **统一风格**：同一专栏内固定于上述 style 体系，不混用，确保视觉统一
4. **可复用**：图内少依赖正文，便于跨语境复用
5. **生成后必过多模态视觉审查**：SVG 生成后，必须由可读图的多模态 agent（如 `designer`）把图像读回来做视觉审查（如果需要，临时转成位图供 agent 审查，但最终文章只用 SVG）——文字是否成方块/乱码/溢出、箭头是否穿过组件内部或遮挡文字、几何/对齐是否合格、构图预算是否合理、内容是否正确。`visual_review` 未 `passed` 前不得接入正文/发布；若当前 agent 无读图能力，须改派多模态 agent，不得跳过。

   **额外硬要求（新增）**：**每做完一张架构图，都必须再做一轮“视觉分析”，专门找表达缺口，而不只是查有没有坏掉。** 这一轮要强制回答：
   - 这张图的一眼主判断是不是清楚，还是读者要先读字才能懂？
   - 有没有信息虽然没溢出，但已经太密、太碎、太挤？
   - 有没有视觉重心不稳、主结构不够显眼、辅助结构反而更抢眼？
   - 有没有“形式上能读，实际上不够快懂”的表达缺口？
   - 有没有图内仍在偷塞第二个判断、第三个判断？

   **注意**：视觉审查通过，不等于视觉分析通过。前者解决“坏没坏”，后者解决“有没有缺口、是不是还不够好”。两轮都过了，架构图才算完成。

6. **字体与边框必须放大（硬性）**：技术图是为**网页 + 移动端**展示，控制字体缩到一张小图上会模糊成一片。生成时：
    - **正文/卡片内文字**：标题、副标题、卡片标题一律大字号、粗体；图片小字只放必要语义，不放大段说明；
    - **除数与注释**：最小可接受字号确保在 手机竖屏近似等宽宽度下仍可读，宁可少写字、放大写；
    - **边框/连线**：卡片边框、依赖/连接线必须足够粗，避免在网页上发虚；箭头头部同步放大；
    - **核对方式**：导出后按"手机屏幕宽度下也能看清字与框"这一标准做视觉审查，不达标须回炉放大重出。

   **执行下限（新增硬规则）**：除非用户明确要求做海报式超大画幅且另有专项设计，不然技术图默认按下面的最小字号执行；如果画面拥挤，优先删字、拆图、增大画布，**不要降到下限以下**：

   | 文本类型 | 最小字号 |
   |---------|---------|
   | 主标题 | **36px** |
   | 副标题 / 总体说明 | **20px** |
   | 卡片标题 / 模块标题 | **20px** |
   | 卡片正文 / 子标签 / 流程节点文字 | **16px** |
   | 尾注 / 辅助说明 / legend / foot label | **16px** |

   - **任何图内文字不得低于 `16px`**（硬性下限）；
   - 若 Blueprint / Flat / Notion 等内置 style 的默认推荐字号低于此标准，**以本站 visual guide 为准**，必须手动放大；
   - 若仍担心移动端可读性，继续上调到 18px、20px，而不是勉强卡在 16px；
   - 多模态视觉审查时，需明确检查是否存在 `< 16px` 的文字样式或等效视觉尺寸过小的问题。

7. **内容与可读性同等优先**：图内英文标注保持简洁大写无衬线，核心解释交给正文，不靠图内长文本。

8. **先定这张图只回答哪一个判断（新增硬规则）**：正文解释型技术图在动手前，必须先写出这张图只回答的**唯一主判断**，例如：
   - 分层
   - 主链
   - 扩展关系
   - 对比
   - 部署归属
   - 运行流

   然后按这个判断约束内容：
   - 目标是**分层**，不要引入执行顺序；
   - 目标是**执行链**，不要引入模块树；
   - 目标是**扩展关系**，不要引入主链细节；
   - 目标是**对比**，不要混入流程展开。

   **禁止一张图同时承担两个以上主判断。**

9. **信息密度不等于概念密度（新增硬规则）**：如果画面显得空，优先按下面顺序补强，而不是先加模块、加技术词：
   1. **构图结构**：spine、rail、orbit、alignment guide、background field；
   2. **角色提示**：layer label、step label、very short secondary label；
   3. **关系提示**：极短连接词，且只能少量使用；
   4. **最后才考虑新增节点或新增概念词**。

   > 空，不等于该加模块；先检查是不是构图没有组织好。

10. **辅助文案预算必须受控（新增硬规则）**：除主标题与节点标题外，图内辅助文案必须受预算约束：
    - 每个节点：标题 1 行，副标签最多 1 行；
    - 全图额外辅助文案：subtitle 最多 1 行；
    - grouping label 最多 2 处；
    - relation label 最多 2~3 处；
    - 如果已经加了 rail、spine、orbit 等结构骨架，应优先删辅助字，而不是继续加说明词。

11. **低节点解释图优先选“主结构一眼可见”的骨架（新增）**：对于 3~8 个节点的概念解释图，优先采用下面的构图模板，而不是自由排盒子：
    - **Vertical spine**：分层 / stack / hierarchy
    - **Single rail**：assembly chain / one-path flow
    - **Hub and attachments**：core + extensions
    - **Side-by-side pair**：对比
    - **Ring / orbit**：围绕核心但非主流程

    解释型配图优先让读者先看懂“这是什么关系”，再读标签。

12. **区分三种连接语义（新增）**：正文概念图中，至少要区分以下三类连接，不要一律画成同一种箭头：
    - **layer relation / dependency**：短、直、克制；
    - **assembly flow**：连续、同向、可形成 rail；
    - **attachment / capability relation**：短、柔、贴核心边界，不应画成主流程冲刺箭头。

    当图要表达“挂能力”“附着关系”时，优先使用柔和 attach connector，而不是强流程箭头。

13. **视觉审查必须校验表达是否跑偏（新增）**：除文字、箭头、几何之外，多模态视觉审查还必须至少检查：
    - 节点与连接关系是否表达了原本那一个主判断；
    - 是否因为补信息密度而提前剧透后文；
    - 辅助文案是否抢走了主结构的注意力。

---

> 新增文章配图请沿用本规范，确保全站技术图的视觉效果一致。
