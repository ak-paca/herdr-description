import copy
import fcntl
import hashlib
import unittest
from unittest.mock import patch

from herdr_description.api import HerdrError
from tests.test_app import AppHarness, FakeHerdr


class RestartEditorsTest(AppHarness, unittest.TestCase):
    def unlocked(self, app=None):
        app = app or self.app
        with (app.runtime.directory / "controller.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def acknowledge(self, record, tab_id="tab-1", app=None, editor="simple"):
        app = app or self.app
        response = self.poll(record, tab_id, app, editor=editor, modified=False)
        request = response["save_request"]
        self.assertEqual(request["action"], "save")
        self.assertEqual(request["reason"], "restart")
        self.assertFalse(request["return_focus"])
        response = self.poll(
            record, tab_id, app, editor=editor, modified=False, ack_request=request["id"],
        )
        self.assertFalse(response["write_requested"])

    def waiting(self, callback, app=None):
        def respond(_):
            self.unlocked(app)
            callback()
        return patch("herdr_description.controller.time.sleep", side_effect=respond)

    def test_action_dispatches_restart_without_holding_runtime_lock(self):
        with patch.object(self.app, "restart_editors", side_effect=self.unlocked) as restart:
            self.app.action("restart-editors")
        restart.assert_called_once_with()

    def test_restart_replaces_editors_preserving_files_identities_and_user_panes(self):
        self.api.add_pane("tab-1", "shell-2")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-2")]
        for record in records:
            self.write(record, record["kind"] + " saved text")
        old_panes = {self.section(record)["pane_id"] for record in records}
        identities = self.app.runtime.identities()
        stored = self.app.store.export_descriptions()
        files = {record["description_id"]: record["file_path"] for record in records}
        self.api.calls.clear()

        def respond():
            for record in records:
                self.acknowledge(record)

        with self.waiting(respond):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertEqual(self.app.runtime.identities(), identities)
        self.assertEqual(self.app.store.export_descriptions(), stored)
        self.assertEqual({item["pane_id"] for item in self.methods("pane.close")}, old_panes)
        self.assertTrue({"shell-1", "shell-2"} <= {pane["pane_id"] for pane in self.api.panes})
        for record in records:
            section = self.section(record)
            self.assertNotIn(section["pane_id"], old_panes)
            self.assertEqual(section["editor"], "simple")
            self.assertEqual(str(self.app.store.file_path(record["description_id"])), files[record["description_id"]])
            self.assertEqual(self.stored(record).content, record["kind"] + " saved text")
        self.assertTrue(all(item["entrypoint"] == "section" for item in self.methods("plugin.pane.open")))

    def test_restart_saves_dirty_simple_editor_before_closing(self):
        record = self.create("pane")
        self.poll(record, editor="simple", modified=True)
        old_pane = self.section(record)["pane_id"]
        self.api.calls.clear()

        def respond():
            response = self.poll(record, editor="simple", modified=True)
            request = response["save_request"]
            response = self.poll(record, editor="simple", modified=True, save={
                "content": "unsaved draft",
                "expected_digest": hashlib.sha256(b"").hexdigest(),
                "request_id": request["id"],
            })
            self.assertTrue(response["saved"])
            self.assertEqual(self.methods("pane.close"), [])
            self.assertEqual(self.stored(record).content, "unsaved draft")

        with self.waiting(respond):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertEqual(self.methods("pane.close"), [{"pane_id": old_pane}])
        self.assertEqual(self.stored(record).content, "unsaved draft")

    def test_restart_saves_legacy_draft_before_replacing_combined_editor(self):
        record = self.legacy_editor()
        old_view = self.app.runtime.view("tab-1")
        self.api.focused_pane_id = old_view["pane_id"]
        identities = self.app.runtime.identities()
        self.api.calls.clear()

        def respond():
            request = self.app.runtime.view("tab-1")["request"]
            self.assertEqual(request["action"], "save")
            self.assertEqual(request["reason"], "restart")
            self.assertFalse(request["return_focus"])
            focused = self.api.focused_pane_id
            response = self.app.tick("tab-1", {"changes": [{**record, "content": "legacy draft"}]})
            self.assertEqual(len(response["saved"]), 1)
            self.assertEqual(self.methods("pane.close"), [])
            self.assertEqual(self.stored(record).content, "legacy draft")
            self.assertEqual(self.api.focused_pane_id, focused)

        with self.waiting(respond):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertEqual(self.methods("pane.close"), [{"pane_id": old_view["pane_id"]}])
        self.assertEqual(self.app.runtime.view("tab-1")["format"], 2)
        self.assertEqual(self.stored(record).content, "legacy draft")
        self.assertEqual(self.app.runtime.identities(), identities)

    def test_clean_native_editor_can_acknowledge_restart_without_writing_file(self):
        record = self.create("tab")
        view = self.app.runtime.view("tab-1")
        view["sections"][record["description_id"]].pop("editor")
        self.app.runtime.put_view(view)
        stored = self.app.store.export_descriptions()
        with self.waiting(lambda: self.acknowledge(record, editor="vim")):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)
        self.assertEqual(self.app.store.export_descriptions(), stored)
        self.assertEqual(self.section(record)["editor"], "simple")

    def test_timeout_preserves_unresponsive_editor_file_mapping_and_pending_save(self):
        record = self.create("tab")
        self.write(record, "saved text")
        self.poll(record, editor="simple", modified=True)
        old_pane = self.section(record)["pane_id"]
        identities = self.app.runtime.identities()
        stored = self.app.store.export_descriptions()
        self.api.calls.clear()
        with self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.section(record)["pane_id"], old_pane)
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.section(record)["request"]["action"], "save")
        self.assertEqual(self.app.runtime.identities(), identities)
        self.assertEqual(self.app.store.export_descriptions(), stored)

    def test_stalled_legacy_editor_is_kept(self):
        record = self.legacy_editor()
        old_view = self.app.runtime.view("tab-1")
        self.api.calls.clear()
        with self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.app.runtime.view("tab-1")["pane_id"], old_view["pane_id"])
        self.assertIsNotNone(self.stored(record))

    def test_failed_save_keeps_editor_and_saved_file(self):
        record = self.create("tab")
        self.write(record, "saved text")
        self.poll(record, editor="simple", modified=True)
        old_pane = self.section(record)["pane_id"]
        self.api.calls.clear()

        def respond():
            request = self.poll(record, editor="simple", modified=True)["save_request"]
            response = self.poll(record, editor="simple", modified=True, save={
                "content": "unsaved draft",
                "expected_digest": hashlib.sha256(b"saved text").hexdigest(),
                "request_id": request["id"],
            })
            self.assertIn("error", response)

        with patch.object(self.app.store, "save_file", side_effect=OSError("write failed")):
            with self.waiting(respond), self.assertRaises(HerdrError):
                self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.section(record)["pane_id"], old_pane)
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.section(record)["request"]["action"], "save")
        self.assertEqual(self.stored(record).content, "saved text")

    def test_one_stalled_editor_prevents_closing_other_acknowledged_editor(self):
        first = self.create("space")
        second = self.create("tab")
        panes = {self.section(record)["pane_id"] for record in (first, second)}
        self.api.calls.clear()
        acknowledged = False

        def respond():
            nonlocal acknowledged
            if not acknowledged:
                self.acknowledge(first)
                acknowledged = True

        with self.waiting(respond), self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertTrue(panes <= {pane["pane_id"] for pane in self.api.panes})
        self.assertIsNotNone(self.stored(first))
        self.assertIsNotNone(self.stored(second))

    def test_restart_refuses_editor_that_becomes_dirty_after_acknowledgment(self):
        record = self.create("tab")
        self.api.calls.clear()

        def respond():
            self.acknowledge(record)
            self.poll(record, editor="simple", modified=True)

        with self.waiting(respond), self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertTrue(self.section(record)["modified"])

    def test_restart_never_closes_reused_pane_with_different_ownership(self):
        record = self.create("tab")
        pane = self.native_pane(record)
        pane["tokens"] = {"herdr_description": "another-editor"}
        pane["cwd"] = "/tmp/unrelated-shell"
        self.api.calls.clear()
        try:
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        except HerdrError:
            pass
        self.assertEqual(self.methods("pane.close"), [])
        self.assertIn(pane, self.api.panes)
        self.assertIsNotNone(self.stored(record))

    def test_restart_never_closes_last_pane_of_tab(self):
        record = self.create("tab")
        old_pane = self.native_pane(record)
        self.api.remove_pane("shell-1")
        self.api.calls.clear()
        with self.waiting(lambda: self.acknowledge(record)):
            try:
                self.app.restart_editors(timeout=0.005, poll_interval=0.001)
            except HerdrError:
                pass
        self.assertEqual(self.methods("pane.close"), [])
        self.assertIn(old_pane, self.api.panes)
        self.assertIsNotNone(self.stored(record))

    def test_restart_rechecks_ownership_after_waiting_for_save(self):
        record = self.create("tab")
        pane = self.native_pane(record)
        self.api.calls.clear()

        def respond():
            self.acknowledge(record)
            pane["tokens"] = {"herdr_description": "replacement-editor"}
            pane["cwd"] = "/tmp/replacement-shell"

        with self.waiting(respond), self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertIn(pane, self.api.panes)
        self.assertIsNotNone(self.stored(record))

    def test_restart_rechecks_source_panes_after_waiting_for_save(self):
        record = self.create("tab")
        pane = self.native_pane(record)
        self.api.calls.clear()

        def respond():
            self.acknowledge(record)
            self.api.remove_pane("shell-1")

        with self.waiting(respond), self.assertRaises(HerdrError):
            self.app.restart_editors(timeout=0.005, poll_interval=0.001)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertIn(pane, self.api.panes)
        self.assertIsNotNone(self.stored(record))

    def test_restart_saves_inactive_tabs_and_rebuilds_only_selected_tab(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        first = self.create("tab")
        second = self.create("tab", pane_id="shell-2")
        identities = self.app.runtime.identities()
        old_panes = {
            self.section(first, "tab-1")["pane_id"],
            self.section(second, "tab-2")["pane_id"],
        }
        self.api.calls.clear()

        def respond():
            self.acknowledge(first, "tab-1")
            self.acknowledge(second, "tab-2")

        with self.waiting(respond):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertEqual({item["pane_id"] for item in self.methods("pane.close")}, old_panes)
        self.assertFalse((self.app.runtime.view("tab-1") or {}).get("sections"))
        self.assertEqual(self.app.runtime.view("tab-2")["format"], 2)
        self.assertEqual(self.app.runtime.identities(), identities)
        self.assertIsNotNone(self.stored(first))
        self.assertIsNotNone(self.stored(second))
        self.assertEqual(len(self.methods("plugin.pane.open")), 1)

    def test_inactive_vim_save_acknowledgment_does_not_restore_its_tab_focus(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        legacy = self.create("tab")
        view = self.app.runtime.view("tab-1")
        view["sections"][legacy["description_id"]].pop("editor")
        self.app.runtime.put_view(view)
        self.poll(legacy, modified=True)
        active = self.create("tab", pane_id="shell-2")
        self.api.calls.clear()

        def respond():
            focused = self.api.focused_pane_id
            self.assertEqual(self.api.snapshot()["focused_tab_id"], "tab-2")
            response = self.poll(legacy, modified=True)
            self.assertTrue(response["write_requested"])
            self.assertEqual(response["save_request"]["reason"], "restart")
            self.app.store.file_path(legacy["description_id"]).write_text("Vim draft", encoding="utf-8")
            response = self.poll(legacy, saved=True, modified=False, return_focus=True)
            self.assertTrue(response["saved"])
            self.assertFalse(response["write_requested"])
            self.assertEqual(self.api.focused_pane_id, focused)
            self.assertEqual(self.api.snapshot()["focused_tab_id"], "tab-2")
            self.acknowledge(active, "tab-2")
            self.assertEqual(self.api.focused_pane_id, focused)

        with self.waiting(respond):
            self.app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertFalse((self.app.runtime.view("tab-1") or {}).get("sections"))
        self.assertEqual(self.app.runtime.view("tab-2")["format"], 2)
        self.assertEqual(self.api.snapshot()["focused_tab_id"], "tab-2")
        self.assertEqual(self.stored(legacy).content, "Vim draft")
        self.assertEqual(len(self.methods("plugin.pane.open")), 1)
        self.assertEqual(
            self.methods("plugin.pane.open")[0]["env"]["DESCRIPTION_SECTION_ID"],
            active["description_id"],
        )

    def test_restart_isolated_to_named_session_with_shared_state_directory(self):
        default_record = self.create("tab")
        self.write(default_record, "default session text")
        default_views = copy.deepcopy(self.app.runtime.views())
        default_identities = self.app.runtime.identities()
        default_panes = copy.deepcopy(self.api.panes)
        named_api = FakeHerdr(socket_path="/tmp/herdr-petprojects.sock")
        named_app = self.make_app(named_api)
        named_record = self.create("tab", app=named_app)
        self.write(named_record, "named session text", app=named_app)
        named_pane_id = self.section(named_record, app=named_app)["pane_id"]
        self.api.calls.clear()
        named_api.calls.clear()

        with self.waiting(lambda: self.acknowledge(named_record, app=named_app), app=named_app):
            named_app.restart_editors(timeout=0.1, poll_interval=0.001)

        self.assertNotEqual(self.app.session, named_app.session)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.app.runtime.views(), default_views)
        self.assertEqual(self.app.runtime.identities(), default_identities)
        self.assertEqual(self.api.panes, default_panes)
        self.assertEqual(self.stored(default_record).content, "default session text")
        self.assertEqual(self.stored(named_record, app=named_app).content, "named session text")
        self.assertEqual(self.methods("pane.close", named_api), [{"pane_id": named_pane_id}])


if __name__ == "__main__":
    unittest.main()
