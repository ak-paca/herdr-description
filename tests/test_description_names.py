import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.api import HerdrError
from herdr_description.indicators import NAME_METADATA_SOURCE
from herdr_description.names import description_name, meaningful_description
from tests.test_indicators import IndicatorHarness


class DescriptionNameParsingTest(unittest.TestCase):
    def test_only_first_logical_line_names_the_description(self):
        cases = {
            "-- new name\ncomment": "new name",
            "--foo": "foo",
            "--  spaced name  \r\nbody": "spaced name",
            "-- first\r-- second": "first",
            "-- Привет ✎ *": "Привет ✎ *",
            " -- indented": None,
            "\n-- second line": None,
            "ordinary notes\n-- later": None,
            "-- ": None,
            "": None,
            "-- unsafe\x1b[31m": None,
            "-- unsafe\x00name": None,
            "-- unsafe\tname": None,
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertEqual(expected, description_name(content))

    def test_blank_header_template_is_not_meaningful(self):
        for content in ("", " \t\n", "--", "-- ", "-- \n \t\n"):
            with self.subTest(content=content):
                self.assertFalse(meaningful_description(content))
        for content in ("-- name", "-- \nbody", "body", " -- ", "\n-- "):
            with self.subTest(content=content):
                self.assertTrue(meaningful_description(content))


class DescriptionNameTest(IndicatorHarness, unittest.TestCase):
    def replace_content(self, record, content):
        Path(record["file_path"]).write_text(content, encoding="utf-8")

    def name_calls(self):
        return [arguments for arguments in self.methods("pane.report_metadata")
                if arguments.get("source") == NAME_METADATA_SOURCE]

    def test_headers_rename_each_exact_scope_with_indicators_disabled(self):
        records = {kind: self.note(kind, "-- Named " + kind + "\ncomment")
                   for kind in ("space", "tab", "pane")}
        self.assertTrue(self.indicators().sync(self.api.snapshot()))
        for kind in records:
            self.assertEqual("Named " + kind, self.visible(kind))
            self.assertEqual("Named " + kind, self.item(kind)["label"])
        before = len(self.api.calls)
        self.assertFalse(self.indicators().sync(self.api.snapshot()))
        self.assertEqual(before, len(self.api.calls))

    def test_child_header_does_not_rename_ancestors_or_siblings(self):
        self.api.add_pane("tab-1", "sibling")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.note("pane", "-- Source name")
        self.indicators().toggle(self.api.snapshot())
        self.assertEqual("✎ Source name", self.visible("pane"))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Tab tab-1✎", self.visible("tab"))
        self.assertEqual("Shell sibling", self.visible("pane", "sibling"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))

    def test_blank_template_keeps_names_and_adds_no_marker(self):
        for kind in ("space", "tab", "pane"):
            self.note(kind, "-- \n  ")
        self.indicators().toggle(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertEqual([], self.rename_calls())

    def test_marker_toggles_preserve_new_names_and_natural_pencils(self):
        records = [self.note(kind, "-- New " + kind + " ✎ *") for kind in ("space", "tab", "pane")]
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        for record in records:
            kind = record["kind"]
            self.assertEqual(self.marked_label(kind, "New " + kind + " ✎ *"), self.visible(kind))
            self.replace_content(record, "-- Changed " + kind + " ✎ *\nnotes")
        indicators.sync(self.api.snapshot())
        indicators.toggle(self.api.snapshot())
        for record in records:
            self.assertEqual("Changed " + record["kind"] + " ✎ *", self.visible(record["kind"]))
        self.assertFalse(indicators.sync(self.api.snapshot()))

    def test_new_header_rebases_existing_marker_even_when_toggle_disables_it(self):
        records = [self.note(kind) for kind in ("space", "tab", "pane")]
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        for record in records:
            self.replace_content(record, "-- Renamed " + record["kind"])
        indicators.toggle(self.api.snapshot())
        for record in records:
            self.assertEqual("Renamed " + record["kind"], self.visible(record["kind"]))

    def test_removed_or_blank_header_keeps_last_explicit_name(self):
        records = [self.note(kind, "-- Chosen " + kind) for kind in ("space", "tab", "pane")]
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        for record in records:
            self.replace_content(record, "-- ")
        indicators.sync(self.api.snapshot())
        for record in records:
            self.assertEqual("Chosen " + record["kind"], self.visible(record["kind"]))
        self.assertEqual({}, self.app.runtime.indicator_state()["name_entries"])
        self.item("tab")["label"] = "Manual name after removal"
        indicators.sync(self.api.snapshot())
        self.assertEqual("Manual name after removal", self.visible("tab"))

    def test_header_removal_keeps_inherited_markers_on_last_explicit_names(self):
        space = self.note("space", "-- Project")
        tab = self.note("tab", "-- Task")
        self.note("pane", "child note")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.replace_content(space, "")
        self.replace_content(tab, "")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Project ✎", self.visible("space"))
        self.assertEqual("Task✎", self.visible("tab"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Project", self.visible("space"))
        self.assertEqual("Task", self.visible("tab"))

    def test_pane_header_overrides_reported_title_until_removed(self):
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Build running")
        self.item("pane")["terminal_title"] = "OSC title"
        record = self.note("pane", "-- Working pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual("✎ Working pane", self.visible("pane"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Working pane", self.visible("pane"))
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Build complete")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Working pane", self.visible("pane"))
        self.replace_content(record, "body without a header")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Build complete", self.visible("pane"))
        self.assertEqual("Working pane", self.item("pane")["label"])
        self.assertEqual("OSC title", self.item("pane")["terminal_title"])
        self.assertTrue(self.name_calls()[-1]["clear_title"])

    def test_pane_header_removal_redecorates_native_metadata_when_body_remains(self):
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Native title")
        record = self.note("pane", "-- Header name\nbody")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.replace_content(record, "body remains")
        indicators.sync(self.api.snapshot())
        self.assertEqual("✎ Native title", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Native title", self.visible("pane"))

    def test_deleting_description_clears_only_owned_metadata(self):
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Native title")
        record = self.note("pane", "-- Named source")
        self.indicators().toggle(self.api.snapshot())
        self.app.store.delete(self.app.session, record["kind"], record["target_id"])
        self.indicators().sync(self.api.snapshot())
        self.assertEqual("Native title", self.visible("pane"))
        self.assertEqual("Named source", self.item("pane")["label"])
        self.assertFalse(any(arguments.get("clear_title") and arguments.get("source") == "shell"
                             for arguments in self.methods("pane.report_metadata")))

    def test_unreadable_file_preserves_header_and_other_metadata(self):
        record = self.note("pane", "-- Named source")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.replace_content(record, "ordinary body")
        with patch.object(self.app.store, "get", side_effect=PermissionError("Temporarily unavailable")):
            before = len(self.api.calls)
            self.assertFalse(indicators.sync(self.api.snapshot()))
            self.assertEqual(before, len(self.api.calls))
            self.assertEqual("✎ Named source", self.visible("pane"))
        indicators.sync(self.api.snapshot())
        self.assertEqual("✎ Named source", self.visible("pane"))
        self.assertEqual({}, self.app.runtime.indicator_state()["name_entries"])

    def test_header_rename_failure_retries_after_restart(self):
        self.note("tab", "-- Wanted name")
        self.api.failed_renames.add("tab.rename")
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.api.failed_renames.clear()
        reopened = self.make_app(self.api)
        self.indicators(reopened).sync(self.api.snapshot())
        self.assertEqual("Wanted name", self.visible("tab"))
        self.assertFalse(self.indicators(reopened).sync(self.api.snapshot()))

    def test_pane_metadata_failure_retries_without_losing_explicit_label(self):
        self.note("pane", "-- Wanted name")
        self.api.failed_renames.add("pane.report_metadata")
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        self.assertEqual("Wanted name", self.item("pane")["label"])
        self.api.failed_renames.clear()
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        self.assertEqual("Wanted name", self.item("pane")["title"])
        self.assertFalse(indicators.sync(self.api.snapshot()))

    def test_failed_header_metadata_removal_retries_after_restart(self):
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="Native title")
        record = self.note("pane", "-- Named source")
        self.indicators().toggle(self.api.snapshot())
        self.replace_content(record, "body remains")
        self.api.failed_renames.add("pane.report_metadata")
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        self.api.failed_renames.clear()
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        self.assertEqual("✎ Native title", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))

    def test_lost_rename_response_preserves_natural_marker_in_new_name(self):
        record = self.note("tab")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.replace_content(record, "-- Natural ✎")
        original_call = self.api.call

        def lost_response(method, **arguments):
            result = original_call(method, **arguments)
            if method == "tab.rename":
                raise HerdrError("Lost rename response")
            return result

        with patch.object(self.api, "call", side_effect=lost_response):
            with self.assertRaises(HerdrError):
                indicators.sync(self.api.snapshot())
        self.replace_content(record, "")
        self.indicators(self.make_app(self.api)).sync(self.api.snapshot())
        self.assertEqual("Natural ✎", self.visible("tab"))

    def test_reused_pane_identity_is_not_renamed_or_cleared(self):
        self.note("pane", "-- Named source")
        indicators = self.indicators()
        indicators.sync(self.api.snapshot())
        self.item("pane").update(terminal_id="new-terminal", label="Replacement", title=None)
        before = len(self.api.calls)
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual(before, len(self.api.calls))
        self.assertEqual("Replacement", self.visible("pane"))

    def test_plugin_and_explicitly_excluded_panes_are_not_named(self):
        self.api.add_pane("tab-1", "plugin")
        self.item("pane", "plugin")["tokens"] = {"herdr_description": "editor"}
        self.note("pane", "-- Plugin name", pane_id="plugin")
        self.note("pane", "-- Source name")
        self.indicators().sync(self.api.snapshot(), excluded_pane_ids={"shell-1"})
        self.assertEqual("Shell plugin", self.visible("pane", "plugin"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertEqual([], self.name_calls())

    def test_header_follows_pane_move_using_stable_terminal_identity(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        record = self.create("pane")
        self.write(record, "-- Portable name\nnotes")
        self.app.action("toggle-indicators")
        self.api.move_pane("shell-1", "tab-2", "moved-source")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": copy.deepcopy(self.item("pane", "moved-source")),
        })
        self.assertEqual("✎ Portable name", self.visible("pane", "moved-source"))
        self.assertEqual("Portable name", self.item("pane", "moved-source")["label"])
        self.assertFalse(self.indicators().sync(self.api.snapshot()))
        self.app.action("toggle-indicators")
        self.assertEqual("Portable name", self.visible("pane", "moved-source"))

    def test_each_successful_save_renames_with_indicators_disabled(self):
        record = self.create("tab")
        base = self.stored(record).content
        first = "-- First name\nfirst body"
        self.assertTrue(self.save(record, first, base=base)["saved"])
        self.assertEqual("First name", self.visible("tab"))
        second = "-- Second name\nsecond body"
        self.assertTrue(self.save(record, second, base=first)["saved"])
        self.assertEqual("Second name", self.visible("tab"))
        self.assertEqual(second, self.stored(record).content)

    def test_failed_automatic_rename_does_not_fail_saved_content(self):
        record = self.create("tab")
        self.api.failed_renames.add("tab.rename")
        content = "-- Saved despite rename failure\nbody"
        response = self.save(record, content, base=self.stored(record).content)
        self.assertTrue(response["saved"])
        self.assertIn("Rename temporarily unavailable", response["warning"])
        self.assertEqual(content, self.stored(record).content)
        self.api.failed_renames.clear()
        self.poll(record, app=self.make_app(self.api))
        self.assertEqual("Saved despite rename failure", self.visible("tab"))

    def test_external_header_edits_are_applied_on_next_poll(self):
        record = self.create("pane")
        self.replace_content(record, "-- External name\nnotes")
        self.poll(record)
        self.assertEqual("External name", self.visible("pane"))
        self.replace_content(record, "-- Revised name\nnotes")
        self.poll(record)
        self.assertEqual("Revised name", self.visible("pane"))
