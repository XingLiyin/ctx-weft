# LoomeJ API 接口文档

## 1. 总体说明

- Base URL：`http://localhost:8005`
- Content-Type：`application/json`（SSE 接口除外）
- 版本前缀：`/api/v1`
- 认证：当前版本无认证（生产环境需自行添加）

---

## 2. Session 管理接口

### 2.1 创建并启动 Session

```
POST /api/v1/sessions
```

**请求体**：
```json
{
  "sessionId":   "sess_custom_id",     // 可选，不填则自动生成 sess_xxxxx
  "templateId":  "default_agent",      // 可选，Agent 模板 ID
  "tenantId":    "default",            // 可选，默认 "default"
  "userPrompt":  "帮我写一个 Python 脚本...",  // 必填
  "llmAccount":  "openai_gpt4",        // 可选，覆盖默认 LLM 账号
  "llmModel":    "gpt-4o",             // 可选，覆盖默认模型
  "tokenBudget": 200000,               // 可选，session 总 token 预算（0=不限制）
  "initialTask": {                     // 可选，自定义根任务参数
    "title":       "Write Python Script",
    "description": "...",
    "skillName":   "python_dev"
  }
}
```

**响应**（200）：SessionDict（见第 6 节）

**行为说明**：
1. 创建 Session 记录（sessions 表）
2. 创建根 Task 记录（tasks 表）
3. 在虚拟线程中调用 `LoomeJRuntime.submit()`
4. SSE 流随即可订阅

**日志**：
```
INFO Creating session: templateId='...', llmProvider='...', llmModel='...', hasInitialTask='false'
INFO Session created: sessionId='sess_xxx'
```

---

### 2.2 获取所有 Session

```
GET /api/v1/sessions
```

**响应**（200）：`List<SessionDict>`

---

### 2.3 获取 Session 详情

```
GET /api/v1/sessions/{sessionId}
```

**响应**（200）：SessionDict  
**响应**（404）：`{"error": "Session not found"}`

---

### 2.4 删除 Session

```
DELETE /api/v1/sessions/{sessionId}
```

**行为**：级联删除 tasks、memory_records、core_events 等  
**响应**（204）：空  
**响应**（404）：`{"error": "Session not found"}`

---

### 2.5 中断 Session

```
POST /api/v1/sessions/{sessionId}/interrupt
```

**请求体**：无

**响应**（200）：
```json
{
  "session": { ...SessionDict... },
  "signaled": true
}
```

**行为**：
1. 调用 `LoomeJRuntime.interruptSession(sessionId)`（设置 cancelToken.cancel()）
2. 运行中的 AgentLoop 检测到取消 → 抛出 CancelledException
3. `RUN_CANCELED` 事件发出 → `SseEventTranslator` 将 `entry.status = "CANCELED"`
4. `signaled=true` 表示信号已发出（任务可能还在处理中，非同步等待）

---

### 2.6 恢复中断的 Session

```
POST /api/v1/sessions/{sessionId}/resume
```

**请求体**（可选）：
```json
{
  "llmAccount": "openai_gpt4",  // 可选，切换 LLM 账号
  "llmModel":   "gpt-4o"        // 可选，切换模型
}
```

**响应**（200）：SessionDict  
**条件**：session.status 必须为 `INTERRUPTED`

**行为**：
1. 调用 `LoomeJRuntime.recoverSession(sessionId)`
2. 从快照或事件日志重建 LoopState
3. 在新虚拟线程中继续执行

---

### 2.7 发送消息（继续对话）

```
POST /api/v1/sessions/{sessionId}/messages
```

**请求体**：
```json
{
  "content":    "继续，帮我优化这段代码",  // 用户消息内容
  "llmAccount": "openai_gpt4",           // 可选
  "llmModel":   "gpt-4o",               // 可选
  "initialTask": { ... }                 // 可选，指定新任务参数
}
```

**响应**（200）：SessionDict

**行为**：
- 如果 session.status 为终态（SUCCEEDED/FAILED）→ 创建新 Run，继续对话
- 如果 session 处于 PAUSED_HITL → 将 content 作为 HITL 响应处理
- 如果 session 处于 RUNNING → 400 Bad Request

**日志**：
```
INFO Sending message to session: sessionId='sess_xxx', hasInitialTask='false'
```

---

### 2.8 获取 Session 的 Task 列表

```
GET /api/v1/sessions/{sessionId}/tasks
```

**响应**（200）：`List<TaskDict>`（见第 7 节）

---

### 2.9 SSE 事件流

```
GET /api/v1/sessions/{sessionId}/stream
```

**响应类型**：`text/event-stream`

**连接行为**：
- 新连接从当前缓冲区最新位置开始推送（不重放历史）
- 若 `sseFinished=true`（session 已完成），立即推送 `done` 事件并关闭
- 连接断开后，前端可重新连接；缓冲区保留

**SSE 事件格式**：
```
data: {"type":"text_delta","delta":"Hello"}\n\n
data: {"type":"done","final_status":"SUCCEEDED"}\n\n
```

**SSE 事件类型列表**：

