---
title: DeepSeek Harness 全景：一切皆插件的通用 agent 引擎
---

# DeepSeek Harness 全景：一切皆插件的通用 agent 引擎

本站拆过的三个终端编程 Agent——OpenCode、Codex、Claude Code——骨架是同一个：一个写死在产品中心的主循环，工具、权限、上下文管理围绕它长出来。你要扩展，要么改核心循环，要么在它预设的钩子上挂东西。

DeepSeek 开源的 `dsh` 不在这个谱系里。它把自己放在产品的下一层：做一台可以被任意产品装配的通用 agent 引擎，服务对象是拿它当内核的产品团队。这个定位落到代码上，就是整栏的核心判断：

> **`dsh` 的源码里没有一个不可变的中央循环可供拆解——只有一棵运行期组装出来的插件树。模型适配器、工具注册表、会话日志、agent 循环本身，全部是插件，全部可以从配置换掉。**

所以这一栏的读法也变了：前三栏是"拆一个主循环"，这一栏是"看一棵插件树怎么拼出来、怎么装配成产品"。这一篇先把总地图铺开，读完你会带着三个问题的答案离开：

- 300 多个包的 monorepo，靠什么规则组织而不散架？
- 同一棵插件树怎么装配出 Web、命令行、SDK、自动化协议、桌面这些产品形态？
- 它和前三栏同能不同构，差别到底落在哪几层？

Cordis 原语、turn/step 事件流、日志投影、capability 缝这些机制细节，留给后面五篇下潜。

## 一、第四种物种：没有特权核心

先给一张定位表：

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness |
|------|------|------|
| 定位 | 终端编程 Agent（面向用户的产品） | 通用 agent harness（被产品装配的引擎） |
| 核心抽象 | 一个主循环（runLoop / reactor / QueryEngine） | 插件树 + 组合框架 |
| 循环可否换 | 写死在产品中心 | `agent-loop` 只是默认实现，swappable |
| 面向 | 替用户写代码 | 装配成任意形态 |

`dsh` 的架构文档把这件事说得很直白：

> There is no privileged core to patch: you extend dsh by mounting a plugin beside the others, and registrations are effects that unwind when the plugin unloads.

这段话拆开是三条设计承诺。第一，扩展的位置在树旁边新挂一个插件，不碰别人的代码；第二，每个注册都是可逆的副作用，插件卸载时它的注册整体回滚；第三，连 agent 循环本身都是插件——`core/agent` 定义 `Agent` 接口和 `ctx.agents` 注册表，`core/agent-loop` 只是实现这个接口的默认驱动。仓库的依赖规则里写着：UI、hook、工具插件一律依赖 `dsh-agent`（接口），不依赖 `dsh-agent-loop`（实现），所以 loop 换掉时其余插件毫发无损。同一层还有挂在 `ctx.llm` 的模型适配器和挂在 `ctx.tools` 的工具注册表——开头那句核心判断点名的四个部件，在源码里各占一个可换的位。

这个选择有代价。终端 Agent 的循环固定，换来的是职责单一、好维护；`dsh` 要让任意产品拿它当引擎，不同产品要不同循环、不同工具集、不同沙箱策略，循环一旦写死，通用性就没了。把"循环"降级成一个可替换的实现，是这个目标下的必然后果。

## 二、仓库组织：三百个包与三条规律

`dsh` 是一个规模可观的 pnpm monorepo。几个实测数字：`packages/` 下有 **307 个** `@deepseek-ai` 域内的包，按 `packages/<组>/<包>` 两级分组，共 50 多个组；`packages/` 内 TypeScript 约 **75 万行**（含测试；剔除测试目录约 34 万行）；加上 `apps/`、`scripts/`、`python/` 等目录，全仓 `.ts` 约 91 万行。根 `pnpm-workspace.yaml` 把它们全部编进一个 workspace：

```yaml
# pnpm-workspace.yaml（节选）
packages:
  - vendor/*          # vendored Cordis 等框架包
  - packages/*/*      # 主体：50+ 组 × 每组若干包
  - native/system     # Landlock 启动器（native addon）
  - apps/*            # 产品壳：cli、web、desktop…
  - python/sdk-runtime # Python SDK 的部署根
```

单仓库 + workspace 让"依赖必须跨包显式声明、按包独立发布、依赖图机器可查"成为可能。`packages/README.md` 的分组表是走读地图，记不住每个组没关系，抓三条规律就够：

- **`core/` 是产品 API 脊柱**：`session`（会话日志）、`system-prompt`、`tools`、`agent`（Agent 接口）、`agent-loop`（默认驱动）、`scope`。这些包不讲"某个能力"，只讲产品内部的数据结构与循环。
- **其余组几乎都是能力族**：`shell`、`fs`、`subprocess`、`sandbox`、`compaction`、`subagent`、`web`、`skill`……每个能力 = 接口声明 + 实现 + 消费方三个角色（第五篇的专项）。
- **`bundle/` 是装配层**：`base`、`web-app`、`headless`、`sdk-app`、`acp-app`、`sdk-minimal`，把上面的包组装成产品。

