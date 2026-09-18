from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from mail_archiver.archiver import ArchiveResult
from mail_archiver.pipeline import summarize

LOG = logging.getLogger("mail_archiver")


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.phase = "idle"
        self.logs: list[str] = []
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.error = ""
        self.message = "还没有开始"
        self.started_at = ""
        self.finished_at = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "kind": self.kind,
                "phase": self.phase,
                "logs": list(self.logs[-80:]),
                "saved": self.saved,
                "skipped": self.skipped,
                "failed": self.failed,
                "error": self.error,
                "message": self.message,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def append_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 400:
                self.logs = self.logs[-300:]


class _JobLogHandler(logging.Handler):
    def __init__(self, state: JobState):
        super().__init__(level=logging.INFO)
        self.state = state
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.append_log(self.format(record))
        except Exception:
            pass


class JobRunner:
    def __init__(self) -> None:
        self.state = JobState()
        self._thread: threading.Thread | None = None

    def start(self, kind: str, fn: Callable[[], ArchiveResult]) -> None:
        with self.state.lock:
            if self.state.running:
                raise RuntimeError("正在整理中，请稍候")
            self.state.running = True
            self.state.kind = kind
            self.state.phase = "running"
            self.state.error = ""
            self.state.saved = 0
            self.state.skipped = 0
            self.state.failed = 0
            self.state.message = "正在整理，请稍候…"
            self.state.started_at = datetime.now().isoformat(timespec="seconds")
            self.state.finished_at = ""
            self.state.logs = []
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], ArchiveResult]) -> None:
        handler = _JobLogHandler(self.state)
        logger = logging.getLogger("mail_archiver")
        logger.addHandler(handler)
        try:
            result = fn()
            with self.state.lock:
                self.state.saved = result.saved
                self.state.skipped = result.skipped
                self.state.failed = result.failed
                self.state.phase = "error" if result.failed else "done"
                self.state.message = summarize(result)
                self.state.finished_at = datetime.now().isoformat(timespec="seconds")
            LOG.info("%s", summarize(result))
        except Exception as exc:
            LOG.exception("任务失败")
            with self.state.lock:
                self.state.phase = "error"
                self.state.error = str(exc)
                self.state.message = f"失败：{exc}"
                self.state.finished_at = datetime.now().isoformat(timespec="seconds")
        finally:
            logger.removeHandler(handler)
            with self.state.lock:
                self.state.running = False


RUNNER = JobRunner()
