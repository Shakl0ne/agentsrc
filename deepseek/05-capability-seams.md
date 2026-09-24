---
title: capability 缝与执行世界：换一个实现，动整套产品
---

# capability 缝与执行世界：换一个实现，动整套产品

上一篇结尾留了一个问题：为什么换一个 provider 能牵一发动全身？这一篇正面拆它。`dsh` 把每个能力——执行 bash、读写文件、起子进程、沙箱隔离——都做成一条 **capability 缝**，缝的机制一句话能说完，但它解释的事横跨整个仓库。读完你会带着三个问题的答案离开：

- 一个能力为什么要拆成三个角色、三个包，接口单独成包图什么？
- 为什么说 fs 与 subprocess 共享一个"执行世界"，换一个 provider 会带着 bash、PTY、LSP 整套搬家？
- 一次工具调用从模型发起到落盘要过多少道裁决，为什么参数不可改而结果可改？

压缩那条缝上一篇已经见过，这篇把它当成熟人引用；工具执行管线的完整机制在这里讲透。

## 一、三件套：一个能力的最小完整拼图

先看定义，架构文档一句话说完：

> A **seam** is a swappable capability with three roles: a **Service Definition** declaring the interface, a **Service Provider** implementing it, and a **Consumer** using it, commonly a model-facing tool. A package may combine roles, but one role alone is not a seam; adding a capability means designing all three.

三个角色对应三种演化方向。**Service Definition** 声明接口：这个能力能做什么、返回什么、暴露哪些事件，它不依赖任何实现。**Service Provider** 给出具体实现，同一时刻同层只有一个挂载。**Consumer** 消费能力，通常是一个模型可见的工具。缺任何一个角色都不构成缝——只有一个接口包没有实现，等于纸上谈兵；工具直接 import 实现包，换实现就得改工具。

![capability 缝的三件套](/images/deepseek/05-seam-triple.svg)

接口为什么必须独立成包？因为 Consumer 只对着接口编程：`dsh-tool-bash` 调的是 `ctx.shell.run(...)` 这个接口方法，`ctx.shell` 下面挂的是本地执行器还是沙箱执行器，它不知道也不需要知道。实现换掉，Consumer 与整棵树上的其他插件都不动。反过来，接口包里只要 import 了一个实现，这条缝就焊死了。

`dsh` 的依赖规则在第一篇出现过：扩展插件只依赖 Service Definition，绝不依赖具体 provider。角色完整性还是机器可查的——`docs/capability-seams.md` 这张生成的图谱把全仓每个 `ctx.*` 服务按三种角色归类，谁少做了一个角色，组合时 fail loud。

## 二、bash 缝：接口、实现与能力事实

用最典型的 bash 缝把三件套落地。`dsh-shell` 定义 `ShellExecutor`：前台 `run` 一条命令读有界输出、`start` 一个后台进程轮询结果，外加一个 `resolve()` 和一个 `sandboxMode` 能力事实。Provider 是四个执行器——`bash-local`（POSIX 本地子进程）、`bash-sandbox`（每条命令过沙箱）、`pwsh-local` / `pwsh-sandbox`（Windows 的 PowerShell 版）；Consumer 是 `tool-bash`、`tool-pwsh` 这组模型工具。

两条设计值得放大。

**其一，request 与 spec 的分离。** 模型和插件传进来的 `ShellExecRequest` 里，`workdir`、`timeoutMs`、`stdoutMaxBytes` 全是可选的；执行器真正执行的 `ShellExecSpec` 里这些字段全是必填。中间隔着一个显式的 `ctx.shell.resolve(request)`：实现自己的配置与请求策略在这里填默认值、封上限。

仓库的规则原文是 "explicit > implicit at package boundaries"：默认值是 owning 实现里一个显式的解析步骤，绝不藏在 `run()` 内部的 `??` 兜底里。`dsh-shell` 的这个拆法被仓库指定为模板，其他缝照此办理。