支撑这套分工的依赖规则很硬："Extension plugins depend on Service Definitions, never concrete providers"——扩展插件只依赖服务定义，绝不依赖具体实现；依赖图由脚本生成、CI 校验。包的边界是约束，不是目录美学。

## 三、装配链：profile、bundle 与 patch

包堆在那里还是包，一个能跑的 `dsh` 是怎么出现的？答案是**按有序层叠装配出一棵插件树**。三个概念：

- **profile（档）**：存在 Harness home（`$DSH_HOME/profiles/<name>`）里的命名组合。它声明装哪些 bundle、留哪些树外插件、放一份自己的 `cordis.patch.yml`。仓库随发行带了五个模板：`web`、`headless`、`sdk`、`sdk-minimal`、`acp`。
- **bundle（包层）**：配置行 + 所挂代码的分发格式。它最重要的性质写在定义里——"whatever it inserts stays patchable by the layers above it"：bundle 插入的每一行配置，都能被更上层的 patch 覆盖。
- **patch（补丁）**：按 `id` 命中某一行、整体替换它的 config，或插入新行。

每个 bundle 在自己 `package.json` 的 `dsh` 字段里自我声明：

```jsonc
// packages/bundle/base/package.json（节选）
"dsh": {
  "bundle": {
    "patch": "./cordis.patch.yml"
  }
}
```

层叠顺序是固定的：**空配置起点 → 按 profile 列出的顺序逐层应用 bundle → profile 自己的 `cordis.patch.yml` → home 级 `cordis.patch.yml` → 命令行 `--patch` 覆盖**。`dsh-base` 是所有主 profile 的第一层，装模型适配器、工具、持久化、沙箱与审批策略、设置、凭据、遥测；`dsh-web-app` 在其上加浏览器应用，`dsh-headless` 加一次性 runner，`dsh-sdk-app` 加 JSON-RPC 服务，`dsh-acp-app` 加 ACP 自动化服务。

想知道你这台机器实际会启动哪棵树，有一个官方命令：

```bash
dsh --profile web --dump-config
```

它不启动、不请求模型，只把叠好的配置树打印出来，打印的每一行都能被你自己的 patch 替换。装配还带热重载：`base` 默认启用 config-only 的 `dsh-hmr`，改 patch 文件即重载；`headless`、SDK、ACP 模板关掉它，一次性任务不需要 watcher。

## 四、一棵树长出一排产品形态

装配链最有说服力的部分是"减法"。`dsh-headless` 的 README 定位是"runs one dsh task from the command line and prints the final answer, then exits"——不装 host、不装 HTTP 服务、不装浏览器，同一个 `dsh-base` 上少装一个面，就变成 CI 里跑一次性任务的命令行工具，`--json` 输出事件流，`--session-id` 续会话。五个发行模板加上保留的 desktop 档，全部从同一棵树上装配出来：

![同一棵插件树装配出的产品形态](/images/deepseek/01-plugin-tree.svg)

| 形态 | 装配 | 用途 |
|------|------|------|
| `web` | base + web-app | 浏览器图形界面 |
| `headless` | base + headless | 脚本/CI 一次性任务 |
| `sdk` | base + sdk-app | TypeScript/Python SDK 的 JSON-RPC 服务 |
| `acp` | base + acp-app | 自动化协议（ACP）服务 |
| `desktop` | base + web-app + Electron 壳 | 桌面应用（保留 profile，CLI 不可管理） |
| `sdk-minimal` | 无 base，单一 bundle 独占显式树 | SDK 最小化装配 |

`sdk-minimal` 是故意的例外：它不套 `dsh-base`，由一个 bundle 独占完整的显式 SDK 树——仓库需要证明"不用基础层也能从零组一棵干净的树"。Python SDK 则展示了复用的深度：它的运行时 wheel 直接把 `dsh` CLI 打包成 `deepseek-harness-sdk-runtime-<platform>-<arch>`，客户端启动的还是 `dsh --profile sdk`，Python 侧只暴露 profile 选择和有序 patch 文件。

仓库还配了一个守门脚本 `verify-application-entrypoints`：所有受支持的 Node 应用必须走 `dsh` profile 启动，包的 bin、demo、根脚本全部归入显式分类，任何绕过 `dsh` 的 Node 应用路径都会被拒绝。"装配即产品"在这里不是口号，是被 CI 强制的纪律。

## 五、与三栏对比与阅读路线

把全篇收进一张对照表：

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness |
|------|------|------|
| 定位 | 终端编程 Agent | 通用 agent harness |
| 核心抽象 | 主循环（runLoop / reactor / QueryEngine） | Cordis 插件树 |
| 底层 | 原生 TS / Rust / Bun | vendored Cordis 组合框架 |
| 规模 | 单包 5 万～51 万行 | 307 包 / packages 内约 75 万行 |
| 产品化路径 | 一个产品一个形态 | profile/bundle 装配出五种形态 |

