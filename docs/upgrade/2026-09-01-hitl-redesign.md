# 升级须知 · HITL 机制重做（2026-09-01）· **破坏性**

## 先读这一条

**host 自己写的 authorizer 与工具 provider，只要碰过旧的 `HitlManager`，都必须重写。
没有兼容路径，没有 deprecation 期，没有 shim。**

判断你是否受影响：在你的代码里搜这些名字。命中任意一个，那个类就必须改：

```
HitlManager            hitl.request(         hitl.wait(
hitl.approve(          hitl.answer(          hitl.reject(
AuthorizationDecision(..., defer=True)       runtime.hitl_manager
on_cold_resolve        was_hot               pending_hitl
```

这些**全部已删除**。它们不会报 `DeprecationWarning`，会直接 `AttributeError` / `TypeError`。

不受影响的：只返回 `allowed=True/False` 的 authorizer（基础 `Authorizer` 签名一个字没变）、
从不问人的工具 provider。这是刻意的——不需要 HITL 的实现看不到任何 HITL 概念。

---

## 1. 怎么改：authorizer

**旧**（authorizer 自己登记、自己等——那正是耦合的源头）：

```python
class MyAuthorizer(Authorizer):
    def __init__(self, hitl):            # ← host 必须把 core 的实例递进来
        self._hitl = hitl

    async def authorize(self, cap, ctx, arguments=None, *, tool_call_id=""):
        hid = await self._hitl.request(form="approval", session_id=ctx.session_id, ...)
        approval = await self._hitl.wait(hid)          # ← 自己等
        return AuthorizationDecision(allowed=approval.accepted, message=approval.message)
```

**新**（authorizer 零依赖：**声明**需要人，然后返回）：

```python
from ctx_weft.protocols import (
    AuthorizationDecision, Authorizer, HitlAsk, ToolResultDelivery,
    HITL_FORM_APPROVAL, HITL_OUTCOME_ACCEPTED,
)

class MyAuthorizer(Authorizer):                        # 不再需要构造参数
    async def authorize(self, cap, ctx, arguments=None, *, tool_call_id=""):
        return AuthorizationDecision(allowed=False, needs_human=HitlAsk(
            form=HITL_FORM_APPROVAL,
            delivery=ToolResultDelivery(tool_call_id=tool_call_id),
            subject_id=cap.id,
            prompt=f"Allow {cap.name}?",
            proposal=arguments,
        ))

    # 可选接口 HumanGatedAuthorizer：gateway 拿到人的决定后喂回来给你解释
    async def on_decision(self, cap, ctx, arguments, tool_call_id, decision):
        return AuthorizationDecision(
            allowed=decision.outcome == HITL_OUTCOME_ACCEPTED,
            message=decision.message,
            modified_arguments=decision.modified_arguments,
        )
```

要点：

- `needs_human` 非 `None` 时 `allowed` **必须**为 `False`。gateway 先判 `allowed`，
  安全不变式不依赖 `needs_human`。
- 声明了 `needs_human` 却没有 `on_decision` = 契约违例：gateway 出一条错误 tool result，
  **绝不**放行、也不静默降级成「把答复当结果」。
- 登记、等待、被驱逐时抛 `HitlPark`——全部归 gateway。你不做，也不能做。

## 2. 怎么改：会问人的工具 provider

`invoke` 是异步生成器，所以让出是 **yield 一个事件**，不是 return 一个值：

```python
from ctx_weft.protocols import CapabilityEvent, HitlAsk, ToolCapabilityProvider, ToolResultDelivery
# 两个**可选**能力接口留在 protocols.capability（它们是 Protocol，不进顶层导出）
from ctx_weft.protocols.capability import HumanResumable   # authorizer 侧对应 HumanGatedAuthorizer


class DeployTool(ToolCapabilityProvider, HumanResumable):
    async def invoke(self, cap_id, args, ctx):
        plan = await self.compute_plan(args)                 # ← 见下面的警告
        yield CapabilityEvent("needs_human", {"ask": HitlAsk(
            form="question",
            delivery=ToolResultDelivery(tool_call_id=ctx.extra["tool_call_id"]),
            prompt=f"确认部署 {plan.summary}？",
            resume_state=plan.to_dict(),
        )})
        # 到此为止：gateway 见 needs_human 即停止消费并关闭本流，其后 yield 的不可见。

    async def resume(self, ask_id, decision, resume_state, ctx):
        plan = Plan.from_dict(resume_state)
        yield CapabilityEvent("result", {"content": await self.apply(plan, decision)})
```

