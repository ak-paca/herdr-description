import curses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from herdr_description.editor import EditorScreen, EditorSession, TextBuffer
from tests.test_editor import FakeBackend


class EditorCommandsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file_path = Path(directory.name) / "description.txt"
        self.file_path.write_text("first\nsecond\nlast", encoding="utf-8")
        self.backend = FakeBackend(self.file_path)
        self.session = EditorSession(self.file_path, self.backend)
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (5, 80)
        self.editor = EditorScreen(self.screen, self.session)

    def test_delete_removes_whole_logical_line_and_keeps_save_baseline(self):
        cases = (
            ("first\nsecond\nlast", 0, 3, "second\nlast", (0, 0)),
            ("first\nsecond\nlast", 1, 3, "first\nlast", (1, 0)),
            ("first\nsecond\nlast", 2, 2, "first\nsecond", (1, 0)),
            ("first\n\nlast", 1, 0, "first\nlast", (1, 0)),
            ("only", 0, 2, "", (0, 0)),
            ("", 0, 0, "", (0, 0)),
            ("first\n", 1, 0, "first", (0, 0)),
        )
        for content, row, column, expected, cursor in cases:
            with self.subTest(content=content, row=row):
                buffer = self.session.buffer
                buffer.replace(content)
                buffer.move_vertical(row - buffer.row)
                buffer.move_line_boundary()
                buffer.move_horizontal(column)
                digest = buffer.digest
                self.editor.handle_key("\x0b")
                self.assertEqual(expected, buffer.content)
                self.assertEqual(cursor, (buffer.row, buffer.column))
                self.assertEqual(content, buffer.baseline)
                self.assertEqual(digest, buffer.digest)
                self.assertEqual(content != expected, buffer.modified)
        self.assertEqual([], self.backend.saves)

    def test_clear_requires_confirmation_and_only_yes_changes_buffer(self):
        for answer in ("y", "Y"):
            with self.subTest(answer=answer):
                buffer = self.session.buffer
                buffer.replace("first\nsecond\nlast")
                buffer.move_vertical(1)
                buffer.insert("draft ")
                content = buffer.content
                baseline = buffer.baseline
                digest = buffer.digest
                self.editor.handle_key("\x0c")
                self.assertIsNotNone(self.editor.prompt)
                self.assertEqual(content, buffer.content)
                self.editor.draw()
                footer = self.screen.addstr.call_args.args
                self.assertIn("[y/N]", footer[2])
                self.assertEqual(curses.A_BOLD, footer[3])
                self.editor.handle_key("x")
                self.assertEqual(content, buffer.content)
                self.assertIsNotNone(self.editor.prompt)
                self.editor.handle_key(answer)
                self.assertIsNone(self.editor.prompt)
                self.assertEqual("", buffer.content)
                self.assertEqual((0, 0), (buffer.row, buffer.column))
                self.assertEqual(baseline, buffer.baseline)
                self.assertEqual(digest, buffer.digest)
                self.assertTrue(buffer.modified)
                self.assertEqual([], self.backend.saves)
                self.assertEqual("first\nsecond\nlast", self.file_path.read_text())

    def test_clear_cancellation_preserves_content_cursor_and_baseline(self):
        buffer = self.session.buffer
        buffer.move_vertical(1)
        buffer.move_horizontal(3)
        before = (buffer.content, buffer.baseline, buffer.row, buffer.column)
        for answer in ("n", "N", "\x1b", "\n", "\r", curses.KEY_ENTER):
            with self.subTest(answer=answer):
                self.editor.handle_key("\x0c")
                self.editor.handle_key(answer)
                self.assertIsNone(self.editor.prompt)
                self.assertEqual(
                    before, (buffer.content, buffer.baseline, buffer.row, buffer.column)
                )
                self.assertFalse(buffer.modified)
        self.assertEqual([], self.backend.saves)

    def test_clear_confirmation_is_visible_on_single_row_terminal(self):
        self.screen.getmaxyx.return_value = (1, 80)
        self.editor.handle_key("\x0c")
        self.editor.draw()
        row, column, text, attributes = self.screen.addstr.call_args.args
        self.assertEqual((0, 0), (row, column))
        self.assertIn("[y/N]", text)
        self.assertEqual(curses.A_BOLD, attributes)
        self.assertEqual("first\nsecond\nlast", self.session.buffer.content)

    def test_clear_saves_only_when_explicitly_requested(self):
        self.editor.handle_key("\x0c")
        self.editor.handle_key("y")
        self.session.tick()
        self.assertEqual("first\nsecond\nlast", self.file_path.read_text())
        self.assertEqual([], self.backend.saves)
        self.editor.handle_key("\x13")
        self.assertEqual("", self.file_path.read_text())
        self.assertEqual("", self.session.buffer.baseline)
        self.assertFalse(self.session.buffer.modified)
        self.assertEqual(1, len(self.backend.saves))

    def test_line_deletion_persists_on_blur_without_changing_other_lines(self):
        self.session.buffer.move_vertical(1)
        self.editor.handle_key("\x0b")
        self.session.tick()
        self.assertEqual("first\nsecond\nlast", self.file_path.read_text())
        self.backend.request_id = 71
        self.session.tick()
        self.assertEqual("first\nlast", self.file_path.read_text())
        self.assertFalse(self.session.buffer.modified)
        self.assertEqual(71, self.backend.saves[0]["request_id"])

    def test_control_q_keeps_editor_open_and_does_not_save(self):
        self.editor.handle_key("x")
        content = self.session.buffer.content
        self.editor.handle_key("\x11")
        self.assertTrue(self.editor.running)
        self.assertEqual(content, self.session.buffer.content)
        self.assertEqual([], self.backend.saves)


