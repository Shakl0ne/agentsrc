---
title: Cordis 组合框架：插件靠什么拼成树
---

# Cordis 组合框架：插件靠什么拼成树

第一篇留下一个问题：307 个互相独立的包，谁也不 import 谁，靠什么在运行期拼成一个能跑的 agent？答案出自一个第三方框架：`dsh` 把叫 Cordis 的组合框架连同它的基础库整个 vendor 进仓库，改名为 `@deepseek-ai/cordis`，让每个 harness 包都声明它为 peer dependency。

这样做的直接后果是：理解 `dsh` 的前提变成了理解 Cordis。这一篇把这台地基机器拆开，读完你会带着三个问题的答案离开：

- 几十个互不 import 的插件，靠什么对象共享服务、互相协作？
- 插件挂上去的每一样东西，凭什么能被干净地卸下来？
- 没有中央调度器，几十个插件的加载顺序由谁决定？

这些原语的用法（拦截请求、注册工具、起子 agent）会在后面四篇反复出现；这一篇只管把地基本身讲透。

## 一、Context：服务仓库与键解耦

Cordis 的第一个原语解决"插件之间共享什么"。它的答案是一句话：**Context 是服务的仓库**。官方 primer 的表述：

> A context is a repository of services. A service claims a stable `ctx.<key>` such as `ctx.tools`, `ctx.llm`, or `ctx.sessions` from a context; other plugins find services via key instead of importing a concrete implementation.

每个服务在 context 上占一个稳定的键，插件 A 要用工具就读 `ctx.tools` 这个键，实现包之间的 import 图上没有这条边。"换一个实现"于是变成"换一个注册到该键的插件"，第一篇的"一切皆插件"在这个机制上才落得了地。

Context 的实现只有一个 146 行的类，本体是三层派生方法：

```ts
// vendor/cordis/src/context.ts:99-125（节选）
extend(meta = {}): this {
  const self = Object.create(getTraceable(this, this))
  for (const prop of Reflect.ownKeys(meta)) {
    Object.defineProperty(self, prop, Reflect.getOwnPropertyDescriptor(meta, prop)!)
  }
  return self // 无 shadow 时直接返回；有 shadow 再包一层，此处略
}
isolate(name: string, label?: symbol) {
  const shadow = Object.create(this[symbols.isolate])
  shadow[name] = label ?? Symbol(name)
  return this.extend({ [symbols.isolate]: shadow })
}
```

`extend()` 用 `Object.create(parent)` 做原型继承：子 context 继承父 context 的每一个属性，`meta` 里的自有属性遮蔽继承值，父 context 不被改动。`isolate()` 在此之上给某个服务名单独开一个作用域标签——在这个子 context 下面读写该服务，解析到的是新标签里的另一套实现，同名的两次 `isolate()` 传同一个 `label` 则加入同一个作用域。第三个派生方法 `intercept()` 走配置路线：给某个服务叠加拦截配置，后代的插件加载时会看到这份配置合并进服务的解析结果，祖先条目先生效。

![Context 的三种派生：原型继承、服务隔离标签、配置拦截](/images/deepseek/02-context-derive.svg)

三个方法共享一个关键性质：**派生不改变父节点**。context 树上的任何一层都可以放心派生子 context，挂自己的服务、开自己的隔离标签，树的其他部分看不见这些改动。整个 Context 类对外是个 Proxy，普通属性读取走服务解析器，所以"读 `ctx.tools`"这个动作本身也过了一次框架的拦截面。

## 二、注册即副作用：一切贡献都可回滚

第二个原语回答"挂上去的东西怎么卸下来"。Cordis 的约定是：**注册是一个副作用，它随注册它的 fiber（纤程）生命周期自动回滚**。挂在 `ctx` 上最常用的两个 API：