**其二，sandboxMode 作为能力事实。** 沙箱型执行器在 `ShellExecutor.sandboxMode` 上暴露自己的模式回退值，工具层读它：有沙箱执行器在位，`bash` 工具就多一个"请求沙箱"的参数；纯本地执行器在位，这个参数自动消失。Consumer 按照实现暴露的能力事实**自适应**自己的 schema——不需要工具知道 bwrap 还是 Landlock。这正是三件套里"三个角色各自演化"的意思：Provider 换了，Consumer 的模型面跟着长出或收起一个开关。

同层互斥同样硬：一个组合只挂一个执行器，挂两个会在加载期因为重复的服务注册 fail loud。"换 provider"因此是原子操作，不存在半套本地半套远程的中间态。

## 三、sandbox 缝：同世界的文件效果约束

沙箱缝的接口极小：`ctx.sandbox.confine(argv, policy, signal)` 把一条同世界子进程 argv 包进文件效果策略。策略词汇只有三档：

```ts
// docs/subsystems/sandbox.md（节选）
type SandboxMode = 'read-only' | 'workspace-write' | 'danger-full-access'
type SandboxEnforcement = 'full' | 'partial'
```

三个词汇的边界都画得很死。`SandboxMode` 只管文件效果：`read-only` 拒绝写入（POSIX 后端额外放行 shell 需要的 `/dev/null`），`workspace-write` 允许工作区根与后端承诺的临时区，`danger-full-access` 彻底旁路——走这个模式的调用根本不会调 `ctx.sandbox`，直接 spawn 原 argv。网络与进程可见性不在这套词汇里。

`SandboxEnforcement` 是后端报告的事实而非承诺：`full` 表示后端管住模式承诺的全部文件效果，`partial` 表示旧内核 ABI 或平台边界只覆盖一部分——要求绝对保证的消费方必须自己拒绝或明示这个差异。

失败方向同样是关死的。请求的模式无法强制时，调用以 `SANDBOX_UNAVAILABLE` 失败，绝不无沙箱裸跑——"Silent unconfined passthrough is never legal" 写进了服务契约。被拒之后，模型可以为这次调用请求放宽一档，交人工审批。

后端按平台各就各位：Linux 上 bwrap 与 Landlock，macOS 上 Seatbelt，Windows 上 ACL 受限令牌。这些后端全部是**同世界**的——沙箱进程仍与宿主共享内核与文件系统。README 把边界说得很清楚：需要隔离整个环境时，答案不是更强的沙箱，而是容器、微 VM 或远程执行器。最后这半句是下一节的入口。

## 四、执行世界：换 provider = 整套搬家

现在回答标题里的"动整套产品"。架构文档的关键句：

> Seams are why one provider swap changes the whole product. Filesystem and subprocess providers share one execution world, so pointing them at a remote sandbox moves Bash, PTY, and LSP with them, with no provider forks.

fs 缝与 subprocess 缝共享同一个**执行世界**：一个进程能打开哪个路径，取决于它所在世界的文件系统；一条命令在哪里执行，取决于那个世界的子进程设施。这两件事在本地实现里是一体的——`fs-local` 读真实文件，`subprocess-local` 起真实进程，共享宿主这个"世界"。

所以把执行世界整体指向远程时，动的不是某一个工具。看 ssh 组的装配：`fs-ssh` 注册 `ctx.fs`（远程文件身份、读取、原子变更守卫）、`subprocess-ssh` 注册 `ctx.subprocess`（可执行查找、进程、控制流与终端）、`sandbox-ssh` 注册 `ctx.sandbox`（远程文件效果约束）——三件套一起换，挂在 `ctx.shell` 上的 bash、PTY，还有靠 `ctx.subprocess` spawn 的 LSP，全部跟着迁到远程，没有任何一条 provider 分叉。

bash、PTY、LSP、各子 agent 后端都通过 `ctx.subprocess` 这个单一缝去 spawn，这正是"牵一发动全身"的正面写法：一换全换、干干净净。

