"""Project JSON in the database; never hydrate discarded payloads in Python."""
from __future__ import annotations

import json

from sqlalchemy import Text, case, literal
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal

from ctx_weft.protocols.events import EventReadProjection, ProjectedEvent, ProjectedStoredEvent
from ctx_weft.providers.events.store.projection import projection_tree

from .models import EventModel


class _JsonProjection(ColumnElement):
    inherit_cache = True
    type = Text()
    _traverse_internals = (
        ("source", InternalTraversal.dp_clauseelement),
        ("event_type", InternalTraversal.dp_clauseelement),
        ("specs", InternalTraversal.dp_clauseelement),
        ("tree_key", InternalTraversal.dp_string),
    )

    def __init__(self, source, event_type, specs):
        self.source = source
        self.event_type = event_type
        self.tree_key = json.dumps(specs, sort_keys=True)
        self.specs = literal(self.tree_key)


@compiles(_JsonProjection, "sqlite")
def _sqlite_projection(element, compiler, **kw):
    source = compiler.process(element.source, **kw)
    kind = compiler.process(element.event_type, **kw)
    specs = compiler.process(element.specs, **kw)
    # A flat projection (including a compact snapshot subtree) needs no recursive
    # walk. This keeps the common snapshot read to one native JSON scan.
    trees = json.loads(element.tree_key)
    if all(tree is not None and all(child is None for child in tree.values())
           and '*' not in tree for tree in trees.values()):
        return f"""(SELECT coalesce(json_group_array(json_object(
            'path', json_array(item.key), 'value', json(
                CASE item.type WHEN 'text' THEN json_quote(item.value)
                WHEN 'null' THEN 'null' WHEN 'true' THEN 'true' WHEN 'false' THEN 'false'
                ELSE item.value END))), '[]')
            FROM json_each({source}) AS item
            WHERE item.key IN (SELECT rule.key FROM json_each({specs}) AS types,
                               json_each(types.value) AS rule WHERE types.key = {kind}))"""
    value = ("CASE item.type WHEN 'text' THEN json_quote(item.value) "
             "WHEN 'null' THEN 'null' WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' "
             "ELSE item.value END")
    return f"""(WITH RECURSIVE projected(doc, path, spec, kind) AS (
        SELECT {source}, '[]', coalesce((
            SELECT CASE WHEN type = 'null' THEN 'null' ELSE value END
            FROM json_each({specs}) WHERE key = {kind}), '{{}}'), json_type({source})
        UNION ALL
        SELECT {value}, json_insert(projected.path, '$[#]', item.key),
               coalesce(rule.value, 'null'), item.type
        FROM projected, json_each(projected.doc) AS item, json_each(projected.spec) AS rule
        WHERE projected.kind IN ('object', 'array')
          AND (rule.key = '*' OR (projected.kind = 'object' AND rule.key = item.key))
    )
    SELECT json_group_array(json_object('path', json(path), 'value', json(
        CASE WHEN spec = 'null' THEN doc
             WHEN kind = 'object' THEN '{{}}'
             WHEN kind = 'array' AND json_type(spec, '$."*"') IS NOT NULL THEN '[]'
             ELSE doc END)))
    FROM projected)"""


@compiles(_JsonProjection, "postgresql")
def _postgres_projection(element, compiler, **kw):
    source = compiler.process(element.source, **kw)
    kind = compiler.process(element.event_type, **kw)
    specs = compiler.process(element.specs, **kw)
    return f"""(WITH RECURSIVE projected(doc, path, spec) AS (
        SELECT CAST({source} AS jsonb), '[]'::jsonb,
               coalesce(CAST({specs} AS jsonb) -> {kind}, '{{}}'::jsonb)
        UNION ALL
        SELECT item.value, projected.path || jsonb_build_array(item.pathkey), rule.value
        FROM projected
        CROSS JOIN LATERAL (
            SELECT to_jsonb(key) AS pathkey, key, value
            FROM jsonb_each(CASE WHEN jsonb_typeof(projected.doc) = 'object'
                                THEN projected.doc ELSE '{{}}'::jsonb END)
            UNION ALL
            SELECT to_jsonb(ordinality - 1), NULL::text, value
            FROM jsonb_array_elements(CASE WHEN jsonb_typeof(projected.doc) = 'array'
                                          THEN projected.doc ELSE '[]'::jsonb END)
                 WITH ORDINALITY AS elements(value, ordinality)
        ) AS item
        CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(projected.spec) = 'object'
                                          THEN projected.spec ELSE '{{}}'::jsonb END) AS rule
        WHERE rule.key = '*' OR (jsonb_typeof(projected.doc) = 'object' AND rule.key = item.key)
    )
    SELECT CAST(jsonb_agg(jsonb_build_object('path', path, 'value',
        CASE WHEN spec = 'null'::jsonb THEN doc
             WHEN jsonb_typeof(doc) = 'object' THEN '{{}}'::jsonb
             WHEN jsonb_typeof(doc) = 'array' AND spec ? '*' THEN '[]'::jsonb
             ELSE doc END) ORDER BY jsonb_array_length(path)) AS text)
    FROM projected)"""


def projected_columns(projection: EventReadProjection):
    specs = {kind: projection_tree(paths) for kind, paths in projection.payload_fields_by_type.items()}
    payload = case(
        (EventModel.type.in_(tuple(specs)), _JsonProjection(EventModel.payload_json, EventModel.type, specs)),
        else_=literal("[]"),
    ) if specs else literal("[]")
    return [
        *(column for column in EventModel.__table__.columns
          if column.name not in {"payload_json", "metadata_json"}),
        payload.label("payload_json"),
        (EventModel.metadata_json if projection.include_metadata else literal("{}")).label("metadata_json"),
    ]


def row_to_projected(row) -> ProjectedStoredEvent:
    values = dict(row)
    position = values.pop("position")
    payload = json.loads(values.pop("payload_json"))
    root = {}
    for entry in payload:
        path, value = entry["path"], entry["value"]
        if not path:
            root = value
            continue
        parent = root
        for key in path[:-1]:
            parent = parent[key]
        key = path[-1]
        if isinstance(parent, list):
            parent.extend([None] * (key + 1 - len(parent)))
        parent[key] = value
    payload = root
    values["payload"] = payload
    values["metadata"] = json.loads(values.pop("metadata_json"))
    values["origin"] = values["origin"] or ""
    values["schema_version"] = values["schema_version"] if values["schema_version"] is not None else 1
    return ProjectedStoredEvent(ProjectedEvent(**values), position)