class EditorWordWrapTest(unittest.TestCase):
    def setUp(self):
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (6, 6)
        self.session = SimpleNamespace(
            buffer=TextBuffer("abcdefghijklmnop\nuvwxyz"),
            title="Description",
            focused=True,
            error="",
            status="Ready · type to edit",
            closed=False,
        )
        self.editor = EditorScreen(self.screen, self.session)

    def body_rows(self):
        height, unused = self.screen.getmaxyx()
        return [
            call.args[2]
            for call in self.screen.addstr.call_args_list
            if call.args[0] < max(1, height - 1)
        ]

    def test_toggle_preserves_exact_draft_baseline_digest_and_cursor(self):
        buffer = self.session.buffer
        buffer.replace("alpha  beta\r\n日本語\te\u0301\n")
        buffer.move_horizontal(3)
        buffer.insert("draft ")
        before = (
            buffer.content, buffer.baseline, buffer.digest, buffer.row, buffer.column
        )
        self.assertFalse(self.editor.word_wrap)
        for expected in (True, False, True):
            self.editor.handle_key("\x17")
            self.editor.draw()
            self.assertEqual(expected, self.editor.word_wrap)
            self.assertEqual(
                before,
                (
                    buffer.content, buffer.baseline, buffer.digest,
                    buffer.row, buffer.column,
                ),
            )
            self.assertTrue(buffer.modified)

    def test_wrap_prefers_whitespace_and_retains_every_space(self):
        self.session.buffer.replace("alpha beta\ngamma")
        self.editor.handle_key("\x17")
        self.editor.draw()
        self.assertEqual(["alpha ", "beta", "gamma"], self.body_rows())
        self.assertEqual("alpha beta\ngamma", self.session.buffer.content)
        self.assertFalse(self.session.buffer.modified)

    def test_long_word_wraps_without_empty_rows_at_internal_boundaries(self):
        self.session.buffer.replace("abcdefghijklmnop")
        self.editor.handle_key("\x17")
        self.session.buffer.move_horizontal(6)
        self.editor.draw()
        self.assertEqual(["abcdef", "ghijkl", "mnop"], self.body_rows())
        self.screen.move.assert_called_with(1, 0)
        self.assertEqual((0, 6), (self.session.buffer.row, self.session.buffer.column))

    def test_vertical_navigation_uses_visual_rows_and_preserves_column(self):
        self.editor.handle_key("\x17")
        self.session.buffer.move_horizontal(2)
        for expected in ((0, 8), (0, 14), (1, 2)):
            self.editor.handle_key(curses.KEY_DOWN)
            self.assertEqual(expected, (self.session.buffer.row, self.session.buffer.column))
        for expected in ((0, 14), (0, 8), (0, 2)):
            self.editor.handle_key(curses.KEY_UP)
            self.assertEqual(expected, (self.session.buffer.row, self.session.buffer.column))

    def test_page_navigation_uses_visible_body_height(self):
        self.screen.getmaxyx.return_value = (4, 6)
        self.session.buffer.replace("abcdefghijklmnopqrstuvwxyz0123456789")
        self.session.buffer.move_horizontal(1)
        self.editor.handle_key("\x17")
        self.editor.handle_key(curses.KEY_NPAGE)
        self.assertEqual((0, 19), (self.session.buffer.row, self.session.buffer.column))
        self.editor.draw()
        self.screen.move.assert_called_with(2, 1)
        self.editor.handle_key(curses.KEY_PPAGE)
        self.assertEqual((0, 1), (self.session.buffer.row, self.session.buffer.column))

    def test_home_and_end_still_target_logical_line_boundaries(self):
        self.editor.handle_key("\x17")
        self.session.buffer.move_horizontal(8)
        self.editor.handle_key(curses.KEY_END)
        self.assertEqual((0, 16), (self.session.buffer.row, self.session.buffer.column))
        self.editor.handle_key(curses.KEY_HOME)
        self.assertEqual((0, 0), (self.session.buffer.row, self.session.buffer.column))

    def test_delete_line_removes_all_its_visual_rows(self):
        self.editor.handle_key("\x17")
        self.session.buffer.move_horizontal(8)
        self.editor.handle_key("\x0b")
        self.assertEqual("uvwxyz", self.session.buffer.content)
        self.assertEqual((0, 0), (self.session.buffer.row, self.session.buffer.column))

    def test_mouse_maps_wrapped_rows_to_original_characters(self):
        self.editor.handle_key("\x17")
        self.editor.draw()
        for x, y, expected in ((2, 1, (0, 8)), (3, 2, (0, 15)), (2, 3, (1, 2))):
            with self.subTest(x=x, y=y), patch(
                "curses.getmouse", return_value=(0, x, y, 0, curses.BUTTON1_CLICKED)
            ):
                self.editor.handle_key(curses.KEY_MOUSE)
                self.assertEqual(
                    expected, (self.session.buffer.row, self.session.buffer.column)
                )

    def test_resize_reflows_without_moving_logical_cursor(self):
        self.screen.getmaxyx.return_value = (4, 10)
        self.session.buffer.replace("abcdefghijklmnopqrstuvwxyz")
        self.session.buffer.move_horizontal(20)
        self.editor.handle_key("\x17")
        for width, expected_column in ((10, 0), (6, 2), (20, 0)):
            with self.subTest(width=width):
                self.screen.getmaxyx.return_value = (4, width)
                self.editor.handle_key(curses.KEY_RESIZE)
                self.editor.draw()
                self.assertEqual((0, 20), (self.session.buffer.row, self.session.buffer.column))
                row, column = self.screen.move.call_args.args
                self.assertTrue(0 <= row < 3)
                self.assertEqual(expected_column, column)
                self.assertEqual("abcdefghijklmnopqrstuvwxyz", self.session.buffer.content)

    def test_wide_and_combining_characters_wrap_by_terminal_cells(self):
        for content, width, expected in (
            ("日本語abcd", 6, ["日本語", "abcd"]),
            ("e\u0301e\u0301e\u0301e\u0301z", 3, ["e\u0301e\u0301e\u0301", "e\u0301z"]),
            ("ab\tcdefgh", 6, ["ab  ", "cdefgh"]),
        ):
            with self.subTest(content=content):
                self.screen.reset_mock()
                self.screen.getmaxyx.return_value = (6, width)
                self.session.buffer.replace(content)
                self.session.buffer.move_line_boundary()
                self.editor.word_wrap = True
                self.editor.draw()
                self.assertEqual(expected, self.body_rows()[:len(expected)])
                self.assertEqual(content, self.session.buffer.content)
                self.assertFalse(self.session.buffer.modified)

    def test_mouse_uses_cell_width_after_unicode_wrap(self):
        self.session.buffer.replace("日本語abcd")
        self.editor.handle_key("\x17")
        self.editor.draw()
        for x, y, expected_column in ((3, 0, 1), (1, 1, 4)):
            with self.subTest(x=x, y=y), patch(
                "curses.getmouse", return_value=(0, x, y, 0, curses.BUTTON1_PRESSED)
            ):
                self.editor.handle_key(curses.KEY_MOUSE)
                self.assertEqual((0, expected_column), (
                    self.session.buffer.row, self.session.buffer.column
                ))

    def test_empty_and_tiny_terminals_keep_cursor_and_drawing_in_bounds(self):
        self.editor.handle_key("\x17")
        for content in ("", "日本 e\u0301\tlongword\n"):
            for height, width in ((0, 0), (1, 1), (2, 1), (1, 3), (2, 0)):
                with self.subTest(content=content, height=height, width=width):
                    self.screen.reset_mock()
                    self.screen.getmaxyx.return_value = (height, width)
                    self.session.buffer.replace(content)
                    self.session.buffer.move_line_boundary(end=True)
                    self.editor.draw()
                    self.editor.handle_key(curses.KEY_UP)
                    self.editor.handle_key(curses.KEY_DOWN)
                    self.editor.draw()
                    self.assertEqual(content, self.session.buffer.content)
                    if not height or not width:
                        self.screen.addstr.assert_not_called()
                        self.screen.move.assert_not_called()
                    else:
                        for call in self.screen.move.call_args_list:
                            row, column = call.args
                            self.assertTrue(0 <= row < max(1, height - 1))
                            self.assertTrue(0 <= column < width)
                        for call in self.screen.addstr.call_args_list:
                            row, column = call.args[:2]
                            self.assertTrue(0 <= row < height)
                            self.assertTrue(0 <= column < width)


if __name__ == "__main__":
    unittest.main()
