import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.api import HerdrError
from herdr_description.editor import EditorScreen, EditorSession
from herdr_description.indicators import NameIndicators
from tests.test_app import AppHarness, FakeHerdr


class IndicatorHerdr(FakeHerdr):
    def __init__(self, socket_path="/tmp/herdr-description-test.sock"):
        self.failed_renames = set()
        self.reported_titles = {}
        super().__init__(socket_path)

    def add_tab(self, workspace_id, tab_id, pane_id):
        super().add_tab(workspace_id, tab_id, pane_id)
        for workspace in self.workspaces:
            workspace.setdefault("label", "Workspace " + workspace["workspace_id"])
        self.tabs[-1]["label"] = "Tab " + tab_id
        self.panes[-1].update(label="Shell " + pane_id, terminal_id="terminal-" + pane_id)

    def add_pane(self, tab_id, pane_id):
        super().add_pane(tab_id, pane_id)
        self.panes[-1].update(label="Shell " + pane_id, terminal_id="terminal-" + pane_id)

    def call(self, method, **arguments):
        if method in self.failed_renames:
            self.calls.append((method, copy.deepcopy(arguments)))
            raise HerdrError("Rename temporarily unavailable")
        if method in ("workspace.rename", "tab.rename"):
            self.calls.append((method, copy.deepcopy(arguments)))
            kind = method.split(".", 1)[0]
            items = self.workspaces if kind == "workspace" else self.tabs
            item = next(item for item in items if item[kind + "_id"] == arguments[kind + "_id"])
            label = arguments["label"]
            if not isinstance(label, str):
                raise HerdrError("Workspace and tab labels must be strings")
            item["label"] = label
            return {}
        if method == "pane.report_metadata":
            self.calls.append((method, copy.deepcopy(arguments)))
            pane = next(pane for pane in self.panes if pane["pane_id"] == arguments["pane_id"])
            if "tokens" in arguments:
                pane["tokens"] = dict(arguments["tokens"])
            if "title" in arguments or arguments.get("clear_title"):
                identity = pane.get("terminal_id") or pane["pane_id"]
                titles = self.reported_titles.setdefault(identity, {"original": pane.get("title")})
                source = arguments.get("source", "default")
                titles.pop(source, None)
                if "title" in arguments:
                    titles[source] = arguments["title"]
                pane["title"] = next((title for title in reversed(list(titles.values())) if title), None)
            return {}
        result = super().call(method, **arguments)
        if method == "pane.rename":
            pane = next(pane for pane in self.panes if pane["pane_id"] == arguments["pane_id"])
            pane["label"] = (arguments["label"] or "").strip() or None
        elif method == "plugin.pane.open" and "plugin_pane" in result:
            pane = next(pane for pane in self.panes if pane["pane_id"] == result["plugin_pane"]["pane"]["pane_id"])
            pane["terminal_id"] = "terminal-" + pane["pane_id"]
            result["plugin_pane"]["pane"] = copy.deepcopy(pane)
        return result


class IndicatorHarness(AppHarness):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": "{}"})
        environment.start()
        self.addCleanup(environment.stop)
        self.api = IndicatorHerdr()
        self.app = self.make_app(self.api)

    def item(self, kind, identifier=None, api=None):
        api = api or self.api
        items, key, default = {
            "space": (api.workspaces, "workspace_id", "space-1"),
            "tab": (api.tabs, "tab_id", "tab-1"),
            "pane": (api.panes, "pane_id", "shell-1"),
        }[kind]
        return next(item for item in items if item[key] == (identifier or default))

    def visible(self, kind, identifier=None, api=None):
        item = self.item(kind, identifier, api)
        return (item.get("title") or item.get("label")) if kind == "pane" else item["label"]

    def marked_label(self, kind, original):
        return {"space": original + " ✎", "tab": original + "✎", "pane": "✎ " + original}[kind]

    def note(self, kind, content="saved notes", **kwargs):
        record = self.seed(kind, **kwargs)
        Path(record["file_path"]).write_text(content, encoding="utf-8")
        return record

    def save(self, record, content, base=None):
        if base is None:
            base = record["content"]
        return self.poll(record, editor="simple", modified=True, save={
            "content": content,
            "expected_digest": hashlib.sha256(base.encode("utf-8")).hexdigest(),
        })

    def indicators(self, app=None):
        app = app or self.app
        return NameIndicators(app.api, app.runtime, app.store)

    def rename_calls(self):
        return [(method, arguments) for method, arguments in self.api.calls
                if method in ("workspace.rename", "tab.rename", "pane.rename")
                or method == "pane.report_metadata" and arguments.get("source") == "herdr-description.indicator"]



