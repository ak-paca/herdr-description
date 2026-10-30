import curses
import hashlib
import json
import locale
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, patch

from herdr_description.editor import EditorScreen, EditorSession, TextBuffer
from tests.test_app import AppHarness


ROOT = Path(__file__).resolve().parent.parent


class TextBufferTest(unittest.TestCase):
    def test_insert_preserves_multiline_unicode_and_exact_newlines(self):
        buffer = TextBuffer("before\nend\n")
        buffer.move_line_boundary(end=True)
        buffer.insert(" 日本語\ncafé 👋")
        self.assertEqual("before 日本語\ncafé 👋\nend\n", buffer.content)
        self.assertEqual((1, 6), (buffer.row, buffer.column))
        self.assertTrue(buffer.modified)

    def test_backspace_and_delete_join_lines_without_losing_text(self):
        buffer = TextBuffer("abc\n日本語\nlast")
        buffer.move_vertical(1)
        buffer.backspace()
        self.assertEqual("abc日本語\nlast", buffer.content)
        self.assertEqual((0, 3), (buffer.row, buffer.column))
        buffer.delete()
        buffer.move_line_boundary(end=True)
        buffer.delete()
        self.assertEqual("abc本語last", buffer.content)

    def test_cursor_moves_across_lines_and_remembers_vertical_column(self):
        buffer = TextBuffer("long line\nx\nanother line")
        buffer.move_horizontal(7)
        buffer.move_vertical(1)
        self.assertEqual((1, 1), (buffer.row, buffer.column))
        buffer.move_vertical(1)
        self.assertEqual((2, 7), (buffer.row, buffer.column))
        buffer.move_line_boundary()
        buffer.move_horizontal(-1)
        self.assertEqual((1, 1), (buffer.row, buffer.column))
        buffer.move_horizontal(1)
        self.assertEqual((2, 0), (buffer.row, buffer.column))
        buffer.move_vertical(-100)
        buffer.move_horizontal(-1)
        self.assertEqual((0, 0), (buffer.row, buffer.column))

    def test_clean_reload_clamps_cursor_and_dirty_reload_preserves_edits(self):
        buffer = TextBuffer("initial\nlong second line")
        buffer.move_vertical(1)
        buffer.move_line_boundary(end=True)
        buffer.reload_if_clean("a")
        self.assertEqual((0, 1), (buffer.row, buffer.column))
        buffer.insert(" draft")
        buffer.reload_if_clean("external")
        self.assertEqual("a draft", buffer.content)
        self.assertEqual("a", buffer.baseline)

    def test_undoing_by_backspace_restores_clean_state(self):
        buffer = TextBuffer("original")
        buffer.insert("x")
        buffer.backspace()
        self.assertFalse(buffer.modified)

    def test_deletion_at_file_boundaries_is_safe(self):
        buffer = TextBuffer("")
        buffer.backspace()
        buffer.delete()
        buffer.move_horizontal(1)
        buffer.move_vertical(-1)
        self.assertEqual("", buffer.content)
        self.assertEqual((0, 0), (buffer.row, buffer.column))


class FakeBackend:
    def __init__(self, file_path):
        self.file_path = file_path
        self.requests = []
        self.request_id = None
        self.focused = False
        self.error = None
        self.closed = False

    def __call__(self, payload):
        self.requests.append(payload)
        if self.closed:
            return {"closed": True}
        if self.error:
            return {"error": self.error}
        saved = False
        content = self.file_path.read_text(encoding="utf-8")
        save = payload.get("save")
        if save:
            current_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if not save.get("force") and save["expected_digest"] != current_digest:
                return {"error": "File changed externally.", "conflict": True}
            content = save["content"]
            self.file_path.write_text(content, encoding="utf-8")
            self.request_id = None
            saved = True
        if payload.get("ack_request") is not None:
            self.request_id = None
        return {
            "record": {
                "content": content,
                "file_path": str(self.file_path),
                "title": "Pane description",
                "focused": self.focused,
            },
            "saved": saved,
            "write_requested": self.request_id is not None,
            "save_request": {"id": self.request_id, "reason": "blur"},
        }

    @property
    def saves(self):
        return [request["save"] for request in self.requests if request.get("save")]


class EditorSessionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file_path = Path(directory.name) / "description.txt"
        self.file_path.write_text("initial\n", encoding="utf-8")
        self.backend = FakeBackend(self.file_path)
        self.session = EditorSession(self.file_path, self.backend)

    def test_poll_never_saves_unless_backend_requests_save(self):
        self.session.buffer.insert("Draft 👋\n")
        for unused in range(20):
            self.session.tick()
        self.assertEqual("initial\n", self.file_path.read_text())
        self.assertEqual([], self.backend.saves)
        self.assertTrue(self.session.buffer.modified)
        self.assertTrue(all(item["modified"] for item in self.backend.requests))
        self.assertTrue(all(item["editor"] == "simple" for item in self.backend.requests))

    def test_initial_digest_preserves_exact_file_bytes(self):
        content = "Привет\r\nsecond\r\n"
        self.file_path.write_bytes(content.encode("utf-8"))
        session = EditorSession(self.file_path, self.backend)
        self.assertEqual(content, session.buffer.content)
        self.assertEqual(
            hashlib.sha256(self.file_path.read_bytes()).hexdigest(),
            session.buffer.digest,
        )

    def test_blur_request_saves_exact_buffer_and_marks_it_clean(self):
        self.session.buffer.insert("Привет 👋\n")
        self.backend.request_id = 101
        self.session.tick()
        self.session.tick()
        self.assertEqual("Привет 👋\ninitial\n", self.file_path.read_text())
        self.assertFalse(self.session.buffer.modified)
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual(101, self.backend.saves[0]["request_id"])
        self.assertEqual(
            hashlib.sha256("initial\n".encode()).hexdigest(),
            self.backend.saves[0]["expected_digest"],
        )
        self.assertEqual("Saved", self.session.status)

    def test_clean_blur_acknowledges_without_rewriting_file(self):
        self.backend.request_id = 5
        self.session.tick()
        self.assertEqual([], self.backend.saves)
        self.assertIn(
            {"editor": "simple", "ack_request": 5, "modified": False},
            self.backend.requests,
        )

    def test_failed_blur_is_attempted_once_and_error_survives_later_polls(self):
        self.session.buffer.insert("draft")
        self.backend.request_id = 10
        self.file_path.write_text("external", encoding="utf-8")
        for unused in range(5):
            self.session.tick()
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual("draftinitial\n", self.session.buffer.content)
        self.assertEqual("external", self.file_path.read_text())
        self.assertIn("externally", self.session.status)
        self.assertTrue(self.session.conflict)
        self.assertTrue(self.session.buffer.modified)

    def test_explicit_force_save_resolves_external_conflict(self):
        self.session.buffer.insert("draft")
        self.file_path.write_text("external", encoding="utf-8")
        self.assertFalse(self.session.save())
        self.assertTrue(self.session.save(force=True))
        self.assertEqual("draftinitial\n", self.file_path.read_text())
        self.assertFalse(self.session.buffer.modified)
        self.assertFalse(self.session.conflict)
        self.assertEqual("", self.session.error)
        self.assertTrue(self.backend.saves[-1]["force"])
        self.assertIsNone(self.backend.saves[-1]["request_id"])

    def test_successful_save_reloads_normalized_content_and_marks_clean(self):
        def normalizing_backend(payload):
            content = '{\n  "enabled": true\n}\n'
            self.file_path.write_text(content, encoding="utf-8")
            return {"saved": True, "record": {"content": content}}

        self.session.buffer.replace('{"enabled": false}')
        self.session.buffer.move_line_boundary(end=True)
        self.session.buffer.insert(" ")
        self.session.poll = normalizing_backend
        self.assertTrue(self.session.save())
        self.assertEqual('{\n  "enabled": true\n}\n', self.session.buffer.content)
        self.assertFalse(self.session.buffer.modified)
        self.assertEqual((0, 1), (self.session.buffer.row, self.session.buffer.column))

    def test_clean_buffer_updates_from_file_while_dirty_buffer_is_preserved(self):
        self.file_path.write_text("new saved file", encoding="utf-8")
        self.session.tick()
        self.assertEqual("new saved file", self.session.buffer.content)
        self.session.buffer.insert("draft")
        self.file_path.write_text("another external edit", encoding="utf-8")
        self.session.tick()
        self.assertEqual("draftnew saved file", self.session.buffer.content)

    def test_reload_discards_draft_and_clears_conflict(self):
        self.session.buffer.insert("draft")
        self.file_path.write_text("externally saved", encoding="utf-8")
        self.session.save()
        self.assertTrue(self.session.reload())
        self.assertEqual("externally saved", self.session.buffer.content)
        self.assertFalse(self.session.buffer.modified)
        self.assertFalse(self.session.conflict)
        self.assertEqual("", self.session.error)

    def test_removed_description_closes_without_recreating_file(self):
        self.session.buffer.insert("draft")
        self.backend.closed = True
        self.file_path.unlink()
        self.session.tick()
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.save())
        self.assertFalse(self.file_path.exists())
        self.assertEqual([], self.backend.saves)

    def test_metadata_focus_updates_do_not_overwrite_draft(self):
        self.session.buffer.insert("draft")
        self.backend.focused = True
        self.session.tick()
        self.assertTrue(self.session.focused)
        self.assertEqual("Pane description", self.session.title)
        self.assertEqual("draftinitial\n", self.session.buffer.content)

    def test_backend_error_preserves_buffer_until_successful_save(self):
        self.session.buffer.insert("draft")
        self.backend.error = "Cannot write file"
        self.assertFalse(self.session.save())
        self.assertTrue(self.session.buffer.modified)
        self.assertEqual("Cannot write file", self.session.error)
        self.backend.error = None
        self.assertTrue(self.session.save())
        self.assertEqual("draftinitial\n", self.file_path.read_text())

    def test_transport_error_is_visible_and_retried_manually(self):
        def unavailable(payload):
            raise OSError("Disconnected")

        self.session.buffer.insert("draft")
        self.session.poll = unavailable
        self.session.tick()
        self.assertEqual("Disconnected", self.session.status)
        self.assertFalse(self.session.save())
        self.session.poll = self.backend
        self.assertTrue(self.session.save())

    def test_editor_shortcuts_confirm_reload_and_overwrite(self):
        screen = EditorScreen(None, self.session)
        screen.handle_key("x")
        screen.handle_key("\x12")
        self.assertEqual("reload", screen.prompt)
        screen.handle_key("n")
        self.assertTrue(self.session.buffer.modified)
        screen.handle_key("\x12")
        screen.handle_key("y")
        self.assertEqual("initial\n", self.session.buffer.content)
        screen.handle_key(curses.KEY_HOME)
        screen.handle_key("x")
        self.file_path.write_text("external", encoding="utf-8")
        screen.handle_key("\x13")
        self.assertEqual("overwrite", screen.prompt)
        screen.handle_key("n")
        self.assertEqual("external", self.file_path.read_text())
        screen.handle_key("\x13")
        screen.handle_key("y")
        self.assertEqual("xinitial\n", self.file_path.read_text())

    def test_editor_input_requires_no_mode_switch(self):
        screen = EditorScreen(None, self.session)
        for key in ("日", "本", "\n", "x", "\t", "y", curses.KEY_BACKSPACE):
            screen.handle_key(key)
        self.assertEqual("日本\nx   initial\n", self.session.buffer.content)
        screen.handle_key("\x11")
        self.assertTrue(screen.running)
        self.assertTrue(self.session.buffer.modified)
        self.assertEqual("日本\nx   initial\n", self.session.buffer.content)
        self.assertEqual("initial\n", self.file_path.read_text())
        screen.handle_key("\x13")
        self.assertEqual("日本\nx   initial\n", self.file_path.read_text())