- `ctx.on(name, listener)`：注册事件监听，返回一个 disposer，调用即摘除；
- `ctx.effect(() => disposer)`：注册一次性效果，disposer 绑到当前 fiber，fiber 卸载时自动执行。

事件监听的存储机制最能说明这个设计。`events.ts` 里监听器不是被塞进一个全局数组了事，而是走 fiber 的 effect 通道：

```ts
// vendor/cordis/src/events.ts:254-260
register(label: string, hooks: Hook[], callback: any, options: EventOptions): () => void {
  const method = options.prepend ? 'unshift' : 'push'
  return this.ctx.fiber.effect(() => {
    hooks[method]({ ctx: this.ctx, callback, ...options })
    return () => this.unregister(hooks, callback)
  }, label)
}
```

`fiber.effect()` 收到"把监听器推进列表"这个动作，返回的 disposer 被登记为该 fiber 的卸载项。fiber 是 Cordis 的生命周期容器：每个插件加载时获得一个 fiber，它顺序收集这个插件注册过的所有效果；插件卸载时 fiber 进入 `UNLOADING` 状态，把这些卸载项逆序执行干净。给已经销毁的 fiber 注册会直接抛 `CordisError('INACTIVE_EFFECT')`——注册只发生在活着的生命周期里，这是硬约束。

对 `dsh` 来说这条原语是"无特权核心"成立的一半。一个插件往系统里加的每样东西（prompt 段、工具 schema、事件监听、服务实例）都从这两个入口进去，卸载插件就等于回滚它的全部贡献。热重载因此不需要专门的清理协议：换掉插件，旧 fiber 卸载，新 fiber 起来，树回到干净状态。

## 三、五种事件模式：观察与委托分家

插件之间的通信走类型化事件。事件名通过 TypeScript 声明合并扩展，`dsh` 的 `SessionEventMap`、`agent/*` 全用这个机制声明。真正要紧的是派发模式：Cordis 把"事件"拆成了五种模式，对应五种协作意图，primer 有一张权威对照表：

| 模式 | 等待？ | 顺序 | 返回值 |
|---|---|---|---|
| `emit` | 否 | 按注册顺序观察 | 无 |
| `waterfall` | 否 | 按注册顺序包装 | 有 |
| `parallel` | 是 | 所有监听并行 | 无 |
| `serial` | 是 | 按注册顺序，直到一个 bail | 有 |
| `bail` | 否 | 按注册顺序，直到一个 bail | 有 |

前三种是"观察"：`emit` 同步广播、`parallel` 并行等待、`serial` 串行定案。后两种是"委托"：监听者可以决定结果。派发模式是这一层的核心：其中 `waterfall` 是整个 `dsh` 扩展面的骨架，值得看实现——它只有十行：

![五种事件派发模式：观察与委托分家](/images/deepseek/02-event-modes.svg)

```ts
// vendor/cordis/src/events.ts:234-243
waterfall(...args: any[]) {
  const cbs = this.dispatch('waterfall', args)
  const inner = args.pop()
  const next = () => {
    const cb = cbs.shift() ?? inner
    return cb(...args)
  }
  args.push(next)
  return next()
}
```

最后一个参数被当作链条最内端的 `next`（通常就是内置的默认行为）。每个监听者拿到 `(...args, next)`：调用 `next()` 就是把控制权交给下一个监听者、再一层层传回结果；不调 `next()` 直接返回，就否决了链条剩下的所有环节，包括内置行为。这正是中间件模型的语义——你是滤镜还是灯泡，由你调不调 `next()` 决定。

两个细节值得记。其一，监听者按注册顺序**从外到内**执行，先注册的在最外层；`prepend: true` 可以插到最前面。其二，模式是事件公共契约的一部分：`dsh` 要求每个新事件用 `@mode` 标注派发模式，生成的目录据此核对声明与派发点的一致性。单决策事件里短接是正设计（策略监听者拥有决定权），包装类事件里监听者必须委托（保证链条不断）——后面 agent-loop 那篇会看到这两种意图分别落在哪些事件上。