class IndicatorTest(IndicatorHarness, unittest.TestCase):
    def test_indicators_are_off_initially_even_for_saved_notes(self):
        originals = {kind: self.visible(kind) for kind in ("space", "tab", "pane")}
        for kind in originals:
            self.note(kind)
        self.assertFalse(self.indicators().sync(self.api.snapshot()))
        self.assertEqual(originals, {kind: self.visible(kind) for kind in originals})
        self.assertEqual([], self.rename_calls())

    def test_saved_scopes_mark_their_ancestors_without_marking_siblings(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-2", "tab-3", "shell-3")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        self.app.action("toggle-indicators")
        for record in records:
            self.assertTrue(self.save(record, record["kind"] + " notes")["saved"])
        self.assertEqual("Workspace space-1 ✎", self.item("space")["label"])
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        self.assertEqual("✎ Shell shell-1", self.visible("pane"))
        self.assertEqual("Tab tab-2", self.item("tab", "tab-2")["label"])
        self.assertEqual("Shell shell-2", self.item("pane", "shell-2")["label"])
        self.assertEqual("Workspace space-2", self.item("space", "space-2")["label"])
        for record in records:
            self.assertFalse(self.native_pane(record)["label"].startswith("✎"))

    def test_shared_space_description_does_not_mark_its_tabs_or_sources(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("space")
        self.write(record, "shared notes")
        self.focus("shell-2")
        self.app.action("toggle-indicators")
        self.assertEqual("Workspace space-1 ✎", self.item("space")["label"])
        for tab in self.api.tabs:
            self.assertFalse(tab["label"].endswith("✎"))
        for pane in self.api.panes:
            self.assertFalse((pane.get("title") or pane["label"]).startswith("✎"))

    def test_empty_whitespace_and_unsaved_text_do_not_add_markers(self):
        self.note("space", " \t\n\u2002")
        self.note("pane", "")
        record = self.create("tab")
        self.app.action("toggle-indicators")
        self.poll(record, modified=True, content="unsaved draft")
        for kind in ("space", "tab", "pane"):
            self.assertNotIn("✎", self.visible(kind))
        self.assertEqual(record["content"], self.stored(record).content)

    def test_toggle_off_restores_names_and_preserves_notes(self):
        records = [self.note(kind) for kind in ("space", "tab", "pane")]
        originals = {kind: self.visible(kind) for kind in ("space", "tab", "pane")}
        self.app.action("toggle-indicators")
        self.app.action("toggle-indicators")
        for record in records:
            self.assertEqual(originals[record["kind"]], self.item(record["kind"])["label"])
            self.assertEqual("saved notes", self.stored(record).content)

    def test_toggle_persists_for_same_session_and_isolates_other_session(self):
        self.note("tab")
        self.app.action("toggle-indicators")
        reopened = self.make_app(self.api)
        reopened.event({"type": "tab_renamed", "tab_id": "tab-1"})
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        other_api = IndicatorHerdr("/tmp/herdr-other-named-session.sock")
        other = self.make_app(other_api)
        self.note("tab", app=other)
        other.event({"type": "tab_renamed", "tab_id": "tab-1"})
        self.assertEqual("Tab tab-1", self.item("tab", api=other_api)["label"])
        reopened.action("toggle-indicators")
        self.assertEqual("Tab tab-1", self.item("tab")["label"])
        other.action("toggle-indicators")
        self.assertEqual("Tab tab-1✎", self.item("tab", api=other_api)["label"])
        self.assertEqual("Tab tab-1", self.item("tab")["label"])

    def test_natural_star_is_preserved_and_repeated_sync_does_not_rename_again(self):
        originals = {}
        for kind in ("space", "tab", "pane"):
            self.item(kind)["label"] += " *"
            originals[kind] = self.visible(kind)
            self.note(kind)
        indicators = self.indicators()
        self.assertTrue(indicators.toggle(self.api.snapshot()))
        before = len(self.rename_calls())
        for _ in range(3):
            self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual(before, len(self.rename_calls()))
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        self.assertFalse(indicators.toggle(self.api.snapshot()))
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))

    def test_manual_renames_while_enabled_survive_toggle_off(self):
        for kind in ("space", "tab", "pane"):
            self.note(kind)
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        for kind in ("space", "tab", "pane"):
            self.item(kind)["label"] = "Renamed " + kind
        self.assertTrue(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind in ("space", "tab", "pane"):
            self.assertEqual("Renamed " + kind, self.visible(kind))

    def test_automatic_pane_label_stays_automatic_after_toggle_off(self):
        pane = self.item("pane")
        pane.update(label=None, display_agent="Shell", terminal_title="OSC command title")
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual("✎", pane["title"])
        self.assertIsNone(pane["label"])
        indicators.toggle(self.api.snapshot())
        self.assertIsNone(pane["label"])
        self.assertEqual("OSC command title", pane["terminal_title"])
        self.assertIsNone(pane["title"])
        self.assertEqual([], self.methods("pane.rename"))
        self.assertIn({
            "pane_id": "shell-1", "source": "herdr-description.indicator", "clear_title": True,
        }, self.methods("pane.report_metadata"))

    def test_unnamed_marked_pane_remains_a_valid_source_during_app_poll(self):
        pane = self.item("pane")
        pane.update(label=None, terminal_title="Automatic terminal title")
        record = self.create("pane")
        self.write(record, "saved pane note")
        self.app.action("toggle-indicators")
        self.assertEqual("✎", pane["title"])
        self.poll(record)
        source = next(source for source in self.app.runtime.view("tab-1")["panes"]
                      if source["pane_id"] == "shell-1")
        self.assertEqual("", source["label"])
        self.assertEqual("saved pane note", self.stored(record).content)
        self.app.action("toggle-indicators")
        self.poll(record)
        self.assertIsNone(pane["label"])
        self.assertIsNone(pane["title"])
        self.assertEqual("Automatic terminal title", pane["terminal_title"])

    def test_pane_indicator_preserves_reported_title_manual_name_and_metadata(self):
        pane = self.item("pane")
        pane["terminal_title"] = "OSC terminal title"
        self.api.call(
            "pane.report_metadata", pane_id="shell-1", source="shell",
            title="Build running", tokens={"project": "example"},
        )
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual("✎ Build running", self.visible("pane"))
        self.assertEqual("Shell shell-1", pane["label"])
        self.assertEqual("OSC terminal title", pane["terminal_title"])
        self.assertEqual({"project": "example"}, pane["tokens"])
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Build running", self.visible("pane"))
        self.assertEqual("Shell shell-1", pane["label"])
        self.assertEqual({"project": "example"}, pane["tokens"])
        self.assertEqual([], self.methods("pane.rename"))

    def test_new_reported_title_gets_one_marker_and_survives_toggle_off(self):
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Old command")
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="New command *")
        indicators.sync(self.api.snapshot())
        self.assertEqual("✎ New command *", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("New command *", self.visible("pane"))

    def test_workspace_and_tab_restore_previous_visible_auto_names(self):
        self.item("space")["label"] = "project-directory"
        self.item("tab")["label"] = "1"
        self.note("space")
        self.note("tab")
        self.app.action("toggle-indicators")
        self.app.action("toggle-indicators")
        self.assertEqual("project-directory", self.item("space")["label"])
        self.assertEqual("1", self.item("tab")["label"])
        self.assertTrue(all(arguments["label"] for method, arguments in self.rename_calls()
                            if method in ("workspace.rename", "tab.rename")))

    def test_external_file_edits_add_and_clear_marker_on_next_poll(self):
        record = self.create("tab")
        self.app.action("toggle-indicators")
        path = Path(record["file_path"])
        path.write_text("external notes", encoding="utf-8")
        self.poll(record)
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        path.write_text(" \n\t", encoding="utf-8")
        self.poll(record)
        self.assertEqual("Tab tab-1", self.item("tab")["label"])

    def test_saving_whitespace_removes_markers_and_keeps_description_records(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        self.app.action("toggle-indicators")
        for record in records:
            self.assertTrue(self.save(record, "notes")["saved"])
        for record in reversed(records):
            self.assertTrue(self.save(record, " \n\t", base="notes")["saved"])
            self.assertNotIn("✎", self.visible(record["kind"]))
            self.assertEqual(" \n\t", self.stored(record).content)

    def test_remove_own_tab_note_keeps_markers_for_descendant_pane_note(self):
        space = self.note("space")
        tab = self.note("tab")
        pane = self.note("pane")
        self.app.action("toggle-indicators")
        self.app.action("remove-tab")
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        self.assertEqual("Workspace space-1 ✎", self.item("space")["label"])
        self.assertEqual("✎ Shell shell-1", self.visible("pane"))
        self.assertIsNone(self.stored(tab))
        self.assertEqual("saved notes", self.stored(space).content)
        self.assertEqual("saved notes", self.stored(pane).content)

    def test_ctrl_x_removes_marker_without_saving_draft(self):
        record = self.create("tab")
        self.write(record, "saved notes")
        self.app.action("toggle-indicators")
        session = EditorSession(
            Path(record["file_path"]),
            lambda payload: self.app.section_tick("tab-1", record["description_id"], payload),
        )
        session.tick()
        session.buffer.insert("unsaved draft")
        editor = EditorScreen(None, session)
        editor.handle_key("\x18")
        self.assertTrue(session.closed)
        self.assertEqual("Tab tab-1", self.item("tab")["label"])
        self.assertIsNone(self.stored(record))
        self.assertFalse(Path(record["file_path"]).exists())

    def test_excluded_plugin_pane_is_not_marked_even_with_a_mapping(self):
        self.api.add_pane("tab-1", "plugin-editor")
        self.note("pane", pane_id="plugin-editor")
        self.note("pane")
        self.indicators().toggle(self.api.snapshot(), excluded_pane_ids={"plugin-editor"})
        self.assertEqual("Shell plugin-editor", self.visible("pane", "plugin-editor"))
        self.assertEqual("✎ Shell shell-1", self.visible("pane"))

    def test_rename_failure_does_not_fail_save_and_later_poll_retries(self):
        record = self.create("tab")
        self.app.action("toggle-indicators")
        self.api.failed_renames.add("tab.rename")
        response = self.save(record, "durable saved notes")
        self.assertTrue(response["saved"])
        self.assertEqual("durable saved notes", self.stored(record).content)
        self.assertEqual("Tab tab-1", self.item("tab")["label"])
        self.api.failed_renames.clear()
        reopened = self.make_app(self.api)
        self.poll(record, app=reopened)
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])

    def test_failed_restore_retries_after_reopening_controller(self):
        self.note("tab")
        self.app.action("toggle-indicators")
        self.api.failed_renames.add("tab.rename")
        with self.assertRaises(HerdrError):
            self.app.action("toggle-indicators")
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        self.api.failed_renames.clear()
        reopened = self.make_app(self.api)
        self.assertTrue(self.indicators(reopened).sync(self.api.snapshot()))
        self.assertEqual("Tab tab-1", self.item("tab")["label"])

    def test_lost_rename_response_recovers_without_doubling_marker(self):
        self.note("tab")
        original_call = self.api.call

        def lost_response(method, **arguments):
            result = original_call(method, **arguments)
            if method == "tab.rename":
                raise HerdrError("Response lost after applying rename")
            return result

        with patch.object(self.api, "call", side_effect=lost_response):
            with self.assertRaises(HerdrError):
                self.indicators().toggle(self.api.snapshot())
        reopened = self.make_app(self.api)
        indicators = self.indicators(reopened)
        indicators.sync(self.api.snapshot())
        self.assertEqual("Tab tab-1✎", self.item("tab")["label"])
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Tab tab-1", self.item("tab")["label"])

    def test_reused_pane_id_does_not_mark_or_restore_replacement_terminal(self):
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.item("pane").update(terminal_id="replacement-terminal", label="Replacement")
        self.item("pane").pop("title", None)
        before = len(self.methods("pane.report_metadata"))
        indicators.sync(self.api.snapshot())
        self.assertEqual("Replacement", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual("Replacement", self.visible("pane"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Replacement", self.visible("pane"))
        self.assertEqual(before, len(self.methods("pane.report_metadata")))

    def test_cross_workspace_move_keeps_one_marker_and_restores_original_name(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        record = self.create("pane")
        self.write(record, "portable notes")
        self.app.action("toggle-indicators")
        terminal_id = self.item("pane")["terminal_id"]
        self.api.move_pane("shell-1", "tab-2", "moved-shell")
        moved = self.item("pane", "moved-shell")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": copy.deepcopy(moved),
        })
        self.assertEqual(terminal_id, moved["terminal_id"])
        self.assertEqual("✎ Shell shell-1", moved["title"])
        self.assertEqual("portable notes", self.app.descriptions("space-2", "tab-2")[0]["content"])
        self.app.action("toggle-indicators")
        self.assertEqual("Shell shell-1", self.visible("pane", "moved-shell"))

    def test_closing_marked_tab_preserves_other_markers_without_renaming_dead_items(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("tab")
        self.write(record, "closed tab notes")
        source_record = self.note("pane")
        self.note("space")
        self.note("tab", tab_id="tab-2")
        self.app.action("toggle-indicators")
        self.api.remove_tab("tab-1")
        self.api.calls.clear()
        self.app.event({"type": "tab_closed", "workspace_id": "space-1", "tab_id": "tab-1"})
        self.assertIsNone(self.stored(record))
        self.assertIsNone(self.stored(source_record))
        self.assertEqual("Workspace space-1 ✎", self.item("space")["label"])
        self.assertEqual("Tab tab-2✎", self.item("tab", "tab-2")["label"])
        self.assertFalse(any(arguments.get("tab_id") == "tab-1"
                             for method, arguments in self.rename_calls()))
        self.assertFalse(any(arguments.get("pane_id") == "shell-1"
                             for method, arguments in self.rename_calls()))
        self.app.action("toggle-indicators")
        self.assertEqual("Tab tab-2", self.item("tab", "tab-2")["label"])


if __name__ == "__main__":
    unittest.main()
