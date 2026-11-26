import asyncio
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from multi_llm_chat.codex_runtime import CodexResult, CodexRuntime, context_fragments
from multi_llm_chat.codex_tasks import CodexTasks
from multi_llm_chat.models import ChatEvent


def trigger(user_id="123"):
    return ChatEvent(
        event_id="e",
        group_id="g",
        role="user",
        source="qq",
        user_id=user_id,
        content="调查问题",
        sent_at=datetime.now(timezone.utc),
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def test_context_utf8_roundtrip():
    value = {"history": "中文🙂" * 1000}
    chunks = context_fragments(value)
    assert all(
        len(v["value"].encode()) <= 900 and v["kind"] == "untrusted"
        for v in chunks.values()
    )
    assert json.loads("".join(chunks[key]["value"] for key in sorted(chunks))) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["123", "2997592724"])
async def test_protocol_early_events_and_independent_review(tmp_path, user_id):
    script = tmp_path / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        + """import json, sys

def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    if method == 'initialize':
        result = {}
    elif method == 'initialized':
        continue
    elif method == 'thread/start':
        with open('params.json', 'w') as f:
            json.dump(req['params'], f)
        result = {'thread': {'id': 'thread'}}
    elif method == 'turn/start':
        send({'method': 'item/completed', 'params': {'threadId': 'other', 'turnId': 'turn', 'item': {'type': 'agentMessage', 'id': 'bad', 'text': 'wrong'}}})
        send({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'type': 'agentMessage', 'id': 'p', 'phase': 'commentary', 'text': '正在检查'}}})
        send({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'type': 'agentMessage', 'id': 'f', 'phase': 'final_answer', 'text': '检查完成'}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed', 'items': []}}})
        result = {'turn': {'id': 'turn'}}
    send({'id': req['id'], 'result': result})
"""
    )
    script.chmod(0o700)
    progress, identities = [], []

    async def on_progress(text):
        progress.append(text)

    async def on_identity(thread, turn):
        identities.append((thread, turn))

    runtime = CodexRuntime(str(script), tmp_path, timeout=3)
    result = await runtime.run(
        "task", "调查", {}, [], on_progress, on_identity, requester_user_id=user_id
    )
    assert result.text == "检查完成"
    assert progress == ["正在检查"]
    assert identities == [("thread", ""), ("thread", "turn")]
    params = json.loads((tmp_path / "params.json").read_text())
    assert f'取得："{user_id}"' in params["developerInstructions"]
    assert "独立判断危险性" in params["developerInstructions"]
    assert params["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_async_submission_snapshot_release_and_completion(tmp_path):
    gate = asyncio.Event()
    entered = asyncio.Event()
    reports = []

    async def run(*args, requester_user_id):
        assert requester_user_id == "123"
        entered.set()
        await args[4]("阶段一")
        await gate.wait()
        return CodexResult("completed", "证据与结果", "t", "u")

    async def report(task, kind):
        reports.append((kind, task.result))
        return "message"

    manager = CodexTasks(
        tmp_path / "tasks", SimpleNamespace(run=run), report, Mock(), 0.01
    )
    await manager.start()
    try:
        image = tmp_path / "original.png"
        image.write_bytes(b"image")
        context = {"history": ["before"]}
        kwargs = dict(
            origin_turn_id="origin",
            bot_id="bot",
            trigger=trigger(),
            instruction="调查",
            context=context,
            image_paths=[str(image)],
        )
        accepted = manager.submit(**kwargs)
        assert accepted["status"] == "accepted"
        assert manager.submit(**kwargs)["task_id"] == accepted["task_id"]
        context["history"].append("after")
        image.unlink()
        task = manager.tasks[accepted["task_id"]]
        assert task.context["history"] == ["before"]
        from pathlib import Path

        assert Path(task.images[0]).read_bytes() == b"image"
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.03)
        assert not reports
        gate.set()
        await until(lambda: task.finished)
        assert not reports
        manager.release_turn("origin")
        await until(lambda: task.notification == "sent")
        assert reports == [("finished", "证据与结果")]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_restart_marks_unfinished_and_does_not_repeat_uncertain_send(tmp_path):
    runtime = Mock()
    runtime.run = Mock(side_effect=AssertionError("must not replay"))
    reports = []

    async def report(task, kind):
        reports.append(task)
        return "sent"

    manager = CodexTasks(tmp_path, runtime, report, Mock(), 0.01)
    await manager.start()
    data = dict(
        origin_turn_id="o",
        bot_id="b",
        trigger=trigger(),
        instruction="work",
        context={},
        image_paths=[],
    )
    task_id = manager.submit(**data)["task_id"]
    manager.mark_sending(task_id)
    await manager.close()
    restarted = CodexTasks(tmp_path, runtime, report, Mock(), 0.01)
    await restarted.start()
    try:
        await asyncio.sleep(0.03)
        assert restarted.tasks[task_id].status == "interrupted"
        assert restarted.tasks[task_id].notification == "uncertain"
        assert reports == []
        runtime.run.assert_not_called()
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, error",
    [("exit", RuntimeError), ("wait", TimeoutError), ("interactive", RuntimeError)],
)
async def test_runtime_failure_timeout_and_interaction(tmp_path, mode, error):
    script = tmp_path / "codex"
    body = {
        "exit": "import sys; sys.exit(1)",
        "wait": "import time; time.sleep(60)",
        "interactive": "import json, sys, time; sys.stdin.readline(); print(json.dumps({'id': 'request', 'method': 'item/tool/requestUserInput'}), flush=True); time.sleep(60)",
    }[mode]
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(0o700)

    async def callback(*args):
        pass

    runtime = CodexRuntime(str(script), tmp_path, timeout=0.2 if mode == "wait" else 3)
    with pytest.raises(error):
        await runtime.run(
            "task", "work", {}, [], callback, callback, requester_user_id="123"
        )