| type | 说明 | 主要字段 |
|------|------|---------|
| `llm_prompt` | LLM 提示发送 | source, round_label, system_prompt, messages, tool_names |
| `text_delta` | 流式文本增量 | delta |
| `reasoning_delta` | 流式推理增量 | delta |
| `text_done` | 完整文本（LLM 响应完成） | text, created_at |
| `reasoning_done` | 完整推理 | text, created_at |
| `token_update` | Token 用量更新 | input_tokens_used, output_tokens_used, context_tokens |
| `tool_call` | 工具调用完成 | tool_name, arguments, result, is_error, created_at |
| `control_tool_call` | 控制工具调用 | tool_name, arguments, result, is_error, created_at |
| `task_created` | 任务创建 | task（TaskDict） |
| `task_updated` | 任务状态更新 | task（TaskDict） |
| `daemon_task_created` | 后台任务创建 | task |
| `daemon_task_updated` | 后台任务更新 | task |
| `daemon_llm_prompt` | 后台 LLM 提示 | source, messages 等 |
| `daemon_control_tool_call` | 后台工具调用 | tool_name, arguments, result |
| `session_update` | Session 状态变更 | status |
| `waiting_input` | 等待人工输入（HITL） | input_type, approval_id, prompt, task_title |
| `interrupted` | 任务被中断 | created_at |
| `task_failed` | 任务失败 | error, error_type, will_retry |
| `done` | Session 完成 | final_status |

---

## 3. HITL 接口

### 3.1 审批通过

```
POST /api/v1/hitl/{approvalId}/approve
```

**请求体**：无  
**响应**（200）：`{"status": "approved"}`

### 3.2 拒绝

```
POST /api/v1/hitl/{approvalId}/reject
```

**响应**（200）：`{"status": "rejected"}`

### 3.3 修改后通过

```
POST /api/v1/hitl/{approvalId}/modify
```

**请求体**：
```json
{
  "modifiedArguments": { "key": "new_value" }
}
```

**响应**（200）：`{"status": "modified"}`

---

## 4. Skill Source 接口

### 4.1 注册远程 Skill Source（STDIO 模式）

```
POST /api/v1/skill-sources/stdio
```

**请求体**：
```json
{
  "sourceName":        "my-skills",       // 唯一名称
  "mcpCommand":        "python",          // 启动命令
  "mcpArgs":           ["/path/to/skill_server.py"],
  "mcpEnv":            {"PYTHONPATH": "/app"},
  "mcpTimeout":        30,
  "toolListSkills":    "listSkills",      // 可选，覆盖默认工具名
  "toolLoadSkillMd":   "loadSkillMd",
  "toolGetSkillFiles": "getSkillFiles",
  "toolLoadSkillRef":  "loadSkillReference",
  "toolExecSkillScript": "execSkillScript"
}
```

**响应**（200）：SkillSourceInfo

### 4.2 注册远程 Skill Source（HTTP 模式）

```
POST /api/v1/skill-sources/http
```

**请求体**：
```json
{
  "sourceName": "remote-skills",
  "mcpUrl":     "http://skill-server:8090",
  "mcpTimeout": 30
}
```

### 4.3 注销 Skill Source

```
DELETE /api/v1/skill-sources/{sourceName}
```

### 4.4 列出所有 Skill Sources

```
GET /api/v1/skill-sources
```

---

## 5. MCP 工具管理接口

### 5.1 列出 MCP Servers

```
GET /api/v1/mcp/servers
```

### 5.2 注册 MCP Server（STDIO）

```
POST /api/v1/mcp/servers/stdio
```

**请求体**：
```json
{
  "name":    "filesystem-mcp",
  "command": "npx",
  "args":    ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
  "timeout": 30
}
```

### 5.3 注销 MCP Server

```
DELETE /api/v1/mcp/servers/{name}
```

---

## 6. SessionDict 结构

```json
{
  "sessionId":      "sess_abc123",
  "status":         "RUNNING",
  "goal":           "Write a Python script to process CSV",
  "userPrompt":     "帮我写一个处理 CSV 的脚本",
  "templateId":     "default_agent",
  "llm_provider":   "openai_gpt4",
  "llmModel":       "gpt-4o",
  "tokenBudget":    200000,
  "inputTokens":    1500,
  "outputTokens":   800,
  "contextTokens":  1500,
  "tasks":          [ ...TaskDict array... ],
  "createdAt":      "2026-06-08T10:00:00Z",
  "updatedAt":      "2026-06-08T10:01:30Z"
}
```

---

## 7. TaskDict 结构

```json
{
  "id":              "task_xyz789",
  "sessionId":       "sess_abc123",
  "parentTaskId":    null,
  "kind":            "REASONING",
  "status":          "ACTIVE",
  "title":           "Write CSV Processor",
  "description":     "Create a Python script...",
  "userPrompt":      "帮我写...",
  "processReport":   null,
  "skillName":       "python_dev",
  "creatorAgentId":  "agent_001",
  "assignedAgentId": "agent_001",
  "result":          null,
  "outputs":         {},
  "error":           null,
  "settings":        {},
  "createdAt":       "2026-06-08T10:00:00Z",
  "updatedAt":       "2026-06-08T10:01:30Z"
}
```

---

## 8. 错误响应格式

```json
{
  "error":   "Session not found",
  "code":    "SESSION_NOT_FOUND",
  "details": "Session 'sess_xxx' does not exist"
}
```

**常见 HTTP 状态码**：
- 200：成功
- 204：删除成功（无响应体）
- 400：请求参数错误（session 状态不允许操作等）
- 404：资源不存在
- 409：冲突（重复注册等）
- 500：服务器内部错误
