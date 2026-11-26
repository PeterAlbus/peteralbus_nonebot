"""Durable delegated tasks and coalesced notifications back to 小P."""

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal
from uuid import uuid4

from pydantic import Field

from .json_store import atomic_write_json_model
from .models import ChatEvent, StrictModel


class CodexTask(StrictModel):
    task_id: str
    origin_turn_id: str
    bot_id: str
    trigger: ChatEvent
    instruction: str
    context: Dict[str, Any]
    images: List[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = (
        "queued"
    )
    thread_id: str = ""
    turn_id: str = ""
    progress: List[str] = Field(default_factory=list)
    progress_seq: int = 0
    reported_seq: int = 0
    result: str = ""
    result_truncated: bool = False
    notification: Literal["pending", "sending", "sent", "uncertain"] = "pending"
    notification_error: str = ""
    message_id: str = ""

    @property
    def finished(self) -> bool:
        return self.status not in {"queued", "running"}


Report = Callable[[CodexTask, str], Awaitable[str]]


class CodexTasks:
    def __init__(
        self,
        directory: Path,
        runtime: Any,
        report: Report,
        logger: Any,
        progress_interval: float = 60,
    ) -> None:
        self.directory, self.runtime, self.report = directory, runtime, report
        self.logger, self.progress_interval = logger, progress_interval
        self.tasks: Dict[str, CodexTask] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.released = set()
        self.reporting: Dict[str, asyncio.Task] = {}
        self.next_notice: Dict[str, float] = {}
        self.worker = None
        self.dispatcher = None
        self.wake = asyncio.Event()
        self.started = False

    def save(self, task: CodexTask) -> None:
        atomic_write_json_model(self.directory / task.task_id / "task.json", task)

    async def start(self) -> None:
        if self.started:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.directory.glob("*/task.json"):
            task = CodexTask.model_validate_json(path.read_text(encoding="utf-8"))
            if not task.finished:
                task.status = "interrupted"
                task.result = (
                    "机器人进程中断，无法确认该任务的执行结果。没有重新执行任务。"
                )
            if task.notification == "sending":
                task.notification = "uncertain"
                task.notification_error = "进程在发送结果期间中断，投递结果未知"
            self.tasks[task.task_id] = task
            self.released.add(task.origin_turn_id)
            self.save(task)
        self.started = True
        self.worker = asyncio.create_task(self._work())
        self.dispatcher = asyncio.create_task(self._dispatch())

    async def close(self) -> None:
        self.started = False
        pending = [
            t for t in (self.worker, self.dispatcher, *self.reporting.values()) if t
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in self.tasks.values():
            if not task.finished:
                task.status = "interrupted"
                task.result = "机器人已停止，任务执行被中断。"
            if task.notification == "sending":
                task.notification = "uncertain"
                task.notification_error = "机器人在发送结果期间停止，投递结果未知"
            self.save(task)

    def submit(
        self,
        *,
        origin_turn_id: str,
        bot_id: str,
        trigger: ChatEvent,
        instruction: str,
        context: Dict[str, Any],
        image_paths: List[str],
    ) -> Dict[str, str]:
        if not self.started:
            raise RuntimeError("Codex 后台任务服务尚未启动")
        # One delegation per 小P reply turn; retries cannot submit the work twice.
        for task in self.tasks.values():
            if task.origin_turn_id == origin_turn_id:
                return {"task_id": task.task_id, "status": task.status}
        task_id = uuid4().hex
        folder = self.directory / task_id
        folder.mkdir(mode=0o700)
        images = []
        try:
            for index, source in enumerate(dict.fromkeys(image_paths)):
                path = Path(source)
                target = folder / f"image-{index}{path.suffix}"
                shutil.copyfile(path, target)
                target.chmod(0o600)
                images.append(str(target.resolve()))
            snapshot = json.loads(json.dumps(context, ensure_ascii=False))
            snapshot["task_images"] = images
            task = CodexTask(
                task_id=task_id,
                origin_turn_id=origin_turn_id,
                bot_id=bot_id,
                trigger=trigger.model_copy(deep=True),
                instruction=instruction,
                context=snapshot,
                images=images,
            )
            self.save(task)
        except Exception:
            shutil.rmtree(folder)
            raise
        self.tasks[task_id] = task
        self.next_notice[task_id] = time.monotonic() + self.progress_interval
        self.queue.put_nowait(task_id)
        return {"task_id": task_id, "status": "accepted"}

    def release_turn(self, turn_id: str) -> None:
        if any(t.origin_turn_id == turn_id for t in self.tasks.values()):
            self.released.add(turn_id)
            self.wake.set()

    def mark_sending(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task.notification = "sending"
        self.save(task)

    def record_sent(self, task_id: str, message_id: str) -> None:
        task = self.tasks[task_id]
        task.notification = "sent"
        task.message_id = message_id
        self.save(task)

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            task = self.tasks[task_id]
            try:
                task.status = "running"
                self.save(task)

                async def progress(text: str) -> None:
                    task.progress.append(text[:6000])
                    task.progress = task.progress[-8:]
                    task.progress_seq += 1
                    self.save(task)
                    self.wake.set()

                async def identity(thread_id: str, turn_id: str) -> None:
                    task.thread_id, task.turn_id = thread_id, turn_id
                    self.save(task)

                result = await self.runtime.run(
                    task.task_id,
                    task.instruction,
                    task.context,
                    task.images,
                    progress,
                    identity,
                    requester_user_id=task.trigger.user_id or "未知",
                )
                task.status = (
                    "completed"
                    if result.status == "completed"
                    else ("interrupted" if result.status == "interrupted" else "failed")
                )
                task.result = result.text[:24000]
                task.result_truncated = len(result.text) > 24000
            except asyncio.CancelledError:
                task.status, task.result = (
                    "interrupted",
                    "机器人已停止，任务执行被中断。",
                )
                raise
            except Exception as error:
                task.status = "failed"
                task.result = f"{type(error).__name__}: {str(error)[:4000]}"
                self.logger.warning(
                    "Codex 任务失败: task_id={}, error_type={}",
                    task_id,
                    type(error).__name__,
                )
            finally:
                self.save(task)
                self.queue.task_done()
                self.wake.set()

    async def _dispatch(self) -> None:
        while True:
            self.wake.clear()
            now = time.monotonic()
            for task in list(self.tasks.values()):
                if (
                    task.origin_turn_id not in self.released
                    or task.task_id in self.reporting
                ):
                    continue
                if task.finished:
                    if task.notification != "pending":
                        continue
                elif task.progress_seq <= task.reported_seq:
                    continue
                if now < self.next_notice.get(task.task_id, 0) and not (
                    task.finished and not task.notification_error
                ):
                    continue
                report = asyncio.create_task(self._report(task))
                self.reporting[task.task_id] = report
                report.add_done_callback(
                    lambda _, key=task.task_id: self.reporting.pop(key, None)
                )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _report(self, task: CodexTask) -> None:
        kind = "finished" if task.finished else "progress"
        snapshot = task.model_copy(deep=True)
        try:
            message_id = await self.report(snapshot, kind)
            if kind == "finished":
                self.record_sent(task.task_id, message_id)
            else:
                task.reported_seq = max(task.reported_seq, snapshot.progress_seq)
                self.save(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if kind == "finished":
                task.notification_error = f"{type(error).__name__}: {str(error)[:1000]}"
            if task.notification == "sending":
                task.notification = "uncertain"
            self.save(task)
            self.logger.warning(
                "Codex 通知未完成: task_id={}, kind={}, error_type={}",
                task.task_id,
                kind,
                type(error).__name__,
            )
        finally:
            self.next_notice[task.task_id] = time.monotonic() + self.progress_interval
