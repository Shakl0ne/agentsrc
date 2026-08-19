---
title: Cordis 组合框架：几十个 package 靠什么拼成一个 agent
---

# Cordis 组合框架：几十个 package 靠什么拼成一个 agent

> 本文基于 `dsh-v0.1.0-rc.7`。项目处于 developer preview，迭代很快，文中机制以该基线为准。

连载第一篇讲清了 `dsh` 的全景：几十个 package，没有一个不可变的中央循环，全靠一台可组合的插件树。但紧接着就冒出一个更硬的问题——**这几十个互相独立的 package，到底靠什么在运行期拼成一个 agent？** 谁负责把它们装进一棵树？谁决定先后？谁提供"每个插件共有的底座"？

答案是一个被 `dsh` **整个 vendor 进仓库**的第三方组合框架：**Cordis**。`dsh` 连做"组合"这件事都不自造轮子，而是把 Cordis 及其基础库源码 `copy` 进仓库、改名为 `@deepseek-ai/cordis`，让它成为每个 harness package 的 peer dependency。

这一篇我们就正进 Cordis 这个地基。它是整个 `dsh` 系列的门面：不读懂 `ctx`、`effect`、`waterfall`、Service、scope 这五件事，后面讲 agent 树、会话日志、工具管线、capability 缝全都无从谈起。

先把这一篇最核心的一句话放在前面：

> **Cordis 是"作用域 + 事件 + 可逆效果"的插件化组合框架。每个东西都是插件，插件在共享作用域（Context）上通过类型化事件通信，所有注册都是可回退的副作用。**


## 一、Context：所有插件共有的"共享作用域"

组合框架要解决的第一问题是：几十个插件**共享什么**来做互相协作。Cordis 给了一个统一的模型——**Context（上下文）**。

用官方手册（`docs/cordis-primer.md`）里的说法，一个 Context 是**服务的仓库**（a repository of services）：

> A context is a repository of services. A service claims a stable `ctx.<key>` such as `ctx.tools`, `ctx.llm`, or `ctx.sessions` from a context; other plugins find services via key instead of importing a concrete implementation.

把它拆开，有三层意思，分别是"仓库""键""解耦"：

1. **Context 是一个服务仓库**。每个服务在 context 之上占一个稳定的键（key），例如 `ctx.tools`（工具）、`ctx.llm`（模型）、`ctx.sessions`（会话）。下面的插件想用某个能力，通过 `ctx` 上的键去取，而**不是 import 一个具体实现文件**。
2. **"经键解耦"是最关键的设计**。插件 A 与插件 B 不 import 彼此，它们只依赖"某个键下有服务"这个约定。这就让"换一个实现"成了"换一个注册到该键的插件"这么自然的一件事——正是第一篇强调的"一切皆插件、无特权核心"落地的地方。
3. **这个共享作用域是有"派生"能力的**（后面第五节会专门展开 scope）。Context 继承链上可以 `extend` 出子 context，子 context 原型继承父 context 的属性，用来做"某个子树的私有注入"。

`vendor/cordis/src/context.ts` 里的 `extend()`，就是这个"原型继承 + 元数据遮蔽"的机制：

```ts
// vendor/cordis/src/context.ts
extend(meta = {}): this {
  const shadow = Reflect.getOwnPropertyDescriptor(this, symbols.shadow)?.value
  const self = Object.create(getTraceable(this, this))
  for (const prop of Reflect.ownKeys(meta)) {
    Object.defineProperty(self, prop, Reflect.getOwnPropertyDescriptor(meta, prop)!)
  }
  if (!shadow) return self
  return Object.assign(Object.create(self), { [symbols.shadow]: shadow })
}
```

`Object.create(parent)` 让子 context 原型继承父 context（源码用 `getTraceable(this, this)` 保证追踪），因此子 context **继承了父 context 的每一个属性**；而 `meta` 里的自有属性可以遮蔽继承的属性。这是 Cordis 做"作用域隔离"的底层拼图：同一个 context 上，可以派生出"只管某个 agent/某类对象"的子 context。

一句话记住这个抽象层：

> **Context 把"插件之间共享什么"抽象成一个可继承、可遮蔽的仓库；插件的协作不靠 import 图，而靠对同一组键的读写。**

### 补充：Context 也是生命周期容器