@pytest.mark.asyncio
async def test_progress_delivered_then_terminal_notification(tmp_path):
    gate = asyncio.Event()
    reports = []

    async def run(*args, **kwargs):
        await args[4]("已找到原因")
        await gate.wait()
        return CodexResult("completed", "验证通过", "t", "u")

    async def report(task, kind):
        reports.append(kind)
        return "message"

    manager = CodexTasks(tmp_path, SimpleNamespace(run=run), report, Mock(), 0.01)
    await manager.start()
    try:
        task_id = manager.submit(
            origin_turn_id="o",
            bot_id="b",
            trigger=trigger(),
            instruction="work",
            context={},
            image_paths=[],
        )["task_id"]
        manager.release_turn("o")
        await until(lambda: reports == ["progress"])
        gate.set()
        await until(lambda: manager.tasks[task_id].notification == "sent")
        assert reports == ["progress", "finished"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_restart_delivers_interrupted_task_without_execution(tmp_path):
    from multi_llm_chat.codex_tasks import CodexTask

    async def run(*args, **kwargs):
        pytest.fail("must not reexecute")

    reports = []

    async def report(task, kind):
        reports.append((task.status, kind))
        return "message"

    manager = CodexTasks(tmp_path, SimpleNamespace(run=run), report, Mock(), 0.01)
    manager.save(
        CodexTask(
            task_id="task",
            origin_turn_id="o",
            bot_id="b",
            trigger=trigger(),
            instruction="work",
            context={},
            status="running",
        )
    )
    await manager.start()
    try:
        await until(lambda: manager.tasks["task"].notification == "sent")
        assert reports == [("interrupted", "finished")]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_failed_progress_does_not_delay_finished_delivery(tmp_path):
    from multi_llm_chat.codex_tasks import CodexTask

    reports = []

    async def report(task, kind):
        reports.append(kind)
        if kind == "progress":
            raise RuntimeError("temporary model error")
        return "message"

    manager = CodexTasks(tmp_path, Mock(), report, Mock(), 60)
    await manager.start()
    try:
        task = CodexTask(
            task_id="task",
            origin_turn_id="o",
            bot_id="b",
            trigger=trigger(),
            instruction="work",
            context={},
            status="running",
            progress_seq=1,
        )
        manager.tasks[task.task_id] = task
        await manager._report(task)
        task.status = "completed"
        task.result = "done"
        manager.release_turn("o")
        await until(lambda: task.notification == "sent")
        assert reports == ["progress", "finished"]
    finally:
        await manager.close()
