"""Prompt editing primitives. No task, memory, or loop semantics live here."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ctx_weft.core.utils.content import content_with_prefix, content_with_suffix
from ctx_weft.protocols import LLMMessage

if TYPE_CHECKING:
    from ctx_weft.core.assembler.prompts import PromptTemplate


@dataclass(frozen=True)
class MessageRef:
    value: str


@dataclass(frozen=True)
class MessageView:
    ref: MessageRef
    role: str


class PromptDocument:
    """Owns message copies and stable references; edits never mutate the input."""

    def __init__(self, messages: Iterable[LLMMessage] = (), *, refs: Iterable[MessageRef] | None = None):
        self.system = ""
        self._messages = deepcopy(list(messages))
        self._refs = list(refs) if refs is not None else [
            MessageRef(f"message:{i}") for i in range(len(self._messages))]
        if len(self._refs) != len(self._messages) or len(set(self._refs)) != len(self._refs):
            raise ValueError("message references must be unique and match the messages")
        self._serial = 0
        self.trace: list[dict[str, Any]] = []

    @property
    def refs(self) -> tuple[MessageRef, ...]:
        return tuple(self._refs)

    @property
    def messages(self) -> list[LLMMessage]:
        return deepcopy(self._messages)

    def _index(self, ref: MessageRef) -> int:
        try:
            return self._refs.index(ref)
        except ValueError:
            raise KeyError(f"unknown message reference: {ref}") from None

    def _new_ref(self) -> MessageRef:
        while True:
            ref = MessageRef(f"generated:{self._serial}")
            self._serial += 1
            if ref not in self._refs:
                return ref

    def set_system(self, text: str) -> None:
        self.system = text

    def wrap(self, ref: MessageRef, *, before: str = "", after: str = "") -> None:
        index = self._index(ref)
        message = self._messages[index]
        content = message.content
        if before:
            content = content_with_prefix(content, before)
        if after:
            content = content_with_suffix(content, after)
        self._messages[index] = replace(message, content=content)
        if before or after:
            self.trace.append(dict(operation="decorate", ref=ref.value, before=before, after=after))

    def decorate(self, template: PromptTemplate, *, bindings: Mapping[str, Iterable[MessageRef]],
                 context: Mapping[str, Any]) -> None:
        targets: dict[MessageRef, set[str]] = {}
        for slot, refs in bindings.items():
            for ref in refs:
                self._index(ref)
                targets.setdefault(ref, set()).add(slot)
        # Render every patch before applying any; failures cannot leave half an edit.
        patches = []
        for ref, message in zip(self._refs, self._messages, strict=True):
            slots = frozenset(targets.get(ref, ()))
            view = MessageView(ref, message.role)
            patches.append((ref, slots, template.before(view, slots, context),
                            template.after(view, slots, context)))
        for ref, slots, before, after in patches:
            self.wrap(ref, before=before, after=after)
            if slots:
                self.trace.append(dict(operation="binding", ref=ref.value, slots=sorted(slots),
                                       template=template.name))

    def insert_before(self, ref: MessageRef, message: LLMMessage) -> MessageRef:
        return self._insert(self._index(ref), message)

    def insert_after(self, ref: MessageRef, message: LLMMessage) -> MessageRef:
        return self._insert(self._index(ref) + 1, message)

    def _insert(self, index: int, message: LLMMessage) -> MessageRef:
        ref = self._new_ref()
        self._refs.insert(index, ref)
        self._messages.insert(index, deepcopy(message))
        self.trace.append(dict(operation="insert", ref=ref.value, index=index))
        return ref

    def append(self, message: LLMMessage, *, merge_with_last: bool = False,
               separator: str = "\n\n") -> MessageRef | None:
        if (message.content == "" and not message.tool_calls and not message.tool_call_id
                and message.reasoning_content is None):
            return None
        last = self._messages[-1] if self._messages else None
        if (merge_with_last and last is not None and last.role == message.role == "user"
                and not last.tool_calls and not message.tool_calls
                and not last.tool_call_id and not message.tool_call_id
                and message.reasoning_content is None):
            if not isinstance(message.content, str):
                raise TypeError("merged append requires text; insert multipart messages explicitly")
            ref = self._refs[-1]
            self.wrap(ref, after=separator + message.content)
            self.trace.append(dict(operation="merge", ref=ref.value))
            return ref
        return self._insert(len(self._messages), message)

    def preview(self):
        """Read-only structured preview; no tool provider or model is invoked."""
        from dataclasses import asdict

        return dict(system=self.system, messages=[
            dict(ref=ref.value, message=asdict(message))
            for ref, message in zip(self._refs, self._messages, strict=True)
        ], trace=deepcopy(self.trace))
