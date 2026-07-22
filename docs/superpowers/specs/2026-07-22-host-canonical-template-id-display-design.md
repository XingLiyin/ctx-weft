# host template_id 全链 canonical 化：展示漂移根治 + session_import 补洞

日期：2026-07-22
状态：已批准（用户确认设计后实施）
前置：方案 A（agent-capability-template-protocol）+ 方案 B（agent-template-provider-
unification）已在两仓 feat/agent-capability-template-protocol 分支完成（未合并）。
本 spec 处理两条终审 ride 项：host 投影 template_id 前缀展示漂移、session_import
导入旧 dump 不做 canonical 化。

## 问题

1. **展示漂移**：方案 A 只在「交给 ctx-weft 的边界」做 canonical（`SessionStartParams`
   两处 + cmd_run），host 自己的记录层没统一——新会话经 ProjectionUpdater 写进
   host 投影（sessions.config_json.template_id）的是规范形 `agent:default`，重启后
   `_entry_from_record` 用它重建 SessionEntry；而重启前内存 entry 是裸 `default`。
   同一会话重启前后展示形态不同。
2. **session_import 洞**：导入升级前导出的会话 dump 时，事件/快照载荷原样落库
   （裸 template_id），而 m010 已打标不重跑——该会话的引擎重放路径（崩溃恢复 /
   冷 HITL / 手动 compact）会 `TemplateNotFoundError`。

## 决策（用户裁定 2026-07-22）

**引擎保持严格，host 全链 canonical 化。** 曾评估的另一路（引擎 TemplateLookup
单 provider 裸 id 回退 + 删 m010 + host 回归全裸）被否——引擎的「边界强制前缀」
语义不动。

推论（用户知情确认）：**m010 必须保留**——引擎重放读 event store 的 template_id，
host 加前缀补不到；删迁移的诉求由本方案放弃。

## 改动点（全在 IpMasterCoworkPy，引擎零改动）

1. **`api/sessions.py` `create_session`**：`template_id` 求值（req 或 store 默认）后
   **立即** `canonical_template_id(...)`；此后 `SessionStartParams.create` 与
   `SessionEntry(...)` 共用同一规范值（params 处原有的包裹随之成为幂等冗余，可去可留，
   以实现整洁为准）。新会话 host 记录从出生即 `agent:<id>`。
2. **`api/models/session.py` `_entry_from_record`**（~946 行）：从 DB 行重建 entry 时
   `template_id=canonical_template_id(...)`——存量 host 行（裸）载入即规范。幂等，
   重启前后展示一致。
3. **resume 边界**（sessions.py 终态→新轮）现有 `canonical_template_id(entry.template_id)`
   保留（entry 已规范时为 no-op）。
4. **`observability/session_import.py`**：导入时对事件载荷顶层 `"template_id"` 键与
   快照 `state_blob_json` 的 `sessions.*.template_id` 应用与 m010 同口径的规范化
   （复用/对齐 `migrations._canonical_tid`），落库即规范——补上「m010 已打标不重跑」
   够不到的导入路径。
5. **不动**：引擎（TemplateLookup/TemplateNotFoundError/严格语义）、
   `canonical_template_id` 助手本身、m010 迁移。

## 可观测变化

会话列表/详情的 `template_id` 统一为 `agent:<id>` 形态（重启前后一致）；模板管理面
（模板选择器/模板 API）仍用裸 id——模板域与会话记录域本就不同，不受影响。

## 测试

1. `_entry_from_record` 给裸 template_id 的 DB 行 → entry 为 `agent:` 前缀；给已
   规范行 → 原样（幂等）。
2. `create_session` 后 SessionEntry.template_id 已是规范形（现有 create_session 测试
   路径上加断言或新小用例）。
3. `session_import` 导入含裸 template_id 的事件 + 快照 dump → 落库后载荷均为
   `agent:` 前缀；已规范载荷幂等不动。
4. 全量对照基线（1 个 test_skills_reference 既有失败）零新增。
