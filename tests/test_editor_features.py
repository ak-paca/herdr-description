import base64
import curses
import json
import locale
import os
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_description.editor import EditorScreen, EditorSession
from herdr_description.editor_support import MouseInput
from tests.test_editor import FakeBackend, ROOT


class LiveBackend(FakeBackend):
    def __init__(self, file_path):
        super().__init__(file_path)
        self.enabled = True
        self.reject_next_autosave = False

    def __call__(self, payload):
        if payload.get("toggle_autosave"):
            self.enabled = not self.enabled
        if self.reject_next_autosave and payload.get("save", {}).get("reason") == "autosave":
            self.enabled = False
            self.reject_next_autosave = False
        if payload.get("save", {}).get("reason") == "autosave" and not self.enabled:
            self.requests.append(payload)
            response = {"autosave_skipped": True}
        else:
            response = super().__call__(payload)
        return {**response, "autosave_supported": True, "autosave_enabled": self.enabled}


class EditorFeatureTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file_path = Path(directory.name) / "description.txt"
        self.file_path.write_text("first\nsecond", encoding="utf-8")
        self.backend = LiveBackend(self.file_path)
        self.session = EditorSession(self.file_path, self.backend)
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (12, 80)
        self.raw = []
        self.browser = Mock()
        self.editor = EditorScreen(self.screen, self.session, self.raw.append, self.browser)

    def replace_content(self, content):
        self.file_path.write_text(content, encoding="utf-8")
        self.session.buffer.replace(content)
        self.session.buffer.row = self.session.buffer.column = 0
        self.editor.clear_selection()

    def writes(self):
        return [call.args for call in self.screen.addstr.call_args_list]

    def mouse(self, x, y, buttons):
        with patch("curses.getmouse", return_value=(0, x, y, 0, buttons)):
            self.editor.handle_key(curses.KEY_MOUSE)

    def test_every_mutation_saves_immediately_with_exact_unicode_and_digest(self):
        self.session.tick()
        for key in ("日", "e", "\u0301", "\n", "\t", "👋", curses.KEY_BACKSPACE, curses.KEY_DC, "\x0b"):
            with self.subTest(key=key):
                previous = self.session.buffer.content
                digest = self.session.buffer.digest
                count = len(self.backend.saves)
                self.editor.handle_key(key)
                expected = self.session.buffer.content
                self.assertNotEqual(previous, expected)
                self.assertEqual(expected, self.file_path.read_text())
                self.assertEqual(count + 1, len(self.backend.saves))
                self.assertEqual("autosave", self.backend.saves[-1]["reason"])
                self.assertEqual(digest, self.backend.saves[-1]["expected_digest"])
                self.assertFalse(self.session.buffer.modified)
        self.editor.handle_key("\x0c")
        count = len(self.backend.saves)
        self.editor.handle_key("y")
        self.assertEqual("", self.file_path.read_text())
        self.assertEqual(count + 1, len(self.backend.saves))

    def test_noop_navigation_help_wrap_copy_and_cancel_never_save(self):
        self.replace_content("")
        self.session.tick()
        for key in (curses.KEY_BACKSPACE, curses.KEY_DC, curses.KEY_LEFT, curses.KEY_UP, "\x17", "\x03", "\x11"):
            self.editor.handle_key(key)
        self.editor.handle_key("\x0c")
        self.editor.handle_key("n")
        self.editor.handle_key("\x1f")
        for key in ("x", "\n", "\x0b", "\x0c", "\x13", "\x01", curses.KEY_NPAGE):
            self.editor.handle_key(key)
        self.editor.handle_key("q")
        self.assertEqual("", self.session.buffer.content)
        self.assertEqual([], self.backend.saves)
        self.assertTrue(self.backend.enabled)

    def test_missing_or_unsupported_capability_stays_manual(self):
        for response in ({}, {"autosave_supported": False, "autosave_enabled": True}):
            with self.subTest(response=response):
                backend = Mock(return_value=response)
                session = EditorSession(self.file_path, backend)
                editor = EditorScreen(self.screen, session)
                session.tick()
                editor.handle_key("x")
                editor.handle_key("\x01")
                self.assertFalse(session.autosave_enabled)
                self.assertEqual(1, backend.call_count)
                self.assertTrue(session.buffer.modified)

    def test_shared_mode_change_flushes_once_and_disabled_mode_still_saves_on_blur(self):
        self.backend.enabled = False
        self.session.tick()
        self.editor.handle_key("x")
        self.assertEqual([], self.backend.saves)
        self.editor.handle_key("\x01")
        self.assertTrue(self.session.autosave_enabled)
        self.assertEqual(1, len(self.backend.saves))
        self.assertTrue(any(request.get("toggle_autosave") for request in self.backend.requests))
        self.session.tick()
        self.session.tick()
        self.assertEqual(1, len(self.backend.saves))
        self.backend.enabled = False
        self.session.tick()
        self.editor.handle_key("y")
        self.assertEqual(1, len(self.backend.saves))
        self.backend.request_id = 42
        self.session.tick()
        self.assertEqual(2, len(self.backend.saves))
        self.assertEqual("blur", self.backend.saves[-1]["reason"])
        self.assertEqual(self.session.buffer.content, self.file_path.read_text())
        self.editor.handle_key("z")
        self.backend.enabled = True
        self.session.tick()
        self.assertEqual(3, len(self.backend.saves))
        self.session.tick()
        self.assertEqual(3, len(self.backend.saves))

    def test_failed_draft_is_not_retried_on_poll_but_new_edit_or_manual_save_retries(self):
        self.session.tick()
        self.backend.error = "Write failed"
        self.editor.handle_key("x")
        self.assertEqual(1, len(self.backend.saves))
        for unused in range(5):
            self.session.tick()
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual("Write failed", self.session.error)
        self.editor.handle_key("y")
        self.assertEqual(2, len(self.backend.saves))
        self.backend.error = None
        self.editor.handle_key("\x13")
        self.assertEqual(3, len(self.backend.saves))
        self.assertEqual("manual", self.backend.saves[-1]["reason"])
        self.assertFalse(self.session.buffer.modified)

    def test_stale_enabled_mode_rejection_preserves_draft_without_error(self):
        self.session.tick()
        self.backend.reject_next_autosave = True
        self.editor.handle_key("x")
        self.assertTrue(self.session.buffer.modified)
        self.assertFalse(self.session.autosave_enabled)
        self.assertEqual("", self.session.error)
        self.assertEqual("first\nsecond", self.file_path.read_text())
        self.session.tick()
        self.assertEqual(1, len(self.backend.saves))
        self.editor.handle_key("\x13")
        self.assertEqual("xfirst\nsecond", self.file_path.read_text())

    def test_failed_manual_save_is_not_retried_by_autosave_poll(self):
        self.session.tick()
        self.session.buffer.insert("draft")
        self.backend.error = "Write failed"
        self.editor.handle_key("\x13")
        for unused in range(3):
            self.session.tick()
        self.assertEqual(1, len(self.backend.saves))
        self.assertEqual("manual", self.backend.saves[0]["reason"])

    def test_two_sessions_observe_shared_mode_and_flush_existing_draft(self):
        self.backend.enabled = False
        self.session.tick()
        other = EditorSession(self.file_path, self.backend)
        other.tick()
        self.editor.handle_key("draft")
        self.assertTrue(self.session.buffer.modified)
        other.toggle_autosave()
        self.assertFalse(self.session.autosave_enabled)
        self.session.tick()
        self.assertTrue(self.session.autosave_enabled)
        self.assertEqual("draftfirst\nsecond", self.file_path.read_text())
        self.assertEqual(1, len(self.backend.saves))
        other.tick()
        self.assertEqual(self.session.buffer.content, other.buffer.content)

    def test_initial_template_places_cursor_after_prefix_without_rewriting_existing_files(self):
        for content, position in (("-- ", 3), ("-- existing", 0), ("", 0), ("-- \n", 0)):
            with self.subTest(content=content):
                self.replace_content(content)
                session = EditorSession(self.file_path, self.backend)
                self.assertEqual(position, session.buffer.column)
                self.assertEqual(content, self.file_path.read_text())
                self.assertFalse(session.buffer.modified)

    def test_shift_selection_copy_and_replacement_preserve_unicode_newlines(self):
        self.replace_content("日e\u0301\nnext")
        for unused in range(5):
            self.editor.handle_key(curses.KEY_SRIGHT)
        self.assertEqual("日e\u0301\nn", self.editor.selected_text)
        self.editor.handle_key("\x03")
        encoded = self.raw[-1].removeprefix("\x1b]52;c;").removesuffix("\x1b\\")
        self.assertEqual("日e\u0301\nn", base64.b64decode(encoded).decode("utf-8"))
        self.assertFalse(self.session.buffer.modified)
        self.session.tick()
        self.editor.draw()
        self.assertTrue(any(attributes & curses.A_REVERSE for unused, unused, unused, attributes in self.writes()))
        self.editor.handle_key("界")
        self.assertEqual("界ext", self.file_path.read_text())
        self.assertIsNone(self.editor.selection)
        self.assertEqual(1, len(self.backend.saves))

    def test_anchor_selection_backspace_delete_and_escape(self):
        for key in (curses.KEY_DC, curses.KEY_BACKSPACE, "\n"):
            with self.subTest(key=key):
                self.replace_content("abc\ndef")
                self.editor.handle_key(curses.KEY_RIGHT)
                self.editor.handle_key("\x00")
                for unused in range(4):
                    self.editor.handle_key(curses.KEY_RIGHT)
                self.assertEqual("bc\nd", self.editor.selected_text)
                self.editor.handle_key(key)
                self.assertEqual("a\nef" if key == "\n" else "aef", self.session.buffer.content)
        self.editor.handle_key("\x00")
        self.editor.handle_key(curses.KEY_RIGHT)
        self.editor.handle_key("\x1b")
        self.assertIsNone(self.editor.selection)
        self.assertEqual("a\nef", self.session.buffer.content)

    def test_mouse_drag_selects_exact_wrapped_text_and_ignores_footer(self):
        self.screen.getmaxyx.return_value = (30, 8)
        self.replace_content("日本語abc\nnext")
        self.editor.handle_key("\x17")
        self.editor.draw()
        self.mouse(2, 0, curses.BUTTON1_PRESSED)
        self.mouse(1, 1, curses.REPORT_MOUSE_POSITION)
        self.mouse(1, 1, curses.BUTTON1_RELEASED)
        self.assertEqual("本語abc", self.editor.selected_text)
        selected = self.editor.selection
        self.mouse(0, self.editor.body_height, curses.BUTTON1_CLICKED)
        self.assertEqual(selected, self.editor.selection)
        self.editor.handle_key("\x03")
        self.assertIn(base64.b64encode("本語abc".encode()).decode(), self.raw[-1])

    def test_compact_footer_wraps_status_and_reserves_dynamic_body_height(self):
        self.session.tick()
        self.screen.getmaxyx.return_value = (8, 18)
        self.editor.draw()
        footer = self.writes()[-1][2]
        self.assertNotIn("^K", footer)
        self.screen.reset_mock()
        self.editor.handle_key("\x17")
        self.editor.draw()
        footer = "".join(text.rstrip() for row, unused, text, unused in self.writes() if row >= self.editor.body_height)
        self.assertIn("auto:on", footer)
        self.assertIn("wrap:on", footer)
        self.assertIn("^/ help", footer)
        self.assertLess(self.editor.body_height, 7)
        self.assertGreaterEqual(self.editor.body_height, 1)
        self.session.error = "A very long error message " * 10
        self.editor.draw()
        self.assertEqual(1, self.editor.body_height)

    def test_help_is_scrollable_on_tiny_terminal_without_mutating_text(self):
        self.screen.getmaxyx.return_value = (2, 7)
        self.editor.handle_key("\x1f")
        self.editor.draw()
        first = self.writes()[-2:]
        self.editor.handle_key(curses.KEY_NPAGE)
        self.editor.draw()
        self.assertNotEqual(first, self.writes()[-2:])
        self.editor.handle_key("x")
        self.editor.handle_key("\n")
        self.editor.handle_key("q")
        self.assertEqual("first\nsecond", self.session.buffer.content)
        self.assertFalse(self.editor.help_open)
        self.screen.getmaxyx.return_value = (1, 80)
        self.editor.handle_key("\x17")
        self.editor.handle_key("\x0c")
        self.editor.draw()
        self.assertIn("[y/N]", self.writes()[-1][2])
        self.editor.handle_key("n")

    def test_wrapped_links_overlay_each_fragment_with_original_full_uri(self):
        uri = "https://example.org/日本/e\u0301?q=a%20b&next=/full/path#fragment"
        self.replace_content("See (" + uri + ").")
        self.screen.getmaxyx.return_value = (30, 14)
        self.editor.handle_key("\x17")
        self.editor.draw()
        output = "".join(self.raw)
        targets = re.findall("\x1b]8;;([^\x1b]+)\x1b\\\\", output)
        self.assertGreater(len(targets), 2)
        self.assertTrue(all(target == uri for target in targets))
        self.assertIn("\x1b7", output)
        self.assertTrue(output.endswith("\x1b8"))
        self.assertTrue(any(attributes & curses.A_UNDERLINE for unused, unused, unused, attributes in self.writes()))
        self.assertTrue(all("\x1b" not in text for unused, unused, text, unused in self.writes()))
        self.assertIn(uri.encode("utf-8"), output.encode("utf-8"))

    def test_link_overlay_cell_positions_include_wide_combining_and_tabs(self):
        uri = "https://example.org/path?q=1&b=2#end"
        self.replace_content("日e\u0301\t" + uri)
        self.editor.draw()
        self.assertIn("\x1b[1;5H\x1b[0;4m\x1b]8;;" + uri, self.raw[-1])
        self.mouse(6, 0, curses.BUTTON1_CLICKED | curses.BUTTON_CTRL)
        self.browser.assert_called_once_with(uri)
        self.session.buffer.move_line_boundary(end=True)
        self.screen.getmaxyx.return_value = (12, 15)
        self.editor.draw()
        self.assertIn("\x1b]8;;" + uri, self.raw[-1])
        self.assertNotIn("日", self.raw[-1].split("\x1b]8;;" + uri)[-1])

    def test_link_metadata_changes_and_removal_force_redraw_even_with_identical_visible_text(self):
        self.screen.getmaxyx.return_value = (8, 12)
        self.replace_content("https://example.org/one")
        self.editor.draw()
        visible = self.writes()[0][2]
        self.screen.reset_mock()
        self.raw.clear()
        self.replace_content("https://example.org/two")
        self.editor.draw()
        self.assertEqual(visible, self.writes()[0][2])
        self.screen.redrawwin.assert_called_once_with()
        self.assertIn("https://example.org/two", self.raw[-1])
        self.screen.reset_mock()
        self.raw.clear()
        self.replace_content("plain text")
        self.editor.draw()
        self.screen.redrawwin.assert_called_once_with()
        self.assertEqual([], self.raw)

    def test_selection_overlay_preserves_reverse_and_help_excludes_links(self):
        self.replace_content("https://example.org/path")
        self.editor.handle_key("\x00")
        self.editor.handle_key(curses.KEY_RIGHT)
        self.editor.draw()
        self.assertIn("\x1b[0;4;7m", self.raw[-1])
        self.screen.reset_mock()
        self.raw.clear()
        self.editor.handle_key("\x1f")
        self.editor.draw()
        self.screen.redrawwin.assert_called_once_with()
        self.assertEqual([], self.raw)

    def test_link_uri_keeps_valid_trailing_path_query_and_fragment_punctuation(self):
        for uri in (
            "https://example.org/path!", "https://example.org/path;",
            "https://example.org/path,", "https://example.org/path.",
            "https://example.org/path:", "https://example.org/path?",
            "https://example.org/?next=value!", "https://example.org/?next=value;",
            "https://example.org/?next=value,", "https://example.org/?next=value.",
            "https://example.org/#part!", "https://example.org/a_(b)",
            "https://example.org/unmatched)", "https://example.org/a'b",
            "https://example.org/?q='hello'",
        ):
            with self.subTest(uri=uri):
                self.raw.clear()
                self.replace_content(uri)
                self.editor.draw()
                self.assertIn("\x1b]8;;" + uri + "\x1b\\", self.raw[-1])

    def test_control_mouse_opens_once_per_fresh_gesture_and_never_on_drag_or_release(self):
        uri = "https://example.org/path"
        self.replace_content(uri)
        control = curses.BUTTON_CTRL
        self.mouse(2, 0, curses.BUTTON1_PRESSED | control)
        self.mouse(4, 0, curses.BUTTON1_PRESSED | control)
        self.mouse(5, 0, curses.REPORT_MOUSE_POSITION | control)
        self.mouse(6, 0, curses.BUTTON1_RELEASED | control)
        self.browser.assert_called_once_with(uri)
        self.assertFalse(self.editor.mouse_dragging)
        self.mouse(2, 0, curses.BUTTON1_PRESSED)
        self.mouse(5, 0, curses.REPORT_MOUSE_POSITION | control)
        self.mouse(6, 0, curses.BUTTON1_RELEASED | control)
        self.browser.assert_called_once_with(uri)
        self.assertFalse(self.editor.mouse_dragging)
        self.mouse(2, 0, curses.BUTTON1_CLICKED | control)
        self.assertEqual(2, self.browser.call_count)

    def test_partial_or_malformed_mouse_packet_resynchronizes_at_next_escape(self):
        for prefix in ("\x1b[<0;9;", "\x1b[<x"):
            with self.subTest(prefix=prefix):
                self.replace_content("https://example.org/path")
                self.raw.clear()
                characters = iter(prefix + "\x1b[<0;1;1M\x1b[<32;6;1M\x1b[<0;6;1m\x03")
                reader = MouseInput(lambda: next(characters))
                while True:
                    try:
                        key = reader.read()
                    except StopIteration:
                        break
                    if key is not None:
                        self.editor.handle_key(key)
                self.assertEqual("https", self.editor.selected_text)
                self.assertEqual("\x1b]52;c;aHR0cHM=\x1b\\", self.raw[-1])
                self.assertEqual("https://example.org/path", self.session.buffer.content)
                self.assertEqual([], self.backend.saves)

    def test_links_preserve_balanced_parentheses_and_never_join_logical_lines(self):
        first = "https://example.org/a_(b)?q=x%2Fy&other=yes#part"
        self.replace_content("(" + first + ").\nhttps://example.net/path\n/continuation")
        self.editor.draw()
        targets = re.findall("\x1b]8;;([^\x1b]+)\x1b\\\\", "".join(self.raw))
        self.assertEqual([first, "https://example.net/path"], targets)
        self.raw.clear()
        self.replace_content("file:///tmp/secret javascript:alert(1) https://bad\u202ehidden.test")
        self.editor.draw()
        self.assertEqual([], self.raw)

    def test_real_terminal_live_save_wrap_help_and_clipboard_protocol(self):
        import fcntl
        import pty
        import termios

        try:
            master, slave = pty.openpty()
        except OSError as error:
            self.skipTest("PTY is unavailable: " + str(error))
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.replace_content("")
        root = self.file_path.parent
        requests = root / "requests.jsonl"
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from herdr_description.editor import run_editor\n"
            "root=Path(sys.argv[1])\n"
            "path=root/'description.txt'\n"
            "enabled=True\n"
            "def poll(payload):\n"
            "    global enabled\n"
            "    with (root/'requests.jsonl').open('a') as log:\n"
            "        log.write(json.dumps(payload)+'\\n')\n"
            "    if payload.get('remove'):\n"
            "        path.unlink()\n"
            "        return {'closed': True}\n"
            "    if payload.get('toggle_autosave'):\n"
            "        enabled=not enabled\n"
            "    saved=bool(payload.get('save'))\n"
            "    if saved:\n"
            "        path.write_text(payload['save']['content'], encoding='utf-8')\n"
            "    return {'record': {'content': path.read_text(encoding='utf-8')},\n"
            "        'saved': saved, 'removable': True,\n"
            "        'autosave_supported': True, 'autosave_enabled': enabled}\n"
            "run_editor(path, poll)\n"
        )
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 14, 24, 0, 0))
        environment = {
            **os.environ, "TERM": "xterm-256color",
            "LC_ALL": locale.setlocale(locale.LC_CTYPE),
        }
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(root)],
            cwd=str(ROOT), env=environment, stdin=slave, stdout=slave, stderr=slave,
        )
        transcript = bytearray()

        def messages():
            return [json.loads(line) for line in requests.read_text().splitlines() if line.endswith("}")]

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
            wait_until(lambda: b"\x1b[?1006h" in transcript)
            uri = "https://example.org/日本?q=a%20b&other=1#part"
            content = uri + "\n界"
            os.write(master, ("\x17" + uri + "\r界").encode("utf-8"))
            wait_until(lambda: self.file_path.read_text() == content)
            wait_until(lambda: ("\x1b]8;;" + uri + "\x1b\\").encode() in transcript)
            self.assertEqual(1, sum("save" in message for message in messages()))
            os.write(master, b"\x1fxq\x01draft")
            wait_until(lambda: any(message.get("modified") and "save" not in message for message in messages()))
            self.assertEqual(content, self.file_path.read_text())
            os.write(master, b"\x01")
            wait_until(lambda: self.file_path.read_text() == content + "draft")
            self.assertEqual(2, sum("save" in message for message in messages()))
            os.write(master, b"\x00" + b"\x1bOD" * 3 + b"\x03")
            wait_until(lambda: b"\x1b]52;c;YWZ0\x1b\\" in transcript)
            self.assertEqual(content + "draft", self.file_path.read_text())
            mouse_start = len(transcript)
            os.write(master, b"\x1b[<0;1;1M\x1b[<32;6;1M\x1b[<0;6;1m\x03")
            wait_until(lambda: b"\x1b]52;c;aHR0cHM=\x1b\\" in transcript[mouse_start:])
            self.assertEqual(content + "draft", self.file_path.read_text())
            self.assertEqual(2, sum("save" in message for message in messages()))
            mouse_start = len(transcript)
            polls = len(messages())
            os.write(master, b"\x1b[<0;9;")
            wait_until(lambda: len(messages()) >= polls + 2)
            self.assertEqual(content + "draft", self.file_path.read_text())
            os.write(master, b"1M\x1b[<32;12;1M\x1b[<0;12;1m\x03")
            wait_until(lambda: b"\x1b]52;c;ZXhh\x1b\\" in transcript[mouse_start:])
            self.assertEqual(content + "draft", self.file_path.read_text())
            self.assertEqual(2, sum("save" in message for message in messages()))
            polls = len(messages())
            os.write(master, b"\x1b[<" + b"9" * 80 + b";1;1M\x1b")
            wait_until(lambda: len(messages()) >= polls + 2)
            self.assertEqual(content + "draft", self.file_path.read_text())
            os.write(master, b"!")
            edited = content[:11] + "!" + content[11:] + "draft"
            wait_until(lambda: self.file_path.read_text() == edited)
            self.assertEqual(3, sum("save" in message for message in messages()))
            polls = len(messages())
            os.write(master, b"\x1f\x1b")
            wait_until(lambda: len(messages()) >= polls + 2)
            os.write(master, b"?")
            wait_until(lambda: self.file_path.read_text() == edited[:12] + "?" + edited[12:])
            os.write(master, b"\x18")
            wait_until(lambda: process.poll() is not None)
            self.assertEqual(0, process.returncode)
            self.assertFalse(self.file_path.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_real_terminal_renders_edits_and_help_while_backend_poll_and_save_are_blocked(self):
        import fcntl
        import pty
        import termios

        try:
            master, slave = pty.openpty()
        except OSError as error:
            self.skipTest("PTY is unavailable: " + str(error))
        poll_gate, poll_release = os.pipe()
        save_gate, save_release = os.pipe()
        for descriptor in (master, slave, poll_gate, poll_release, save_gate, save_release):
            self.addCleanup(os.close, descriptor)
        self.replace_content("")
        root = self.file_path.parent
        requests = root / "requests.jsonl"
        script = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from herdr_description.editor import run_editor\n"
            "root=Path(sys.argv[1])\n"
            "path=root/'description.txt'\n"
            "first_poll=True\n"
            "first_save=True\n"
            "def poll(payload):\n"
            "    global first_poll, first_save\n"
            "    with (root/'requests.jsonl').open('a') as log:\n"
            "        log.write(json.dumps(payload)+'\\n')\n"
            "    if first_poll:\n"
            "        first_poll=False\n"
            "        os.read(int(sys.argv[2]), 1)\n"
            "    if payload.get('remove'):\n"
            "        path.unlink()\n"
            "        return {'closed': True}\n"
            "    saved=bool(payload.get('save'))\n"
            "    if saved:\n"
            "        if first_save:\n"
            "            first_save=False\n"
            "            os.read(int(sys.argv[3]), 1)\n"
            "        path.write_text(payload['save']['content'], encoding='utf-8')\n"
            "    return {'record': {'content': path.read_text(encoding='utf-8')},\n"
            "        'saved': saved, 'removable': True,\n"
            "        'autosave_supported': True, 'autosave_enabled': True}\n"
            "run_editor(path, poll)\n"
        )
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 60, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(root), str(poll_gate), str(save_gate)],
            cwd=str(ROOT), env={**os.environ, "TERM": "xterm-256color",
                                "LC_ALL": locale.setlocale(locale.LC_CTYPE)},
            stdin=slave, stdout=slave, stderr=slave, pass_fds=(poll_gate, save_gate),
        )
        transcript = bytearray()

        def messages():
            if not requests.exists():
                return []
            return [json.loads(line) for line in requests.read_text().splitlines() if line.endswith("}")]

        def wait_until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.02)[0]:
                    transcript.extend(os.read(master, 65536))
                if predicate():
                    return
                if process.poll() is not None:
                    self.fail("Editor exited: " + transcript.decode(errors="replace")[-2000:])
            self.fail("Editor timed out: " + transcript.decode(errors="replace")[-2000:])

        try:
            wait_until(lambda: messages() and b"\x1b[?1006h" in transcript)
            os.write(master, "first界".encode())
            wait_until(lambda: "界".encode() in transcript)
            self.assertEqual(self.file_path.read_text(), "")
            self.assertEqual(len(messages()), 1)
            os.write(poll_release, b"1")
            wait_until(lambda: any(message.get("save") for message in messages()))
            first = next(message["save"]["content"] for message in messages() if message.get("save"))
            self.assertEqual(first, "first界")
            uri = "https://example.org/日本?q=a%20b&other=1#part"
            latest = first + "\n" + uri + "\n最新"
            frame_start = len(transcript)
            os.write(master, ("\x17\r" + uri + "\r最新").encode())
            wait_until(lambda: "新".encode() in transcript[frame_start:])
            wait_until(lambda: ("\x1b]8;;" + uri + "\x1b\\").encode() in transcript[frame_start:])
            self.assertEqual(self.file_path.read_text(), "")
            os.write(master, b"\x1f")
            wait_until(lambda: b"Commands" in transcript[frame_start:])
            os.write(master, b"q\x1b[<0;1;1M\x1b[<32;6;1M\x1b[<0;6;1m\x03")
            wait_until(lambda: b"\x1b]52;c;Zmlyc3Q=\x1b\\" in transcript[frame_start:])
            self.assertEqual(sum("save" in message for message in messages()), 1)
            os.write(save_release, b"1")
            wait_until(lambda: self.file_path.read_text() == latest)
            self.assertEqual(sum("save" in message for message in messages()), 2)
            os.write(master, b"\x18")
            wait_until(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(self.file_path.exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
