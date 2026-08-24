"""文件系统能力协议层。

把"文件系统操作"这一类 capability 从其他可选工具中分出来，只约定 core 真正依赖的两件事：

  1. 固定的工具名（FS_PROVIDER_NAME 前缀 + FsTool 常量），调用方与实现共用同一份。
  2. SpillSink：CapabilityGateway 截断超长工具输出时，把全文落盘并返回路径——core 因此
     不直接碰文件系统，也不需要知道「落到哪」。

注意：per-session workspace 的指定 / 登记 / 路径锚定**不在协议内**。那是 SpillSink 实现
（ctx_weft.providers.capability_filesystem.FilesystemToolsProvider）与 host 接线的细节：
host 在执行前把工作目录登记给具体 provider，core 全程只通过 SpillSink.spill() 与之交互。
session 结束时的清理走通用的 SessionScopedCapabilityProvider（见 protocols.capability）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ctx_weft.protocols.context import ProviderContext

FS_PROVIDER_NAME = "fs"


class FsTool:
    """文件系统工具的标准 capability id（前缀在协议层钉死，模板/授权按此引用）。"""

    SHELL = f"{FS_PROVIDER_NAME}:shell"
    READ_FILE = f"{FS_PROVIDER_NAME}:read_file"
    WRITE_FILE = f"{FS_PROVIDER_NAME}:write_file"
    GLOB = f"{FS_PROVIDER_NAME}:glob"


class SpillSink(ABC):
    """core 的「落盘 sink」契约：把超长内容落盘到某持久位置，返回落盘路径。

    CapabilityGateway 在工具输出超阈值时调用 spill()，把全文落盘、result 改为
    「截断提示 + 路径 + 预览」。core 不直接碰文件系统、也不知道 workspace——只知道
    「有个 sink 能把内容落盘并返回路径」。该 session 没有可落盘位置时 spill() 应 raise，
    调用方（gateway）据此回退到硬截断。

    实现见 ctx_weft.providers.capability_filesystem.FilesystemToolsProvider。
    """

    @abstractmethod
    async def spill(self, content: str, ctx: ProviderContext, *, name_hint: str = "") -> str:
        ...


BLOB_REF_PREFIX = "blob:"


class BlobStore(ABC):
    """core 的「二进制 sink」契约：存取图片等二进制内容，core 只见 ref。

    与同处的 SpillSink 同形——core 不直接碰存储，只知道「有个 sink 能存能取」。
    宿主侧实现落点也相同（FilesystemToolsProvider 已持有 per-session workspace）。

    put 必须**内容寻址且幂等**：同样的 data 返回同样的 ref，重复调用不重复存。
    这同时给到三件事：写入端去重、重放安全、以及 rehydrate 字节稳定——同一 ref
    每次还原出的 base64 完全一致，Anthropic 的 prompt cache 前缀不会被打碎。

    get 对不存在 / 已回收的 ref 返回 None，**不得 raise**：blob 过期、宿主换机、
    GC 误删都会发生，调用方据此降级为文本占位，绝不因取图失败中断 loop。
    """

    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        ...

    @abstractmethod
    async def get(
        self, ref: str, ctx: ProviderContext
    ) -> "tuple[bytes, str] | None":
        ...


class NullBlobStore(BlobStore):
    """未注册 BlobStore 时的默认实现——保证不接 blob 的宿主行为完全不变。

    put 刻意抛错：Phase 1 内没有任何调用方（外部化在 Phase 3），抛错可在
    Phase 3 接线错误时立刻暴露，而不是静默产出一个假 ref。
    """

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        raise NotImplementedError(
            "No BlobStore registered; register one via "
            "ProviderRegistry.register_blob_store() before externalizing content."
        )

    async def get(
        self, ref: str, ctx: ProviderContext
    ) -> "tuple[bytes, str] | None":
        return None