## 四、Service 与 inject：加载顺序由依赖声明

第三个原语回答"谁先谁后"。先看插件的三种形态，教程里的完整清单：

```ts
// docs/cordis-tutorial/01-first-plugin.md
// 1. 函数插件：named export 一个 apply
export function apply(ctx: Context) {}
// 2. 对象插件：带 apply 方法的对象
export const objectPlugin = { name: 'object-plugin', apply(ctx: Context) {} }
// 3. 类插件：Service 子类
export class MyService extends Service {
  constructor(ctx: Context) { super(ctx, 'myTutorialService') }
}
```

函数形态最常用，直到你需要暴露一个服务才升级成类形态。`Service` 基类的构造函数干的事出奇地少：

```ts
// vendor/cordis/src/service.ts:42-58（节选）
constructor(protected ctx: Context, name: string) {
  name ??= this.constructor['provide'] as string
  let self = this
  if (self[symbols.invoke]) {
    self = createCallable(name, joinPrototype(/* … */), tracker)
  }
  self.ctx = ctx
  self.name = name
  self.ctx.reflect.provide(name, self, this[symbols.check])
  return self
}
```

构造即注册：`super(ctx, name)` 一调用，服务实例就通过 `ctx.reflect.provide` 挂到了 context 的键上，注册的逆操作同样绑定在 owning fiber 上，fiber 卸载时服务自动摘除。

那么顺序呢？教程里有一句话说破了机制：

> Entries start concurrently, so list position guarantees nothing about which plugin loads first; ordering comes from service dependencies (`inject`), not from position in the file.

`cordis.yml` 里的条目**并发启动**，文件里的行位不保证任何顺序。顺序来自 `inject`：一个插件声明它需要哪些服务（如 `inject: ['tools', 'llm']`，裸服务名、不带 `ctx.` 前缀），Cordis 就等到这些服务全部到位才激活它。于是几十个插件的装配时序从每个插件的依赖声明里推导出来，不存在一个手动排启动顺序的中央调度器。primer 把这一点列为五个核心想法之一：load order is expressed through service requirements rather than manual boot sequencing。

这也是 `dsh` 敢把"无特权核心"贯彻到底的原因之一：连"谁先谁后"都不需要一个核心来管，靠声明就够了。

## 五、scope：per-agent 私有注册

Context 是全局共享的仓库，但真实产品里每个 agent 不该看见彼此的工具与监听。`dsh` 在 Cordis 之上加了一层薄原语：`dsh-scope`（`packages/core/scope/`）。它的用法一句话：`createScope(ctx, key)` 铸造一个带标签的子 context，通过它注册的一切既在标签内可见，也随标签的生命周期回收。

```ts
// packages/core/scope/README.md
const scope = createScope(ctx, agent)
scope.ctx.on('agent/status', ({ agent, status }) => track(agent, status))
// later:
await scope.dispose()   // unwinds every registration made through scope.ctx
```

这个包的核心契约是一句话：**注册上下文同时决定可见性与所有权**——一个通过某 scope 注册的贡献，在这个 scope 里可见、也随这个 scope 回收，不存在"在 A 处可见、却随 B 卸载"的错位。core 组的注册表全部建在它上面：一个工具通过 `agent.ctx` 注册，只有那个 agent 看得见。

scope 之间还有一条父子链，两个方向能力不对称：**注册视图沿链向下继承**（子 scope 看得见祖先的层），**事件准入沿链向上延伸**（挂在祖先 key 上的监听收得到发给后代的定向事件）。反方向都不成立。绑定是一次性的，重复绑定抛错，成环被拒绝。

![scope 父子链：注册视图向下继承，事件准入向上延伸](/images/deepseek/02-scope-chain.svg)