`extend`、`isolate`、`intercept` 都返回子 context，但真正的"树"怎么被组织、插件怎么被种进去，靠的是 **Fiber**（纤程）。我们先把 fiber 放一放，因为第一次读 Cordis，最该先理解的是"注册即副作用"这一点。Context 是"静态的仓库"，Fiber 是"动态的生命周期"——插件的加载、热重载、卸载都由一条 Fiber 承载。


## 二、注册即副作用：`ctx.effect()` 与 `ctx.on()`

"无特权核心 + 一切可回退"这句口号，靠什么落成一个可执行机制？答案在 Cordis 的一条核心约定里，`docs/architecture.md` 原话是这么说的：

> Registrations are effects that **unwind** when their plugin unloads.

即：**注册是一个副作用，插件被卸载时会反向撤销**。具体到 `ctx` 上就是两个最常用的 API：

- `ctx.on(name, listener)`：注册一个事件监听。返回一个 disposer，调用它就能把这个监听卸掉。
- `ctx.effect()`：注册一段一次性效果，并把它绑定到当前 fiber 上——Fiber 卸载时自动逆向执行。

`events.ts` 里 `on()` 的声明，注意它的返回类型**直接就是一个 disposer**（`=> boolean`）：

```ts
// vendor/cordis/src/events.ts
on<K extends keyof Events>(
  name: K,
  listener: Events[K],
  options?: boolean | EventOptions,
): () => boolean
```

这里的设计意图有三层：

1. **每个注册都有对应的逆操作（disposer）**。"注册" 不是一个再也收不回来的全局状态，而是一个可撤销的对象。`on` 返回的 `() => boolean` 就是撤销凭证。
2. **Fiber 自动帮你回收。** 因为 `effect` 绑定了 owner fiber，热重载或卸载时，这个 fiber 里注册过的所有东西都被顺序摘除，不会遗留下来。
3. **注册即效果、可逆卸载**。`dsh` 的很多扩展点（prompt 段、工具 schema、适配器、provider）都是通过 `effect`/`on` 装进去的，卸载插件就整体回滚。这保证"扩展可以加也可以撤"，而不用改核心循环。

到这里，"无特权核心"就有了第一个可操作的含义：**插件的扩展位置不是"改循环"，而是"新挂一个插件"，卸载时它的副作用整体回滚**。

一句话记住：

> **`ctx.on` / `ctx.effect` 是一对"注册 + 回收"，它们的返回值就是可逆卸载的凭证。管理热插拔自洽的，不是"谁的代码写得干净"，而是"Cordis 把每个注册都绑到 Fiber 生命周期上"。**


## 三、事件分两大类：观察（emit）与委托（waterfall）

插件之间通信的主要途径是**类型化事件（Typed Events）**。Cordis 把事件分成两族，对应两种不同的协作意图：

- **观察类**：`emit`（同步广播）、`parallel`（并行 await）、`serial`（串行定案，先注册者先回调、遇到 `bail` 即停）。这些只是"看到一件事发生了"，不负责把一件事"做出来"；`bail` 是"谁先返回定案值即停"的单决策原语。
- **委托类**：`waterfall`——**最重要的一个**，是 Cordis 做"中间件链/委托"的方式。

`events.ts` 顶部把 dispatch 模式列得很清楚：

```ts
// vendor/cordis/src/events.ts
export type DispatchMode = 'emit' | 'parallel' | 'serial' | 'bail' | 'waterfall'
```

而 `serial` 的定义说明它是"顺序执行、直到有一个返回中断值（非 null/false/undefined）就停下"：

```ts
serial<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): Promisify<ReturnType<Events[K]>>
```

### waterfall：串行委托，"谁都要先接续 next() 才叫委托"

`ctx.waterfall` 是 Cordis 为"一道请求在被多个插件按顺序包装"设计的中间件原语。`docs/cordis-primer.md` 的 Waterfall Semantics 一节把语义写得很精确：

> A listener receives `(...args, next)`. Call `next()` to delegate the possibly wrapped result to the next service; return without `next()` to short-circuit.

翻译过来就是：

- 水瀑的每个 listener 拿到 `(...args, next)`，即"请求参数"+"下一个 continuation"。
- 调 `next()` = 让下一个 listener 登场并**把结果传递回来**（你可以包一层再传）。
- 不调 `next()` 直接返回 = **短接/截断**这条链。

这跟后端常见的**中间件模型**本质上是一回事，但和"emit（随便谁看到都行）"是两种不同协作。`dsh` 里大量用 waterfall 做跨插件截断/重写：比如下一篇讲的 `agent/pre-step`（决定模型看到什么）就是一个 waterfall。