fs 组内部的分层也体现了缝的纵深：`fs` 是接口（执行世界路径、有界文本 IO、带版本守卫的原子变更），`fs-local` / `fs-sandbox` 是两个 provider（后者按每次调用的沙箱模式给写操作上栅栏、读直接放行），`fs-observation-policy` 是挂在 `fs/*` 事件上的策略（编辑前必须读过、记录观察到的是否存在），`tool-fs` / `tool-fs-search` 是模型面的读写与检索工具。五层各管一段，替换 `fs-local` 时策略与工具两层原地不动。

![本地与 SSH 两个执行世界的整套迁移](/images/deepseek/05-execution-world.svg)

## 五、工具裁决链：Consumer 的自由与约束

工具是缝最常见的 Consumer，也是裁决链最重的一层。`ctx.tools` 注册表里一个 `ToolDefinition` = 模型可见的 schema + **强制的 canonical output 声明** + `execute` 函数 + 调度元数据 + 可选的展示回调。注册表给模型的投影走显式白名单：`output`、`execute`、`finalizeContent`、`timeoutMs`、`isConcurrencySafe`、`presentCall`、`presentResult` 这些字段**永不进模型请求**——模型看到的只有 `name`、`description`、`parameters`。

自家插件用 `defineTool()` 拿类型安全：参数先验证再进 body，返回值按 `output.schema` 推断。

一次调用从模型发出到落盘，完整过这条链：

1. **`tool/call` 先落日志**——调用还没执行，事实已经记录；
2. **`tools/pre-execute` 瀑布**（allow / deny / ask / cancel）：hooks、权限、沙箱策略都在这层挂；
3. **`ctx.approval` 一次性审批**：`ask` 在这里等人工裁决，批一次（allowed-once）放行；审批服务缺席或无法应答，一律按 deny 收场；
4. **单调守卫**：注册进注册表的最终策略，返回类型**刻意没有 allow 分支**——`undefined` 维持瀑布原判，返回理由只会收紧；
5. **`tools/execute` 瀑布**（around-dispatch）：超时、重试、指标这类包装逻辑挂在这里，且只有这一层允许替换取消信号；
6. **工具 body** 执行，写操作再过 `fs/write-intent` 这类能力自己的闸；
7. **`projectContent`**：定义自带的执行内容准备，把执行中产出的文本与图片装进结果，赶在 post-execute 之前；
8. **`tools/post-execute` 瀑布**：接受、block、替换结果、附加上下文；
9. **`finalizeContent`**：工具自己的最后一道 content-only 修正，随后 `tools/result` 以冻结的最终事实通知观察者。

![工具调用的裁决链](/images/deepseek/05-tool-pipeline.svg)

单调守卫的设计值得单独看，`tools.md` 的原话：

> Its return type deliberately has no allow result: `undefined` preserves the waterfall decision, while a returned reason can only reduce permission, so a later listener cannot undo it.

裁决在这条链上是**只紧不松**的：pre-execute 的 allow 只代表"这一层放行"，后面的守卫随时可以否掉；而任何一层给出的 deny，后面的监听者都翻不了案。审批缺席即拒绝、守卫无 allow 分支、静默无沙箱裸跑不合法——三件事是同一个失败哲学在三个位置的样子。

链的两端自由度刻意不对称。**进闸（参数）不可改**：pre-execute 不允许改写调用参数，因为参数已经 logged 且展示过，改了会让历史、审计、UI、执行彼此脱钩；**出闸（结果）可改**：post-execute 可以替换 content 或 value——这是策略层对结果的正当修饰，且整条链的每个异常最终都规范成带 `isError` 的正常结果。保真进、灵活出，历史一致性与策略空间各得其所。

## 六、与三栏对比

| 维度 | OpenCode / Codex / Claude Code | DeepSeek Harness |
|------|------|------|
| 工具与沙箱 | 产品内建，硬编码在循环周围 | 缝：三件套各占一个包 |
| 换沙箱/执行后端 | 改代码 | 换一个 Provider |
| bash+文件+LSP 整体迁移 | 满仓库改 | 换共享执行世界的一组 provider |
| 工具权限 | 产品的权限模块 | pre-execute/审批/单调守卫，全挂插件 |

