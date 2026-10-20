import copy
import unittest
from unittest.mock import patch

from herdr_description.api import HerdrError
from herdr_description.layout import ColumnLayout
from tests.test_app import AppHarness


def pane(pane_id):
    return {"type": "pane", "pane_id": pane_id}


def split(direction, ratio, first, second):
    return {
        "type": "split", "direction": direction, "ratio": ratio,
        "first": first, "second": second,
    }


def leaves(node):
    if node["type"] == "pane":
        return [node["pane_id"]]
    return leaves(node["first"]) + leaves(node["second"])


class FullHeightLayoutTest(AppHarness, unittest.TestCase):
    def sources(self, tree):
        for pane_id in leaves(tree):
            if not any(item["pane_id"] == pane_id for item in self.api.panes):
                self.api.add_pane("tab-1", pane_id)
            native = next(item for item in self.api.panes if item["pane_id"] == pane_id)
            native.update(terminal_id="terminal-" + pane_id, pid=1000 + len(pane_id))
        self.api.trees["tab-1"] = copy.deepcopy(tree)
        return {
            item["pane_id"]: copy.deepcopy(item)
            for item in self.api.panes if item["pane_id"] in leaves(tree)
        }

    def assert_column(self, source_tree, records, width=30):
        root = self.api.trees["tab-1"]
        self.assertEqual(root["direction"], "right")
        self.assertAlmostEqual(root["ratio"], 1 - width / 100)
        self.assertEqual(root["first"], source_tree)
        description_ids = {self.native_pane(record)["pane_id"] for record in records}
        self.assertEqual(set(leaves(root["second"])), description_ids)
        rectangles = {
            item["pane_id"]: item["rect"]
            for item in self.api.rectangles(root)
        }
        for description_id in description_ids:
            rect = rectangles[description_id]
            self.assertAlmostEqual(rect["x"], 200 * (1 - width / 100))
            self.assertAlmostEqual(rect["width"], 200 * width / 100)
            self.assertAlmostEqual(rect["height"], 120 / len(description_ids))
        self.assertAlmostEqual(min(rectangles[item]["y"] for item in description_ids), 0)
        self.assertAlmostEqual(max(
            rectangles[item]["y"] + rectangles[item]["height"]
            for item in description_ids
        ), 120)
        self.assertEqual({item["tab_id"] for item in self.api.tabs}, {"tab-1"})

    def assert_native_sources(self, before):
        actual = {
            item["pane_id"]: item
            for item in self.api.panes if item["pane_id"] in before
        }
        self.assertEqual(actual, before)
        self.assertEqual(self.methods("layout.apply"), [])
        self.assertEqual(self.methods("shell.close"), [])
        self.assertEqual(self.methods("shell.create"), [])
        self.assertEqual(self.methods("terminal.create"), [])
        self.assertEqual(self.methods("pane.close"), [])

    def assert_editor_identity(self, record, before):
        self.assertEqual(
            {key: value for key, value in self.native_pane(record).items() if key != "label"},
            {key: value for key, value in before.items() if key != "label"},
        )

    def legacy_column(self):
        record = self.create("pane", pane_id="shell-1")
        self.write(record, "saved source notes")
        editor = copy.deepcopy(self.native_pane(record))
        source_tree = split(
            "down", 0.37,
            split("right", 0.63, pane("shell-1"), pane("shell-2")),
            split("right", 0.41, pane("shell-3"), pane("shell-4")),
        )
        before = self.sources(source_tree)
        self.api.trees["tab-1"]["second"]["second"] = split(
            "right", 0.7, pane("shell-4"), pane(editor["pane_id"]),
        )
        return record, editor, source_tree, before

    def descriptions_before_sources(self):
        records = [self.create("space"), self.create("tab")]
        for record in records:
            self.write(record, record["kind"] + " saved notes")
            native = self.native_pane(record)
            native["terminal_id"] = "terminal-" + native["pane_id"]
            native["pid"] = 2000 + len(native["pane_id"])
        editors = {record["description_id"]: copy.deepcopy(self.native_pane(record)) for record in records}
        self.api.add_pane("tab-1", "shell-2")
        self.api.remove_pane("shell-1")
        source = next(item for item in self.api.panes if item["pane_id"] == "shell-2")
        source.update(terminal_id="terminal-shell-2", pid=1002)
        return records, editors, {"shell-2": copy.deepcopy(source)}

    def interrupt_layout(self):
        record, editor, source_tree, before = self.legacy_column()
        view = self.app.runtime.view("tab-1")
        self.api.focused_pane_id = "shell-2"
        call = self.api.call
        moves = 0

        def fail(method, **arguments):
            nonlocal moves
            if method == "pane.move":
                moves += 1
                if moves == 3:
                    raise HerdrError("Injected native move failure")
            return call(method, **arguments)

        with patch.object(self.api, "call", side_effect=fail), self.app.runtime.lock():
            with self.assertRaises(HerdrError):
                ColumnLayout(self.api, self.app.runtime, 30).ensure(view, [editor["pane_id"]])
        self.assertTrue(self.app.runtime.view("tab-1").get("layout_pending"))
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assertEqual(self.app.store.file_path(record["description_id"]).read_text(), "saved source notes")
        self.assertEqual(self.methods("pane.close"), [])
        return record, editor, source_tree, before

    def test_two_horizontal_sources_get_full_height_tab_and_space_column(self):
        source_tree = split("right", 0.32, pane("shell-1"), pane("shell-2"))
        before = self.sources(source_tree)
        tab = self.create("tab", pane_id="shell-2")
        self.assert_column(source_tree, [tab])
        tab_editor = copy.deepcopy(self.native_pane(tab))
        space = self.create("space")
        self.assert_column(source_tree, [tab, space])
        self.assertEqual(self.native_pane(tab), tab_editor)
        self.assert_native_sources(before)
        moved = {call["pane_id"] for call in self.methods("pane.move")}
        self.assertTrue(moved)
        self.assertTrue(moved <= set(before))
        self.assertTrue(all(not call["focus"] for call in self.methods("pane.move")))

    def test_mixed_source_splits_and_ratios_survive_last_description_removal(self):
        source_tree = split(
            "down", 0.28,
            split("right", 0.61, pane("shell-1"), pane("shell-2")),
            split(
                "right", 0.39, pane("shell-3"),
                split("down", 0.72, pane("shell-4"), pane("shell-5")),
            ),
        )
        before = self.sources(source_tree)
        record = self.create("tab", pane_id="shell-4")
        editor_id = self.native_pane(record)["pane_id"]
        self.assert_column(source_tree, [record])
        self.assert_native_sources(before)
        self.app.action("remove-tab")
        self.poll(record)
        self.assertEqual(self.api.trees["tab-1"], source_tree)
        self.assertEqual(
            {item["pane_id"]: item for item in self.api.panes}, before,
        )
        self.assertEqual(self.methods("pane.close"), [{"pane_id": editor_id}])

    def test_width_is_fraction_of_entire_tab_and_stable_focus_does_not_stage(self):
        self.app.config.panel_width_percent = 45
        source_tree = split("down", 0.64, pane("shell-1"), pane("shell-2"))
        self.sources(source_tree)
        record = self.create("tab")
        self.assert_column(source_tree, [record], width=45)
        self.api.calls.clear()
        self.focus("shell-2")
        self.focus(self.native_pane(record)["pane_id"])
        self.app.event({}, startup=True, verify_identities=False)
        self.assert_column(source_tree, [record], width=45)
        self.assertEqual(self.methods("pane.move"), [])
        self.assertEqual(self.methods("pane.swap"), [])
        self.assertEqual(self.methods("layout.set_split_ratio"), [])
        self.api.call("layout.set_split_ratio", tab_id="tab-1", path=[], ratio=0.62)
        self.api.calls.clear()
        self.focus("shell-2")
        self.assert_column(source_tree, [record], width=38)
        self.assertEqual(self.methods("pane.move"), [])
        self.assertEqual(self.methods("layout.set_split_ratio"), [])

    def test_focus_and_restore_repair_old_partial_height_layout_with_same_editors(self):
        for trigger in ("focus", "restore"):
            with self.subTest(trigger=trigger):
                if trigger == "focus":
                    record, editor, source_tree, before = self.legacy_column()
                else:
                    self.api.trees["tab-1"] = copy.deepcopy(source_tree)
                    self.api.trees["tab-1"]["second"]["second"] = split(
                        "right", 0.7, pane("shell-4"), pane(editor["pane_id"]),
                    )
                self.api.calls.clear()
                if trigger == "focus":
                    self.focus("shell-3")
                    self.assertEqual(self.api.focused_pane_id, "shell-3")
                else:
                    self.api.focused_pane_id = editor["pane_id"]
                    self.app.event({}, startup=True, verify_identities=False)
                    self.assertEqual(self.api.focused_pane_id, editor["pane_id"])
                self.assert_column(source_tree, [record])
                self.assert_editor_identity(record, editor)
                self.assert_native_sources(before)
                self.assertEqual(self.methods("plugin.pane.open"), [])

    def test_background_repair_preserves_other_selected_tab(self):
        record, editor, source_tree, before = self.legacy_column()
        self.api.add_tab("space-1", "other-tab", "other-shell")
        self.api.focused_pane_id = "other-shell"
        with self.app.runtime.lock():
            ColumnLayout(self.api, self.app.runtime, 30).ensure(
                self.app.runtime.view("tab-1"), [editor["pane_id"]],
            )
        self.assertEqual(self.api.focused_pane_id, "other-shell")
        self.assertEqual(self.api.trees["tab-1"]["first"], source_tree)
        self.assert_editor_identity(record, editor)
        self.assert_native_sources(before)
        self.assertEqual({item["tab_id"] for item in self.api.tabs}, {"tab-1", "other-tab"})

    def test_delayed_staging_move_events_preserve_pane_notes_and_files(self):
        source_tree = split("right", 0.42, pane("shell-1"), pane("shell-2"))
        self.sources(source_tree)
        record = self.create("pane", pane_id="shell-2")
        self.write(record, "notes must remain attached")
        editor = copy.deepcopy(self.native_pane(record))
        records = self.app.store.export_descriptions()
        identities = self.app.runtime.identities()
        path = self.app.store.file_path(record["description_id"])
        events = copy.deepcopy(self.api.move_events)
        self.assertGreaterEqual(len(events), 2)
        self.api.calls.clear()
        for event in events:
            self.app.event(event)
        self.assertEqual(self.app.store.export_descriptions(), records)
        self.assertEqual(self.app.runtime.identities(), identities)
        self.assert_editor_identity(record, editor)
        self.assertEqual(self.stored(record).tab_id, self.app.target("space-1", "tab-1", "tab"))
        self.assertEqual(path.read_text(), "notes must remain attached")
        self.assert_column(source_tree, [record])
        self.assertEqual(self.methods("plugin.pane.open"), [])
        self.assertEqual(self.methods("pane.close"), [])

    def test_source_exit_repairs_descriptions_left_of_surviving_work_pane(self):
        records, editors, before = self.descriptions_before_sources()
        self.api.focused_pane_id = "shell-2"
        self.api.calls.clear()
        self.app.event({
            "type": "pane_exited", "pane_id": "shell-1",
            "workspace_id": "space-1", "tab_id": "tab-1",
        })
        self.assert_column(pane("shell-2"), records)
        self.assert_native_sources(before)
        self.assertEqual(self.api.focused_pane_id, "shell-2")
        for record in records:
            self.assert_editor_identity(record, editors[record["description_id"]])
            self.assertEqual(self.stored(record).content, record["kind"] + " saved notes")
        self.assertEqual(self.methods("plugin.pane.open"), [])
        self.assertEqual(self.methods("tab.close"), [])

    def test_restore_repairs_descriptions_above_nested_work_panes(self):
        records, editors, _ = self.descriptions_before_sources()
        description_tree = copy.deepcopy(self.api.trees["tab-1"]["first"])
        source_tree = split("down", 0.34, pane("shell-2"), pane("shell-3"))
        before = self.sources(source_tree)
        self.api.trees["tab-1"] = split("down", 0.43, description_tree, source_tree)
        focused = editors[records[0]["description_id"]]["pane_id"]
        self.api.focused_pane_id = focused
        self.api.calls.clear()
        self.app.event({}, startup=True, verify_identities=False)
        self.assert_column(source_tree, records)
        self.assert_native_sources(before)
        self.assertEqual(self.api.focused_pane_id, focused)
        for record in records:
            self.assert_editor_identity(record, editors[record["description_id"]])
        self.assertEqual(self.methods("plugin.pane.open"), [])

    def test_generic_recovery_preserves_staged_description_processes_and_documents(self):
        records, editors, before = self.descriptions_before_sources()
        descriptions = [editors[record["description_id"]]["pane_id"] for record in records]
        stored = self.app.store.export_descriptions()
        paths = {record["description_id"]: self.app.store.file_path(record["description_id"]) for record in records}
        self.api.focused_pane_id = descriptions[0]
        call = self.api.call
        staged = None

        def fail_after_staging_editor(method, **arguments):
            nonlocal staged
            if method == "pane.move" and staged:
                raise HerdrError("Injected move failure with a live editor in staging")
            result = call(method, **arguments)
            if method == "pane.move" and arguments["pane_id"] in descriptions:
                staged = copy.deepcopy(result["pane"])
            return result

        with patch.object(self.api, "call", side_effect=fail_after_staging_editor), self.app.runtime.lock():
            with self.assertRaises(HerdrError):
                ColumnLayout(self.api, self.app.runtime, 30).ensure(
                    self.app.runtime.view("tab-1"), descriptions,
                )
        self.assertIsNotNone(staged)
        self.assertNotEqual(staged["tab_id"], "tab-1")
        self.assertEqual(self.app.store.export_descriptions(), stored)
        self.assertTrue(self.app.runtime.view("tab-1").get("layout_pending"))
        self.app = self.make_app(self.api)
        self.api.calls.clear()
        self.app.event({
            "type": "pane_focused", "pane_id": staged["pane_id"],
            "workspace_id": "space-1", "tab_id": staged["tab_id"],
        }, startup=True, verify_identities=False)
        self.assert_column(pane("shell-2"), records)
        self.assert_native_sources(before)
        self.assertEqual(self.api.focused_pane_id, descriptions[0])
        self.assertFalse(self.app.runtime.view("tab-1").get("layout_pending"))
        self.assertEqual(self.app.store.export_descriptions(), stored)
        for record in records:
            self.assert_editor_identity(record, editors[record["description_id"]])
            self.assertEqual(self.app.store.file_path(record["description_id"]), paths[record["description_id"]])
            self.assertEqual(paths[record["description_id"]].read_text(), record["kind"] + " saved notes")
        self.assertEqual(self.methods("plugin.pane.open"), [])

    def test_partial_move_failure_recovers_before_processing_late_focus(self):
        record, editor, source_tree, before = self.interrupt_layout()
        self.app = self.make_app(self.api)
        self.api.calls.clear()
        self.app.event({
            "type": "pane_focused", "pane_id": "shell-2",
            "tab_id": "tab-1", "workspace_id": "space-1",
        })
        self.assertFalse(self.app.runtime.view("tab-1").get("layout_pending"))
        self.assert_column(source_tree, [record])
        self.assert_native_sources(before)
        self.assert_editor_identity(record, editor)
        self.assertEqual(self.api.focused_pane_id, "shell-2")
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assertEqual(self.methods("plugin.pane.open"), [])

    def test_lost_move_response_recovers_the_existing_temporary_tab(self):
        record, editor, source_tree, before = self.legacy_column()
        view = self.app.runtime.view("tab-1")
        self.api.focused_pane_id = "shell-2"
        call = self.api.call
        failed = False

        def lose_response(method, **arguments):
            nonlocal failed
            result = call(method, **arguments)
            if method == "pane.move" and not failed:
                failed = True
                raise OSError("Native response was lost after moving the pane")
            return result

        with patch.object(self.api, "call", side_effect=lose_response), self.app.runtime.lock():
            with self.assertRaises(HerdrError):
                ColumnLayout(self.api, self.app.runtime, 30).ensure(view, [editor["pane_id"]])
        temporary_tabs = [tab for tab in self.api.tabs if tab["tab_id"] != "tab-1"]
        self.assertEqual(len(temporary_tabs), 1)
        self.assertTrue(self.app.runtime.view("tab-1").get("layout_pending"))
        self.app.event(copy.deepcopy(self.api.move_events[0]))
        self.assert_column(source_tree, [record])
        self.assert_editor_identity(record, editor)
        self.assert_native_sources(before)
        self.assertEqual(self.api.focused_pane_id, "shell-2")
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assertEqual(len([
            call for call in self.methods("pane.move")
            if call["destination"]["type"] == "new_tab"
        ]), 1)

    def test_refused_native_move_keeps_pending_plan_and_every_live_pane(self):
        record, editor, source_tree, before = self.legacy_column()
        view = self.app.runtime.view("tab-1")
        call = self.api.call

        def refuse(method, **arguments):
            if method == "pane.move":
                return {"changed": False, "reason": "same_tab"}
            return call(method, **arguments)

        with patch.object(self.api, "call", side_effect=refuse), self.app.runtime.lock():
            with self.assertRaises(HerdrError):
                ColumnLayout(self.api, self.app.runtime, 30).ensure(view, [editor["pane_id"]])
        self.assertTrue(self.app.runtime.view("tab-1").get("layout_pending"))
        self.assertEqual(set(item["pane_id"] for item in self.api.panes), set(before) | {editor["pane_id"]})
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assertEqual(self.methods("pane.close"), [])
        self.app.event({}, startup=True, verify_identities=False)
        self.assert_column(source_tree, [record])

    def test_recovery_refuses_reused_source_terminal_without_deleting_notes(self):
        record, editor, _, _ = self.interrupt_layout()
        staged = next(item for item in self.api.panes if item["tab_id"] != "tab-1")
        staged["terminal_id"] = "replacement-terminal"
        self.api.calls.clear()
        with self.assertRaises(HerdrError):
            self.app.action("remove-pane")
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assert_editor_identity(record, editor)
        self.assertTrue(self.app.runtime.view("tab-1").get("layout_pending"))
        self.assertEqual(self.methods("pane.move"), [])
        self.assertEqual(self.methods("pane.close"), [])

    def test_recovery_refuses_source_moved_to_unrelated_tab(self):
        record, editor, _, _ = self.interrupt_layout()
        staged = next(item for item in self.api.panes if item["tab_id"] != "tab-1")
        self.api.add_tab("space-1", "user-tab", "user-shell")
        self.api.move_pane(staged["pane_id"], "user-tab")
        self.api.calls.clear()
        with self.assertRaises(HerdrError):
            self.app.event({}, startup=True, verify_identities=False)
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assert_editor_identity(record, editor)
        self.assertEqual(self.methods("pane.move"), [])
        self.assertEqual(self.methods("pane.close"), [])

    def test_recovery_refuses_unexpected_pane_in_temporary_tab(self):
        record, editor, _, _ = self.interrupt_layout()
        staging = next(item["tab_id"] for item in self.api.panes if item["tab_id"] != "tab-1")
        self.api.add_pane(staging, "unexpected-shell")
        self.api.calls.clear()
        with self.assertRaises(HerdrError):
            self.app.event({}, startup=True, verify_identities=False)
        self.assertEqual(self.stored(record).content, "saved source notes")
        self.assert_editor_identity(record, editor)
        self.assertTrue(any(item["pane_id"] == "unexpected-shell" for item in self.api.panes))
        self.assertEqual(self.methods("pane.move"), [])
        self.assertEqual(self.methods("pane.close"), [])
