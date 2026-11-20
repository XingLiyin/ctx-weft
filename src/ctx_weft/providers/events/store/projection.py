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
    EventValueSource,
    StoredEventValue,
)


def validate_value_page(sources, through_position, after, limit):
    if (not sources or any(not isinstance(kind, str) or not kind
                          or not isinstance(source, EventValueSource) for kind, source in sources.items())
            or type(through_position) is not int or through_position < 0
            or type(limit) is not int or limit <= 0
            or after is not None and (not isinstance(after, StoredEventValue)
                or type(after.position) is not int or not 0 < after.position <= through_position
                or not isinstance(after.value, str) or not after.value)):
        raise ValueError("Invalid distinct value page")


def event_string_value(event, source):
    value = event.payload
    for key in source.path:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value:
        value = getattr(event, source.fallback_field, None) if source.fallback_field else None
    return value if isinstance(value, str) and value else None


def validate_read_selection(payload_equals, event_ids=()):
    """Validate generic read predicates equally for all storage backends."""
    if not isinstance(event_ids, tuple) or not all(isinstance(key, str) and key for key in event_ids):
        raise ValueError("event_ids requires a tuple of nonempty strings")
    if payload_equals is not None:
        path, expected = payload_equals
        if (not isinstance(path, tuple) or not path
                or not all(isinstance(key, str) and key for key in path)
                or not isinstance(expected, str)):
            raise ValueError("payload_equals requires a nonempty string path and a string value")


def payload_matches(payload, predicate):
    if predicate is None:
        return True
    path, expected = predicate
    value = payload
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return isinstance(value, str) and value == expected


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