class BackendIntegrationTest(AppHarness, unittest.TestCase):
    def editor(self, record):
        return EditorSession(
            Path(record["file_path"]),
            lambda payload: self.app.section_tick(
                "tab-1", record["description_id"], payload
            ),
        )

    def test_real_mapping_save_on_blur_updates_file_once_and_preserves_focus(self):
        record = self.create("pane")
        editor = self.editor(record)
        editor.tick()
        editor.toggle_autosave()
        editor.buffer.insert("заметка 👋\nsecond line")
        editor.tick()
        self.assertEqual(record["content"], Path(record["file_path"]).read_text())
        self.assertEqual([], self.methods("notification.show"))
        self.focus("shell-1")
        editor.tick()
        editor.tick()
        self.assertFalse(editor.buffer.modified)
        self.assertEqual(record["content"] + "заметка 👋\nsecond line", self.stored(record).content)
        self.assertEqual("shell-1", self.api.focused_pane_id)
        self.assertEqual(
            ["Pane description saved"],
            [item["title"] for item in self.methods("notification.show")],
        )

    def test_real_conflict_preserves_draft_without_repeated_toasts_then_can_overwrite(self):
        record = self.create("tab")
        editor = self.editor(record)
        editor.tick()
        editor.toggle_autosave()
        editor.buffer.insert("my draft")
        editor.tick()
        Path(record["file_path"]).write_text("external change", encoding="utf-8")
        self.api.focused_pane_id = "shell-1"
        for unused in range(4):
            editor.tick()
        self.assertTrue(editor.conflict)
        self.assertTrue(editor.buffer.modified)
        self.assertEqual(record["content"] + "my draft", editor.buffer.content)
        self.assertEqual("external change", self.stored(record).content)
        self.assertEqual(1, len(self.methods("notification.show")))
        self.assertTrue(editor.save(force=True))
        self.assertFalse(editor.buffer.modified)
        self.assertEqual(record["content"] + "my draft", self.stored(record).content)
        self.assertEqual(2, len(self.methods("notification.show")))
        self.assertEqual("shell-1", self.api.focused_pane_id)


