import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

from .editor import EditorSession


AUTOSAVE_DELAY = 0.15
AUTOSAVE_MAX_DELAY = 1.0
MAX_PENDING_CONTROLS = 16


@dataclass(frozen=True)
class _Job:
    kind: str
    generation: int
    content: str
    payload: str
    flush_version: int = 0
    request_id: object = None


def _run_worker(callback, jobs, completions):
    while True:
        job = jobs.get()
        if job is None:
            return
        try:
            response = callback(json.loads(job.payload))
            if not isinstance(response, dict):
                raise ValueError("Invalid response from the description plugin.")
        except Exception as error:
            response = {"error": str(error) or type(error).__name__}
        completions.put((job, response))


class AsyncEditorSession(EditorSession):
    def __init__(self, file_path, poll, clock=time.monotonic):
        super().__init__(file_path, poll)
        self._clock = clock
        self._generation = 0
        self._observed_content = self.buffer.content
        self._last_edit_at = None
        self._first_dirty_at = None
        self._sequence = 0
        self._controls = deque()
        self._flush = None
        self._pending_request = None
        self._pending_request_reason = None
        self._sealed_request = None
        self._sealed_reason = None
        self._detached = False
        self._poll_requested = False
        self._clean_ack = False
        self._removing = False
        self._shutting_down = False
        self._stopped = False
        self.pending_prompt = None
        self._inflight = None
        self._jobs = queue.Queue(maxsize=1)
        self._completions = queue.Queue(maxsize=1)
        self._worker = threading.Thread(
            target=_run_worker, args=(poll, self._jobs, self._completions),
            name="description-backend", daemon=False,
        )
        self._worker.start()

    def _observe(self):
        content = self.buffer.content
        if content == self._observed_content:
            return
        self._observed_content = content
        self._generation += 1
        self._last_edit_at = self._clock()
        if self.buffer.modified and self._first_dirty_at is None:
            self._first_dirty_at = self._last_edit_at
        elif not self.buffer.modified:
            self._first_dirty_at = None
            self._clean_ack = True

    def _request_flush(self, reason, force=False):
        self._sequence += 1
        if self._flush is not None:
            reason = "manual" if self._flush[1] == "manual" else reason
            force = force or self._flush[2]
        self._flush = (self._sequence, reason, force)

    def _enqueue_control(self, kind):
        if self.closed or self._stopped or self._removing or self._detached:
            return False
        if kind == "reload" and self.buffer.editing_error:
            return False
        if len(self._controls) >= MAX_PENDING_CONTROLS:
            self.error = "The editor is busy; try the command again."
            return False
        self._observe()
        self._sequence += 1
        self._controls.append((self._sequence, kind, self._generation, self.buffer.content))
        self.pump()
        return True

    def tick(self):
        if not self.closed and not self._shutting_down and not self._detached:
            self._poll_requested = True
        self.pump()

    def autosave(self):
        self._observe()
        self.pump()
        return self.autosave_enabled and self.buffer.modified and not self.closed

    def save(self, request_id=None, force=False, reason="manual"):
        if self.closed or self._stopped or self._removing or self._detached or self.buffer.editing_error:
            return False
        self._observe()
        if force:
            self.pending_prompt = None
        if request_id is not None:
            self._pending_request = request_id
            self._pending_request_reason = reason
            if request_id not in self._handled_requests:
                self._handled_requests.append(request_id)
        self._request_flush(reason, force)
        self.pump()
        return True

    def toggle_autosave(self):
        if not self.autosave_supported:
            return False
        return self._enqueue_control("toggle")

    def reload(self):
        return self._enqueue_control("reload")

    def remove(self):
        if self.closed or self._stopped or self._removing or self._detached or not self.can_remove:
            return False
        self._observe()
        self._removing = True
        self._flush = None
        self._poll_requested = False
        self._clean_ack = False
        self.pump()
        return False

    def _dispatch(self, kind, payload, generation=None, content=None, flush_version=0):
        job = _Job(
            kind, self._generation if generation is None else generation,
            self.buffer.content if content is None else content,
            json.dumps({"editor": "simple", "async": True, **payload}), flush_version,
            payload.get("ack_request"),
        )
        self._inflight = job
        self._jobs.put_nowait(job)

    def _autosave_due(self):
        if (
            not self.autosave_enabled or not self.buffer.modified or self._shutting_down
            or (self.buffer.content, self.buffer.digest) == self._autosave_attempted
            or self._last_edit_at is None
        ):
            return False
        now = self._clock()
        return (
            now - self._last_edit_at >= AUTOSAVE_DELAY
            or self._first_dirty_at is not None
            and now - self._first_dirty_at >= AUTOSAVE_MAX_DELAY
        )

    def _dispatch_next(self):
        if self._inflight is not None or self.closed or self._stopped or self._detached:
            return
        if self._controls and (
            self._removing or self._flush is None or self._controls[0][0] < self._flush[0]
        ):
            unused, kind, generation, content = self._controls.popleft()
            payload = {"modified": self.buffer.modified}
            if kind == "toggle":
                payload["toggle_autosave"] = True
            self._dispatch(kind, payload, generation, content)
            return
        if self._removing:
            self._dispatch("remove", {"remove": True})
            return
        if self._flush is not None and not self.buffer.modified:
            self._flush = None
            self._clean_ack = True
        if self._flush is not None or self._autosave_due():
            version, reason, force = self._flush or (0, "autosave", False)
            request = {
                "content": self.buffer.content,
                "expected_digest": self.buffer.digest,
                "request_id": self._pending_request,
                "reason": reason,
                "defer_ack": True,
            }
            if force:
                request["force"] = True
            self._autosave_attempted = (self.buffer.content, self.buffer.digest)
            self._first_dirty_at = self._clock()
            self._dispatch("save", {"modified": True, "save": request}, flush_version=version)
            return
        if self._controls:
            self._dispatch_next()
            return
        if self._clean_ack and not self.buffer.modified:
            payload = {"modified": False}
            if self._pending_request is not None:
                payload["ack_request"] = self._pending_request
                if self._pending_request_reason in ("restart", "retire"):
                    self._sealed_request = self._pending_request
                    self._sealed_reason = self._pending_request_reason
                    self.buffer.editing_error = "Closing editor; edits paused. Refocus to resume."
            self._clean_ack = False
            self._poll_requested = False
            self._dispatch("ack", payload)
            return
        if self._poll_requested:
            self._poll_requested = False
            self._dispatch("poll", {"modified": self.buffer.modified})

    def _metadata(self, response):
        if "removable" in response:
            self.can_remove = response["removable"] is True
        was_enabled = self.autosave_enabled
        if "autosave_supported" in response:
            self.autosave_supported = response["autosave_supported"] is True
        if "autosave_enabled" in response:
            self.autosave_enabled = response["autosave_enabled"] is True
        self.autosave_enabled = self.autosave_supported and self.autosave_enabled
        if self.autosave_enabled and not was_enabled:
            self._autosave_attempted = None
        record = response.get("record") or {}
        self.title = record.get("title", self.title)
        self.focused = bool(record.get("focused", self.focused))
        if response.get("error"):
            self.error = str(response["error"])
            self.conflict = bool(response.get("conflict", self.conflict))
        return record

    def _apply(self, job, response):
        self._inflight = None
        if response.get("closed"):
            if (
                not self._removing and not response.get("discarded")
                and job.generation != self._generation and self.buffer.modified
            ):
                self._detached = True
                self.can_remove = False
                self.autosave_enabled = False
                self.autosave_supported = False
                self.buffer.editing_error = ""
                self.error = "Description was removed or detached; unsaved text is retained."
            else:
                self.closed = True
            self._controls.clear()
            self._flush = None
            self._poll_requested = False
            return
        record = self._metadata(response)
        content = record.get("content")
        unchanged = self._generation == job.generation and self.buffer.content == job.content
        if job.kind == "save":
            if response.get("saved"):
                saved_content = content if isinstance(content, str) else job.content
                if unchanged and self.buffer.content != saved_content:
                    self.buffer.replace(saved_content)
                    self._observed_content = saved_content
                    self._generation += 1
                else:
                    self.buffer.baseline = saved_content
                self.error = ""
                self.conflict = False
                self.pending_prompt = None
                self.saved = True
                self._clean_ack = True
                if not self.buffer.modified:
                    self._first_dirty_at = None
                    self._flush = None
            else:
                if self._flush is not None and self._flush[0] == job.flush_version:
                    self._flush = None
                if not response.get("error") and not response.get("autosave_skipped"):
                    self.error = self.error or "The description could not be saved."
                request = json.loads(job.payload)["save"]
                if self.conflict and request["reason"] == "manual":
                    self.pending_prompt = "overwrite"
        elif job.kind == "remove":
            self._removing = False
            self._autosave_attempted = (self.buffer.content, self.buffer.digest)
            self.error = self.error or "The description could not be removed."
        elif job.kind == "reload":
            if response.get("error") or not isinstance(content, str):
                self.error = self.error or "The description could not be reloaded."
            elif unchanged:
                self.buffer.replace(content)
                self._observed_content = content
                self._generation += 1
                self._first_dirty_at = None
                self._autosave_attempted = None
                self._clean_ack = True
                self.error = ""
                self.conflict = False
                self.pending_prompt = None
        elif isinstance(content, str) and unchanged and not self.buffer.modified:
            self.buffer.reload_if_clean(content)
            self._observed_content = self.buffer.content
        rejected_ack = bool(
            job.kind == "ack" and job.request_id is not None
            and response.get("write_requested")
            and (response.get("save_request") or {}).get("id") == job.request_id
        )
        if rejected_ack:
            self.error = self.error or "Save acknowledgement was not accepted; retry save."
        if job.kind == "ack" and not response.get("error") and not rejected_ack:
            if job.request_id == self._pending_request:
                self._pending_request = None
                self._pending_request_reason = None
        if response.get("warning"):
            self.error = str(response["warning"])
        request = response.get("save_request") or {}
        request_id = request.get("id")
        if self._sealed_request is not None and not response.get("error") and (
            self._sealed_reason == "restart" and response.get("restart_cancelled") == self._sealed_request
            or self._sealed_reason == "retire" and response.get("retired") is False
            or response.get("write_requested") and request_id is not None
        ):
            self._sealed_request = None
            self._sealed_reason = None
            self.buffer.editing_error = ""
        if (
            response.get("write_requested") and request_id not in self._handled_requests
            and not response.get("error") and not self._removing
        ):
            self._handled_requests.append(request_id)
            self._pending_request = request_id
            self._pending_request_reason = request.get("reason")
            self._request_flush(request.get("reason") or "blur")

    def pump(self):
        self._observe()
        while True:
            try:
                job, response = self._completions.get_nowait()
            except queue.Empty:
                break
            self._apply(job, response)
        self._dispatch_next()

    def wait_idle(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.pump()
            if self._inflight is None:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.001)

    def shutdown(self):
        if self._stopped:
            return
        self._shutting_down = True
        self._poll_requested = False
        self._observe()
        if not self.closed and not self._removing and not self._detached and (
            self.buffer.modified or self._inflight is not None and self._inflight.kind == "save"
        ):
            self._request_flush("manual")
        try:
            self.wait_idle(timeout=None)
        finally:
            self._jobs.put(None)
            self._worker.join()
            self._stopped = True

    @property
    def status(self):
        if self.buffer.editing_error:
            return " | ".join(item for item in (self.error, self.buffer.editing_error) if item)
        if not self.error and self.buffer.modified and (
            self._flush is not None or self._inflight is not None and self._inflight.kind == "save"
        ):
            return "Saving…"
        return super().status
