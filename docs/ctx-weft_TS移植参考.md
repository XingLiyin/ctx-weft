# CtxWeft TypeScript 移植参考

> **2026-06-05 · 基于 `master` 分支现状整理**
>
> 本文是把 `ctx-weft`（Python）+ `ipmastercowork` 移植到 TypeScript 的参考。内容来自对现有
> 代码的逐模块对照：协议层、循环引擎、事件溯源、持久化、崩溃恢复、session 管理、知识库接入。
>
> 已有 Java 移植 `loomej-core` / `loomej-shell` 可作平行参照；本文聚焦 TS 特有的形态与取舍。
>
> 相关文档：[设计文档](./ctx-weft_设计文档.md) · core 的 [README](../README.md) / [ARCHITECTURE](../ARCHITECTURE.md)

---

## 目录

1. [结论与动机](#1-结论与动机)
2. [适配性评估](#2-适配性评估为什么-ts-是三个目标里最贴的)
3. [仓库形态](#3-仓库形态)
4. [语言映射规则](#4-语言映射规则)
5. [协议的 TS 形式](#5-协议的-ts-形式)
6. [core 实现要点](#6-core-实现要点)
7. [四大子系统](#7-四大子系统)
   - [7.1 数据持久化](#71-数据持久化)
   - [7.2 崩溃恢复](#72-崩溃恢复)
   - [7.3 Session 管理](#73-session-管理)
   - [7.4 知识库接入](#74-知识库接入)
8. [部署拓扑：仍然需要后端](#8-部署拓扑仍然需要后端)
9. [风险与维护](#9-风险与维护三份实现的漂移)
10. [落地路线](#10-落地路线)

---

## 1. 结论与动机

**架构上完全适合移植，且 TS 是 Python / Java / TS 三个目标里契合度最高的。**

理由不是"TS 流行"，而是这套架构的核心设计恰好踩在 TS 的原生强项上：

- **协议优先** → TS `interface` 是原生结构化类型（比 Java 的 nominal `implements` 更贴）。
- **流式（`AsyncIterator`）** → JS 原生 `AsyncIterable` / async generator，`for await...of`。
- **单事件循环并发** → Node 与 asyncio 同为单线程协作式，语义近乎 1:1（远比 Java 真线程省心）。
- **生态** → MCP 官方参考实现就是 TS；Anthropic / OpenAI 一流官方 TS SDK；SSE 是 Node 基础能力。

**该不该做，取决于动机：**

| 动机 | 判断 |
|------|------|
| 有 Node/边缘/Serverless 后端、要给 JS 生态发 SDK | ✅ 强理由 |
| **想与现有 TS 前端共享类型**（消除事件分类三处对齐的漂移） | ✅ TS 独有收益 |
| 只是 "because we can" | ❌ 三份实现的维护税不划算 |

> 注：与本文档配套的判断见对话沉淀——agent 时代降低了"生成 + 维护"成本，使第三份移植更可行；但
> 降不了"三份漂移"的连贯性风险，因此 [§9](#9-风险与维护三份实现的漂移) 的 spec + 一致性测试是前提。

---

## 2. 适配性评估：为什么 TS 是三个目标里最贴的

| 现有设计 | Python 形态 | 映射到 TS |
|----------|-------------|-----------|
| 协议优先 | `typing.Protocol`（鸭子类型补丁） | `interface`（原生结构化类型） |
| 流式 | `def f() -> AsyncIterator[T]` | `f(): AsyncIterable<T>`（async generator） |
| 并发模型 | asyncio 单线程协作式 | Node 单事件循环——语义对齐 |
| 状态补丁 | `dataclasses.replace(s, **patch)` | `{ ...s, ...patch }` |
| 取消 | 自研 `CancelToken` | `AbortController` / `AbortSignal`（更地道） |
| 锁 | `asyncio.Lock` | `async-mutex`（单循环里仍需防 await 点重入） |
| 后台任务 | `asyncio.create_task(drain)` | fire-and-forget Promise + **必须接住 rejection** |

**唯一需要改设计的地方**：core 现在依赖**运行时类型分派**（`isinstance(p, SkillCapabilityProvider)`，
见 `runtime._skill_provider_index` 与 `CapabilityGateway` 对 `ToolCapabilityProvider` 的判断）。
TS 接口在运行时被擦除，无反射，必须改用**判别字段 + type guard**（见 [§5](#5-协议的-ts-形式)）。

---

## 3. 仓库形态

pnpm monorepo：

```
ctx_weft-ts/
├─ packages/
│  ├─ core/          @ctx_weft/core    —— 零运行时依赖（对标 Python "零额外依赖"）
│  ├─ host/          @ctx_weft/host    —— Fastify + Postgres + adapters
│  └─ shared/        @ctx_weft/shared  —— 事件/DTO 类型，core 与前端共用
└─ apps/web/         前端（已有，import @ctx_weft/shared）
```

`packages/core/src/` 与 Python `src/ctx_weft/core/` 同构：

```
core/src/
├─ protocols/    memory.ts capability.ts knowledge.ts llm.ts template.ts   (全是 interface)
├─ events/       types.ts  bus.ts
├─ state/        eventStore.ts  models.ts
├─ loop/         driver.ts  capabilityGateway.ts  steps/{reason,act,observe,finalize,compact,suspend,metadataFiller}.ts
├─ assembler/    assembler.ts  budget.ts  composer.ts  sources/*.ts
├─ orchestrator/ taskManager.ts  sessionManager.ts  lifecycleManager.ts  controlCapability.ts  hitlManager.ts
├─ control/      reducers.ts  converters.ts  replay.ts
└─ runtime.ts
```

tsconfig：`strict` + `module: NodeNext` + `target: ES2022`（顶层 await、AsyncIterator 原生），ESM。
`packages/core/package.json` 的 `dependencies` 基本为空（顶多 `ulid`）——保持 core 零 I/O 的边界。

---

## 4. 语言映射规则

| Python | TS |
|--------|-----|
| `class X(Protocol)` | `interface X` |
| `name: str`（实例属性） | `readonly name: string` |
| `@property def context_limit` | `readonly contextLimit: number`（getter 满足） |
| `async def f() -> T` | `f(): Promise<T>` |
| `def f() -> AsyncIterator[T]` | `f(): AsyncIterable<T>` |
| `tuple[list[R], int]` | `[R[], number]`（元组类型） |
| `StrEnum` | 字符串字面量联合 `"a" \| "b"` |
| ABC + 默认方法 | `abstract class` |
| `isinstance(x, P)` 运行时分派 | **`kind` 判别字段 + type guard** |
| `dataclass`（带默认值） | `interface` + 工厂函数 / `zod` schema（边界校验） |
| SQLAlchemy | Drizzle（类型化 SQL） |
| FastAPI | Fastify / Hono |
| `asyncio.timeout` | `AbortSignal.timeout(ms)` |

---

## 5. 协议的 TS 形式

协议本体 = `interface`；带默认实现的 = `abstract class`；凡运行时分派的 = 加 `kind` 判别字段。

```ts
// protocols/llm.ts
export interface LLMChunk {
  kind: "token" | "reasoning" | "tool_call" | "tool_call_partial" | "usage" | "done";
  text?: string;
  toolCall?: ToolCall;
  usage?: LLMUsage;
  finishReason?: string;
}

export interface LLMClient {
  readonly contextLimit: number;
  readonly maxOutputTokens: number;
  readonly supportsToolCalling: boolean;
  complete(request: LLMRequest, stream?: boolean): AsyncIterable<LLMChunk>;
  countTokens(text: string): Promise<number>;
}
```

实现：结构匹配即可赋值，无需 `implements`；但**推荐写 `implements`** 让签名错误当场暴露。

```ts
class MyLLM implements LLMClient {
  readonly contextLimit = 128_000;
  readonly maxOutputTokens = 4096;
  readonly supportsToolCalling = true;

  async *complete(req: LLMRequest): AsyncIterable<LLMChunk> {
    for await (const c of myApi.stream(req.system, req.messages)) yield { kind: "token", text: c.text };
    yield { kind: "usage", usage: { promptTokens: 100, completionTokens: 50, totalTokens: 150 } };
    yield { kind: "done", finishReason: "stop" };
  }
  async countTokens(text: string) { return Math.ceil(text.length / 4); }
}
```

接口继承（Capability 层）+ 默认方法（用抽象类）+ 运行时分派（判别字段）：

```ts
// protocols/capability.ts
export interface CapabilityProvider {
  readonly name: string;
  list(ctx: ProviderContext): Promise<Capability[]>;
  retrieve(ctx: ProviderContext): Promise<Capability[]>;
  describe(ctx: ProviderContext): Promise<CapabilityProviderInfo>;
}

export interface ToolCapabilityProvider extends CapabilityProvider {
  invoke(capabilityId: string, args: Record<string, unknown>, ctx: ProviderContext): AsyncIterable<CapabilityEvent>;
  cancel(invocationId: string, ctx: ProviderContext): Promise<void>;
}

// 默认实现（Python ABC 里 retrieve 回落到 list）→ 抽象类
export abstract class BaseCapabilityProvider implements CapabilityProvider {
  abstract readonly name: string;
  abstract list(ctx: ProviderContext): Promise<Capability[]>;
  abstract describe(ctx: ProviderContext): Promise<CapabilityProviderInfo>;
  retrieve(ctx: ProviderContext): Promise<Capability[]> { return this.list(ctx); }
}

// 运行时分派（接口被擦除，无 isinstance）→ kind + type guard
export interface SkillCapabilityProvider extends CapabilityProvider {
  readonly kind: "skill";
  loadDefinition(id: string, ctx: ProviderContext): Promise<SkillDefinition>;
}
export function isSkillProvider(p: CapabilityProvider): p is SkillCapabilityProvider {
  return (p as { kind?: string }).kind === "skill";
}
```

> 建议给所有 provider 协议都带 `kind`，统一成 discriminated union，分派更清晰。

边界（LLM JSON / MCP / HTTP）用 `zod` 做运行时校验 + 类型推导合一——Python dataclass 在边界其实不强制校验，这是 TS 的净增益：

```ts
const LLMChunkSchema = z.object({ kind: z.enum([...]), text: z.string().optional() /* ... */ });
type LLMChunk = z.infer<typeof LLMChunkSchema>;   // 类型即 schema，单一真相
```

---

## 6. core 实现要点

### EventBus —— `asyncio.Queue/stream()` → async generator + 有界缓冲

`emit` 内联 `await handler` 的语义与 Python 一致（持久化订阅者在 emit 里同步跑、会阻塞 loop——所以
token 设瞬态的优化照样必要）。背压用有界队列 + 丢最旧。

```ts
export interface EventBus {
  emit(event: Event): Promise<void>;
  subscribe(filter: EventFilter, handler: (e: Event) => Promise<void>): Subscription;
  stream(filter: EventFilter): AsyncIterable<Event>;
}

export class InProcessEventBus implements EventBus {
  private subs = new Map<string, Sub>();

  async emit(event: Event): Promise<void> {
    for (const sub of this.subs.values()) {
      if (!matches(event, sub.filter)) continue;
      if (sub.handler) await sub.handler(event);   // 内联执行，同 Python 语义
      else sub.push(event);                        // 流订阅者：入有界队列，满则丢最旧
    }
  }

  async *stream(filter: EventFilter): AsyncIterable<Event> {
    const sub = this.addStreamSub(filter);         // 内部：队列 + waker promise
    try { while (true) yield await sub.next(); }
    finally { this.subs.delete(sub.id); }
  }
}
```

### StepDriver —— `async def run -> AsyncIterator` → async generator，近乎直译

```ts
export class StepDriver {
  constructor(private steps: Map<string, Step>, private initialStep = "reason") {}

  async *run(initial: LoopState, ctx: LoopContext): AsyncGenerator<StepOutcome> {
    let state = initial;
    await this.persistUserPrompt(state, ctx);            // 起步持久化 USER_PROMPT
    await this.ensureBlackboardSubscriptions(state, ctx);

    let next: string | null = this.initialStep;
    while (next !== null) {
      ctx.signal.throwIfAborted();                       // ← CancelToken
      const step = this.steps.get(next);
      if (!step) throw new Error(`Step '${next}' not registered`);

      await ctx.eventBus.emit(makeEvent(state, "StepStarted", { stepName: next }));
      const outcome = await step.execute(state, ctx);
      state = { ...state, ...outcome.statePatch };        // ← dataclasses.replace
      for (const ev of outcome.events) await ctx.eventBus.emit(ev);
      await ctx.eventBus.emit(makeEvent(state, "StepCompleted", { nextStep: outcome.nextStep }));

      next = outcome.nextStep;
      yield outcome;
    }
  }
}
```

### 取消 / 后台任务 / 锁

```ts
const ac = new AbortController();
this.cancelTokens.set(sessionId, ac);
// interrupt: ac.abort()  → loop 内 signal.throwIfAborted() 抛出 → 转 CANCELED

void this.drain(taskManager).catch(err => logger.error("drain crashed", err));  // ★ 接住 rejection

private lock = new Mutex();                       // async-mutex
await this.lock.runExclusive(async () => { /* ... */ });
```

### EventStore 接口 + 内存实现（token 跳过内建）

```ts
export interface EventStore {
  append(e: Event): Promise<void>;
  readBySession(id: string): Promise<Event[]>;
  listActiveSessionIds?(): Promise<string[]>;      // 可选扩展
  readAfter?(id: string, afterId: string): Promise<Event[]>;
  saveSnapshot?(s: RunSnapshot): Promise<void>;
  loadLatestSnapshot?(id: string): Promise<RunSnapshot | null>;
}

export class InMemoryEventStore implements EventStore {
  async append(e: Event) {
    if (TRANSIENT_EVENT_TYPES.has(e.type)) return;   // token 不落库，对标 Python 改动
    /* ... */
  }
}
```

> `TRANSIENT_EVENT_TYPES = { "LLMTokenStreamed", "LLMReasoningStreamed" }` 定义在 core，作为单一真相，
> 持久化 / 投影 / 快照各订阅者统一引用——避免像早期 Python host 那样以字符串字面量各维护一份。

---

## 7. 四大子系统

共享 Drizzle schema（持久化 / 恢复 / session 都用它；`jsonb` 是相对 Python `TEXT + json.dumps` 的升级）：

```ts
// host/db/schema.ts
export const events = pgTable("events", {
  id: varchar("id", { length: 64 }).primaryKey(),        // evt_ULID，时间可排序
  runId: varchar("run_id", { length: 64 }),
  sessionId: varchar("session_id", { length: 64 }).notNull(),
  taskId: varchar("task_id", { length: 64 }),
  agentId: varchar("agent_id", { length: 64 }),
  tenantId: varchar("tenant_id", { length: 64 }).notNull().default("default"),
  type: varchar("type", { length: 64 }).notNull(),
  sequence: integer("sequence").notNull(),
  payload: jsonb("payload").$type<Record<string, unknown>>().notNull(),
  metadata: jsonb("metadata").$type<Record<string, unknown>>().notNull(),
  timestamp: timestamp("timestamp", { withTimezone: true }).notNull(),
}, (t) => ({ bySession: index("ix_events_session").on(t.sessionId, t.id) }));

export const snapshots = pgTable("snapshots", { /* id, sessionId, lastEventId, lastEventSequence, stateBlob jsonb, createdAt */ });
export const sessions  = pgTable("sessions",  { /* 投影表 */ });
export const tasks     = pgTable("tasks",     { /* 投影表 */ });
```

### 7.1 数据持久化

两条独立通道（同 Python）：**事件日志**（`event_store`）+ **投影表**（`ProjectionUpdater` 写 sessions/tasks）。
都是总线订阅者。另有 SSE 帧持久化（state_store，按 `_NO_PERSIST` 跳过 delta）走第三条通道。

```ts
// state/postgresEventStore.ts
export class PostgresEventStore implements EventStore {
  constructor(private db: Db, private keepSnapshots = 3) {}

  async append(e: Event) { await this.db.insert(events).values(eventToRow(e)); }

  async readBySession(id: string) {
    return (await this.db.select().from(events)
      .where(eq(events.sessionId, id)).orderBy(events.id)).map(rowToEvent);
  }
  async readAfter(id: string, afterId: string) {
    return (await this.db.select().from(events)
      .where(and(eq(events.sessionId, id), gt(events.id, afterId)))
      .orderBy(events.id)).map(rowToEvent);
  }
  async listActiveSessionIds() {                          // SessionCreated 但无 SessionFinished
    const finished = this.db.select({ id: events.sessionId }).from(events)
      .where(eq(events.type, "SessionFinished"));
    return (await this.db.selectDistinct({ id: events.sessionId }).from(events)
      .where(and(eq(events.type, "SessionCreated"), notInArray(events.sessionId, finished))))
      .map(r => r.id);
  }
  async saveSnapshot(s: RunSnapshot) {
    await this.db.transaction(async (tx) => {
      await tx.insert(snapshots).values(snapshotToRow(s));
      const keep = tx.select({ id: snapshots.id }).from(snapshots)   // 保留最新 N 张
        .where(eq(snapshots.sessionId, s.sessionId)).orderBy(desc(snapshots.id)).limit(this.keepSnapshots);
      await tx.delete(snapshots)
        .where(and(eq(snapshots.sessionId, s.sessionId), notInArray(snapshots.id, keep)));
    });
  }
  async loadLatestSnapshot(id: string) {
    const [row] = await this.db.select().from(snapshots)
      .where(eq(snapshots.sessionId, id)).orderBy(desc(snapshots.createdAt)).limit(1);
    return row ? rowToSnapshot(row) : null;
  }
}

// persistence/eventPersister.ts —— token 不落库
export class EventPersister {
  constructor(private store: EventStore) {}
  onEvent = async (e: Event) => {
    if (TRANSIENT_EVENT_TYPES.has(e.type)) return;
    await this.store.append(e);
  };
}
```

`ProjectionUpdater` 是 `switch(e.type)` 写 sessions/tasks 表，纯粹直译。

**快照写入时机**（`SnapshotWriter`）：在 `RunFinished` 边界、距上次快照累计事件 ≥ 阈值时定期写，
`SessionFinished` 收尾。写入用 `rebuildView`（快照 + 增量）保证 O(delta) 而非 O(全量)——因为
handler 在 emit 里内联跑、会阻塞 loop。阈值与保留数走环境变量（`CTX_WEFT_SNAPSHOT_EVERY_N_EVENTS` /
`CTX_WEFT_SNAPSHOT_KEEP`，默认 50 / 3）。

### 7.2 崩溃恢复

核心：`rebuildView`（快照 + 增量，否则全量）+ 纯函数 reducer + `recoverSession` 重建队列。

```ts
// control/reducers.ts —— 纯函数，逐行直译
export async function rebuildView(store: EventStore, sessionId: string): Promise<RunStateView> {
  const snap = await store.loadLatestSnapshot?.(sessionId);
  if (snap) {
    const view = deserializeView(snap.stateBlob);
    const delta = await store.readAfter!(sessionId, snap.lastEventId);
    return applyEvents(delta, view);                     // 只回放增量 → O(delta)
  }
  return reduceEvents(await store.readBySession(sessionId), sessionId);   // 兜底全量
}

function apply(view: RunStateView, ev: Event): void {
  switch (ev.type) {
    case "SessionCreated": view.sessions.set(ev.sessionId, sessionViewFrom(ev)); break;
    case "TaskCreated":    view.tasks.set(taskId(ev), taskViewFrom(ev)); break;
    case "TaskRequeued":   /* 回 PENDING、清 outputs、恢复改写后的 prompt */ break;
    default:
      if (ev.type in TASK_STATUS_BY_EVENT && ev.taskId)  // 共享映射，与前端/投影同源
        view.tasks.get(ev.taskId)!.status = TASK_STATUS_BY_EVENT[ev.type];
  }
}

// runtime.ts
async recover(onInterrupted?: (id: string) => Promise<void>): Promise<number> {
  const ids = (await this.eventStore.listActiveSessionIds?.()) ?? [];
  for (const id of ids) await onInterrupted?.(id);        // host 标记 INTERRUPTED / 拉起
  return ids.length;
}

async recoverSession(sessionId: string): Promise<void> {
  const view = await rebuildView(this.eventStore, sessionId);
  const sess = view.sessions.get(sessionId);
  if (!sess?.templateId) throw new Error(`cannot recover ${sessionId}: no template`);

  const allTasks = [...view.tasks.values()].map(taskFromProjection);
  const terminal = new Set(allTasks.filter(t => TERMINAL.has(t.status)).map(t => t.id));
  if (allTasks.every(t => TERMINAL.has(t.status))) throw new Error("no resumable tasks");

  const tm = new TaskManager(sessionId, this.eventBus);
  const daemons = tm.restore(allTasks, terminal);          // 重建队列，返回需重启的 daemon
  tm.setRunner(this.makeTaskRunner({
    session: sessionFromProjection(sess),
    preResolved: agentsFromView(view),                     // 保留 spawnDepth / parent
  }));
  this.registerAndDrain(sessionFromProjection(sess), tm, daemons);   // 续跑
}
```

要点：恢复成本 = O(增量)（有快照时）。`id` 用 ULID 主键保证排序正确（`readAfter` 依赖 `id > afterId`）。

### 7.3 Session 管理

`startSession`（新建/恢复二合一）→ `SessionManager` 建 Session + rootTask + TaskManager →
`registerAndDrain` 后台 drain。取消用 `AbortController`。

```ts
// runtime.ts
async startSession(p: SessionStartParams): Promise<RunHandle> {
  const ac = new AbortController();                         // CancelToken → AbortController
  const { session, taskManager, rootTask } = p.sessionId
    ? await this.sessionManager.resume(p, ac.signal)
    : await this.sessionManager.create(p, ac.signal);
  this.cancelTokens.set(session.id, ac);
  this.registerAndDrain(session, taskManager, []);
  return new RunHandle(session, rootTask, this.eventBus);   // 立即返回，后台跑
}

interruptSession(id: string): boolean {
  const ac = this.cancelTokens.get(id);
  if (!ac) return false;
  ac.abort();
  return true;
}

private registerAndDrain(session: Session, tm: TaskManager, daemons: Task[]) {
  this.controlCapability.registerSession(session, tm);
  tm.onDone(() => { this.cancelTokens.delete(session.id); this.controlCapability.deregister(session.id); });
  for (const d of daemons) void tm.runDaemon(d);
  void tm.drain().catch(err => logger.error("drain crashed for %s", session.id, err));  // ★ 接住 rejection
}
```

```ts
// orchestrator/taskManager.ts —— drain 调度循环
async drain(): Promise<void> {
  while (!this.stopped) {
    if (this.inflight.size >= this.maxConcurrent) { await this.slotFreed(); continue; }
    const task = this.queue.takePending();
    if (!task) {
      if (this.inflight.size === 0) break;                  // 无 pending 且无在跑 → 收工
      await this.wake();                                    // 等"入队/完成"信号（Promise waker）
      continue;
    }
    const p = this.runner(this.sessionId, task.id)
      .catch(err => this.onTaskError(task, err))            // 重试 / 置 FAILED
      .finally(() => { this.inflight.delete(p); this.signal(); });
    this.inflight.add(p);
  }
}
```

```ts
// RunHandle —— 事件流订阅来自总线
export class RunHandle {
  async *events(): AsyncIterable<Event> {
    for await (const ev of this.bus.stream({ runId: this.runId })) {
      yield ev;
      if (ev.type === "RunFinished") return;
    }
  }
  async waitForFinish(timeoutMs = 300_000): Promise<LoopState | null> { /* race stream vs AbortSignal.timeout */ }
}
```

host 那层另有 `SessionEntry`（SSE 缓冲 + 状态），从总线 `stream()` 消费、翻成 SSE 帧、按 `_NO_PERSIST`
跳过 delta——和 Python 同构。

### 7.4 知识库接入

`KnowledgeProvider` 接口（流式 retrieve）+ 多 provider 按 priority 升序 + `KnowledgeRetrievalSource`
把结果作为 **user 引用块**塞进 messages（**不进 system**）。

```ts
// protocols/knowledge.ts
export interface KnowledgeProvider {
  readonly name: string;
  retrieve(query: KnowledgeQuery, ctx: ProviderContext): AsyncIterable<KnowledgeDoc>;
  describe(ctx: ProviderContext): Promise<KnowledgeProviderInfo>;
}

// registry：priority 升序，数字小先查
registerKnowledge(p: KnowledgeProvider, priority = 0) {
  this.knowledge.push({ p, priority });
  this.knowledge.sort((a, b) => a.priority - b.priority);
}
```

具体 adapter——pgvector 直接用 Drizzle raw sql：

```ts
export class PgVectorKnowledge implements KnowledgeProvider {
  readonly name = "pgvector";
  constructor(private db: Db, private embed: (t: string) => Promise<number[]>) {}

  async *retrieve(q: KnowledgeQuery): AsyncIterable<KnowledgeDoc> {
    const v = toVector(await this.embed(q.text));
    const rows = await this.db.execute(sql`
      SELECT id, content, 1 - (embedding <=> ${v}) AS score
      FROM kb_docs ORDER BY embedding <=> ${v} LIMIT ${q.topK ?? 5}`);
    for (const r of rows) yield { id: r.id, content: r.content, score: Number(r.score), source: this.name };
  }
  async describe() { return { name: this.name }; }
}
```

assembler source——扇出多 provider、带超时、合并 top-k、落 messages：

```ts
// assembler/sources/knowledge.ts
export class KnowledgeRetrievalSource implements ContextSource {
  async fetch(req: ContextRequest, deps: AssemblerDeps): Promise<ContextBlock[]> {
    const query: KnowledgeQuery = { text: req.task.userPrompt, topK: 5, intent: "reference" };
    const docs: KnowledgeDoc[] = [];
    for (const provider of deps.knowledgeProviders) {           // 已按 priority 升序
      try {
        for await (const d of withTimeout(provider.retrieve(query, deps.ctx), 3000))  // AbortSignal.timeout
          docs.push(d);
      } catch (e) { logger.warn("knowledge %s failed/timeout", provider.name, e); }    // 单个失败不阻断
    }
    if (docs.length === 0) return [];
    const top = docs.sort((a, b) => b.score - a.score).slice(0, 5);
    return [{
      placement: "messages",                                   // 关键：user 引用块，不进 system
      role: "user",
      priority: 30,
      content: "## Reference material I just looked up\n" +
               top.map(d => `- ${d.content} (score=${d.score.toFixed(2)}, ${d.source})`).join("\n"),
    }];
  }
}
```

---

## 8. 部署拓扑：仍然需要后端

"纯 TS" 指**全栈同一门语言**，不是"没有后端"。拓扑与现在的 Python host 一致：

```
浏览器 (apps/web, TS)
      │  HTTP + SSE
      ▼
Node 后端 = @ctx_weft/host (Fastify)      ← 后端接口（REST + SSE）
      │  进程内调用
      ▼
@ctx_weft/core (纯逻辑, 零依赖)
      │
      ├── Postgres (Drizzle)
      ├── LLM API (Anthropic/OpenAI SDK)
      └── MCP / 工具
```

- **`@ctx_weft/core`**：纯 TS 库，零 I/O，不监听端口。
- **`@ctx_weft/host`**：后端——Fastify 暴露 REST + SSE，持有 DB 连接、密钥、跑后台 drain。角色等同现 Python host。
- **前端**：经 HTTP/SSE 调 host，和现在调 Python host 无异。

**为什么不能省掉后端**：密钥不能进浏览器；DB 连接 / 崩溃恢复必须在服务端；agent loop、后台 drain、
多 session 调度要常驻进程；事件流单一真相在服务端经 SSE 广播给多端；MCP stdio / 文件 / bash 需服务端环境。

**纯前端跑** 仅适合玩具/单用户 demo（内存 provider + LLM 代理），无持久化 / 密钥安全 / 多端 / 恢复。

---

## 9. 风险与维护：三份实现的漂移

技术上 TS 移植很顺。真正的成本是**维护乘数**——`blackboard` intent、`reopen` 级联、reducer 逻辑、
事件分类，要在 **Python / Java / TS 三套**里保持语义一致。

应对（也是值不值得做的前提）：

1. **一份语言无关的 spec**：事件清单（`EVENT_TYPES`）、Step 状态机、reducer 规则、`TASK_STATUS_BY_EVENT`、
   blackboard 语义、瞬态事件集合——作为三份实现的唯一真相。
2. **一套跨语言一致性测试**：同一组事件序列喂进三份 reducer，断言投影一致。Python 侧已有
   `rebuild_view` 测试（见 `tests/unit/test_snapshot_recovery.py`），把它做成可移植的黄金用例。
3. **共享类型收益**：`@ctx_weft/shared` 让 core 与前端共用 `Event` / `EventType` / payload 类型，把
   "core ↔ 前端" 这段从三处对齐塌缩为单一真相（Java 版给不了）。

---

## 10. 落地路线

| 阶段 | 内容 | 产出 |
|------|------|------|
| 0 | 抽语言无关 spec + 跨语言一致性测试骨架 | `docs/spec/`（事件/状态机/reducer 规则）+ 黄金用例 |
| 1 | `@ctx_weft/shared`：事件 / 协议 / DTO 类型（含 `kind` 判别） | 前端可立即引用 |
| 2 | `@ctx_weft/core`：events / state / control / loop（零 I/O，最干净） | 内存实现 + reducer 通过一致性测试 |
| 3 | core：assembler / orchestrator（含 blackboard / reopen） | 端到端 `runSingleTask` 用 MockLLM 跑通 |
| 4 | `@ctx_weft/host`：Fastify + Drizzle（持久化 / 恢复 / 投影 / 快照） | Postgres 实现 + 崩溃恢复测试 |
| 5 | adapters：LLM（官方 SDK）/ MCP（官方 TS SDK）/ 知识库（pgvector） | 接真实 LLM |
| 6 | 前端切到 `@ctx_weft/host` + `@ctx_weft/shared` | 全栈 TS 闭环 |

**优先级**：先做 `core` 的纯逻辑部分（移植最无痛、收益最快验证），持久化 + 恢复因共享 schema 且自成
闭环，是第二块理想落地点。

---

> 一句话：**core 是零依赖纯逻辑包（驱动循环 + 事件流 + 协议），host 是 Fastify + Drizzle + 三个总线
> 订阅者把它接上真实世界**——与 Python 版同构，只在并发原语、运行时分派、持久化/服务库三处换形态，
> 外加一个"与前端共享类型"的净收益。三份实现的漂移用 spec + 一致性测试约束。
