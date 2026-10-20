import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, Mock, patch

from herdr_description.app import edit_settings
from herdr_description.editor import EditorScreen, EditorSession
from tests.test_app import AppHarness


class EditorRemoveTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "description.txt"
        self.path.write_text("original", encoding="utf-8")
        self.poll = Mock(return_value={"removable": True})
        self.session = EditorSession(self.path, self.poll)
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (4, 180)
        self.editor = EditorScreen(self.screen, self.session)

    def test_removal_requires_explicit_backend_support(self):
        for response in ({}, {"removable": False}, {"removable": 1}, {"removable": "true"}):
            with self.subTest(response=response):
                self.poll.return_value = response
                self.session.tick()
                self.poll.reset_mock()
                self.assertFalse(self.session.can_remove)
                self.assertFalse(self.session.remove())
                self.editor.handle_key("\x18")
                self.poll.assert_not_called()
                self.assertFalse(self.session.closed)
                self.assertTrue(self.editor.running)
                self.assertEqual("original", self.path.read_text())

    def test_remove_discards_unsaved_edits_without_saving_or_recreating_file(self):
        self.session.tick()
        self.session.buffer.insert("unsaved ")

        def remove(payload):
            self.assertEqual({"editor": "simple", "remove": True}, payload)
            self.path.unlink()
            return {"closed": True}

        self.poll.reset_mock(side_effect=True)
        self.poll.side_effect = remove
        self.editor.handle_key("\x18")
        self.session.tick()
        self.assertFalse(self.session.save())
        self.assertFalse(self.session.remove())
        self.poll.assert_called_once_with({"editor": "simple", "remove": True})
        self.assertFalse(self.path.exists())
        self.assertTrue(self.session.closed)
        self.assertFalse(self.editor.running)
        self.assertIsNone(self.editor.prompt)
        self.assertEqual("unsaved original", self.session.buffer.content)
        self.assertTrue(self.session.buffer.modified)

    def test_failed_removal_preserves_draft_and_error_then_allows_retry(self):
        self.session.tick()
        self.session.buffer.insert("unsaved ")
        self.poll.return_value = {
            "error": "Description is no longer focused.",
            "record": {"content": "external"},
        }
        self.editor.handle_key("\x18")
        self.assertEqual("Description is no longer focused.", self.session.error)
        self.assertEqual("unsaved original", self.session.buffer.content)
        self.assertEqual("original", self.session.buffer.baseline)
        self.assertTrue(self.session.buffer.modified)
        self.assertFalse(self.session.closed)
        self.assertTrue(self.editor.running)
        self.assertEqual("original", self.path.read_text())
        self.poll.return_value = {"removable": True}
        self.session.tick()
        self.assertEqual("Description is no longer focused.", self.session.error)
        self.poll.return_value = {"closed": True}
        self.editor.handle_key("\x18")
        self.assertFalse(self.editor.running)

    def test_disconnected_or_incomplete_removal_keeps_editor_open(self):
        self.session.tick()
        self.session.buffer.insert("draft ")
        for response, exception, expected in (
            ({}, None, "The description could not be removed."),
            (None, OSError("Disconnected"), "Disconnected"),
        ):
            with self.subTest(expected=expected):
                self.session.error = ""
                self.poll.return_value = response
                self.poll.side_effect = exception
                self.editor.handle_key("\x18")
                self.assertEqual(expected, self.session.error)
                self.assertEqual("draft original", self.session.buffer.content)
                self.assertFalse(self.session.closed)
                self.assertTrue(self.editor.running)

    def test_footer_offers_remove_only_while_supported(self):
        self.screen.getmaxyx.return_value = (4, 80)
        for removable in (False, True, False):
            with self.subTest(removable=removable):
                self.poll.return_value = {"removable": removable}
                self.session.tick()
                self.editor.draw()
                footer = self.screen.addstr.call_args.args[2]
                self.assertEqual(removable, "^X remove" in footer)

    def test_ctrl_x_exits_curses_loop_without_another_poll_or_save(self):
        self.poll.side_effect = [{"removable": True}, {"closed": True}]
        self.screen.get_wch.side_effect = ["\x18", AssertionError("Editor kept running")]
        with patch.multiple(
            curses, raw=DEFAULT, noecho=DEFAULT, curs_set=DEFAULT, mousemask=DEFAULT,
            start_color=DEFAULT, use_default_colors=DEFAULT,
            init_pair=DEFAULT, color_pair=DEFAULT,
        ):
            self.editor.run()
        self.assertTrue(self.session.closed)
        self.assertEqual(
            [{"editor": "simple", "modified": False}, {"editor": "simple", "remove": True}],
            [call.args[0] for call in self.poll.call_args_list],
        )

    def test_settings_ctrl_x_preserves_configuration_and_local_edits(self):
        path = self.directory / "config.json"
        content = '{"editor": "simple"}\n'
        path.write_text(content, encoding="utf-8")

        def run_editor(file_path, poll):
            backend = Mock(wraps=poll)
            session = EditorSession(file_path, backend)
            session.tick()
            backend.reset_mock()
            session.buffer.insert("draft ")
            editor = EditorScreen(self.screen, session)
            editor.handle_key("\x18")
            editor.draw()
            backend.assert_not_called()
            self.assertFalse(session.can_remove)
            self.assertFalse(session.closed)
            self.assertTrue(editor.running)
            self.assertEqual("draft " + content, session.buffer.content)
            self.assertNotIn("^X remove", self.screen.addstr.call_args.args[2])

        with patch("herdr_description.app.run_editor", side_effect=run_editor):
            edit_settings(self.directory)
        self.assertEqual(content, path.read_text())


