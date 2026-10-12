"""EventBus 协议的实现们。当前只有 in_process；多进程部署换 Redis Streams 等外部总线。"""

from ctx_weft.providers.events.bus.in_process import InProcessEventBus

__all__ = ["InProcessEventBus"]
