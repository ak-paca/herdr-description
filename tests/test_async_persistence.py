import hashlib
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_description.api import HerdrError
from herdr_description.config import toggle_autosave
from herdr_description.editor import EditorScreen
from tests.test_app import AppHarness


class DeferredPersistenceTest(AppHarness, unittest.TestCase):
    def editor_poll(self, record, **payload):
        return self.poll(record, editor="simple", **payload)

    def deferred_save(self, record, content, base, request=None, reason="blur"):
        return self.editor_poll(record, modified=True, save={
            "content": content,
            "expected_digest": hashlib.sha256(base.encode("utf-8")).hexdigest(),
            "request_id": request["id"] if request else None,
            "reason": reason,
            "defer_ack": True,
        })

    def test_blur_requires_clean_ack_after_latest_deferred_write(self):
        record = self.create("tab")
        self.editor_poll(record, modified=True)
        self.focus("shell-1")
        request = self.editor_poll(record, modified=True)["save_request"]
        first = self.deferred_save(record, "first draft", record["content"], request)
        self.assertTrue(first["saved"])
        self.assertTrue(first["write_requested"])
        self.assertTrue(self.section(record)["modified"])
        dirty = self.editor_poll(record, modified=True, ack_request=request["id"])
        self.assertEqual(dirty["save_request"], request)
        self.assertEqual(self.stored(record).content, "first draft")
        latest = "first draft\n日本語 👋\n"
        self.deferred_save(record, latest, "first draft", request)
        self.assertTrue(self.section(record)["modified"])
        response = self.editor_poll(record, modified=False, ack_request=request["id"])
        self.assertFalse(response["write_requested"])
        self.assertFalse(self.section(record)["modified"])
        self.assertEqual(self.stored(record).content, latest)
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_restart_waits_through_older_write_dirty_ack_and_latest_write(self):
        record = self.create("pane")
        self.editor_poll(record, modified=True)
        old_pane = self.section(record)["pane_id"]
        steps = []

        def respond(_):
            request = self.section(record)["request"]
            self.assertEqual(request["reason"], "restart")
            self.assertFalse(request["return_focus"])
            self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
            if not steps:
                response = self.deferred_save(record, "first", record["content"], request, "restart")
                self.assertTrue(response["write_requested"])
            elif len(steps) == 1:
                response = self.editor_poll(record, modified=True, ack_request=request["id"])
                self.assertEqual(response["save_request"], request)
            elif len(steps) == 2:
                response = self.deferred_save(record, "latest\n界", "first", request, "restart")
                self.assertTrue(response["write_requested"])
            else:
                response = self.editor_poll(record, modified=False, ack_request=request["id"])
                self.assertFalse(response["write_requested"])
            steps.append(response)

        with patch("herdr_description.controller.time.sleep", side_effect=respond):
            self.app.restart_editors(timeout=1, poll_interval=0.001)
        self.assertEqual(len(steps), 4)
        self.assertEqual(self.stored(record).content, "latest\n界")
        self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_manual_focus_restore_waits_for_clean_ack_and_ignores_stale_ack(self):
        self.api.add_pane("tab-1", "shell-2")
        record = self.create("tab")
        editor = self.section(record)["pane_id"]
        self.focus("shell-2")
        self.focus(editor)
        self.editor_poll(record, modified=True)
        self.app.action("save")
        old_request = self.editor_poll(record, modified=True)["save_request"]
        self.deferred_save(record, "saved once", record["content"], old_request, "manual")
        self.assertEqual(self.api.focused_pane_id, editor)
        self.app.action("save")
        new_request = self.editor_poll(record, modified=True)["save_request"]
        self.assertNotEqual(new_request["id"], old_request["id"])
        older = self.deferred_save(record, "older completion", "saved once", old_request, "manual")
        self.assertEqual(older["save_request"], new_request)
        self.assertTrue(self.section(record)["modified"])
        stale = self.editor_poll(record, modified=False, ack_request=old_request["id"])
        self.assertEqual(stale["save_request"], new_request)
        self.assertEqual(self.api.focused_pane_id, editor)
        dirty = self.editor_poll(record, modified=True, ack_request=new_request["id"])
        self.assertEqual(dirty["save_request"], new_request)
        self.deferred_save(record, "saved latest", "older completion", new_request, "manual")
        self.assertEqual(self.api.focused_pane_id, editor)
        response = self.editor_poll(record, modified=False, ack_request=new_request["id"])
        self.assertFalse(response["write_requested"])
        self.assertEqual(self.api.focused_pane_id, "shell-2")

    def test_retired_editor_remains_until_latest_deferred_write_is_acknowledged(self):
        self.api.add_pane("tab-1", "remains")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("pane", pane_id="shell-1")
        old_pane = self.section(record)["pane_id"]
        self.editor_poll(record, modified=True)
        self.api.move_pane("shell-1", "tab-2")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": {"workspace_id": "space-1", "tab_id": "tab-2", "pane_id": "shell-1"},
        })
        request = self.editor_poll(record, modified=True)["save_request"]
        self.deferred_save(record, "moved first", record["content"], request)
        self.editor_poll(record, modified=True, ack_request=request["id"])
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.deferred_save(record, "moved latest", "moved first", request)
        unacknowledged = self.editor_poll(record, modified=False)
        self.assertEqual(unacknowledged.get("save_request"), request)
        stale = self.editor_poll(record, modified=False, ack_request=request["id"] - 1)
        self.assertEqual(stale.get("save_request"), request)
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        response = self.editor_poll(record, modified=False, ack_request=request["id"])
        self.assertTrue(response["closed"])
        self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertEqual(self.stored(record).content, "moved latest")
        self.assertEqual(self.poll(record, "tab-2")["record"]["description_id"], record["description_id"])

    def test_disabled_autosave_rejects_stale_deferred_batch_without_losing_draft(self):
        record = self.create("tab")
        self.editor_poll(record, modified=True)
        toggle_autosave(self.app.config_dir)
        response = self.deferred_save(record, "queued draft", record["content"], reason="autosave")
        self.assertTrue(response["autosave_skipped"])
        self.assertFalse(response["saved"])
        self.assertFalse(response["autosave_enabled"])
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.stored(record).content, record["content"])
        self.assertEqual(Path(record["file_path"]).read_text(), record["content"])

    def test_failed_or_conflicting_deferred_write_retains_request_and_dirty_state(self):
        record = self.create("tab")
        self.editor_poll(record, modified=True)
        self.focus("shell-1")
        request = self.editor_poll(record, modified=True)["save_request"]
        with patch.object(self.app.store, "save_file", side_effect=OSError("disk unavailable")):
            failed = self.deferred_save(record, "unsaved", record["content"], request)
        self.assertIn("error", failed)
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.section(record)["request"], request)
        Path(record["file_path"]).write_text("external update", encoding="utf-8")
        conflict = self.deferred_save(record, "unsaved", record["content"], request)
        self.assertTrue(conflict["conflict"])
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.section(record)["request"], request)
        self.assertEqual(Path(record["file_path"]).read_text(), "external update")

    def test_clean_async_retired_editor_requests_ack_before_native_close(self):
        self.api.add_pane("tab-1", "remains")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("pane", pane_id="shell-1")
        old_pane = self.section(record)["pane_id"]
        self.api.move_pane("shell-1", "tab-2")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": {"workspace_id": "space-1", "tab_id": "tab-2", "pane_id": "shell-1"},
        })
        response = self.editor_poll(record, modified=False, **{"async": True})
        self.assertFalse(response.get("closed"))
        self.assertTrue(response["retired"])
        self.assertEqual(response["save_request"]["reason"], "retire")
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        closed = self.editor_poll(
            record, modified=False, ack_request=response["save_request"]["id"], **{"async": True},
        )
        self.assertTrue(closed["closed"])
        self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))

    def test_refocus_after_clean_ack_persistently_cancels_restart_even_after_focus_leaves(self):
        record = self.create("tab")
        old_pane = self.section(record)["pane_id"]
        observed = []

        def respond(_):
            request = self.editor_poll(record, modified=False, **{"async": True})["save_request"]
            self.assertEqual(request["reason"], "restart")
            response = self.editor_poll(record, modified=False, ack_request=request["id"], **{"async": True})
            self.assertFalse(response["write_requested"])
            self.focus(old_pane)
            self.focus("shell-1")
            response = self.editor_poll(record, modified=False, **{"async": True})
            self.assertEqual(response["restart_cancelled"], request["id"])
            observed.append(request["id"])

        with patch("herdr_description.controller.time.sleep", side_effect=respond):
            with self.assertRaises(HerdrError):
                self.app.restart_editors(timeout=1, poll_interval=0.001)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.section(record)["restart_cancelled"], observed[0])
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertEqual(self.api.focused_pane_id, "shell-1")