为什么 Cordis 强调"waterfall listener 必须调 `next()`"？因为不调就等于"我要独裁这个结果"。看 `agent/turn-stopping` 这种单决策事件的语义：**单决策事件里短接是正设计**（策略监听可以"独立定案"）；**观察者/包装者必须委托**（它要保证链不断）。所以 waterfall 是协作模型，靠 `next` 这份合同表达"你是滤镜还是灯泡"。

结合 `DispatchMode` 表记一句话：

> **`emit/serial/bail` 是"看、排队、定案"；`waterfall` 是"一个个串起来处理一份结果"。dsh 的扩展点大多是 waterfall，正因为它能表达"多个插件层层包装、任一层可截断"。**


## 四、Service 声明与注入：被"键"解耦的可换实现

`ctx` 是仓库、插件靠键来取，那"服务"本身是怎么注册、怎么被别人依赖的？Cordis 的思路很抽象但优雅：

**服务 = 一个实现了 `Service` 类的对象，注册就是把自己挂到某个键上。**

`service.ts` 里的 `abstract class Service`，它注册的机制一句话：

```ts
// vendor/cordis/src/service.ts
constructor(protected ctx: Context, name: string) {
  name ??= this.constructor['provide'] as string
  let self = this
  // …callable 处理、tracker 追踪等略…
  self.ctx = ctx
  self.name = name
  self.ctx.reflect.provide(name, self, this[symbols.check])
  return self
}
```

即：构造一个 `Service` 的实例时，就会向 context 的反射服务（reflect）以它的 `provider` 键注册自己，并获得一个**可逆的注册**（`reflect.provide` 返回 disposer），当服务所在 fiber 卸载时自动撤销。这就是"把服务接进上下文"的最小路径。

插件怎么**申请**一个依赖？用 `inject`。`docs/cordis-primer.md` 明确：

> Declare service dependency via `inject`. A plugin that names required services waits until those services exist, so **load order is expressed through service requirements** rather than manual boot sequencing.

这是全文最漂亮的抽象之一：

- **注入顺序 = 由"需要什么"推导，而不是靠手排装顺序。**
- 一个插件声明依赖时写的是**裸服务名**（如 `inject: ['tools', 'llm']`，不带 `ctx.` 前缀），Cordis 只有等这些服务都"到了"才执行它；谁在前谁在后不重要。
- 这样，几十个插件的装配顺序**不需要一个中央调度器**，而是从每个插件的依赖关系图里推导出来。这又一次印证了第一篇"没有特权核心"的判断：**连"谁先谁后"都不是核心管，而是靠声明。**

所以"service 声明 / `inject` 注入 / 按需等待"这套，把第一节的"靠键解耦"升级成了"靠依赖图自动排顺序"。它们与第一节的 `ctx` 一起，构成"插件插到哪都行、谁先谁无所谓"，这正是 `dsh` 能任意外壳（一次装配成 CLI / Web / headless）的机制基础。


## 五、scope：per-agent 的私有注册（往"隔离"走一步）

前面讲的是"全局共享作用域"。但真正的产品里，**每个 agent 不该看到所有一切**——比如会话 A 与 agent B 的上下文、工具集应当彼此隔离。这一层靠 `dsh-scope` 这个 package 提供，`packages/core/scope/`。

`core/scope` 的 README 开头就把它的作用点明：

> Scoped registration primitive. `createScope(ctx, key)` creates a tagged Cordis context whose backing fiber owns every registration made through it.

翻译过来：

- **`createScope(ctx, key)`**：在当前 context 之上"铸造"一个带标签的 Cordis context。任何通过这个 scope 注册的东西，都由**这个 scope 对应的 fiber 来拥有**——即它的生命周期归属于这个 scope。
- **`scopeOf(ctx)`**：读某个 context 带没带 scope 标签。
- **`bindScopeParent(key, parent)`**：把 scope 组成父链，形成"子可见父、父不可见子"的可见性规则。

这行的关键是**作用域与可见性两件事绑在一起**：一个注册"在哪个 scope 里有效（visible）"，就必须"由那个 scope 的 fiber 持有"（ownership）。`scope` 的 README 里有一句严格设计契约：

> The registration context determines both visibility and ownership, preventing a registration from being visible in one scope but disposed with another.

