import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from herdr_description.editor import EditorScreen
from herdr_description.editor_async import AsyncEditorSession, MAX_PENDING_CONTROLS


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Backend:
    def __init__(self, path):
        self.path = path
        self.requests = []
        self.threads = []
        self.enabled = True
        self.closed = False
        self.request_id = None
        self.block_kind = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.normalize = None
        self.editor_focused = False
        self.ack_error = False
        self.ack_reply_error = False
        self.last_ack = None
        self.request_reason = "restart"
        self.retired = False

    def __call__(self, payload):
        self.requests.append(payload)
        self.threads.append(threading.get_ident())
        kind = "save" if payload.get("save") else "ack" if "ack_request" in payload else "poll"
        if self.block_kind == kind:
            self.block_kind = None
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Test backend was not released")
        if self.closed:
            return {"closed": True}
        if kind == "ack" and self.ack_error:
            return {"error": "Could not acknowledge the save."}
        if payload.get("remove"):
            self.path.unlink()
            self.closed = True
            return {"closed": True}
        if payload.get("toggle_autosave"):
            self.enabled = not self.enabled
        content = self.path.read_bytes().decode("utf-8")
        result = {}
        save = payload.get("save")
        if save:
            if save["reason"] == "autosave" and not self.enabled:
                result["autosave_skipped"] = True
            elif not save.get("force") and save["expected_digest"] != hashlib.sha256(content.encode()).hexdigest():
                result.update(error="File changed externally.", conflict=True)
            else:
                content = self.normalize(save["content"]) if self.normalize else save["content"]
                self.path.write_bytes(content.encode("utf-8"))
                result["saved"] = True
                if not save.get("defer_ack"):
                    self.request_id = None
        if (
            payload.get("ack_request") is not None
            and payload["ack_request"] == self.request_id and payload.get("modified") is False
        ):
            self.last_ack = payload.get("ack_request")
            self.request_id = None
        result.update({
            "record": {"content": content, "title": "Description"},
            "autosave_supported": True,
            "autosave_enabled": self.enabled,
            "removable": True,
            "write_requested": self.request_id is not None,
            "save_request": {"id": self.request_id, "reason": self.request_reason},
            "editor_focused": self.editor_focused,
            "restart_cancelled": self.last_ack if self.editor_focused else None,
            "retired": self.retired,
        })
        if kind == "ack" and self.ack_reply_error:
            raise OSError("ACK reply was lost after committing.")
        return result

    @property
    def saves(self):
        return [payload["save"] for payload in self.requests if "save" in payload]

    def block(self, kind):
        self.entered.clear()
        self.release.clear()
        self.block_kind = kind


class Screen:
    def getmaxyx(self):
        return 12, 80

    def erase(self):
        pass

    def addstr(self, *args):
        pass

    def move(self, *args):
        pass

    def refresh(self):
        pass

    def redrawwin(self):
        pass


class AsyncEditorSessionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "description.txt"
        self.path.write_text("initial", encoding="utf-8")
        self.clock = Clock()
        self.backend = Backend(self.path)
        self.session = AsyncEditorSession(self.path, self.backend, clock=self.clock)
        self.addCleanup(self.session.shutdown)
        self.addCleanup(self.backend.release.set)
        self.screen = EditorScreen(Screen(), self.session, raw_writer=lambda text: None)
        self.session.tick()
        self.assertTrue(self.session.wait_idle())

    def edit(self, text):
        self.session.buffer.insert(text)
        self.session.autosave()

    def settle(self):
        self.assertTrue(self.session.wait_idle())

    def wait_blocked(self):
        deadline = time.monotonic() + 1
        while not self.backend.entered.wait(0.001) and time.monotonic() < deadline:
            self.session.pump()
        self.assertTrue(self.backend.entered.is_set())

    def test_blocked_poll_leaves_typing_navigation_help_and_draw_responsive(self):
        self.backend.block("poll")
        self.session.tick()
        self.wait_blocked()
        self.screen.handle_key("x")
        self.screen.handle_key("\x1f")
        self.screen.draw()
        self.assertTrue(self.screen.help_open)
        self.assertEqual("xinitial", self.session.buffer.content)
        self.assertFalse(self.backend.release.is_set())
        self.backend.release.set()
        self.settle()
        self.assertEqual("xinitial", self.session.buffer.content)
        self.assertNotIn(threading.get_ident(), self.backend.threads)
        self.assertEqual(1, len(set(self.backend.threads)))

    def test_burst_is_coalesced_and_debounced(self):
        for character in "a" * 400:
            self.edit(character)
        self.settle()
        self.assertEqual([], self.backend.saves)
        self.clock.advance(0.14)
        self.session.pump()
        self.assertEqual([], self.backend.saves)
        self.clock.advance(0.02)
        self.settle()
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual("a" * 400 + "initial", self.path.read_text())
        self.assertTrue(self.backend.saves[0]["defer_ack"])
        self.assertFalse(self.session.buffer.modified)
        self.assertFalse(self.backend.requests[-1]["modified"])

    def test_continuous_typing_flushes_within_maximum_interval(self):
        self.edit("0")
        for unused in range(9):
            self.clock.advance(0.1)
            self.edit("x")
            self.settle()
        self.assertEqual([], self.backend.saves)
        self.clock.advance(0.11)
        self.edit("y")
        self.settle()
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual("0" + "x" * 9 + "yinitial", self.path.read_text())

    def test_stale_save_preserves_newer_text_cursor_selection_and_digest(self):
        self.edit("a")
        self.backend.block("save")
        self.clock.advance(0.16)
        self.session.pump()
        self.wait_blocked()
        self.edit("b")
        self.screen.selection_anchor = (0, 0)
        cursor = self.session.buffer.row, self.session.buffer.column
        self.backend.release.set()
        self.settle()
        self.assertEqual("abinitial", self.session.buffer.content)
        self.assertEqual("ainitial", self.session.buffer.baseline)
        self.assertEqual(cursor, (self.session.buffer.row, self.session.buffer.column))
        self.assertEqual((0, 0), self.screen.selection_anchor)
        self.assertTrue(self.session.buffer.modified)
        self.clock.advance(0.16)
        self.settle()
        self.assertEqual(hashlib.sha256(b"ainitial").hexdigest(), self.backend.saves[-1]["expected_digest"])
        self.assertEqual("abinitial", self.path.read_text())
        self.assertFalse(self.session.buffer.modified)

    def test_manual_flush_during_inflight_save_uses_latest_draft_immediately(self):
        self.edit("a")
        self.backend.block("save")
        self.clock.advance(0.16)
        self.session.pump()
        self.wait_blocked()
        self.edit("b")
        self.session.save()
        self.edit("c")
        self.backend.release.set()
        self.settle()
        self.assertEqual(["ainitial", "abcinitial"], [save["content"] for save in self.backend.saves])
        self.assertEqual("manual", self.backend.saves[-1]["reason"])
        self.assertFalse(self.session.buffer.modified)

    def test_restart_ack_waits_for_newest_draft_after_inflight_save(self):
        self.edit("a")
        self.backend.request_id = 73
        self.backend.block("save")
        self.session.tick()
        self.wait_blocked()
        self.edit("b")
        self.backend.release.set()
        self.settle()
        self.assertEqual(["ainitial", "abinitial"], [save["content"] for save in self.backend.saves])
        acks = [index for index, payload in enumerate(self.backend.requests) if "ack_request" in payload]
        saves = [index for index, payload in enumerate(self.backend.requests) if "save" in payload]
        self.assertEqual(1, len(acks))
        self.assertGreater(acks[0], saves[-1])
        self.assertIsNone(self.backend.request_id)
        self.assertFalse(self.backend.requests[acks[0]]["modified"])

    def test_clean_request_is_acknowledged_without_write(self):
        self.backend.request_id = 5
        self.session.tick()
        self.settle()
        self.assertEqual([], self.backend.saves)
        self.assertIsNone(self.backend.request_id)
        self.assertEqual(5, self.backend.requests[-1]["ack_request"])

    def test_autosave_off_cancels_pending_debounce(self):
        self.edit("draft")
        self.session.toggle_autosave()
        self.settle()
        self.clock.advance(2)
        self.settle()
        self.assertFalse(self.session.autosave_enabled)
        self.assertEqual([], self.backend.saves)
        self.session.save()
        self.settle()
        self.assertEqual("draftinitial", self.path.read_text())

    def test_server_can_skip_an_inflight_autosave_after_mode_change(self):
        self.edit("draft")
        self.backend.block("save")
        self.clock.advance(0.16)
        self.session.pump()
        self.wait_blocked()
        self.backend.enabled = False
        self.backend.release.set()
        self.settle()
        self.assertTrue(self.session.buffer.modified)
        self.assertEqual("", self.session.error)
        self.assertEqual("initial", self.path.read_text())
        self.assertFalse(self.session.autosave_enabled)

    def test_conflict_does_not_retry_unchanged_draft_and_manual_save_prompts(self):
        self.edit("draft")
        self.path.write_text("external", encoding="utf-8")
        self.clock.advance(0.16)
        self.settle()
        for unused in range(5):
            self.clock.advance(1)
            self.session.tick()
            self.settle()
        self.assertEqual(1, len(self.backend.saves))
        self.assertTrue(self.session.conflict)
        self.assertEqual("draftinitial", self.session.buffer.content)
        self.session.conflict = False
        self.screen.handle_key("\x13")
        self.settle()
        self.screen.draw()
        self.assertEqual("overwrite", self.screen.prompt)
        self.screen.handle_key("y")
        self.settle()
        self.assertEqual("draftinitial", self.path.read_text())
        self.assertFalse(self.session.conflict)

    def test_reload_never_discards_text_typed_after_request(self):
        self.edit("draft")
        self.settle()
        self.path.write_text("external", encoding="utf-8")
        self.backend.block("poll")
        self.session.reload()
        self.wait_blocked()
        self.edit("new")
        self.backend.release.set()
        self.settle()
        self.assertEqual("draftnewinitial", self.session.buffer.content)
        self.assertEqual("initial", self.session.buffer.baseline)

    def test_settings_normalization_only_replaces_matching_generation(self):
        self.backend.enabled = False
        self.backend.normalize = str.upper
        self.session.tick()
        self.settle()
        self.edit("a")
        self.backend.block("save")
        self.session.save()
        self.wait_blocked()
        self.edit("b")
        self.backend.release.set()
        self.settle()
        self.assertEqual(["ainitial", "abinitial"], [save["content"] for save in self.backend.saves])
        self.assertEqual("ABINITIAL", self.session.buffer.content)
        self.assertFalse(self.session.buffer.modified)
        self.assertEqual(hashlib.sha256(b"AINITIAL").hexdigest(), self.backend.saves[-1]["expected_digest"])

    def test_remove_waits_for_inflight_save_and_never_recreates_file(self):
        self.edit("a")
        self.backend.block("save")
        self.clock.advance(0.16)
        self.session.pump()
        self.wait_blocked()
        self.edit("b")
        self.screen.handle_key("\x18")
        self.assertTrue(self.screen.running)
        self.backend.release.set()
        self.settle()
        self.assertTrue(self.session.closed)
        self.session.shutdown()
        self.assertFalse(self.path.exists())
        self.assertEqual(1, len(self.backend.saves))
        self.assertTrue(self.backend.requests[-1]["remove"])
        self.assertFalse(self.session._worker.is_alive())

    def test_closed_backend_is_not_flushed_on_shutdown(self):
        self.edit("draft")
        self.backend.closed = True
        self.session.tick()
        self.settle()
        self.session.shutdown()
        self.assertEqual([], self.backend.saves)
        self.assertEqual("initial", self.path.read_text())

    def test_stale_closed_poll_retains_newer_draft_without_recreating_file(self):
        self.backend.block("poll")
        self.session.tick()
        self.wait_blocked()
        self.edit("new")
        self.backend.closed = True
        self.path.unlink()
        self.backend.release.set()
        self.settle()
        self.assertFalse(self.session.closed)
        self.assertEqual("newinitial", self.session.buffer.content)
        self.assertIn("unsaved text is retained", self.session.error)
        self.assertFalse(self.session.save())
        self.clock.advance(2)
        self.session.tick()
        self.session.shutdown()
        self.assertEqual([], self.backend.saves)
        self.assertFalse(self.path.exists())

    def test_restart_ack_seals_edits_but_keeps_help_copy_and_navigation_available(self):
        self.backend.request_id = 9
        self.backend.block("ack")
        self.session.tick()
        self.wait_blocked()
        with self.assertRaisesRegex(RuntimeError, "edits paused"):
            self.session.buffer.insert("LATEST")
        self.screen.handle_key("x")
        self.assertIn("edits paused", self.screen.notice)
        self.assertEqual("initial", self.session.buffer.content)
        self.screen.handle_key("\x1f")
        self.assertTrue(self.screen.help_open)
        self.screen.draw()
        self.screen.handle_key("q")
        self.session.buffer.move_horizontal(3)
        self.screen.selection_anchor = (0, 0)
        self.assertEqual("ini", self.screen.selected_text)
        self.assertTrue(self.screen.copy_selection())
        self.backend.release.set()
        self.settle()
        self.assertTrue(self.session.buffer.editing_error)
        self.backend.editor_focused = True
        self.session.tick()
        self.settle()
        self.assertEqual("", self.session.buffer.editing_error)
        self.edit("resumed")
        self.assertIn("resumed", self.session.buffer.content)

    def test_failed_restart_ack_unseals_only_after_poll_proves_request_still_pending(self):
        self.backend.request_id = 9
        self.backend.ack_error = True
        self.session.tick()
        self.settle()
        self.assertTrue(self.session.buffer.editing_error)
        self.assertIn("acknowledge", self.session.error)
        with self.assertRaisesRegex(RuntimeError, "edits paused"):
            self.session.buffer.insert("new")
        self.session.tick()
        self.settle()
        self.assertEqual("", self.session.buffer.editing_error)
        self.edit("new")
        self.assertEqual("newinitial", self.session.buffer.content)

    def test_lost_restart_ack_reply_keeps_edits_sealed_after_commit(self):
        self.backend.request_id = 9
        self.backend.ack_reply_error = True
        self.session.tick()
        self.settle()
        self.assertIsNone(self.backend.request_id)
        self.assertTrue(self.session.buffer.editing_error)
        self.assertIn("ACK reply was lost", self.session.status)
        self.assertIn("edits paused", self.session.status)
        self.screen.handle_key("x")
        self.assertIn("edits paused", self.screen.notice)
        self.assertEqual("initial", self.session.buffer.content)
        self.session.tick()
        self.settle()
        self.assertTrue(self.session.buffer.editing_error)
        self.backend.editor_focused = True
        self.session.tick()
        self.settle()
        self.assertEqual("", self.session.buffer.editing_error)

    def test_lost_retirement_ack_reply_keeps_edits_sealed_after_commit(self):
        self.backend.request_id = 9
        self.backend.request_reason = "retire"
        self.backend.retired = True
        self.backend.ack_reply_error = True
        self.session.tick()
        self.settle()
        self.assertIsNone(self.backend.request_id)
        self.assertTrue(self.session.buffer.editing_error)
        self.session.tick()
        self.settle()
        with self.assertRaisesRegex(RuntimeError, "edits paused"):
            self.session.buffer.insert("new")
        self.backend.retired = False
        self.session.tick()
        self.settle()
        self.assertEqual("", self.session.buffer.editing_error)

    def test_remove_preserves_preceding_control_order_and_cancels_autosave(self):
        self.backend.block("poll")
        self.session.tick()
        self.wait_blocked()
        self.edit("draft")
        self.session.toggle_autosave()
        self.session.reload()
        self.session.remove()
        self.clock.advance(2)
        self.backend.release.set()
        self.settle()
        self.assertTrue(self.backend.requests[-3]["toggle_autosave"])
        self.assertNotIn("remove", self.backend.requests[-2])
        self.assertTrue(self.backend.requests[-1]["remove"])
        self.assertEqual([], self.backend.saves)
        self.assertTrue(self.session.closed)

    def test_shutdown_flushes_latest_draft_and_joins_worker(self):
        self.edit("draft")
        self.session.shutdown()
        self.assertEqual("draftinitial", self.path.read_text())
        self.assertFalse(self.session._worker.is_alive())

    def test_shutdown_preserves_undo_to_original_while_save_is_inflight(self):
        self.edit("x")
        self.backend.block("save")
        self.clock.advance(0.16)
        self.session.pump()
        self.wait_blocked()
        self.session.buffer.backspace()
        self.session.autosave()
        self.assertFalse(self.session.buffer.modified)
        self.backend.release.set()
        self.session.shutdown()
        self.assertEqual("initial", self.path.read_text())
        self.assertEqual(["xinitial", "initial"], [save["content"] for save in self.backend.saves])

    def test_control_queue_is_bounded_and_reports_overflow(self):
        self.backend.block("poll")
        self.session.tick()
        self.wait_blocked()
        for unused in range(MAX_PENDING_CONTROLS + 2):
            self.session.toggle_autosave()
        self.assertEqual(MAX_PENDING_CONTROLS, len(self.session._controls))
        self.assertIn("busy", self.session.error)
        self.backend.release.set()
        self.settle()


if __name__ == "__main__":
    unittest.main()