前三栏把"agent 怎么干活"写进产品中心；`dsh` 把这个问题外包给装配者，自己只保证每个部件可换、每次注册可回滚、每棵树可审计。两种路线没有高下，一个面向开箱即用，一个面向被任意装配。

沿着这棵树，后面五篇的路线是：

1. **Cordis 组合框架**——插件靠什么原语共存：Context、effect、waterfall、Service 注入、scope、Loader。
2. **agent-loop**——默认驱动怎么跑一个回合：turn/step 事件流、单一 inbox、waterfall 与 serial 的分工。
3. **会话日志**——模型上下文从哪来：追加式事件日志、`deriveMessages()` 投影、"model-visible ⟺ logged" 不变量。
4. **capability 缝**——为什么换一个实现能牵一发动全身：三件套、执行世界、provider 整套迁移。
5. **扩展的三个极致**——自改 toolset、CC/Codex hooks 翻译桥、跨产品 subagent 委托。

一句话收束：**`dsh` 不是一个 agent loop，而是一套让任意 agent loop 都能被拼出来的组合框架。**

## 源码索引

- `docs/architecture.md` — Cordis / Profiles and bundles / Application launch / Core packages
- `packages/README.md` — 分组表与依赖规则
- `pnpm-workspace.yaml` — workspace 编制
- `packages/bundle/base/package.json` — `dsh.bundle.patch` 自我声明
- `packages/boot/app-boot/README.md` — profile 层叠顺序、HMR、启动失败策略
- `packages/bundle/headless/README.md` — 一次性任务模式
- `packages/bundle/sdk-minimal/` — 无 base 的独立树例外
- `scripts/verify-application-entrypoints.ts` — 启动路径守门

## 章节小测

<script setup>
const q = [
  {
    question: '下列哪一项最符合 `dsh` 的架构定位？',
    options: ['一切皆插件，循环本身也只是默认实现', '中央主循环固定，其余模块围绕它注册', '循环写死在核心，扩展必须改代码', '与 OpenCode 相同，runLoop 位于中心'],
    correct: 0,
    explanation: 'dsh 的 everything is a plugin 加无特权核心：agent-loop 只是默认实现且 swappable。B/C 是前三栏"循环写死"的模式，D 把 OpenCode 的结构与 dsh 混为一谈。'
  },
  {
    question: 'profile 装配的层叠顺序，正确的是？',
    options: ['home patch → bundle 层序 → profile patch → 覆盖', '按包名字典序应用，与声明顺序无关', 'bundle 层序 → profile patch → home patch → --patch', '--patch → profile patch → bundle 层序 → home patch'],
    correct: 2,
    explanation: '每个 profile 从空配置起步，先按列出顺序叠 bundle，再叠 profile 自己的 cordis.patch.yml，然后是 home 级文件，命令行 --patch 权限最高。A 把 home 级提前了，D 整个倒置，B 的字典序不存在——层序由 profile 声明决定。'
  },
  {
    question: '`dsh --profile web --dump-config` 的用途是？',
    options: ['打印这台机器实际会启动的配置树', '导出 Web 前端的构建产物', '新建一个叫 dump-config 的 profile', '从当前 profile 卸载指定插件'],
    correct: 0,
    explanation: 'dump-config 不启动、不请求模型，只打印叠好的整棵插件树，且每一行都能被自己的 patch 替换。B 把配置树当成了构建产物，C 混淆了它和 profile 创建命令，D 把审查能力当成了管理操作。'
  },
  {
    question: '`sdk-minimal` 这个 bundle 的特殊之处在于？',
    options: ['它是 base 的精简版，只装半个 base', '它不用 dsh-base，单一 bundle 独占完整显式树', '它只能被 Python SDK 使用，其他入口禁止', '它把 agent-loop 替换成了专用循环'],
    correct: 1,
    explanation: '架构文档称它为 deliberate exception：不套 dsh-base，由一个 bundle 持有完整的显式 SDK 树，用来证明从零组一棵干净的树是可行的。A 的"精简 base"说反了它的独立路线，C 没有这个入口限制，D 与循环无关。'
  },
  {
    question: '"Extension plugins depend on Service Definitions, never concrete providers" 这条规则直接保证了什么？',
    options: ['扩展插件的加载顺序由依赖图自动推导', '所有插件必须发布到 npm 公共仓库', 'Service Provider 之间禁止互相调用', '换 Provider 实现时扩展插件不用改代码'],
    correct: 3,
    explanation: '插件只依赖接口包（如 dsh-agent），不 import 具体 provider（如 dsh-agent-loop），所以替换实现时消费方不动。A 说的是 inject 依赖声明的另一件事，B/C 都不是这条规则的内容。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
