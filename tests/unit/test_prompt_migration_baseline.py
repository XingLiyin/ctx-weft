"""Frozen output captured before the message/template compiler migration."""
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.assembler import prompts
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.prompts import render_template_file
from ctx_weft.protocols import IdentityFacet, ImagePart, TextPart


def block(kind, content, **metadata):
    return ContextBlock(id=f"{kind}-{metadata.get('timestamp', '')}", source=kind,
                        kind=kind, target="messages", content=content, priority=1,
                        token_estimate=1, metadata=metadata)


def cases():
    variants = [
        ("act", {}), ("compact", {"compact_scope": "task"}),
        ("compact", {"compact_scope": "agent"}), ("recognize_intent", {}),
    ]
    for purpose in ("observe", "background_observe", "background_recap"):
        for boundary in ("normal", "plain_text", "finish", "finish_park", "interrupt",
                         "dispatch", "max_turns", "context_limit", "actor_done"):
            variants.append((purpose, {"observe_boundary": boundary}))
    for purpose, extra in variants:
        for shape in ("empty", "fresh", "single", "followup", "resume", "interleaved",
                      "legacy", "foreign", "image", "no_role", "bare", "blank_call"):
            task = NS(id="parent", title="Task", description="Description", user_prompt="Ask",
                      user_prompt_in_memory=shape not in ("empty", "fresh"),
                      outputs="Delivered result", unattended=False)
            template = NS(identity={"act": IdentityFacet(text="SOUL")})
            blocks = []
            if shape != "bare":
                blocks += [block("background", "Project"), block("directive", "# Skill\nBody", skill_name="s"),
                           block("guidance", "GUIDANCE"),
                           block("capabilities", "Description", capability_kind="tool",
                                 capability_name="provider__tool", provider_name="provider",
                                 provider_description="Provider", input_schema={"properties": {"q": {"type": "string"}}})]
            if shape != "no_role":
                blocks.append(block("identity", "IDENTITY"))
            if shape not in ("empty", "fresh"):
                content = ([TextPart(text="Ask"), ImagePart(data="image-data", media_type="image/png", source_type="base64")]
                           if shape == "image" else "Ask")
                blocks.append(block("history", content, role="user", type="user_prompt", timestamp="1",
                                    memory_event_id="event-1", task_id=("child" if shape == "foreign" else "" if shape == "legacy" else "parent")))
            if shape in ("followup", "resume", "interleaved", "blank_call"):
                blocks.append(block("history", "" if shape == "blank_call" else "Working", role="assistant",
                                    type="llm_response", timestamp="2", memory_event_id="event-2",
                                    tool_calls=[{"id": "call", "name": "provider__tool", "input": {"q": "x"}}]))
                blocks.append(block("history", "Result", role="tool", timestamp="3", memory_event_id="event-3", tool_call_id="call"))
            if shape in ("followup", "interleaved"):
                blocks.append(block("history", "Follow up", role="user", type="user_prompt", timestamp="4",
                                    memory_event_id="event-4", task_id="parent" if shape == "followup" else "child"))
            if shape == "bare":
                task.title = task.description = ""
                template = None
            if shape == "empty":
                task.title = task.description = task.user_prompt = ""
                blocks = []
            options = dict(extra, subtasks=[{"task_id": "child", "title": "Child", "outcome": "success", "note": "OK"}])
            options["judge_task"] = purpose != "background_recap"
            current_purpose = "observe" if purpose.startswith("background_") else purpose
            request = NS(purpose=current_purpose, intent=None, task=task, template=template,
                         extra=options, token_counter=len)
            key = f"{purpose}/{extra.get('observe_boundary', extra.get('compact_scope', 'default'))}/{shape}"
            yield key, blocks, request


async def capture():
    result = {}
    for key, blocks, request in cases():
        prompt = await DefaultComposer().compose(blocks, request)
        result[key] = dict(system=prompt.system, messages=[dataclasses.asdict(m) for m in prompt.messages],
                           tools=[dataclasses.asdict(t) for t in prompt.tools], token_count=prompt.token_count)
    return result


def restore_legacy_observe_labels(payload, key, locale):
    """Normalize declared role/localization changes and the unified submission protocol.

    Frozen fixtures remain untouched. All content, message structure, submission
    instructions and token counts outside these explicit changes must still match.
    """
    purpose = key.split("/")[0]
    if purpose not in {"observe", "background_observe", "background_recap"}:
        return
    if purpose == "background_recap":
        heading = "## Summary Principles" if locale == "en" else "## 总结原则"
    else:
        heading = "## Evaluation Principles" if locale == "en" else "## 评价原则"

    def restore(text):
        old = text.replace(heading + "\n\n", "## Your Current Role\n\n", 1)
        old = old.replace("control__report_observation", (
            "control__collect_process_report" if purpose == "background_recap"
            else "control__report_task_outcome"))
        old = old.replace(" 不要填写任务判决、失败原因或下一轮提示字段。", "")
        old = old.replace(" Do not fill task verdict, failure reason, or next-step hint fields.", "")
        if purpose == "background_recap":
            fallback = render_template_file(f"compact/role_fallback.{locale}.jinja")
            old = old.replace(fallback, prompts.BLOCK_FALLBACK["compact"], 1)
        payload["token_count"] += len(old) - len(text)  # cases() uses len
        return old

    for message in payload["messages"]:
        if isinstance(message["content"], str):
            message["content"] = restore(message["content"])
        else:
            for part in message["content"]:
                if part.get("type") == "text":
                    part["text"] = restore(part["text"])


@pytest.mark.asyncio
async def test_all_prompts_match_pre_migration_output():
    baseline = json.loads((Path(__file__).parents[1] / "fixtures" / "prompt_migration.json").read_text(encoding="utf-8"))
    actual = await capture()
    assert actual.keys() == baseline.keys()
    for key in actual:
        restore_legacy_observe_labels(actual[key], key, "en")
        if key.split("/")[0] in {"observe", "background_observe"}:
            # Intentional additive change: judge now also receives compact principles.
            # Keep the original fixture frozen and prove every other byte is unchanged.
            addition = ("## Summary Principles\n\n"
                        + render_template_file("compact/role_fallback.en.jinja")
                        + "\n\n---\n\n")
            removed = 0
            for message in actual[key]["messages"]:
                if isinstance(message["content"], str):
                    removed += message["content"].count(addition)
                    message["content"] = message["content"].replace(addition, "")
                else:
                    for part in message["content"]:
                        if part.get("type") == "text":
                            removed += part["text"].count(addition)
                            part["text"] = part["text"].replace(addition, "")
            assert removed == 1, key
            actual[key]["token_count"] -= len(addition)  # these cases use len as token_counter
        assert actual[key] == baseline[key], key


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_observe_entry_merge_preserves_bilingual_requests(locale, monkeypatch):
    """Frozen pre-merge requests; only declared observe label changes are allowed."""
    baseline = json.loads((Path(__file__).parents[1] / "fixtures" / "observe_entry_merge.json")
                          .read_text(encoding="utf-8"))
    load = prompts.load_prompt_template
    monkeypatch.setattr(prompts, "load_prompt_template", lambda name: load(name, locale))
    actual = await capture()
    assert {f"{locale}/{key}" for key in actual} == {
        key for key in baseline if key.startswith(locale + "/")}
    for key, payload in actual.items():
        restore_legacy_observe_labels(payload, key, locale)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        assert digest == baseline[f"{locale}/{key}"], f"{locale}/{key}"