边界要说清楚：README 明确 scope "routes trusted same-process plugins; it is not a sandbox or an authority boundary"。它是同进程内的信任路由，组织"每个 agent 各自的工具集"，安全隔离由沙箱与审批策略在别的层负责。把 scope 当安全边界用，是对这台机器的误读。

## 六、Loader：cordis.yml 怎么变成一棵树

运行时原语讲完，最后一块是装配：`cordis.yml` 里的一行行声明怎么变成挂满插件的树。这活由 `@deepseek-ai/cordis-plugin-include`（vendor 进来的 `vendor/include`）承担：读配置行、解析插件模块、按 `inject` 依赖图挂载，然后配合第一篇讲过的层叠——bundle 层、profile patch、home patch、`--patch` 逐层覆盖。

配置行里最 `dsh` 味的是 `!!js` 表达式，它的求值时机被严格规定过：

- entry 的 `config` 在**该插件声明的注入激活之后**、对插件自己的 context 求值——所以表达式里可以引用 `ctx.serviceName` 读到依赖服务；
- `disabled` 字段在**每次做 mount 决策时**对 loader context 求值——所以同一个配置行可以按平台或环境开关；
- 其他 entry 元信息保持字面值，嵌套行的表达式保留到目标激活那一刻才求值。

这套时机设计让"声明式 + 可补丁 + 可条件化激活"三件事共存：`dsh-base` 里用 `disabled: !!js process.platform === 'win32'` 关掉 Windows 上不适用的 shell 后端，靠的就是第二条。

把这一篇和第一篇接起来，`dsh` 的装配全景就完整了：**Loader 管"哪些插件被装、每个 entry 被谁覆盖"，Cordis 运行时管"装进来的插件怎么共存、怎么协作、怎么卸载"**。前者是工厂的装配线，后者是机器的运转律。

## 七、与三栏对比：组合框架即抽象层

把 Cordis 和三个终端 Agent 的组合机制放在一张表上：

| 维度 | OpenCode | Codex | DeepSeek Harness |
|------|---------|-------|------------------|
| 组合机制 | Effect-TS 依赖注入（Layer/Service） | Rust 结构内自建事件体系 | vendored Cordis |
| 服务定位 | Effect 的 Service 上下文 | 结构体字段直连 | `ctx.<key>` 键解耦 |
| 事件系统 | Effect 的 PubSub | 自建事件枚举 | 五模式 typed events + `@mode` |
| 生命周期 | Effect 的 finalizer | 手动 teardown | fiber 逆序回滚 |
| 装配方式 | 代码里组合 Layer | 编译期固定 | cordis.yml + patch 层叠 |

最深的一处差别在哲学上：OpenCode 用 Effect-TS 解决"依赖注入"这一件事，Codex 把组合逻辑作为内部结构的一部分自己写，两者都不把"组合"本身当成一个可替换的层。`dsh` 把依赖、事件、生命周期这整层外包给一个 vendored 框架，等于宣称：连"怎么组装自己"都不算产品的私有代码。要迁移到新产品形态，适配面落在 Cordis 的插件协议上，不需要碰 `dsh` 的任何内部代码。

代价也直白：读 `dsh` 之前必须先读一个第三方框架的心智模型；框架升级要整个 vendor 同步；调试时栈里多了一层不属于自己的代码。这是一笔用学习成本换组合性的交易，`dsh` 认为值得。

## 八、小结：五件事拼出插件树

把这台地基机器收进五条：

1. **Context（服务仓库）**——`ctx.<key>` 键解耦，`extend`/`isolate`/`intercept` 三种派生互不改父。
2. **注册即副作用**——`effect`/`on` 的一切贡献绑在 fiber 上，卸载逆序回滚，对已销毁 fiber 注册直接抛错。
3. **五模式事件**——观察（emit/parallel/serial）与委托（waterfall/bail）分家，模式是事件的公共契约。
4. **Service + inject**——构造即注册，加载顺序由依赖声明推导，没有中央调度器。
5. **scope**——注册上下文统一可见性与所有权，per-agent 隔离，但它是信任路由不是沙箱。