class AsyncPersistenceIntegrationTest(AppHarness, unittest.TestCase):
    def pump_until(self, session, predicate):
        deadline = time.monotonic() + 5
        pause = threading.Event()
        while time.monotonic() < deadline:
            session.pump()
            if predicate():
                return
            pause.wait(0.002)
        self.fail("Asynchronous editor did not reach the expected state")

    def make_session(self, record, backend):
        from herdr_description.editor_async import AsyncEditorSession

        session = AsyncEditorSession(Path(record["file_path"]), backend)
        self.addCleanup(session.shutdown)
        session.tick()
        self.pump_until(session, lambda: session.autosave_enabled and session.can_remove)
        return session

    def test_restart_waits_for_latest_buffer_while_older_write_is_in_flight(self):
        record = self.create("tab")
        old_pane = self.section(record)["pane_id"]
        entered = [threading.Event(), threading.Event()]
        releases = [threading.Event(), threading.Event()]
        ack_entered = threading.Event()
        ack_release = threading.Event()
        saves = []
        responses = []

        def backend(payload):
            if payload.get("save"):
                index = len(saves)
                saves.append(payload["save"].copy())
                if index < len(entered):
                    entered[index].set()
                    if not releases[index].wait(5):
                        raise TimeoutError("Blocked test save was not released")
            if payload.get("ack_request"):
                ack_entered.set()
                if not ack_release.wait(5):
                    raise TimeoutError("Blocked restart acknowledgement was not released")
            response = self.app.section_tick("tab-1", record["description_id"], payload)
            responses.append((payload, response))
            return response

        session = self.make_session(record, backend)
        self.addCleanup(ack_release.set)
        for release in releases:
            self.addCleanup(release.set)
        session.buffer.insert("older")
        session.save()
        self.pump_until(session, entered[0].is_set)
        older = session.buffer.content
        session.buffer.insert("\n最新 👋")
        session.autosave()
        latest = session.buffer.content
        restart_done = threading.Event()
        restart_errors = []

        def restart():
            try:
                self.app.restart_editors(timeout=4, poll_interval=0.002)
            except Exception as error:
                restart_errors.append(error)
            finally:
                restart_done.set()

        restart_thread = threading.Thread(target=restart, daemon=True)
        restart_thread.start()

        def finish_restart():
            for release in releases:
                release.set()
            ack_release.set()
            restart_thread.join(5)

        self.addCleanup(finish_restart)
        self.pump_until(session, lambda: (self.section(record).get("request") or {}).get("reason") == "restart")
        request = self.section(record)["request"]
        releases[0].set()
        self.pump_until(session, entered[1].is_set)
        self.assertFalse(restart_done.is_set())
        self.assertEqual(self.stored(record).content, older)
        self.assertEqual(session.buffer.content, latest)
        self.assertTrue(session.buffer.modified)
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.section(record)["request"], request)
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        releases[1].set()
        self.pump_until(session, ack_entered.is_set)
        screen = Mock()
        screen.getmaxyx.return_value = (14, 80)
        editor = EditorScreen(screen, session, raw_writer=Mock())
        for key in ("!", "\r", "\x7f", "\x0b"):
            editor.handle_key(key)
            self.assertEqual(session.buffer.content, latest)
            self.assertIn("edits paused", editor.notice)
        editor.draw()
        self.assertTrue(any("edits paused" in str(call.args) for call in screen.addstr.call_args_list))
        self.assertFalse(restart_done.is_set())
        ack_release.set()
        self.pump_until(session, restart_done.is_set)
        restart_thread.join(timeout=1)
        self.assertEqual(restart_errors, [])
        self.assertEqual([save["content"] for save in saves], [older, latest])
        self.assertEqual(self.stored(record).content, latest)
        self.assertFalse(session.buffer.modified)
        self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertEqual(self.api.focused_pane_id, "shell-1")
        self.assertTrue(any(
            payload.get("ack_request") == request["id"] and payload.get("modified") is False
            for payload, response in responses
        ))

    def test_remove_cancels_queued_edits_and_waits_for_in_flight_save(self):
        record = self.create("tab")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def backend(payload):
            calls.append(payload)
            if payload.get("save"):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Blocked test save was not released")
            return self.app.section_tick("tab-1", record["description_id"], payload)

        session = self.make_session(record, backend)
        self.addCleanup(release.set)
        session.buffer.insert("in flight")
        session.save()
        self.pump_until(session, entered.is_set)
        session.buffer.insert(" queued newer")
        session.autosave()
        session.remove()
        session.buffer.insert(" after removal request")
        session.autosave()
        self.assertFalse(session.closed)
        self.assertTrue(Path(record["file_path"]).exists())
        release.set()
        self.pump_until(session, lambda: session.closed)
        session.shutdown()
        self.assertFalse(Path(record["file_path"]).exists())
        self.assertIsNone(self.stored(record))
        self.assertEqual(sum(bool(payload.get("save")) for payload in calls), 1)
        removal = next(index for index, payload in enumerate(calls) if payload.get("remove"))
        self.assertFalse(any(payload.get("save") for payload in calls[removal + 1:]))

    def test_delayed_clean_poll_for_missing_record_preserves_newer_draft_and_native_editor(self):
        record = self.create("tab")
        old_pane = self.section(record)["pane_id"]
        should_block = threading.Event()
        entered = threading.Event()
        release = threading.Event()

        def backend(payload):
            if should_block.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Blocked test poll was not released")
            return self.app.section_tick("tab-1", record["description_id"], payload)

        session = self.make_session(record, backend)
        self.addCleanup(release.set)
        should_block.set()
        session.tick()
        self.pump_until(session, entered.is_set)
        self.app.store.delete(self.app.session, record["kind"], record["target_id"])
        session.buffer.insert("newer local text\n界")
        session.autosave()
        latest = session.buffer.content
        release.set()
        self.pump_until(session, lambda: session.closed or session.error)
        self.assertFalse(session.closed)
        self.assertTrue(session.buffer.modified)
        self.assertEqual(session.buffer.content, latest)
        self.assertTrue(session.error)
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertIn(old_pane, {pane["pane_id"] for pane in self.api.panes})

    def assert_typing_allowed_during_ack(self, reason, fail_ack=False):
        record = self.create("tab")
        old_pane = self.section(record)["pane_id"]
        entered = threading.Event()
        release = threading.Event()
        failed = []

        def backend(payload):
            if payload.get("ack_request"):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Blocked acknowledgement was not released")
            response = self.app.section_tick("tab-1", record["description_id"], payload)
            if payload.get("ack_request") and fail_ack and not failed:
                failed.append(payload["ack_request"])
                return {"error": "acknowledgement reply unavailable"}
            return response

        session = self.make_session(record, backend)
        self.addCleanup(release.set)
        session.buffer.insert("first saved text")
        session.autosave()
        if reason == "manual":
            self.app.action("save")
        else:
            self.focus("shell-1")
        session.tick()
        self.pump_until(session, entered.is_set)
        screen = Mock()
        screen.getmaxyx.return_value = (14, 80)
        editor = EditorScreen(screen, session, raw_writer=Mock())
        before = session.buffer.content
        editor.handle_key("!")
        latest = session.buffer.content
        self.assertNotEqual(latest, before)
        self.assertEqual(session.buffer.editing_error, "")
        self.assertNotIn("edits paused", editor.notice)
        release.set()
        if fail_ack:
            self.pump_until(session, lambda: session.error == "acknowledgement reply unavailable")
            self.assertEqual(session.buffer.editing_error, "")
            editor.handle_key("?")
            self.assertNotEqual(session.buffer.content, latest)
            latest = session.buffer.content
            self.assertNotIn("edits paused", editor.notice)
        self.pump_until(session, lambda: not session.buffer.modified and self.stored(record).content == latest)
        self.assertEqual(session.buffer.content, latest)
        self.assertIn(old_pane, {pane["pane_id"] for pane in self.api.panes})
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))

    def test_manual_ack_keeps_typing_enabled_and_preserves_newer_draft(self):
        self.assert_typing_allowed_during_ack("manual")

    def test_blur_ack_keeps_typing_enabled_and_preserves_newer_draft(self):
        self.assert_typing_allowed_during_ack("blur")

    def test_manual_ack_error_keeps_typing_enabled_and_preserves_newer_draft(self):
        self.assert_typing_allowed_during_ack("manual", fail_ack=True)

    def test_blur_ack_error_keeps_typing_enabled_and_preserves_newer_draft(self):
        self.assert_typing_allowed_during_ack("blur", fail_ack=True)

    def test_move_waits_for_newer_draft_and_seals_only_final_retirement_ack(self):
        self.api.add_pane("tab-1", "remains")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("pane", pane_id="shell-1")
        old_pane = self.section(record)["pane_id"]
        should_block = threading.Event()
        poll_entered = threading.Event()
        poll_release = threading.Event()
        ack_entered = threading.Event()
        ack_release = threading.Event()

        def backend(payload):
            if should_block.is_set() and not poll_entered.is_set():
                poll_entered.set()
                if not poll_release.wait(5):
                    raise TimeoutError("Blocked retirement poll was not released")
            if payload.get("ack_request"):
                ack_entered.set()
                if not ack_release.wait(5):
                    raise TimeoutError("Blocked retirement acknowledgement was not released")
            return self.app.section_tick("tab-1", record["description_id"], payload)

        session = self.make_session(record, backend)
        self.addCleanup(poll_release.set)
        self.addCleanup(ack_release.set)
        should_block.set()
        session.tick()
        self.pump_until(session, poll_entered.is_set)
        self.api.move_pane("shell-1", "tab-2")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": {"workspace_id": "space-1", "tab_id": "tab-2", "pane_id": "shell-1"},
        })
        session.buffer.insert("newer moved draft 界")
        session.autosave()
        latest = session.buffer.content
        poll_release.set()
        self.pump_until(session, ack_entered.is_set)
        self.assertEqual(self.stored(record).content, latest)
        self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        screen = Mock()
        screen.getmaxyx.return_value = (14, 80)
        editor = EditorScreen(screen, session, raw_writer=Mock())
        editor.handle_key("!")
        self.assertEqual(session.buffer.content, latest)
        self.assertIn("edits paused", editor.notice)
        ack_release.set()
        self.pump_until(session, lambda: session.closed)
        self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))
        self.assertEqual(self.stored(record).content, latest)
        self.assertEqual(self.poll(record, "tab-2")["record"]["content"], latest)

    def assert_restart_ack_error_remains_sealed(self, lost_reply=False, resume=False):
        record = self.create("tab")
        old_pane = self.section(record)["pane_id"]
        failed = []
        error_message = "reply lost after acknowledgement" if lost_reply else "post-commit read failure"

        def backend(payload):
            try:
                if payload.get("ack_request") and not failed:
                    failed.append(payload["ack_request"])
                    if lost_reply:
                        self.app.section_tick("tab-1", record["description_id"], payload)
                        raise OSError(error_message)
                    with patch.object(self.app, "descriptions", side_effect=OSError(error_message)):
                        return self.app.section_tick("tab-1", record["description_id"], payload)
                return self.app.section_tick("tab-1", record["description_id"], payload)
            except OSError as error:
                return {"error": str(error)}

        session = self.make_session(record, backend)
        session.buffer.insert("saved draft 界")
        session.save()
        self.pump_until(session, lambda: session.wait_idle(timeout=0))
        saved = session.buffer.content
        screen = Mock()
        screen.getmaxyx.return_value = (14, 80)
        editor = EditorScreen(screen, session, raw_writer=Mock())
        handled = []

        def exchange():
            session.tick()
            self.pump_until(session, lambda: session.wait_idle(timeout=0))

        def respond(_):
            request = self.section(record)["request"]
            self.assertEqual(request["reason"], "restart")
            exchange()
            self.assertEqual(failed, [request["id"]])
            self.assertEqual(session.error, error_message)
            section = self.section(record)
            self.assertIsNone(section["request"])
            self.assertFalse(section["modified"])
            self.assertEqual(section["restart_acknowledged"], request["id"])
            self.assertIn("edits paused", session.buffer.editing_error)
            editor.handle_key("!")
            self.assertEqual(session.buffer.content, saved)
            self.assertIn("edits paused", editor.notice)
            with self.assertRaisesRegex(RuntimeError, "edits paused"):
                session.buffer.insert("must not be accepted")
            editor.draw()
            self.assertTrue(any("edits paused" in str(call.args) for call in screen.addstr.call_args_list))
            exchange()
            self.assertIn("edits paused", session.buffer.editing_error)
            if resume:
                self.focus(old_pane)
                exchange()
                self.assertEqual(self.section(record)["restart_cancelled"], request["id"])
                self.assertEqual(session.buffer.editing_error, "")
                editor.handle_key("!")
                self.assertNotEqual(session.buffer.content, saved)
                self.assertNotIn("edits paused", editor.notice)
            handled.append(request["id"])

        with patch("herdr_description.controller.time.sleep", side_effect=respond):
            if resume:
                with self.assertRaisesRegex(HerdrError, "refocused"):
                    self.app.restart_editors(timeout=1, poll_interval=0.001)
            else:
                self.app.restart_editors(timeout=1, poll_interval=0.001)
        self.assertEqual(handled, failed)
        if resume:
            latest = session.buffer.content
            self.pump_until(session, lambda: not session.buffer.modified and self.stored(record).content == latest)
            self.assertNotIn({"pane_id": old_pane}, self.methods("pane.close"))
        else:
            self.assertIn({"pane_id": old_pane}, self.methods("pane.close"))
            self.assertEqual(self.stored(record).content, saved)
            self.assertEqual(session.buffer.content, saved)

    def test_post_commit_restart_ack_error_stays_sealed_until_persisted_refocus_cancellation(self):
        self.assert_restart_ack_error_remains_sealed(resume=True)

    def test_lost_restart_ack_reply_stays_sealed_until_parent_safely_closes_editor(self):
        self.assert_restart_ack_error_remains_sealed(lost_reply=True)


if __name__ == "__main__":
    unittest.main()
