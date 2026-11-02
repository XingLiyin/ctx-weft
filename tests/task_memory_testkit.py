"""Assertions for persisted labels and the real shared history program."""

from dataclasses import replace
from types import SimpleNamespace

from ctx_weft.core.assembler.builder import compile_history, history_records
from ctx_weft.core.assembler.legacy_task_memory import normalize_legacy_tasks
from ctx_weft.core.assembler.prompts import runtime_prompt


def render_task_record(kind, *, locale="en", **values):
    facts = dict(
        kind=kind, task_id="", parent_task_id="", title="", description="", outcome="", body=""
    )
    facts.update(values)
    return runtime_prompt("task_memory_record", locale=locale, record=facts).strip()


def project_task_history(records):
    return [
        replace(
            r, content=runtime_prompt("task_memory_record", record=r.metadata["legacy_task_record"])
        )
        if r.metadata.get("legacy_task_record")
        else r
        for r in normalize_legacy_tasks(records)
    ]


def history_view(records):
    records = list(records)
    prompt = compile_history(records)
    alternatives = [
        SimpleNamespace(lean=messages) for messages in prompt.metadata["alternatives"].values()
    ]
    return history_records(records), alternatives
