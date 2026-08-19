# 配图指南

> 本站（agentsrc）**配图生成**的唯一权威，适用于所有专栏（Claude Code / OpenCode / Codex / 新增专栏）。
> 技术配图一律使用 **Fireworks Tech Graph** skill 脚本化生成，确保几何正确、风格统一、可导出多种格式。

本规范只管**图像的技术生产**（风格选型、生成方式、命名、存放、引用）。写作时如何决定"配几张、放哪节、传达什么"属于**写作侧**的职责，另有写作规范文档负责；本文档与之互补、**互不引用**。

---

## 一、生成方式

- 技术配图（架构、流程、时序、部署、数据流等技术概念图）一律用 **Fireworks Tech Graph** skill 生成。
- 产出物为**几何校验过的 SVG**，可导出 **PNG / GIF（动效）/ 离线 HTML**。
- 图内**英文标注、禁止中文**；SVG 导出后放入站点资源目录。
- 不使用图像生成模型（如 DALL·E / Midjourney / Stable Diffusion 等）烧"插画式"配图。

> skill 安装在 `~/.config/opencode/skills/fireworks-tech-graph/`。

---

## 二、主力 Style 与使用场景

全站统一采用以下 5 种内置 style，按**图的语义类型**挑选。其余 style（UML、时序、ER、事件流等特殊图型）按 skills 自带 `references/style-diagram-matrix.md` 临时挑选。

| Style | 图类型 / 场合 | 风格特征 |
|-------|--------------|---------|
| **Blueprint**（默认主力） | 架构概览、分层、模块关系、源码精读正文里的技术图 | 深蓝底 + 青色网格，工程图纸感，锐角 |
| **Dark Luxury** | 封面 / 开门图、设计哲思 / 总结页的立牌视觉 | 纯黑底 + 香槟金作辅，编辑级高端质感 |
| **Glassmorphism** | 面向读者的产品 / 概念呈现、演示性 / Hero 感画面 | 深色渐变 + 半透明玻璃卡片，现代展示感 |
| **Cloud Fabric** | 云部署 / 多区域 / 网络 / 容器集群归属类拓扑 | 网格底，清晰标注"在哪运行、谁属谁" |
| **Ops Pulse** | 可靠性 / 事故 / 延迟 / SRE / 黄金信号追踪类 | 运维观测类视图，强调状态与追踪 |

> 默认主力为 **Blueprint**；有明确更适配的语义（部署→Cloud Fabric、运维→Ops Pulse、封面→Dark Luxury 等）时换用对应 style。

---

## 三、生成命令骨架

在 agent 环境下运行时，需先定位 skill 根目录（OpenCode 下 `CLAUDE_SKILL_DIR` 可能不缺省，使用绝对路径）：

```bash
SKILL_ROOT="$HOME/.config/opencode/skills/fireworks-tech-graph"
```

常用操作（均在 `$SKILL_ROOT` 下）：

| 操作 | 命令 |
|------|------|
| 从模板生成 SVG | `python3 "$SKILL_ROOT/scripts/generate-from-template.py" <layout> <out.svg> '{"title":"...","nodes":[],"arrows":[]}'` |
| 校验 SVG（语法/几何/可渲染） | `"$SKILL_ROOT/scripts/validate-svg.sh" <file.svg>` |
| 校验 + 导出 PNG | `"$SKILL_ROOT/scripts/generate-diagram.sh" -t <type> -o <out.svg>`（再导出 PNG） |
| 动效 GIF / HTML（按需） | 参考 skill 内 `references/motion-effects.md` / `export-interactive-html.py` |

生成前先加载对应 style token 文件：`"$SKILL_ROOT/references/style-N-<name>.md"`，以取得准确色值、圆角、阴影等 SVG 模式。

---

## 四、命名、存放与引用

| 项 | 规范 |
|----|------|
| 文件格式 | 源文件 `.svg`；站点引用 PNG（16:9 或 1:1 视场景） |
| 命名 | `{type}-{scene}.svg`，如 `architecture-01.svg`、`deploy-01.svg` |
| 存放 | 站点 `public/images/{type}/`，如 `public/images/architecture/` |
| 引用 | 相对路径：`![描述](/images/{type}/{filename})`，避免绝对路径 |
| 品牌标注 | 图内英文标签保持简洁、大写、无衬线；核心解释靠正文承载不依赖图内文字 |

---

## 五、使用建议

1. **先文后图**：先写文章、明确每张图传达的核心设计点，再批量生成，避免装饰性配图
2. **一张图一个点**：每张图只聚焦一个设计取舍，直接服务正文的抽象论证
3. **统一风格**：同一专栏内固定于上述 style 体系，不混用，确保视觉统一
4. **可复用**：图内少依赖正文，便于跨语境复用
5. **生成后必过多模态视觉审查**：SVG 导出 PNG 后，必须派一个**多模态 subagent**（如 `designer`）把 PNG 读回来做视觉审查——文字是否成方块/乱码/溢出、箭头是否穿过组件内部或遮挡文字、几何/对齐是否合格、构图预算是否合理、内容是否正确。`visual_review` 未 `passed` 前不得接入正文/发布；若当前 agent 无读图能力，须改派多模态 agent，不得跳过硬伤检查。

---

> 新增文章配图请沿用本规范，确保全站技术图的视觉效果一致。