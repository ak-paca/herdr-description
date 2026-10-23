import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.api import HerdrError
from tests.test_indicators import IndicatorHarness, IndicatorHerdr


class IndicatorInheritanceTest(IndicatorHarness, unittest.TestCase):
    def test_pane_note_marks_only_pane_and_its_ancestors(self):
        self.api.add_pane("tab-1", "sibling-pane")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-2", "tab-3", "shell-3")
        self.note("pane")
        self.app.action("toggle-indicators")
        self.assertEqual("✎ Shell shell-1", self.visible("pane"))
        self.assertEqual("Tab tab-1✎", self.visible("tab"))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Shell sibling-pane", self.visible("pane", "sibling-pane"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))
        self.assertEqual("Shell shell-2", self.visible("pane", "shell-2"))
        self.assertEqual("Workspace space-2", self.visible("space", "space-2"))
        self.assertEqual("Tab tab-3", self.visible("tab", "tab-3"))
        self.assertEqual("Shell shell-3", self.visible("pane", "shell-3"))

    def test_tab_note_marks_its_space_without_marking_child_panes_or_sibling_tab(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.note("tab")
        self.app.action("toggle-indicators")
        self.assertEqual("Tab tab-1✎", self.visible("tab"))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))
        self.assertEqual("Shell shell-2", self.visible("pane", "shell-2"))

    def test_empty_ancestor_notes_do_not_hide_nonempty_descendant_note(self):
        self.note("space", " \n\t")
        self.note("tab", "")
        self.note("pane")
        self.app.action("toggle-indicators")
        for kind in ("space", "tab", "pane"):
            self.assertTrue(self.visible(kind).startswith("✎ ") if kind == "pane"
                            else self.visible(kind).endswith("✎"))

    def test_clearing_own_ancestor_notes_keeps_markers_until_last_child_is_cleared(self):
        records = [self.note(kind) for kind in ("space", "tab", "pane")]
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        for record in records[:2]:
            Path(record["file_path"]).write_text(" \n", encoding="utf-8")
            indicators.sync(self.api.snapshot())
            for kind in ("space", "tab", "pane"):
                self.assertTrue(self.visible(kind).startswith("✎ ") if kind == "pane"
                            else self.visible(kind).endswith("✎"))
        Path(records[-1]["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertEqual(3, len(self.app.store.list_mappings(self.app.session)))

    def test_clearing_and_deleting_multiple_descendants_recomputes_each_ancestor(self):
        self.api.add_pane("tab-1", "sibling-pane")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        first = self.note("pane")
        sibling = self.note("pane", pane_id="sibling-pane")
        other_tab = self.note("tab", tab_id="tab-2")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual(1, len(self.methods("workspace.rename")))
        Path(first["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Tab tab-1✎", self.visible("tab"))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.app.store.delete(self.app.session, sibling["kind"], sibling["target_id"])
        indicators.sync(self.api.snapshot())
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Tab tab-2✎", self.visible("tab", "tab-2"))
        self.app.store.delete(self.app.session, other_tab["kind"], other_tab["target_id"])
        indicators.sync(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))

    def test_closing_last_contributing_pane_clears_ancestors(self):
        self.api.add_pane("tab-1", "sibling-pane")
        record = self.note("pane")
        self.app.action("toggle-indicators")
        self.api.remove_pane("shell-1")
        self.api.calls.clear()
        self.closed_event("shell-1")
        self.assertIsNone(self.stored(record))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Shell sibling-pane", self.visible("pane", "sibling-pane"))
        self.assertFalse(any(arguments.get("pane_id") == "shell-1"
                             for _, arguments in self.rename_calls()))

    def test_closing_last_contributing_tab_clears_shared_space(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.note("pane")
        self.app.action("toggle-indicators")
        self.api.remove_tab("tab-1")
        self.api.calls.clear()
        self.app.event({"type": "tab_closed", "workspace_id": "space-1", "tab_id": "tab-1"})
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))
        self.assertFalse(any(arguments.get("tab_id") == "tab-1"
                             for _, arguments in self.rename_calls()))

    def test_moves_transfer_ancestor_markers_and_restore_original_names(self):
        self.api.add_pane("tab-1", "source-sibling")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-2", "tab-3", "shell-3")
        originals = {
            ("space", "space-1"): "Original space *",
            ("space", "space-2"): "Destination ✎",
            ("tab", "tab-1"): "Source tab ✎",
            ("tab", "tab-2"): "Same-space tab *",
            ("tab", "tab-3"): "Other-space tab ✎",
        }
        for (kind, identifier), name in originals.items():
            self.item(kind, identifier)["label"] = name
        record = self.create("pane")
        self.write(record, "portable note")
        self.app.action("toggle-indicators")
        self.api.move_pane("shell-1", "tab-2", "moved-once")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": copy.deepcopy(self.item("pane", "moved-once")),
        })
        self.assertEqual("Source tab ✎", self.visible("tab", "tab-1"))
        self.assertEqual("Same-space tab *✎", self.visible("tab", "tab-2"))
        self.assertEqual("Original space * ✎", self.visible("space", "space-1"))
        self.assertEqual("Destination ✎", self.visible("space", "space-2"))
        self.api.move_pane("moved-once", "tab-3", "moved-twice")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "moved-once",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-2",
            "pane": copy.deepcopy(self.item("pane", "moved-twice")),
        })
        self.assertEqual("Same-space tab *", self.visible("tab", "tab-2"))
        self.assertEqual("Original space *", self.visible("space", "space-1"))
        self.assertEqual("Other-space tab ✎✎", self.visible("tab", "tab-3"))
        self.assertEqual("Destination ✎ ✎", self.visible("space", "space-2"))
        self.assertEqual("✎ Shell shell-1", self.visible("pane", "moved-twice"))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))
        self.app.action("toggle-indicators")
        for (kind, identifier), name in originals.items():
            self.assertEqual(name, self.visible(kind, identifier))
        self.assertEqual("Shell shell-1", self.visible("pane", "moved-twice"))

    def test_missing_child_file_preserves_existing_ancestor_markers(self):
        record = self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        Path(record["file_path"]).unlink()
        self.assertFalse(indicators.sync(self.api.snapshot()))
        for kind in ("space", "tab", "pane"):
            self.assertTrue(self.visible(kind).startswith("✎ ") if kind == "pane"
                            else self.visible(kind).endswith("✎"))
        Path(record["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))

    def test_unreadable_child_does_not_hide_known_good_sibling(self):
        self.api.add_pane("tab-1", "sibling-pane")
        unreadable = self.note("pane")
        sibling = self.note("pane", pane_id="sibling-pane")
        get = self.app.store.get

        def read(session_id, kind, target_id):
            if target_id == unreadable["target_id"]:
                raise PermissionError("Temporarily unreadable note")
            return get(session_id, kind, target_id)

        indicators = self.indicators()
        with patch.object(self.app.store, "get", side_effect=read):
            indicators.toggle(self.api.snapshot())
            self.assertEqual("Workspace space-1 ✎", self.visible("space"))
            self.assertEqual("Tab tab-1✎", self.visible("tab"))
            self.assertEqual("✎ Shell sibling-pane", self.visible("pane", "sibling-pane"))
            self.assertEqual("Shell shell-1", self.visible("pane"))
            Path(sibling["file_path"]).write_text("", encoding="utf-8")
            indicators.sync(self.api.snapshot())
            self.assertEqual("Workspace space-1 ✎", self.visible("space"))
            self.assertEqual("Tab tab-1✎", self.visible("tab"))
        Path(unreadable["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))

    def test_excluded_plugin_note_does_not_mark_any_ancestor(self):
        self.api.add_pane("tab-1", "plugin-editor")
        self.item("pane", "plugin-editor")["tokens"] = {"herdr_description": "editor"}
        self.note("pane", pane_id="plugin-editor")
        self.indicators().toggle(self.api.snapshot())
        self.assertEqual("Shell plugin-editor", self.visible("pane", "plugin-editor"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual([], self.rename_calls())

    def test_explicitly_excluded_note_does_not_mark_any_ancestor(self):
        self.note("pane")
        self.indicators().toggle(self.api.snapshot(), excluded_pane_ids={"shell-1"})
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1", self.visible("space"))

    def test_reused_pane_identity_removes_inherited_markers_without_marking_replacement(self):
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.item("pane").update(terminal_id="replacement-terminal", label="Replacement")
        self.item("pane").pop("title", None)
        indicators.sync(self.api.snapshot())
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Replacement", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Workspace space-1", self.visible("space"))

    def test_stale_pane_mapping_after_unreconciled_move_does_not_mark_ancestors(self):
        self.api.add_pane("tab-1", "sibling-pane")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.api.move_pane("shell-1", "tab-2")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Tab tab-2", self.visible("tab", "tab-2"))
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Shell shell-1", self.visible("pane"))

    def test_other_session_does_not_inherit_markers_with_same_host_identifiers(self):
        self.note("pane")
        self.app.action("toggle-indicators")
        other_api = IndicatorHerdr("/tmp/independent-indicator-session.sock")
        other = self.make_app(other_api)
        other.action("toggle-indicators")
        self.assertEqual("Workspace space-1", self.visible("space", api=other_api))
        self.assertEqual("Tab tab-1", self.visible("tab", api=other_api))
        self.assertEqual("Shell shell-1", self.visible("pane", api=other_api))
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Tab tab-1✎", self.visible("tab"))


class WorkspaceNameTest(IndicatorHarness, unittest.TestCase):
    def test_all_untracked_workspaces_keep_original_names_without_any_notes(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        self.api.add_tab("space-3", "tab-3", "shell-3")
        originals = {"space-1": "  Project", "space-2": "✎Natural pencil", "space-3": "Project *"}
        for identifier, name in originals.items():
            self.item("space", identifier)["label"] = name
            self.assertIsNone(self.app.runtime.identity(identifier))
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual([], self.app.store.list_mappings(self.app.session))
        for identifier, name in originals.items():
            self.assertEqual(name, self.visible("space", identifier))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual([], self.rename_calls())
        indicators.toggle(self.api.snapshot())
        for identifier, name in originals.items():
            self.assertEqual(name, self.visible("space", identifier))

    def test_untracked_workspace_is_unchanged_next_to_marked_workspace(self):
        self.note("pane")
        self.api.add_tab("space-2", "tab-2", "shell-2")
        self.assertIsNone(self.app.runtime.identity("space-2"))
        self.indicators().toggle(self.api.snapshot())
        self.assertEqual("Workspace space-1 ✎", self.visible("space"))
        self.assertEqual("Workspace space-2", self.visible("space", "space-2"))

    def test_unreadable_first_note_does_not_change_names(self):
        record = self.note("pane")
        Path(record["file_path"]).unlink()
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Workspace space-1", self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual([], self.rename_calls())

    def test_new_workspace_keeps_original_name_while_indicators_are_enabled(self):
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.api.add_tab("space-2", "tab-2", "shell-2")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Workspace space-2", self.visible("space", "space-2"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual([], self.rename_calls())

    def test_assigning_identity_and_editing_first_note_preserves_original_leading_spaces(self):
        original = "  Project ✎ *"
        self.item("space")["label"] = original
        self.assertIsNone(self.app.runtime.identity("space-1"))
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        record = self.note("pane", "")
        self.assertIsNotNone(self.app.runtime.identity("space-1"))
        indicators.sync(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        Path(record["file_path"]).write_text("first descendant note", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual(original + " ✎", self.visible("space"))
        Path(record["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))

    def test_manual_unmarked_workspace_renames_preserve_user_leading_spaces(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.item("space")["label"] = "Renamed without leading spaces"
        self.item("space", "space-2")["label"] = "  Renamed with leading spaces"
        indicators.sync(self.api.snapshot())
        self.assertEqual("Renamed without leading spaces", self.visible("space"))
        self.assertEqual("  Renamed with leading spaces", self.visible("space", "space-2"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Renamed without leading spaces", self.visible("space"))
        self.assertEqual("  Renamed with leading spaces", self.visible("space", "space-2"))

    def test_manual_marked_workspace_rename_survives_clearing_last_note(self):
        record = self.note("pane")
        indicators = self.indicators()
        indicators.toggle(self.api.snapshot())
        self.item("space")["label"] = "Renamed project * ✎"
        indicators.sync(self.api.snapshot())
        self.assertEqual("Renamed project * ✎", self.visible("space"))
        Path(record["file_path"]).write_text("", encoding="utf-8")
        indicators.sync(self.api.snapshot())
        self.assertEqual("Renamed project *", self.visible("space"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("Renamed project *", self.visible("space"))


class IndicatorMigrationTest(IndicatorHarness, unittest.TestCase):
    def legacy_padding(self, prefix="  ", record=None):
        original = "  Natural spaces ✎ *"
        self.item("space")["label"] = prefix + original
        self.app.runtime.put_indicator_state({"enabled": True, "entries": {
            "space:space-1": {
                "kind": "space", "host_id": "space-1", "workspace_id": "space-1",
                "space_id": self.app.identity("space-1") if record else None,
                "terminal_id": None, "description_id": record["description_id"] if record else None,
                "original_label": original, "decorated_label": prefix + original,
                "prefix": prefix, "suffix": "", "confirmed": True, "pending": False,
            },
        }})
        return original

    def legacy_markers(self, suffix=" *", prefix=None):
        entries = {}
        originals = {}
        for kind, host_id in (("space", "space-1"), ("tab", "tab-1"), ("pane", "shell-1")):
            item = self.item(kind)
            item["label"] += " * ✎"
            original_title = "Command ✎ *" if kind == "pane" else None
            if kind == "pane":
                self.api.call("pane.report_metadata", pane_id=host_id, source="shell", title=original_title)
            originals[kind] = self.visible(kind)
            record = self.note(kind)
            entry = {
                "kind": kind, "host_id": host_id, "workspace_id": "space-1",
                "space_id": self.app.identity("space-1"), "terminal_id": item.get("terminal_id"),
                "description_id": record["description_id"], "original_label": item["label"],
                "decorated_label": originals[kind] + suffix, "confirmed": True, "pending": False,
            }
            if prefix is not None:
                entry.update(prefix=prefix, suffix="", decorated_label=prefix + originals[kind])
            elif suffix != " *":
                entry["suffix"] = suffix
            if kind == "pane":
                entry["original_title"] = original_title
                self.api.call(
                    "pane.report_metadata", pane_id=host_id,
                    source="herdr-description.indicator", title=entry["decorated_label"],
                )
            else:
                item["label"] = entry["decorated_label"]
            entries[kind + ":" + host_id] = entry
        self.app.runtime.put_indicator_state({"enabled": True, "entries": entries})
        return originals

    def test_legacy_markers_upgrade_once_preserving_natural_stars_and_pencils(self):
        originals = self.legacy_markers()
        indicators = self.indicators(self.make_app(self.api))
        self.assertTrue(indicators.sync(self.api.snapshot()))
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        before = len(self.rename_calls())
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        self.assertEqual(before, len(self.rename_calls()))
        self.assertFalse(indicators.toggle(self.api.snapshot()))
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))
        self.assertFalse(indicators.sync(self.api.snapshot()))

    def test_disabling_legacy_markers_before_sync_restores_stored_originals(self):
        originals = self.legacy_markers()
        self.assertFalse(self.indicators().toggle(self.api.snapshot()))
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_pencil_suffix_migrates_to_current_format_preserving_original_name(self):
        originals = self.legacy_markers(suffix=" ✎")
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))

    def test_single_column_pencil_prefix_migrates_to_current_format(self):
        originals = self.legacy_markers(prefix="✎")
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))

    def test_two_column_pencil_prefix_migrates_workspace_and_tab_to_suffix(self):
        originals = self.legacy_markers(prefix="✎ ")
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))

    def test_single_column_padding_is_removed_without_changing_original_leading_spaces(self):
        original = self.legacy_padding(prefix=" ")
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))

    def test_two_column_padding_is_removed_on_enabled_sync(self):
        original = self.legacy_padding()
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))

    def test_disabling_legacy_padding_preserves_original_leading_spaces(self):
        original = self.legacy_padding()
        self.indicators(self.make_app(self.api)).toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_missing_note_does_not_prevent_removing_owned_workspace_padding(self):
        record = self.note("pane")
        Path(record["file_path"]).unlink()
        original = self.legacy_padding(record=record)
        indicators = self.indicators()
        indicators.sync(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))
        self.assertEqual("Tab tab-1", self.visible("tab"))
        self.assertEqual("Shell shell-1", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))

    def test_removing_legacy_padding_preserves_manual_rename(self):
        self.legacy_padding()
        self.item("space")["label"] = "  Renamed  project ✎"
        self.indicators().sync(self.api.snapshot())
        self.assertEqual("Renamed  project ✎", self.visible("space"))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_failed_padding_removal_retries_on_toggle_off(self):
        original = self.legacy_padding()
        self.api.failed_renames.add("workspace.rename")
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        self.api.failed_renames.clear()
        self.indicators(self.make_app(self.api)).toggle(self.api.snapshot())
        self.assertEqual(original, self.visible("space"))

    def test_manual_rename_with_old_prefix_preserves_natural_leading_spaces_and_pencil(self):
        self.legacy_markers(prefix="✎ ")
        self.item("space")["label"] = "✎   Renamed space *"
        self.item("tab")["label"] = "✎ ✎Natural tab"
        indicators = self.indicators()
        indicators.sync(self.api.snapshot())
        self.assertEqual("  Renamed space * ✎", self.visible("space"))
        self.assertEqual("✎Natural tab✎", self.visible("tab"))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("  Renamed space *", self.visible("space"))
        self.assertEqual("✎Natural tab", self.visible("tab"))

    def test_disabling_pencil_suffix_before_sync_removes_only_plugin_suffix(self):
        originals = self.legacy_markers(suffix=" ✎")
        self.indicators().toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))

    def test_manual_rename_with_pencil_suffix_migrates_without_losing_natural_prefix(self):
        self.legacy_markers(suffix=" ✎")
        self.item("space")["label"] = "✎Natural pencil space ✎"
        self.item("tab")["label"] = "Renamed tab ✎"
        indicators = self.indicators()
        indicators.sync(self.api.snapshot())
        self.assertEqual("✎Natural pencil space ✎", self.visible("space"))
        self.assertEqual("Renamed tab✎", self.visible("tab"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        self.assertEqual("✎Natural pencil space", self.visible("space"))
        self.assertEqual("Renamed tab", self.visible("tab"))

    def test_legacy_manual_renames_with_old_suffix_upgrade_and_restore_once(self):
        self.legacy_markers()
        for kind in ("space", "tab"):
            self.item(kind)["label"] = "Renamed " + kind + " *"
        self.api.call("pane.report_metadata", pane_id="shell-1", source="shell", title="New command *")
        indicators = self.indicators()
        indicators.sync(self.api.snapshot())
        for kind in ("space", "tab"):
            self.assertEqual(self.marked_label(kind, "Renamed " + kind), self.visible(kind))
        self.assertEqual("✎ New command *", self.visible("pane"))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind in ("space", "tab"):
            self.assertEqual("Renamed " + kind, self.visible(kind))
        self.assertEqual("New command *", self.visible("pane"))

    def test_disabling_legacy_manual_renames_preserves_new_names(self):
        self.legacy_markers()
        self.item("space")["label"] = "Renamed space *"
        self.item("tab")["label"] = "Renamed tab without marker"
        self.indicators().toggle(self.api.snapshot())
        self.assertEqual("Renamed space", self.visible("space"))
        self.assertEqual("Renamed tab without marker", self.visible("tab"))

    def test_disabling_after_failed_migration_restores_old_star_marker(self):
        originals = self.legacy_markers()
        self.api.failed_renames.add("tab.rename")
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        self.assertEqual(originals["tab"] + " *", self.visible("tab"))
        self.api.failed_renames.clear()
        self.indicators(self.make_app(self.api)).toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_disabling_after_failed_prefix_migration_restores_original_names(self):
        originals = self.legacy_markers(prefix="✎ ")
        self.api.failed_renames.update({"workspace.rename", "tab.rename"})
        with self.assertRaises(HerdrError):
            self.indicators().sync(self.api.snapshot())
        for kind in ("space", "tab"):
            self.assertEqual("✎ " + originals[kind], self.visible(kind))
        self.api.failed_renames.clear()
        self.indicators(self.make_app(self.api)).toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_lost_prefix_migration_response_can_toggle_off_without_retry(self):
        originals = self.legacy_markers(prefix="✎ ")
        call = self.api.call

        def lost_response(method, **arguments):
            result = call(method, **arguments)
            if method in ("workspace.rename", "tab.rename"):
                raise HerdrError("Response lost after applying prefix migration")
            return result

        with patch.object(self.api, "call", side_effect=lost_response):
            with self.assertRaises(HerdrError):
                self.indicators().sync(self.api.snapshot())
        self.indicators(self.make_app(self.api)).toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))
        self.assertFalse(self.indicators().sync(self.api.snapshot()))

    def test_lost_migration_response_retries_without_duplicate_marker(self):
        originals = self.legacy_markers()
        call = self.api.call

        def lost_response(method, **arguments):
            result = call(method, **arguments)
            if method == "tab.rename":
                raise HerdrError("Response lost after applying migration")
            return result

        with patch.object(self.api, "call", side_effect=lost_response):
            with self.assertRaises(HerdrError):
                self.indicators().sync(self.api.snapshot())
        indicators = self.indicators(self.make_app(self.api))
        indicators.sync(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(self.marked_label(kind, original), self.visible(kind))
        self.assertFalse(indicators.sync(self.api.snapshot()))
        indicators.toggle(self.api.snapshot())
        for kind, original in originals.items():
            self.assertEqual(original, self.visible(kind))


if __name__ == "__main__":
    unittest.main()