class EditorScreenTest(unittest.TestCase):
    def setUp(self):
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (4, 80)
        self.session = SimpleNamespace(
            buffer=TextBuffer("first\nsecond\nthird\nfourth\nfifth\nsixth"),
            title="Pane description",
            focused=True,
            error="",
            status="Ready · type to edit",
            closed=False,
            can_remove=False,
        )
        self.editor = EditorScreen(self.screen, self.session)

    def test_text_uses_first_row_and_reclaimed_header_row(self):
        self.session.buffer.move_vertical(2)
        self.editor.draw()
        writes = [call.args for call in self.screen.addstr.call_args_list]
        self.assertEqual(
            [(0, 0, "first", 0), (1, 0, "second", 0), (2, 0, "third", 0)],
            writes[:3],
        )
        self.assertEqual(3, writes[3][0])
        self.assertEqual(curses.A_DIM, writes[3][3])
        self.assertNotIn(self.session.title, "".join(call[2] for call in writes))
        self.screen.move.assert_called_once_with(2, 0)

    def test_scrolling_and_page_navigation_use_full_body_height(self):
        self.editor.handle_key(curses.KEY_NPAGE)
        self.assertEqual(3, self.session.buffer.row)
        self.editor.draw()
        self.assertEqual(1, self.editor.first_row)
        self.screen.addstr.assert_any_call(0, 0, "second", 0)
        self.screen.addstr.assert_any_call(2, 0, "fourth", 0)
        self.screen.move.assert_called_once_with(2, 0)
        self.editor.handle_key(curses.KEY_PPAGE)
        self.assertEqual(0, self.session.buffer.row)
        self.editor.draw()
        self.assertEqual(0, self.editor.first_row)
        self.screen.move.assert_called_with(0, 0)

    def test_mouse_selects_first_and_last_body_rows_and_ignores_footer(self):
        for x, y, expected in (
            (2, 0, (0, 2)),
            (1, 2, (2, 1)),
            (4, 3, (2, 1)),
            (-1, 0, (2, 1)),
            (80, 0, (2, 1)),
        ):
            with self.subTest(x=x, y=y), patch(
                "curses.getmouse", return_value=(0, x, y, 0, curses.BUTTON1_CLICKED)
            ):
                self.editor.handle_key(curses.KEY_MOUSE)
                self.assertEqual(
                    expected, (self.session.buffer.row, self.session.buffer.column)
                )

    def test_mouse_positions_account_for_scrolling_and_wide_characters(self):
        self.session.buffer.replace("unused\n日本語\nend")
        self.editor.first_row = 1
        self.editor.first_column = 2
        with patch(
            "curses.getmouse", return_value=(0, 2, 0, 0, curses.BUTTON1_PRESSED)
        ):
            self.editor.handle_key(curses.KEY_MOUSE)
        self.assertEqual((1, 2), (self.session.buffer.row, self.session.buffer.column))

    def test_single_row_terminal_keeps_text_and_cursor_editable(self):
        self.screen.getmaxyx.return_value = (1, 80)
        self.session.buffer.move_vertical(2)
        self.editor.draw()
        self.screen.addstr.assert_called_once_with(0, 0, "third", 0)
        self.screen.move.assert_called_once_with(0, 0)
        with patch(
            "curses.getmouse", return_value=(0, 2, 0, 0, curses.BUTTON1_CLICKED)
        ):
            self.editor.handle_key(curses.KEY_MOUSE)
        self.assertEqual((2, 2), (self.session.buffer.row, self.session.buffer.column))

    def test_two_row_terminal_retains_one_text_row_and_footer(self):
        self.screen.getmaxyx.return_value = (2, 80)
        self.editor.draw()
        self.screen.addstr.assert_any_call(0, 0, "first", 0)
        self.assertEqual(1, self.screen.addstr.call_args_list[-1].args[0])
        self.screen.move.assert_called_once_with(0, 0)

    def test_zero_sized_terminal_does_not_move_cursor_or_write(self):
        self.screen.getmaxyx.return_value = (0, 0)
        self.editor.draw()
        self.screen.addstr.assert_not_called()
        self.screen.move.assert_not_called()

    def test_prompts_and_errors_keep_default_colors_with_bold_footer(self):
        for prompt, error, expected in (
            ("reload", "", "Discard unsaved edits"),
            ("overwrite", "", "Overwrite the externally"),
            (None, "Could not save", "Could not save"),
        ):
            with self.subTest(prompt=prompt, error=error):
                self.editor.prompt = prompt
                self.session.error = error
                self.session.status = error
                self.editor.draw()
                row, column, text, attributes = self.screen.addstr.call_args.args
                self.assertEqual((3, 0), (row, column))
                self.assertTrue(text.startswith(expected))
                self.assertEqual(curses.A_BOLD, attributes)

    def test_terminal_default_colors_are_applied_to_background_and_text(self):
        self.session.closed = True
        with patch.multiple(
            curses, raw=DEFAULT, noecho=DEFAULT, curs_set=DEFAULT, mousemask=DEFAULT,
            start_color=DEFAULT, use_default_colors=DEFAULT,
            init_pair=DEFAULT, color_pair=DEFAULT,
        ) as terminal:
            terminal["color_pair"].return_value = 256
            self.editor.run()
            terminal["start_color"].assert_called_once_with()
            terminal["use_default_colors"].assert_called_once_with()
            terminal["init_pair"].assert_called_once_with(1, -1, -1)
            terminal["color_pair"].assert_called_once_with(1)
        self.screen.bkgd.assert_called_once_with(" ", 256)
        self.screen.attrset.assert_called_once_with(256)

    def test_unsupported_default_colors_fall_back_without_a_fixed_palette(self):
        self.session.closed = True
        with patch.multiple(
            curses, raw=DEFAULT, noecho=DEFAULT, curs_set=DEFAULT, mousemask=DEFAULT,
            start_color=DEFAULT, use_default_colors=DEFAULT,
            init_pair=DEFAULT, color_pair=DEFAULT,
        ) as terminal:
            terminal["use_default_colors"].side_effect = curses.error()
            self.editor.run()
            terminal["init_pair"].assert_not_called()
            terminal["color_pair"].assert_not_called()
        self.screen.bkgd.assert_called_once_with(" ", curses.A_NORMAL)
        self.screen.attrset.assert_called_once_with(curses.A_NORMAL)