> ⚠ **`resume_state` 只省掉热重入。** 热窗口被驱逐或进程崩了之后，续跑走冷路径：
> `ReconcileStep` 重新调 `invoke`，上面的 `compute_plan(args)` **会再跑一遍**。
> 所以 `needs_human` 之前的工作必须**幂等或便宜**；不可重复的副作用（扣款、发工单、
> 真正的部署动作）只能放进 `resume()`。详见设计文档 §2.2 / §9.3。

答复直接就是结果（`ask_user` 那一类）时置 `reply_as_result=True`，重入根本不发生，
也就不需要实现 `HumanResumable`。

## 3. host API 变更

| 旧 | 新 |
|----|----|
| `runtime.hitl_manager.list_pending(session_id)` | `runtime.list_pending_hitl(session_id=None) -> list[HitlRequestView]` |
| `runtime.hitl_registry.list_pending(...)`（返回 core 内部的 `PendingHitl`） | 同上——`PendingHitl` **不出 core** |
| `hitl.approve(id, message, modified_arguments)` | `runtime.reply_to_hitl(HitlReply(hitl_id=…, outcome="accepted", modified_arguments=…))` |
| `hitl.answer(id, text)` | `runtime.reply_to_hitl(HitlReply(hitl_id=…, outcome="accepted", message=text))` |
| `hitl.reject(id, message)` | `runtime.reply_to_hitl(HitlReply(hitl_id=…, outcome="rejected", message=…))` |
| 应答后 host 自己判 `was_hot` 再调 `recover_session` | **不再需要**：`reply_to_hitl` 内部按 `claimed` 分流并驱动冷续跑 |

`reply_to_hitl` 对已终局的请求返回 `None`（幂等，不重复续跑）。
应答时想指定续跑用的模型：`HitlReply(..., resume_hint=ResumeHint(llm_account=…, llm_model=…))`。

契约类型现在都从 `ctx_weft.protocols` 直接导出：
`HitlAsk` / `HitlDecision` / `HitlReply` / `HitlRequestView` / `ResumeHint` /
`Delivery` 及其三个成员 `ToolResultDelivery` / `UserTurnDelivery` / `NoResumeDelivery`。

## 4. 投影 / 会话状态变更

- **`SessionPausedHitl` 不再被发出。** 会话暂停态改由 `HitlOpened` 直接驱动。
  reducer 保留旧分支只为读存量日志。
- 暂停态的判据是 **`delivery`，不是 `form`**：`UserTurnDelivery`（等用户说话，无面板）
  → `PAUSED`；`ToolResultDelivery` / `NoResumeDelivery` → `PAUSED_HITL`。
  **host 自定义的 form 因此第一次能拿到正确行为**（旧实现按字面量 `form == "wait"` 判定）。
- `HitlResolved` → 会话回 `RUNNING`（仅当仍处暂停态，不覆盖已到的终态）。
- pending 列表的真相源是 `HitlRegistry`，**`RunStateView` 里不再另存一份**。
  host 若曾读 `view.pending_hitl`，改读 `runtime.list_pending_hitl(...)`。

## 5. 事件模型变更

8 个事件收敛到 2 个：

| 旧 | 新 |
|----|----|
| `HitlRequired` + `SessionPausedHitl` | `HitlOpened` |
| `HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` / `HitlCancelled` | `HitlResolved`（`outcome` 是开放 `str`） |

- 新事件的载荷：`HitlOpened` 带 `hitl_id / form / delivery / stage / tool_call_id /
  invocation_key / resume_state / reply_as_result / prompt / detail / fields / proposal / subject_id`；
  `HitlResolved` 带 `hitl_id / outcome / claimed`，以及可选的 `message` / `modified_arguments`。
- **存量事件不重写。** `fold_hitl_snapshot` 双读新旧两套（设计文档 §12.3），在途请求跨升级
  边界仍能恢复。旧事件折出来的记录 `invocation_key` 为 `""`（通配），行为与升级前逐条同构。
- host 若对 5 个旧终态事件做过订阅 / 投影 / 审计，**新流量里不会再有它们**——请改订
  `HitlResolved` 并按 `outcome` 分支。

## 6. 延伸阅读

- 权威设计：`docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`（§10 = 不变式清单）
- 运行时说明：`docs/spec/07-hitl-suspend-resume.md`
- 授权侧交界：`docs/spec/05-authz-and-hitl.md`