class EditorRemoveIntegrationTest(AppHarness, unittest.TestCase):
    def editor(self, record, tab_id="tab-1"):
        return EditorSession(
            Path(record["file_path"]),
            lambda payload: self.app.section_tick(tab_id, record["description_id"], payload),
        )

    def test_ctrl_x_removes_current_description_and_keeps_other_descriptions(self):
        pane_record = self.create("pane")
        tab_record = self.create("tab")
        poll = Mock(
            side_effect=lambda payload: self.app.section_tick(
                "tab-1", tab_record["description_id"], payload
            )
        )
        session = EditorSession(Path(tab_record["file_path"]), poll)
        session.tick()
        session.buffer.insert("unsaved draft")
        editor = EditorScreen(None, session)
        editor.handle_key("\x18")
        session.tick()
        self.assertFalse(session.save())
        self.assertTrue(session.closed)
        self.assertFalse(editor.running)
        self.assertIsNone(self.stored(tab_record))
        self.assertFalse(Path(tab_record["file_path"]).exists())
        self.assertIsNotNone(self.stored(pane_record))
        self.assertTrue(Path(pane_record["file_path"]).exists())
        self.assertFalse(any(call.args[0].get("save") for call in poll.call_args_list))

    def test_ctrl_x_shared_space_closes_both_editors_without_saving_either_draft(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space_record = self.create("space")
        tab_record = self.create("tab")
        self.focus("shell-2")
        pane_ids = [
            self.native_pane(space_record, tab_id)["pane_id"]
            for tab_id in ("tab-1", "tab-2")
        ]
        self.focus(pane_ids[1])
        sessions = [
            self.editor(space_record, tab_id) for tab_id in ("tab-1", "tab-2")
        ]
        for index, session in enumerate(sessions):
            session.tick()
            session.buffer.insert(f"unsaved draft {index}")
        EditorScreen(None, sessions[1]).handle_key("\x18")
        sessions[0].tick()
        self.assertTrue(all(session.closed for session in sessions))
        self.assertIsNone(self.stored(space_record))
        self.assertFalse(Path(space_record["file_path"]).exists())
        self.assertIsNotNone(self.stored(tab_record))
        self.assertEqual("", self.stored(tab_record).content)
        self.assertTrue(all(
            pane_id not in {pane["pane_id"] for pane in self.api.panes}
            for pane_id in pane_ids
        ))

    def test_ctrl_x_targets_editor_description_when_source_focus_points_elsewhere(self):
        self.api.add_pane("tab-1", "shell-2")
        first = self.create("pane", pane_id="shell-1")
        second = self.create("pane", pane_id="shell-2")
        self.focus("shell-1")
        self.focus(self.native_pane(second)["pane_id"])
        snapshot = self.api.snapshot()
        self.assertEqual(
            "shell-1",
            self.app.selected_source(snapshot, "tab-1", snapshot["focused_pane_id"]),
        )
        session = self.editor(second)
        session.tick()
        session.buffer.insert("unsaved draft")
        EditorScreen(None, session).handle_key("\x18")
        self.assertTrue(session.closed)
        self.assertIsNone(self.stored(second))
        self.assertFalse(Path(second["file_path"]).exists())
        self.assertIsNotNone(self.stored(first))
        self.assertTrue(Path(first["file_path"]).exists())
        self.assertTrue(
            {"shell-1", "shell-2"} <= {pane["pane_id"] for pane in self.api.panes}
        )


if __name__ == "__main__":
    unittest.main()
