import copy
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.api import HerdrError
from tests.test_app import AppHarness, FakeHerdr


class LastSourcePaneTest(AppHarness, unittest.TestCase):
    def create_all(self):
        return [
            self.create("space"),
            self.create("tab"),
            self.create("pane", pane_id="shell-1"),
        ]

    def assert_deleted(self, records):
        for record in records:
            self.assertIsNone(self.stored(record))
            self.assertFalse(Path(record["file_path"]).exists())
        remaining = {
            record.description_id for record in self.app.store.export_descriptions()
        }
        self.assertFalse({record["description_id"] for record in records} & remaining)

    def assert_preserved(self, records, app=None):
        for record in records:
            self.assertIsNotNone(self.stored(record, app))
            self.assertTrue(Path(record["file_path"]).exists())

    def source_event(self, event_type="pane_exited"):
        self.app.event({
            "type": event_type,
            "workspace_id": "space-1",
            "pane_id": "shell-1",
        })

    def confirm(self, answer="yes"):
        output = io.StringIO()
        with patch.dict(os.environ, {
            "DESCRIPTION_WORKSPACE_ID": "space-1",
            "DESCRIPTION_TAB_ID": "tab-1",
            "DESCRIPTION_PANE_ID": "shell-1",
            "DESCRIPTION_CLOSE_KIND": "pane",
        }), patch("builtins.input", return_value=answer), patch("sys.stdout", output):
            self.app.confirm_close()
        return output.getvalue()

    def close_last_source(self, event_type):
        records = self.create_all()
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.source_event(event_type)

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assertEqual(self.api.tabs, [])
        self.assertEqual(self.api.workspaces, [])
        self.assertEqual(self.api.panes, [])
        self.assert_deleted(records)
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertIsNone(self.app.runtime.identity("space-1"))

    def test_exit_without_tab_id_closes_description_only_tab_and_last_space(self):
        self.close_last_source("pane_exited")

    def test_close_without_tab_id_closes_description_only_tab_and_last_space(self):
        self.close_last_source("pane_closed")

    def test_each_single_description_kind_does_not_keep_last_tab_and_space_alive(self):
        for kind in ("space", "tab", "pane"):
            with self.subTest(kind=kind):
                api = FakeHerdr()
                app = self.make_app(api, name=kind)
                record = self.create(kind, app=app, pane_id="shell-1")
                api.remove_pane("shell-1")

                app.event({
                    "type": "pane_exited", "workspace_id": "space-1", "pane_id": "shell-1",
                })

                self.assertEqual(self.methods("tab.close", api), [{"tab_id": "tab-1"}])
                self.assertEqual(api.panes, [])
                self.assertEqual(api.tabs, [])
                self.assertEqual(api.workspaces, [])
                self.assertIsNone(self.stored(record, app))
                self.assertFalse(Path(record["file_path"]).exists())
                self.assertEqual(app.store.export_descriptions(), [])
                self.assertIsNone(app.runtime.view("tab-1"))
                self.assertIsNone(app.runtime.identity("space-1"))

    def test_background_tab_cleanup_preserves_shared_space_and_active_sibling(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space, tab, pane = self.create_all()
        other = self.create("tab", pane_id="shell-2")
        other_pane = self.create("pane", pane_id="shell-2")
        self.focus("shell-2")
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.source_event()

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assertEqual(self.api.tabs, [{"workspace_id": "space-1", "tab_id": "tab-2"}])
        self.assertEqual(self.api.workspaces, [{"workspace_id": "space-1"}])
        self.assertEqual(self.api.focused_pane_id, "shell-2")
        self.assert_deleted([tab, pane])
        self.assert_preserved([space, other, other_pane])
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertIsNotNone(self.app.runtime.view("tab-2"))

    def test_cleanup_preserves_another_space_and_another_session(self):
        records = self.create_all()
        self.api.add_tab("space-2", "tab-2", "shell-2")
        other_space = [
            self.create("space", pane_id="shell-2"),
            self.create("tab", pane_id="shell-2"),
            self.create("pane", pane_id="shell-2"),
        ]
        other_api = FakeHerdr("/tmp/herdr-description-other-session.sock")
        other_app = self.make_app(other_api)
        other_session = [
            self.create("space", app=other_app),
            self.create("tab", app=other_app),
            self.create("pane", app=other_app, pane_id="shell-1"),
        ]
        other_snapshot = other_api.snapshot()
        other_view = other_app.runtime.view("tab-1")
        self.api.remove_pane("shell-1")

        self.source_event()

        self.assert_deleted(records)
        self.assert_preserved(other_space)
        self.assert_preserved(other_session, other_app)
        self.assertEqual(self.api.workspaces, [{"workspace_id": "space-2"}])
        self.assertEqual(other_api.snapshot(), other_snapshot)
        self.assertEqual(other_app.runtime.view("tab-1"), other_view)

    def test_replayed_source_and_container_events_are_idempotent(self):
        records = self.create_all()
        self.api.add_tab("space-2", "tab-2", "shell-2")
        other = self.seed("pane", "tab-2", "shell-2")
        self.api.remove_pane("shell-1")
        self.source_event()
        calls = copy.deepcopy(self.methods("tab.close"))

        self.source_event()
        self.source_event("pane_closed")
        self.app.event({
            "type": "tab_closed", "workspace_id": "space-1", "tab_id": "tab-1",
        })
        self.app.event({"type": "workspace_closed", "workspace_id": "space-1"})

        self.assertEqual(self.methods("tab.close"), calls)
        self.assert_deleted(records)
        self.assert_preserved([other])
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertIsNone(self.app.runtime.identity("space-1"))

    def assert_remaining_pane_prevents_close(self, metadata=None):
        space, tab, pane = self.create_all()
        self.api.add_pane("tab-1", "remaining")
        remaining = next(item for item in self.api.panes if item["pane_id"] == "remaining")
        remaining.update(metadata or {})
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.source_event()

        self.assertEqual(self.methods("tab.close"), [])
        self.assertIn("remaining", {item["pane_id"] for item in self.api.panes})
        self.assertEqual(len(self.api.tabs), 1)
        self.assertEqual(len(self.api.workspaces), 1)
        self.assert_deleted([pane])
        self.assert_preserved([space, tab])

    def test_an_undescribed_source_pane_keeps_the_tab_alive(self):
        self.assert_remaining_pane_prevents_close()

    def test_another_plugins_pane_keeps_the_tab_alive(self):
        self.assert_remaining_pane_prevents_close({
            "label": "another plugin", "tokens": {"another_plugin": "active"},
        })

    def test_unknown_description_marker_is_not_proof_of_ownership(self):
        self.assert_remaining_pane_prevents_close({
            "label": "herdr-description:foreign",
            "tokens": {"herdr_description": "herdr-description:foreign"},
        })

    def test_reused_stored_pane_id_without_ownership_proof_keeps_tab_alive(self):
        space, tab, pane = self.create_all()
        reused = self.native_pane(tab)
        reused_id = reused["pane_id"]
        reused.update(label="shell", cwd="/tmp", tokens={})
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.source_event()

        self.assertEqual(self.methods("tab.close"), [])
        self.assertIn(reused_id, {item["pane_id"] for item in self.api.panes})
        self.assert_deleted([pane])
        self.assert_preserved([space, tab])
        self.assertIsNotNone(self.app.runtime.view("tab-1"))

    def test_private_editor_cwd_proves_ownership_when_metadata_is_absent(self):
        records = self.create_all()
        for record in records:
            self.native_pane(record).pop("tokens")
        self.api.remove_pane("shell-1")

        self.source_event()

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted(records)
        self.assertEqual(self.api.workspaces, [])

    def test_new_source_in_fresh_snapshot_prevents_automatic_tab_close(self):
        records = [self.create("space"), self.create("tab")]
        self.api.remove_pane("shell-1")
        snapshot = self.api.snapshot
        snapshots = []

        def add_source_before_close():
            snapshots.append(True)
            if len(snapshots) == 2:
                self.api.add_pane("tab-1", "new-shell")
            return snapshot()

        with patch.object(self.api, "snapshot", side_effect=add_source_before_close):
            self.source_event()

        self.assertGreaterEqual(len(snapshots), 2)
        self.assertEqual(self.methods("tab.close"), [])
        self.assert_preserved(records)
        self.assertIn("new-shell", {item["pane_id"] for item in self.api.panes})

    def failed_tab_close_preserves_container_descriptions(self, fail):
        space, tab, pane = self.create_all()
        self.api.remove_pane("shell-1")
        original_call = self.api.call

        def unsuccessful_close(method, **arguments):
            if method == "tab.close":
                if fail:
                    raise HerdrError("Cannot close tab")
                return {}
            return original_call(method, **arguments)

        with patch.object(self.api, "call", side_effect=unsuccessful_close):
            with self.assertRaises(HerdrError):
                self.source_event()

        self.assert_preserved([space, tab])
        self.assertEqual(len(self.api.tabs), 1)
        self.assertEqual(len(self.api.workspaces), 1)
        self.assertIsNotNone(self.app.runtime.view("tab-1"))
        self.assertIsNotNone(self.app.runtime.identity("space-1"))

    def test_failed_automatic_tab_close_preserves_container_descriptions(self):
        self.failed_tab_close_preserves_container_descriptions(fail=True)

    def test_noop_automatic_tab_close_preserves_container_descriptions(self):
        self.failed_tab_close_preserves_container_descriptions(fail=False)

    def test_startup_cleans_existing_description_only_tab(self):
        records = self.create_all()
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.app.event({}, startup=True)

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted(records)
        self.assertEqual(self.api.workspaces, [])
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_focus_keeps_description_only_tab_until_explicit_startup(self):
        records = self.create_all()
        self.api.remove_pane("shell-1")
        self.api.calls.clear()

        self.focus(self.native_pane(records[1])["pane_id"])

        self.assertEqual(self.methods("tab.close"), [])
        self.assert_preserved(records)
        self.assertIsNotNone(self.app.runtime.view("tab-1"))
        self.app.event({}, startup=True)
        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted(records)

    def test_source_close_after_focus_still_finds_and_closes_original_tab(self):
        records = self.create_all()
        self.api.remove_pane("shell-1")
        self.focus(self.native_pane(records[1])["pane_id"])
        self.assertEqual(self.methods("tab.close"), [])
        self.assert_preserved(records)

        self.source_event("pane_closed")

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted(records)
        self.assertEqual(self.api.workspaces, [])
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_legacy_combined_editor_does_not_keep_tab_and_space_alive(self):
        tab = self.legacy_editor()
        space = self.seed("space")
        self.api.remove_pane("shell-1")

        self.source_event()

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted([space, tab])
        self.assertEqual(self.api.workspaces, [])
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_moving_last_source_preserves_its_note_and_closes_old_containers(self):
        space, tab, pane = self.create_all()
        self.write(pane, "portable note")
        path = Path(pane["file_path"])
        self.api.add_tab("space-2", "tab-2", "shell-2")
        other = self.seed("tab", "tab-2")
        self.api.move_pane("shell-1", "tab-2", "moved-shell")

        self.app.event({
            "type": "pane_moved",
            "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1",
            "previous_tab_id": "tab-1",
            "pane": {
                "workspace_id": "space-2", "tab_id": "tab-2", "pane_id": "moved-shell",
            },
        })

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted([space, tab])
        self.assertIsNone(self.stored(pane))
        moved = next(
            item for item in self.app.descriptions("space-2", "tab-2")
            if item["kind"] == "pane"
        )
        self.assertEqual(moved["description_id"], pane["description_id"])
        self.assertEqual(moved["pane_id"], "moved-shell")
        self.assertEqual(Path(moved["file_path"]), path)
        self.assertEqual(path.read_text(encoding="utf-8"), "portable note")
        self.assert_preserved([other])
        self.assertEqual(self.api.workspaces, [{"workspace_id": "space-2"}])
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertIsNotNone(self.app.runtime.view("tab-2"))

    def move_event(self, workspace_id, pane_id):
        moved = next(pane for pane in self.api.panes if pane["pane_id"] == pane_id)
        self.assertEqual(moved["workspace_id"], workspace_id)
        self.app.event({
            "type": "pane_moved",
            "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1",
            "previous_tab_id": "tab-1",
            "pane": copy.deepcopy(moved),
        })

    def assert_move_after_focus_preserves_note(self, workspace_id, pane_id, startup=False):
        source = next(pane for pane in self.api.panes if pane["pane_id"] == "shell-1")
        source["terminal_id"] = "terminal-1"
        space, tab, pane = self.create_all()
        self.write(pane, "portable note")
        self.api.add_tab(workspace_id, "tab-2", "shell-2")
        other = self.seed("tab", "tab-2")
        self.api.move_pane("shell-1", "tab-2", pane_id)
        self.api.calls.clear()

        self.focus(pane_id)

        self.assertEqual(self.methods("tab.close"), [])
        self.assert_preserved([space, tab, pane, other])
        self.assertEqual(Path(pane["file_path"]).read_text(encoding="utf-8"), "portable note")
        self.assertIsNotNone(self.app.runtime.view("tab-1"))

        if startup:
            self.app.event({}, startup=True)
            self.assertEqual(self.methods("tab.close"), [])
            self.assert_preserved([space, tab, pane, other])
            self.assertIsNotNone(self.app.runtime.view("tab-1"))

        self.move_event(workspace_id, pane_id)

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted([tab] + ([space] if workspace_id != "space-1" else []))
        self.assert_preserved([other] + ([space] if workspace_id == "space-1" else []))
        moved = next(
            item for item in self.app.descriptions(workspace_id, "tab-2")
            if item["kind"] == "pane"
        )
        self.assertEqual(moved["description_id"], pane["description_id"])
        self.assertEqual(moved["pane_id"], pane_id)
        self.assertEqual(moved["file_path"], pane["file_path"])
        self.assertEqual(Path(moved["file_path"]).read_text(encoding="utf-8"), "portable note")
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_same_workspace_move_note_survives_focus_before_move_hook(self):
        self.assert_move_after_focus_preserves_note("space-1", "shell-1")

    def test_cross_workspace_move_note_survives_focus_before_move_hook(self):
        self.assert_move_after_focus_preserves_note("space-2", "moved-shell")

    def test_startup_preserves_pending_move_with_same_pane_id(self):
        self.assert_move_after_focus_preserves_note("space-1", "shell-1", startup=True)

    def test_startup_preserves_pending_move_with_changed_pane_id_and_same_terminal(self):
        self.assert_move_after_focus_preserves_note("space-2", "moved-shell", startup=True)

    def test_unrelated_source_exit_does_not_close_tab_awaiting_move_hook(self):
        space, tab, pane = self.create_all()
        self.write(pane, "pending move note")
        self.api.add_tab("space-2", "tab-2", "shell-2")
        other = self.create("tab", pane_id="shell-2")
        self.api.move_pane("shell-1", "tab-2", "moved-shell")
        self.api.remove_pane("shell-2")
        self.api.calls.clear()

        self.app.event({
            "type": "pane_exited", "workspace_id": "space-2", "pane_id": "shell-2",
        })

        self.assertEqual(self.methods("tab.close"), [])
        self.assert_preserved([space, tab, pane, other])
        self.assertEqual(Path(pane["file_path"]).read_text(encoding="utf-8"), "pending move note")
        self.assertIsNotNone(self.app.runtime.view("tab-1"))

        self.move_event("space-2", "moved-shell")

        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted([space, tab])
        moved = next(
            item for item in self.app.descriptions("space-2", "tab-2")
            if item["kind"] == "pane"
        )
        self.assertEqual(moved["description_id"], pane["description_id"])
        self.assertEqual(Path(moved["file_path"]).read_text(encoding="utf-8"), "pending move note")
        self.assert_preserved([other])

    def test_guarded_last_source_close_warns_about_all_removed_scopes(self):
        records = self.create_all()

        output = self.confirm()

        self.assertIn("1 space, 1 tab, 1 pane (3 total)", output)
        self.assertEqual(self.methods("tab.close"), [{"tab_id": "tab-1"}])
        self.assert_deleted(records)
        self.assertEqual(self.api.workspaces, [])
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_guarded_last_source_close_keeps_shared_space_and_warns_tab_scope(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space, tab, pane = self.create_all()
        other = self.seed("pane", "tab-2", "shell-2")

        output = self.confirm()

        self.assertIn("0 space, 1 tab, 1 pane (2 total)", output)
        self.assert_deleted([tab, pane])
        self.assert_preserved([space, other])
        self.assertEqual(self.api.workspaces, [{"workspace_id": "space-1"}])
        self.assertEqual(self.api.tabs, [{"workspace_id": "space-1", "tab_id": "tab-2"}])

    def test_canceling_last_source_close_preserves_all_scopes(self):
        records = self.create_all()
        snapshot = self.api.snapshot()

        output = self.confirm(answer="")

        self.assertIn("1 space, 1 tab, 1 pane (3 total)", output)
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.methods("tab.close"), [])
        self.assertEqual(self.api.snapshot(), snapshot)
        self.assert_preserved(records)


if __name__ == "__main__":
    unittest.main()
