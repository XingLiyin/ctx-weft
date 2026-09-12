# Delta Spec: snapshot-recovery

## Purpose

定义快照恢复的一致切面契约：快照必须恰好由不超过某已提交位置的事件生成，全量回放与快照+增量两条恢复路径领域状态等价——堵死「延迟提交的旧 ID 被快照跳过」（H2）。

## ADDED Requirements

### Requirement: 一致切面算法

快照创建 SHALL 固定为：`C = committed_head(session)` → 取最新且 projection_version 匹配、cursor ≤ C 的快照 S（可为空）→ `read_range(S.cursor+1 .. C)` → apply → 保存 `snapshot(view, last_commit_position=C)`。快照触发事件只是「请求做快照」，SHALL NOT 作为快照边界本身；MUST NOT 取无上界的最新 view 再把较早触发事件的位置写成 cursor。

#### Scenario: 延迟提交不再丢失

- **WHEN** 事件 B 已提交并触发快照，事件 A 随后才提交（position 大于 B 之前、提交顺序在后）
- **THEN** 新一次快照/恢复按 position 截断，A 与 B 都出现在快照恢复结果中，与全量回放等价

#### Scenario: 获取 head 后并发提交不混入

- **WHEN** 快照 writer 取得 C 后同会话又有新提交
- **THEN** 快照 blob 不含超过 C 的事件，新事件由下一次 delta 应用一次

### Requirement: 两路恢复等价

对任意会话，全量回放（read_range 全量）与 snapshot(C)+delta(>C) SHALL 产出等价领域状态（session/task/agent/HITL/outputs，不只 task 集合）；两条路径使用同一排序语义（position）。

#### Scenario: 等价性对照

- **WHEN** 同一会话分别走全量与快照增量恢复
- **THEN** 归一化随机 ID/时间戳后，两份投影的业务字段逐项一致

### Requirement: 不可用快照降级

快照损坏、projection_version 不匹配、或 legacy 快照无提交位置时 SHALL 忽略该快照执行有序全量回放并重新创建；MUST NOT 猜测位置继续增量读取。失败的快照创建是性能降级（记录错误、可从日志重建），不是数据丢失。

#### Scenario: 版本不匹配忽略快照

- **WHEN** 持久化中的最新快照 projection_version 与当前代码不匹配
- **THEN** 恢复走全量回放，不使用该快照也不报致命错