class CursesIntegrationTest(unittest.TestCase):
    def test_real_terminal_typing_saves_on_blur_and_closes_after_removal(self):
        import fcntl
        import pty
        import termios

        try:
            master, slave = pty.openpty()
        except OSError as error:
            self.skipTest("PTY is unavailable: " + str(error))
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "note.txt"
            trigger = root / "blur"
            requests = root / "requests.jsonl"
            file_path.write_text("", encoding="utf-8")
            script = (
                "import json, sys\n"
                "from pathlib import Path\n"
                "from herdr_description.editor import run_editor\n"
                "root=Path(sys.argv[1])\n"
                "path=root/'note.txt'\n"
                "def poll(payload):\n"
                "    with (root/'requests.jsonl').open('a') as log:\n"
                "        log.write(json.dumps(payload)+'\\n')\n"
                "    if payload.get('remove'):\n"
                "        path.unlink()\n"
                "        return {'closed': True}\n"
                "    saved=False\n"
                "    if payload.get('save'):\n"
                "        path.write_text(payload['save']['content'], encoding='utf-8')\n"
                "        if not payload['save'].get('defer_ack'):\n"
                "            (root/'blur').unlink(missing_ok=True)\n"
                "        saved=True\n"
                "    if payload.get('ack_request') == 123 and not payload.get('modified') and not payload.get('save'):\n"
                "        (root/'blur').unlink(missing_ok=True)\n"
                "    return {'record': {'content': path.read_text(encoding='utf-8'),\n"
                "        'title': 'Pane description', 'focused': True}, 'saved': saved, 'removable': True,\n"
                "        'write_requested': (root/'blur').exists(),\n"
                "        'save_request': {'id': 123, 'reason': 'blur'}}\n"
                "run_editor(path, poll)\n"
            )
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 70, 0, 0))
            environment = {
                **os.environ,
                "TERM": "xterm-256color",
                "LC_ALL": locale.setlocale(locale.LC_CTYPE),
            }
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(root)],
                cwd=str(ROOT), env=environment, stdin=slave, stdout=slave, stderr=slave,
            )
            transcript = bytearray()

            def messages():
                return [json.loads(line) for line in requests.read_text().splitlines() if line.endswith("}")]

            def acknowledged():
                completed = False
                for message in messages():
                    if message.get("ack_request") == 123 and not message.get("modified"):
                        completed = True
                    elif completed and message.get("modified") is False and not message.get("save"):
                        return True
                return False

            def wait_until(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        transcript.extend(os.read(master, 65536))
                    if predicate():
                        return
                    if process.poll() is not None:
                        self.fail("Editor exited: " + transcript.decode(errors="replace")[-2000:])
                self.fail("Editor timed out: " + transcript.decode(errors="replace")[-2000:])

            try:
                wait_until(requests.exists)
                os.write(master, "Привет 👋\rsecond line".encode("utf-8"))
                wait_until(lambda: '"modified": true' in requests.read_text())
                self.assertEqual("", file_path.read_text())
                trigger.touch()
                wait_until(lambda: file_path.read_text() == "Привет 👋\nsecond line")
                wait_until(acknowledged)
                os.write(master, b"unsaved draft\x18")
                wait_until(lambda: process.poll() is not None)
                self.assertEqual(0, process.returncode)
                self.assertFalse(file_path.exists())
                completed = messages()
                self.assertEqual(1, sum("save" in message for message in completed))
                self.assertEqual(
                    {"editor": "simple", "async": True, "remove": True}, completed[-1]
                )
                self.assertNotIn(b"Pane description", transcript)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