加上 Loader 把声明变成树，`dsh` 的全部上层建筑——agent 循环、会话日志、工具管线、能力缝——都站在这五件事上。下一篇走进树的主干：`agent-loop`，一个把自己写成"默认实现"的循环。

## 源码索引

- `docs/cordis-primer.md` — 五个核心想法、dispatch 模式表、waterfall 语义、Loader 求值时机
- `vendor/cordis/src/context.ts` — Context、extend/isolate/intercept
- `vendor/cordis/src/events.ts` — 五种 dispatch 实现、on/register 与 fiber 绑定
- `vendor/cordis/src/service.ts` — Service 基类、构造即注册
- `vendor/cordis/src/fiber.ts` — effect 收集与 UNLOADING 状态机
- `vendor/cordis/src/registry.ts` — inject 声明解析
- `vendor/include/` — `@deepseek-ai/cordis-plugin-include`，cordis.yml 装载
- `packages/core/scope/` — dsh-scope：createScope、父子链、可见性契约
- `docs/cordis-tutorial/` — 三种插件形态、生命周期、服务、事件的动手教程

## 章节小测

<script setup>
const q = [
  {
    question: 'Cordis 里插件之间定位彼此的服务，靠什么？',
    options: ['import 对方包的导出类', '在 context 上按稳定键读写', '走全局单例注册中心', '由配置文件硬编码绑定'],
    correct: 1,
    explanation: 'Context 是服务仓库：服务占一个 `ctx.<key>`，使用方按键取用而非 import 实现，所以换实现只是换注册到该键的插件。A 会把使用方绑死在实现包上，C/D 都不是 Cordis 的机制。'
  },
  {
    question: '一个 waterfall 监听者不调 `next()` 直接返回，后果是？',
    options: ['整条链只剩内置行为执行', '框架自动替它补调 next', '链条其余环节与内置行为都被否决', '运行时抛 CordisError'],
    correct: 2,
    explanation: 'waterfall 的语义：不调 next 即否决，链条剩下的监听者连同最内端的内置行为都不会执行。A 说反了，B 不存在这种兜底，D 是对已销毁 fiber 注册时才会抛的错。'
  },
  {
    question: '几十个插件的加载顺序，Cordis 靠什么决定？',
    options: ['按 cordis.yml 的行位依次启动', '按包名字典序加载', '由 root 插件手动编排', '由 inject 声明的服务依赖推导'],
    correct: 3,
    explanation: '条目并发启动、行位不保证顺序；声明了 `inject` 的插件等所需服务全部到位才激活，顺序从依赖图推导出来。A 与事实相反，B/C 的机制不存在。'
  },
  {
    question: '关于 `dsh-scope` 的定位，正确的说法是？',
    options: ['它是同进程的信任路由，不是沙箱', '它是不可绕过的安全边界', '它只影响事件，不管注册的回收', '它只能在 web profile 下生效'],
    correct: 0,
    explanation: 'README 明确 scope 路由可信的同进程插件、不是沙箱或权限边界；安全隔离由沙箱与审批策略负责。B 夸大了它的安全承诺，C 漏掉了它对注册生命周期的所有权，D 无此限制。'
  },
  {
    question: '一个插件通过 `ctx.on()` 注册的监听器，什么时候被摘除？',
    options: ['等进程退出时才统一清理', '事件触发满一次后自动移除', '插件所在 fiber 卸载时自动回滚', '需要手动调用全局清理接口'],
    correct: 2,
    explanation: '监听器作为 effect 存进当前 fiber 的卸载项，fiber 卸载进入 UNLOADING 时逆序执行 disposer。B 说的是 `once()`，A/D 都不是这套生命周期模型。'
  }
]
</script>

<Quiz :questions="q"></Quiz>
