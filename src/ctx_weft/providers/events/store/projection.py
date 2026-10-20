"""Generic JSON field projection, shared by storage implementations."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from typing import Any

from ctx_weft.protocols.events import (
    Event,
    EventReadProjection,
    ProjectedEvent,
    ProjectedStoredEvent,
    StoredEvent,
)


def projection_tree(paths: tuple[tuple[str, ...], ...]) -> dict | None:
    """None is a retained subtree; an empty dict selects no fields."""
    if () in paths:
        return None
    tree: dict[str, Any] = {}
    for path in paths:
        node = tree
        for index, key in enumerate(path):
            if key in node and node[key] is None:
                break
            if index == len(path) - 1:
                node[key] = None
            else:
                node = node.setdefault(key, {})
    def validate(node):
        if node is None:
            return
        if "*" in node and len(node) > 1:
            raise ValueError("A wildcard cannot have named siblings in a projection")
        for child in node.values():
            validate(child)
    validate(tree)
    return tree


def project_value(value: Any, tree: dict | None) -> Any:
    if tree is None:
        return deepcopy(value)
    if "*" in tree:
        child = tree["*"]
        if isinstance(value, dict):
            return {key: project_value(item, child) for key, item in value.items()}
        if isinstance(value, list):
            return [project_value(item, child) for item in value]
    elif isinstance(value, dict):
        return {key: project_value(value[key], child)
                for key, child in tree.items() if key in value}
    # Preserve malformed/null/scalar intermediate values, rather than inventing
    # an object that the full reader would never have seen.
    return deepcopy(value)


def project_stored_event(row: StoredEvent, projection: EventReadProjection) -> ProjectedStoredEvent:
    event = row.event
    tree = projection_tree(projection.payload_fields_by_type.get(str(event.type), ()))
    envelope = {f.name: getattr(event, f.name) for f in fields(Event)
                if f.name not in {"payload", "metadata"}}
    return ProjectedStoredEvent(ProjectedEvent(
        **envelope, payload=project_value(event.payload, tree),
        metadata=deepcopy(event.metadata) if projection.include_metadata else {},
    ), row.position)