三个终端 Agent 把工具和沙箱当作产品中心的能力写进代码；`dsh` 把它们做成可插换的缝，再把共享执行世界的几条缝绑成一组，让"换环境"从满仓库打补丁变成一次整套迁移。加上工具裁决链把"给模型一个工具"从一次性决定拆成每一步都可裁决的管线，这一篇和前四篇拼起来正好是 `dsh` 的能力面全景。

最后一篇看它把"可换"推到的三个极致：agent 改自己、翻译别家 hooks、跨产品委托。

## 源码索引

- `docs/architecture.md` — Capability seams 定义与执行世界
- `packages/shell/README.md` — bash 缝的包族与互斥装配
- `docs/subsystems/shell.md` — request/spec 分离、sandboxMode 能力事实
- `packages/sandbox/sandbox/README.md` + `docs/subsystems/sandbox.md` — 沙箱模式、enforcement、fail-closed
- `packages/fs/README.md` — fs 缝五层包族
- `packages/ssh/README.md` — 远程执行世界三件套
- `docs/subsystems/tools.md` — 注册表契约、单调守卫、执行管线
- `docs/tool-execution-pipeline.md` — 裁决链全图
- `docs/capability-seams.md` — 机器生成的角色图谱

## 章节小测

<script setup>
const q = [
  {
    question: '按 `dsh` 的定义，一个能力最少要设计哪三种角色？',
    options: ['接口、实现、消费方', '接口、实现、配置', '工具、沙箱、审批', '接口、事件、日志'],
    correct: 0,
    explanation: 'capability 缝 = Service Definition + Provider + Consumer，缺一不算 seam。B 用配置顶替消费方，C 是具体能力名，D 是机制要素不是角色划分。'
  },
  {
    question: '接口包（Service Definition）为什么不能 import 任何实现包？',
    options: ['为了减少包的安装体积', '因为实现包不许被别人引用', '换实现时消费方也得跟着改', '为了让编译速度更快'],
    correct: 2,
    explanation: '接口一旦绑死实现，Consumer 就跟着焊死；接口独立成包，Provider 随便换、消费方与整棵树不动。A/B/D 都不是这条依赖规则的理由。'
  },
  {
    question: '单调守卫（ToolGuard）的返回类型刻意没有 allow 分支，目的是？',
    options: ['让守卫更短更容易写', '后续监听者无法把拒绝翻回放行', '减少一次类型判断的开销', '让审批流程接管允许权'],
    correct: 1,
    explanation: '返回 undefined 维持原判、返回理由只会收紧：裁决只紧不松，防止监听者之间互相翻案。A/C 与设计意图无关，D 说的是 approval 的位置而非守卫。'
  },
  {
    question: '一次工具调用带 `danger-full-access` 模式，实际会发生什么？',
    options: ['沙箱以最宽规则包住进程', '调用被拒绝并要求审批', '沙箱后端切换到全量模式', '绕过沙箱，直接执行原命令'],
    correct: 3,
    explanation: 'danger-full-access 是旁路：消费方直接 spawn 原 argv，根本不调 ctx.sandbox。A/C 把旁路误读成最宽围栏，B 混淆了"请求放宽一档"的人工审批路径。'
  },
  {
    question: '"换一个 provider 会带着 bash、PTY、LSP 整套搬家"的根本原因是？',
    options: ['这些工具写在同一个包里', '配置文件强制绑定它们的行', '它们共享同一个执行世界', '它们的接口签名完全相同'],
    correct: 2,
    explanation: 'fs 与 subprocess 共享一个执行世界，bash/PTY/LSP 都经 ctx.subprocess 这条缝去 spawn；把世界指向远程，ssh 三件套（fs-ssh/subprocess-ssh/sandbox-ssh）一起换，所有消费者自动迁移。A/C/D 都不是机制本身。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