这补上了 Context 模型的一个洞：光有"共享 context"还不够，产品里还需要**按 agent/session 的隔离**；`scope` 就是在"共享"之上加一层"按桶隔离"的注册原语。`agent-loop` 会为每个存活的 agent 创建一个 scope（引用下一篇）。

不过要注意 scope 的边界：`core/scope` 的 README 里写明 "Scopes route trusted same-process plugins; they are not sandboxes or authority boundaries"——它的定位是 **"同进程信任的隔离路由"**，不是安全沙箱。权限/安全隔离会由"capability 缝 + 权限 policy"在更高的层级处理（这是第 6 篇的主题）。这里先把概念记住：scope 是"每 agent 私有注册"的原语，属于组织产品层，而不是安全边界。


## 六、Loader 与 patch：cordis.yml 怎么把声明变成一棵活树

前三节把"context/effect/waterfall/service/scope"这些**运行时原语**讲了。最后一个问题必须回答：**这些对象不是写死在代码里，而是通过 `cordis.yml` 配置声明，然后靠 Loader 装载起来**。这就轮到 Cordis 的 config + loader。

`dsh` 的装配过程（见 `app-boot/README.md`）大致是这样：

- 每个 bundle 有 `cordis.patch.yml`（见第 1 篇），内含一行行的 entry（`{ id, name, inject, config, disabled }`）。
- Loader（`cordis-plugin-loader`）去装这些 entry：找到对应的插件包、解析依赖、mount 进 tree。
- **patch 叠加**：不同层的 patch（bundle → profile → home → `--patch`）通过 Include 的 patch 算法（`applyEntryPatches`）叠到最终树，后加的 patch 通过 `id` 命中并**整体替换**某个 entry 的 `config`。

`boot/app-boot` 的 `boot()` 是那个"把一切跑起来"的入口：建 root context → 装 Loader → 在 config 树 entry 真正 mount 前跑可选 host 准备 → mount include tree → 校验 entry 已 loaded/activated → 返回 root context。一旦失败就 dispose 部分 context 并 reject。

`cordis.yml` 里的配置还支持 `!!js` 表达式（配置里可插入 JS 表达式）。`docs/cordis-primer.md` 的 Loader Configuration 说明了它的处理时机：entry 的 `config` 在声明注入激活之后、对应该插件自己的 context 求值；`disabled` 字段则**每次做 mount 决策时**都对 loader context 求值。其他 entry 元信息保持字面值。这给了装配"声明式 + 可补丁 + 可条件化激活（按平台/env 开关某 row）"的组合能力——比如 `dsh-base` 里用 `disabled: !!js process.platform === 'win32'` 决定在 Windows 上关掉 bash 那一套 shell。

记住这一层的抽象切线：

> **运行时原语（Context）管"插件怎么协作"；Loader + patch 管"哪些插件被装、按什么顺序、每个 entry 被谁覆盖"。**前者回答"机械怎么动"，后者回答"工厂怎么组装"，两者加起来才是"一棵 plugin tree"。


## 七、OpenCode/Codex 对比：第三方框架即抽象层

读到这是不是已经很惊讶——Cordis 它既是"依赖注入框架"又是"事件系统"又是"生命周期容器"？这正是 `dsh` 与三栏最不同的一点：**它对"引擎底座"的选择是"用一个第三方框架"，而不是"自造一个"**。

对比口径（延续第 1 篇的"同能不同构"）：

| 维度 | OpenCode | Codex | DeepSeek Harness |
|------|---------|-------|------------------|
| 组合机制 | Effect-TS 依赖注入（~40 个 Service） | 硬编码的原生事件 Reactor | **vendored Cordis（Context + events + effect + scope）** |
| 是否有独立"组合架子" | 有（Effect-TS） | 弱（结构内自建） | 显式"插件框架即抽象层" |
| 循环可否被第三方替换 | Effect-TS 是底层，循环仍自写 | 循环自写 | **`agent-loop` 默认，swappable** |
| 装配方式 | 代码里声明 Service | 事件注册表写死 | **cordis.yml/Loader + patch 层** |

最深的一点差异是：OpenCode 用 Effect-TS **只解决"依赖注入"这一件事**；Codex 干脆把组合逻辑写死在事件 Reactor 内部，几乎没有"另一个框架"。而 `dsh` 把**整个"依赖 + 事件 + 生命周期"的框架层也 vendor 进来当抽象层**——这意味着它要扩展，**不必改 `dsh` 内部代码**，只要"挂一个新的 Cordis 插件"就能替换装配里几乎任何部件（包括 loop 本身）。

