import copy
import itertools
import unittest

from tests.test_app import AppHarness, FakeHerdr


class VisibilityTest(AppHarness, unittest.TestCase):
    def test_uncreated_descriptions_leave_tabs_untouched_on_startup_and_focus(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-2", "tab-3", "shell-3")
        layouts = copy.deepcopy(self.api.trees)
        self.app.event({}, startup=True)
        for pane_id in ("shell-1", "shell-2", "shell-3", "shell-1"):
            self.focus(pane_id)
        self.assertEqual(self.app.runtime.views(), [])
        self.assertEqual(self.app.runtime.identities(), {})
        self.assertEqual(self.app.store.export_descriptions(), [])
        self.assertEqual(self.methods("plugin.pane.open"), [])
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.api.trees, layouts)

    def test_all_scope_combinations_create_only_applicable_sections_independent_of_focus(self):
        for space, tab, pane in itertools.product((False, True), repeat=3):
            with self.subTest(space=space, tab=tab, pane=pane):
                api = FakeHerdr()
                app = self.make_app(api, "{}-{}-{}".format(space, tab, pane))
                api.add_pane("tab-1", "undescribed")
                api.add_tab("space-1", "tab-2", "shell-2")
                api.add_tab("space-2", "tab-3", "shell-3")
                for kind, enabled in (("space", space), ("tab", tab), ("pane", pane)):
                    if enabled:
                        self.seed(kind, app=app)
                for pane_id in ("shell-1", "undescribed", "shell-1"):
                    self.focus(pane_id, app)
                    view = app.runtime.view("tab-1")
                    self.assertEqual(view is not None, space or tab or pane)
                    if view:
                        self.assertEqual(len(view["sections"]), sum((space, tab, pane)))
                    self.assertEqual(api.focused_pane_id, pane_id)
                self.focus("shell-2", app)
                self.assertEqual(app.runtime.view("tab-2") is not None, space)
                self.focus("shell-3", app)
                self.assertIsNone(app.runtime.view("tab-3"))
                self.focus("undescribed", app)
                self.assertEqual(len(self.methods("plugin.pane.open", api)), sum((space, tab, pane)) + int(space))
                self.assertEqual(len(app.store.export_descriptions()), sum((space, tab, pane)))

    def test_new_tab_in_described_space_gets_editable_section_without_focus_theft(self):
        record = self.create("space")
        self.api.add_tab("space-1", "new-tab", "new-shell")
        self.api.focused_pane_id = "new-shell"
        self.app.event({"type": "tab_created", "workspace_id": "space-1", "tab_id": "new-tab"})
        self.assertEqual(list(self.app.runtime.view("new-tab")["sections"]), [record["description_id"]])
        self.assertEqual(self.poll(record, "new-tab")["record"]["kind"], "space")
        self.assertEqual(self.api.focused_pane_id, "new-shell")
        self.assertEqual(len(self.methods("plugin.pane.open")), 2)

    def test_remove_space_closes_all_copies_and_preserves_other_scopes_without_focus_theft(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-1", "tab-3", "shell-3")
        space = self.seed("space")
        tab = self.seed("tab")
        pane = self.seed("pane", tab_id="tab-2", pane_id="shell-2")
        for pane_id in ("shell-1", "shell-2", "shell-3"):
            self.focus(pane_id)
        ids = {tab_id: self.native_pane(space, tab_id)["pane_id"] for tab_id in ("tab-1", "tab-2", "tab-3")}
        self.app.action("remove-space")
        for tab_id, pane_id in ids.items():
            self.assertEqual(self.poll(space, tab_id, modified=True), {"closed": True})
            self.closed_event(pane_id, tab_id)
        self.assertEqual(set(self.app.runtime.view("tab-1")["sections"]), {tab["description_id"]})
        self.assertEqual(set(self.app.runtime.view("tab-2")["sections"]), {pane["description_id"]})
        self.assertIsNone(self.app.runtime.view("tab-3"))
        self.assertEqual(self.api.focused_pane_id, "shell-3")
        self.assertEqual(len(self.methods("plugin.pane.open")), 5)

    def test_removing_last_description_restores_layout_and_focus_for_each_scope(self):
        for kind in ("space", "tab", "pane"):
            with self.subTest(kind=kind):
                api = FakeHerdr()
                app = self.make_app(api, kind)
                api.add_pane("tab-1", "shell-right")
                layout = copy.deepcopy(api.trees["tab-1"])
                record = self.create(kind, app=app)
                pane_id = self.native_pane(record, app=app)["pane_id"]
                app.action("remove-" + kind)
                self.assertEqual(self.poll(record, app=app, modified=True), {"closed": True})
                self.closed_event(pane_id, app=app)
                self.assertIsNone(app.runtime.view("tab-1"))
                self.assertEqual(api.trees["tab-1"], layout)
                self.assertEqual(api.focused_pane_id, "shell-1")
                self.focus("shell-right", app)
                self.assertEqual(len(self.methods("plugin.pane.open", api)), 1)

    def test_unannounced_removal_retains_dirty_buffer_until_it_is_clean(self):
        record = self.create("tab")
        pane_id = self.native_pane(record)["pane_id"]
        self.app.store.delete(self.app.session, "tab", record["target_id"])
        self.focus("shell-1")
        self.assertIn("error", self.poll(record, modified=True, content="recoverable draft"))
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.poll(record), {"closed": True})
        self.closed_event(pane_id)
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertEqual(self.app.store.export_descriptions(), [])

    def test_pane_only_section_reopens_for_undescribed_focused_source(self):
        self.api.add_pane("tab-1", "undescribed")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("pane")
        old_id = self.native_pane(record)["pane_id"]
        self.focus("shell-2")
        self.api.remove_pane(old_id)
        self.closed_event(old_id)
        self.assertFalse((self.app.runtime.view("tab-1") or {}).get("sections"))
        self.assertIsNone(self.app.runtime.view("tab-2"))
        self.focus("undescribed")
        self.assertNotEqual(self.native_pane(record)["pane_id"], old_id)
        self.assertFalse(self.poll(record)["record"]["focused"])
        self.assertEqual(self.api.focused_pane_id, "undescribed")

    def test_closing_described_source_removes_only_its_section(self):
        self.api.add_pane("tab-1", "remains")
        record = self.create("pane")
        pane_id = self.native_pane(record)["pane_id"]
        self.api.remove_pane("shell-1")
        self.api.focused_pane_id = "remains"
        self.closed_event("shell-1", event_type="pane_closed")
        self.assertEqual(self.poll(record, modified=True), {"closed": True})
        self.closed_event(pane_id)
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertEqual([pane["pane_id"] for pane in self.api.panes], ["remains"])
        self.assertEqual(self.api.focused_pane_id, "remains")

    def test_missing_source_close_event_hides_section_but_retains_saved_notes(self):
        self.api.add_pane("tab-1", "remains")
        record = self.create("pane")
        self.write(record, "recoverable notes")
        pane_id = self.native_pane(record)["pane_id"]
        self.api.remove_pane("shell-1")
        self.focus("remains")
        self.assertTrue(self.poll(record)["closed"])
        self.closed_event(pane_id)
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertEqual(self.stored(record).content, "recoverable notes")
        self.assertEqual(self.stored(record).description_id, record["description_id"])
        self.focus("remains")
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertEqual(len(self.methods("plugin.pane.open")), 1)

    def test_move_preserves_dirty_old_editor_until_clean_then_hides_it(self):
        self.api.add_pane("tab-1", "remains")
        self.api.add_tab("space-2", "tab-2", "shell-2")
        record = self.create("pane")
        old_id = self.native_pane(record)["pane_id"]
        self.api.move_pane("shell-1", "tab-2")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": {"workspace_id": "space-2", "tab_id": "tab-2", "pane_id": "shell-1"},
        })
        self.focus("remains")
        self.assertFalse(self.poll(record, modified=True, content="preserve my edits")["saved"])
        self.assertNotIn({"pane_id": old_id}, self.methods("pane.close"))
        self.assertTrue(self.poll(record)["closed"])
        self.closed_event(old_id)
        self.assertIsNone(self.app.runtime.view("tab-1"))
        self.assertIsNotNone(self.app.runtime.view("tab-2"))

    def test_reused_user_pane_is_untouched_when_no_description_applies(self):
        record = self.create("tab")
        self.app.store.delete(self.app.session, "tab", record["target_id"])
        pane = self.native_pane(record)
        pane.pop("tokens")
        pane["label"] = "my shell"
        pane["cwd"] = "/tmp"
        self.api.calls.clear()
        self.focus("shell-1")
        self.assertEqual(self.methods("pane.close"), [])
        self.assertIn(pane, self.api.panes)
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_removing_middle_section_redistributes_remaining_heights(self):
        self.api.add_pane("tab-1", "shell-2")
        records = [self.create("space"), self.create("tab")]
        records += [self.create("pane", pane_id=pane) for pane in ("shell-1", "shell-2")]
        removed_id = self.native_pane(records[1])["pane_id"]
        self.app.action("remove-tab")
        self.poll(records[1], modified=True)
        self.closed_event(removed_id)
        ids = {self.native_pane(record)["pane_id"] for record in (records[0], records[2], records[3])}
        rectangles = [item["rect"] for item in self.api.rectangles(self.api.trees["tab-1"]) if item["pane_id"] in ids]
        self.assertEqual(len(rectangles), 3)
        for rect in rectangles:
            self.assertAlmostEqual(rect["height"], 40)


if __name__ == "__main__":
    unittest.main()
