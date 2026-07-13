# LLM 检测覆盖「模型可用性」设计

日期：2026-06-24 · 分支：feature-dev · 版本：0.4.x

## 问题

配置 LLM 账号时的「检测/ping」只验证 HTTP 连通性 + 凭据有效性，不验证模型名是否可用。
模型名拼错 / 大小写错 / 该账号下不存在该模型时，检测照样「通过」，要到真正发起对话才报
`LLMCallError`，用户难以定位。

### 证据

- host `src/ipmastercowork/api/llms.py`
  - `POST /ping`（:140）调 `provider.ping(style, api_key, base_url)`，完全不涉及 model。
  - `POST /{name}/ping`（:151）的 `model` query 参数「accepted but ignored」。
  - 失败时返回 `200 + ok:false, latency:0`，**不带任何错误原因**。
- schema `src/ipmastercowork/api/schemas/llms.py:51`：`model ... ignored (connectivity probe)`。
- core `ctx-weft/.../providers/llm/provider.py:220` `ping()` 实质是 `fetch_models()`——
  只拉模型列表证明连通+鉴权，既不带配置的模型名，也不做真实补全，更不交叉核对。
- 前端 `frontend-desktop/src/components/LLMSettingsPage.tsx` 三条检测路径**已经在传 model**
  （新账号表单 `ping({...,model})`、已注册账号 `pingRegistered(name,model)→?model=`、单独检测按钮），
  但三处 `onSuccess` 回调**未检查 `data.ok`**，无脑置 'ok' 显示延迟——即便 host 返回 `ok:false` 也会显示「通过」。

## 目标

让「检测」真正覆盖「配置的模型可用」，且失败时给出可定位的原因。端到端：模型名错 →
检测显示「失败 + 原因」，而非「通过」。

## 非目标

- 不改 `complete()` 流式协议、不改 TaskManager 重试逻辑。
- 不动 `frontend/`（无 LLM 设置 UI，实现时确认）。
- 不引入「交叉核对模型列表」策略（依赖供应商 /models 完整性，易误判；已否决）。

## 策略

**真实最小补全**（用户拍板，最权威）：用配置的模型发一次 `max_tokens=16` 的补全并 drain 流，
成功才算通过。同时验证模型名存在 + 该账号对该模型有调用权限。

`max_tokens` 取 16 而非 1：避开个别推理模型对极小 `max_tokens` 的拒绝（降低误判）。

## 架构（三层）

### 1. core `ctx-weft`（本仓改，记入回灌队列）

`providers/llm/provider.py` 的 `LLMProvider` 新增：

```python
_PROBE_MAX_TOKENS = 16

async def _probe_model(self, adapter: LLMClient, model: str) -> None:
    """用 model 发一次最小补全并 drain。模型名错/无权限 → adapter 抛 LLMCallError。"""
    req = LLMRequest(
        model=model, system="",
        messages=[LLMMessage(role="user", content="ping")],
        max_tokens=_PROBE_MAX_TOKENS,
    )
    async for _ in adapter.complete(req, stream=True):
        pass

async def verify_model(self, style, api_key, base_url, model) -> float:
    """ad-hoc 凭据：构建瞬态 adapter，探测，返回 latency ms。失败抛异常。"""

async def verify_model_for_account(self, name, model) -> float:
    """已注册账号：复用 live adapter，探测，返回 latency ms。账号不存在抛 KeyError。"""
```

- `verify_model` 复用 `_build_adapter`（unsupported style 抛 ValueError）。
- `verify_model_for_account` 复用 `self._adapters[name]`，`get_account(name)` 先校验存在。
- **保留** `ping` / `ping_account`（纯连通探测，host 在 model 未给时回退使用）。

### 2. host `api/llms.py` + `schemas/llms.py`

- `PingResponse` 增 `error: str | None = None`。
- 删除 `PingRequest.model` 的「ignored」注释（改为：检测时用于模型可用性验证）。
- `POST /ping`：`req.model` 非空 → `verify_model(style, key, base_url, model)`，否则 `ping(...)`；
  失败 `except Exception as e` → `PingResponse(ok=False, latency_ms=0, error=_trim(str(e)))`（仍 HTTP 200）。
- `POST /{name}/ping`：`model` query 非空 → `verify_model_for_account(name, model)`，否则 `ping_account(name)`；
  `KeyError → HTTP 404`（与现有一致），其余 `Exception` → `ok:false + error`。
- `_trim`：错误 message 截断到合理长度（如 300 字符）避免过长 body。

### 3. 前端 `frontend-desktop`

- `api/llms.ts`：`PingResponse` 加 `error?: string`。
- `LLMSettingsPage.tsx` 让失败可见：
  - 三处 ping `onSuccess` 改为尊重 `data.ok`：`ok:false` → `state:'error'`，
    `error: data.error || t('llm.connectFailed')`。
  - 「添加模型先 ping 再持久化」(`addMut`)、「新账号表单添加时先 ping」(`addMut`)：
    `ok:false` 时**不**调用 `addModel` / 不入模型列表，把 `data.error` 作为错误展示。

## 数据流

```
点检测 → 前端带 model 调 /ping 或 /{name}/ping
  → host 判 model 在否 → verify_model[_for_account] → _build_adapter → _probe_model
      → adapter.complete(max_tokens=16) drain
          模型错/无权限 → HTTP 4xx → LLMCallError → host 捕获 → ok:false + error
          成功 → ok:true + latency_ms
  → 前端按 data.ok 显示 通过(latency) / 失败(error)
```

## 错误处理

| 情形 | 结果 |
|------|------|
| 连通/鉴权失败、模型不存在、无权限 | `ok:false` + 截断 message（HTTP 200，沿用不抛错约定）|
| `verify_model_for_account` 账号不存在 | HTTP 404（与现有一致）|
| unsupported style（ad-hoc） | `ok:false` + error |

## 测试

- core `tests/unit/test_llm_provider_models.py`：
  - mock adapter：`complete` stub 为产出 chunk（成功）/ 抛 `LLMCallError`（失败）。
  - 断言 `verify_model` 成功返 latency>0、失败抛出；`_probe_model` 传对 model 名与 `max_tokens=16`；
    `verify_model_for_account` 账号不存在抛 KeyError、存在时复用 live adapter。
- host `tests/test_llms_ping_models.py`：
  - `POST /ping` 带 model → 走 verify；模型错 → `ok:false` 且 `error` 非空。
  - `POST /ping` 不带 model → 退回连通探测（既有行为）。
  - `POST /{name}/ping?model=` → 走 verify；不带 model → 退回 `ping_account`；账号不存在 → 404。

## 回灌

core 的 `verify_model` / `verify_model_for_account` / `_probe_model` seam 为本仓新增，
需记入上游回灌队列（wefta→weft 反向：weft→wefta PR）。