这告诉我们一个工程取向：当你要做一个"可被任意产品装配的引擎"，把"组合你内部各个模块"的这一层做成**一个可被替换的第三方框架**，会显著降低"迁到一个新产品"的二次成本——你只需要适配 Cordis 的插件化，而不必改 `dsh` 的内部。


## 八、小结：五件事拼出插件树

现在整理一下：读懂 `dsh` 的地基，其实是你理解五个 Cordis 原语：

1. **Context（共享作用域）**——所有插件共有的仓库，靠 `ctx.<key>` 解耦。
2. **注册即副作用（`effect`/`on`）**——每个注册返回可逆卸载凭证，热重载自洽。
3. **waterfall（委托）**——中间件链路，"next=" 表达截断/包装；是拦截与重写的骨架。
4. **Service 声明 + inject 注入**——依赖图自动排序，"谁先谁后"不靠手动排时序。
5. **scope（per-agent 私有注册）**——在"共享"之上做"按 agent/session 隔离"，同进程路由，不是安全边界。

再加上 **Loader + cordis.yml**：运行时原语负责"怎么共存"，装配器负责"怎么组装成一棵可跑的树"。

下一次埋好了伏笔：** agent-loop 就是在这样一棵 Cordis 树上长得"默认驱动"那一段**，而 `agent/pre-step`、`agent/request` 这些 waterfall 事件，正是在实践我们在第三节讲的"拦截/包装"语法。让我们顺着树走进它 —— 下一篇 **03-agent-loop**。


## 章节小测

<script setup>
const q = [
  {
    question: 'Cordis 里 "加载顺序" 主要靠什么 推导出来？',
    options: ['由上往下层的中央调度器手动排定启动顺序', '由每个插件用 `inject` 声明的依赖关系自动推导', '按代码文件的 import 顺序依次加载', '按配置文件中写定的行号顺序固定执行'],
    correct: 1,
    explanation: '需要的是"用服务的依赖声明自动推导加载顺序"；这样不依赖手动排时序，也正是"无特权核心"的落地。其余三项都要中心化排序或写死顺序。'
  },
  {
    question: 'Cordis 的哪种事件模式，配合 `next()` 最像中间件/管道？',
    options: ['emit（同步广播，观察者都看到事件）', 'serial（顺序 await，直到一个 bails）', 'waterfall（配合 next() 逐个委托并传递结果）', 'parallel（并行 await 所有监听）'],
    correct: 2,
    explanation: 'waterfall 给每个 listener 一个 next()，可以阻止或包装结果再传给下一位，这是中间件链的核心；emit/serial/parallel 是观察/定案而非逐层包装。'
  },
  {
    question: '`ctx.effect()` 与 `ctx.on()` 共同体现 Cordis 的哪条关键原则？',
    options: ['注册是全局、唯一的，卸载后仍留在内存', '注册是一次性消费，调用后立即销毁', '注册是可回退的副作用，随 fiber 卸载而自动撤销', '注册只允许注册一次，复用需重载'],
    correct: 2,
    explanation: '`effect`/`on` 的核心是"注册即副作用、可逆卸载"，返回 disposer 且绑定 fiber 生命周期，热重载/卸载时自动回滚。其余都是把注册视为不可逆状态。'
  },
  {
    question: '关于 `dsh-scope` 提供的每 agent 隔离，下面说法正确的是？',
    options: ['它是同进程内的信任路由，并非安全沙箱', '它是不透传的安全边界，可替代权限策略', '它只影响事件，不影响任何注册的生命周期', '它只能在 CLI 模式下生效'],
    correct: 0,
    explanation: '`createScope` 创建带标签作用域，让注册同时拿到可见性与生命周期，但 README 明确"非沙箱/权限边界"，是信任路由。B 说它是安全边界错误，D 说只在 CLI 生效错误，C 则漏了 scope 拥有每个注册的回收。'
  },
  {
    question: '`dsh` 为什么 vendor Cordis 而不自建框架？整篇最能说明取舍的是？',
    options: ['把组合层也交给一个框架，让模块都以插件编写', '为了省掉写 Event 总线的几行代码', '因为 npm 上找不到替代品', '为了完全依赖第三方而不可被替换'],
    correct: 0,
    explanation: 'vendor Cordis 是把"组合/事件/生命周期"这一层整个变成外部框架，使 agent-loop 等都可 swappable；这服务于"通用 harness"目标。其余三项都不是 vendor Cordis 的真正动机。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
