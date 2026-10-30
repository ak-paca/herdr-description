import curses
import hashlib
import locale
import time
import unicodedata
import webbrowser
from collections import deque
from pathlib import Path

from .editor_support import MouseInput, copy_sequence, hyperlink_overlay, links_in_line, terminal_write


class TextBuffer:
    def __init__(self, content=""):
        self.lines = content.split("\n")
        self.baseline = content
        self.row = 0
        self.column = 0
        self.preferred_column = None
        self.editing_error = ""

    @property
    def content(self):
        return "\n".join(self.lines)

    @property
    def modified(self):
        return self.content != self.baseline

    @property
    def digest(self):
        return hashlib.sha256(self.baseline.encode("utf-8")).hexdigest()

    def replace(self, content):
        self.lines = content.split("\n")
        self.baseline = content
        self.row = min(self.row, len(self.lines) - 1)
        self.column = min(self.column, len(self.lines[self.row]))
        self.preferred_column = None

    def reload_if_clean(self, content):
        if not self.modified and content != self.content:
            self.replace(content)

    def insert(self, text):
        self._check_editable()
        before = self.lines[self.row][:self.column]
        after = self.lines[self.row][self.column:]
        inserted = text.split("\n")
        self.lines[self.row:self.row + 1] = (before + text + after).split("\n")
        if len(inserted) > 1:
            self.row += len(inserted) - 1
            self.column = len(inserted[-1])
        else:
            self.column += len(text)
        self.preferred_column = None

    def backspace(self):
        self._check_editable()
        if self.column:
            line = self.lines[self.row]
            self.lines[self.row] = line[:self.column - 1] + line[self.column:]
            self.column -= 1
        elif self.row:
            self.column = len(self.lines[self.row - 1])
            self.lines[self.row - 1:self.row + 1] = [
                self.lines[self.row - 1] + self.lines[self.row]
            ]
            self.row -= 1
        self.preferred_column = None

    def delete(self):
        self._check_editable()
        line = self.lines[self.row]
        if self.column < len(line):
            self.lines[self.row] = line[:self.column] + line[self.column + 1:]
        elif self.row + 1 < len(self.lines):
            self.lines[self.row:self.row + 2] = [line + self.lines[self.row + 1]]
        self.preferred_column = None

    def delete_line(self):
        self._check_editable()
        del self.lines[self.row]
        if not self.lines:
            self.lines = [""]
        self.row = min(self.row, len(self.lines) - 1)
        self.column = 0
        self.preferred_column = None

    def clear(self):
        self._check_editable()
        self.lines = [""]
        self.row = 0
        self.column = 0
        self.preferred_column = None

    def _check_editable(self):
        if self.editing_error:
            raise RuntimeError(self.editing_error)

    def move_horizontal(self, delta):
        for unused in range(abs(delta)):
            if delta < 0:
                if self.column:
                    self.column -= 1
                elif self.row:
                    self.row -= 1
                    self.column = len(self.lines[self.row])
            elif self.column < len(self.lines[self.row]):
                self.column += 1
            elif self.row + 1 < len(self.lines):
                self.row += 1
                self.column = 0
        self.preferred_column = None

    def move_vertical(self, delta):
        if self.preferred_column is None:
            self.preferred_column = self.column
        self.row = max(0, min(len(self.lines) - 1, self.row + delta))
        self.column = min(self.preferred_column, len(self.lines[self.row]))

    def move_line_boundary(self, end=False):
        self.column = len(self.lines[self.row]) if end else 0
        self.preferred_column = None


class EditorSession:
    def __init__(self, file_path, poll):
        self.file_path = Path(file_path)
        self.poll = poll
        self.buffer = TextBuffer(self.file_path.read_bytes().decode("utf-8"))
        if self.buffer.content == "-- ":
            self.buffer.column = 3
        self.title = "Description"
        self.focused = False
        self.error = ""
        self.conflict = False
        self.closed = False
        self.saved = False
        self.can_remove = False
        self.autosave_supported = False
        self.autosave_enabled = False
        self._autosave_attempted = None
        self._handled_requests = deque(maxlen=64)

    def _exchange(self, payload, saved_content=None):
        try:
            response = self.poll({"editor": "simple", **payload})
            if not isinstance(response, dict):
                raise ValueError("Invalid response from the description plugin.")
        except Exception as error:
            self.error = str(error) or type(error).__name__
            return {}
        if response.get("closed"):
            self.closed = True
            return response
        if "removable" in response:
            self.can_remove = response["removable"] is True
        was_enabled = self.autosave_enabled
        if "autosave_supported" in response:
            self.autosave_supported = response["autosave_supported"] is True
        if "autosave_enabled" in response:
            self.autosave_enabled = response["autosave_enabled"] is True
        self.autosave_enabled = self.autosave_supported and self.autosave_enabled
        if self.autosave_enabled and not was_enabled:
            self._autosave_attempted = None
        record = response.get("record") or {}
        self.title = record.get("title", self.title)
        self.focused = bool(record.get("focused", self.focused))
        if response.get("error"):
            self.error = str(response["error"])
            self.conflict = bool(response.get("conflict", self.conflict))
        if response.get("saved") and saved_content is not None:
            content = record.get("content")
            self.buffer.replace(content if isinstance(content, str) else saved_content)
            self.error = ""
            self.conflict = False
            self.saved = True
        if isinstance(record.get("content"), str):
            self.buffer.reload_if_clean(record["content"])
        if response.get("warning"):
            self.error = str(response["warning"])
        return response

    def tick(self):
        if self.closed:
            return
        response = self._exchange({"modified": self.buffer.modified})
        request = response.get("save_request") or {}
        request_id = request.get("id")
        if (
            not self.closed
            and response.get("write_requested")
            and request_id not in self._handled_requests
        ):
            self._handled_requests.append(request_id)
            if self.buffer.modified:
                self.save(request_id=request_id, reason="blur")
            else:
                self._exchange({"ack_request": request_id, "modified": False})
        self.autosave()

    def autosave(self):
        if not self.autosave_enabled or not self.buffer.modified or self.closed:
            return False
        draft = (self.buffer.content, self.buffer.digest)
        if draft == self._autosave_attempted:
            return False
        self._autosave_attempted = draft
        return self.save(reason="autosave")

    def toggle_autosave(self):
        if self.closed or not self.autosave_supported:
            return False
        response = self._exchange({"toggle_autosave": True})
        if not response.get("error"):
            self.autosave()
        return self.autosave_enabled

    def save(self, request_id=None, force=False, reason="manual"):
        if self.closed:
            return False
        if not self.buffer.modified:
            return True
        content = self.buffer.content
        self._autosave_attempted = (content, self.buffer.digest)
        request = {
            "content": content,
            "expected_digest": self.buffer.digest,
            "request_id": request_id,
            "reason": reason,
        }
        if force:
            request["force"] = True
        response = self._exchange(
            {"modified": True, "save": request},
            saved_content=content,
        )
        if (
            not response.get("saved") and not response.get("error")
            and not response.get("autosave_skipped") and not self.closed
        ):
            self.error = self.error or "The description could not be saved."
        return bool(response.get("saved")) and not self.closed

    def remove(self):
        if self.closed or not self.can_remove:
            return False
        response = self._exchange({"remove": True})
        if not self.closed:
            self.error = self.error or "The description could not be removed."
        return bool(response.get("closed")) and self.closed

    def reload(self):
        if self.closed:
            return False
        response = self._exchange({"modified": self.buffer.modified})
        content = (response.get("record") or {}).get("content")
        if self.closed or response.get("error") or not isinstance(content, str):
            self.error = self.error or "The description could not be reloaded."
            return False
        self.buffer.replace(content)
        self.error = ""
        self.conflict = False
        return True

    @property
    def status(self):
        if self.error:
            return self.error
        if self.buffer.modified:
            return "Unsaved"
        return "Saved" if self.saved else "Ready"


def _cell_width(character, column=0):
    if character == "\t":
        return 4 - column % 4
    if unicodedata.category(character) in ("Mn", "Me"):
        return 0
    if unicodedata.east_asian_width(character) in ("W", "F"):
        return 2
    return 1


def _width(text):
    result = 0
    for character in text:
        result += _cell_width(character, result)
    return result


def _visible(text, start, width):
    column = 0
    rendered = []
    for character in text:
        length = _cell_width(character, column)
        end = column + length
        if column >= start + width and length:
            break
        if length == 0:
            if rendered:
                rendered.append(character)
        elif end > start:
            if character == "\t" or not character.isprintable():
                rendered.append(" " * (min(end, start + width) - max(column, start)))
            elif column < start or end > start + width:
                rendered.append(" " * (min(end, start + width) - max(column, start)))
            else:
                rendered.append(character)
        column = end
    return "".join(rendered)


class EditorScreen:
    def __init__(self, screen, session, raw_writer=None, open_url=None):
        self.screen = screen
        self.session = session
        self.first_row = 0
        self.first_column = 0
        self.prompt = None
        self.running = True
        self.word_wrap = False
        self.preferred_visual_column = None
        self.selection_anchor = None
        self.selection_active = False
        self.mouse_dragging = False
        self._control_mouse_down = False
        self.help_open = False
        self.help_scroll = 0
        self.notice = ""
        self.raw_writer = raw_writer if raw_writer is not None else terminal_write
        self.open_url = open_url if open_url is not None else webbrowser.open
        self._link_fragments = []

    @property
    def selection(self):
        buffer = self.session.buffer
        cursor = (buffer.row, buffer.column)
        if self.selection_anchor is not None:
            row = min(self.selection_anchor[0], len(buffer.lines) - 1)
            self.selection_anchor = (row, min(self.selection_anchor[1], len(buffer.lines[row])))
        if self.selection_anchor is None or self.selection_anchor == cursor:
            return None
        return tuple(sorted((self.selection_anchor, cursor)))

    @property
    def selected_text(self):
        selection = self.selection
        if selection is None:
            return ""
        (start_row, start_column), (end_row, end_column) = selection
        lines = self.session.buffer.lines[start_row:end_row + 1]
        if len(lines) == 1:
            return lines[0][start_column:end_column]
        return "\n".join([lines[0][start_column:], *lines[1:-1], lines[-1][:end_column]])

    def clear_selection(self):
        self.selection_anchor = None
        self.selection_active = False
        self.mouse_dragging = False

    def copy_selection(self):
        text = self.selected_text
        if not text:
            self.notice = "Select text to copy"
            return False
        try:
            self.raw_writer(copy_sequence(text))
        except (OSError, UnicodeError) as error:
            self.notice = "Copy failed: " + str(error)
            return False
        self.notice = "Copied"
        return True

    def _delete_selection(self):
        self.session.buffer._check_editable()
        selection = self.selection
        self.clear_selection()
        if selection is None:
            return False
        (start_row, start_column), (end_row, end_column) = selection
        buffer = self.session.buffer
        buffer.lines[start_row:end_row + 1] = [
            buffer.lines[start_row][:start_column] + buffer.lines[end_row][end_column:]
        ]
        buffer.row, buffer.column = start_row, start_column
        buffer.preferred_column = None
        return True

    def _footer(self):
        if self.prompt == "reload":
            return "Discard unsaved edits and reload? [y/N]"
        if self.prompt == "overwrite":
            return "Overwrite the externally changed file? [y/N]"
        if self.prompt == "clear":
            return "Clear ALL text? [y/N]"
        auto = "on" if getattr(self.session, "autosave_enabled", False) else "off"
        wrap = "on" if self.word_wrap else "off"
        return f"{self.notice or self.session.status} | auto:{auto} wrap:{wrap} ^/ help"

    def _text_rows(self, text, width):
        width = max(1, width)
        rows = []
        row = ""
        cells = 0
        for character in text:
            length = _cell_width(character, cells)
            if character == "\n" or (length and cells + length > width and row):
                rows.append(row)
                row, cells = "", 0
                length = _cell_width(character)
            if character != "\n":
                row += character
                cells += length
        rows.append(row)
        return rows

    def _layout(self):
        height, width = self.screen.getmaxyx()
        footer = self._footer()
        rows = self._text_rows(footer, width) if self.word_wrap else [footer]
        available = max(0, height - 1)
        if height == 1 and self.prompt:
            available = 1
        rows = rows[:available]
        if self.prompt and available and len(self._text_rows(footer, width)) > available:
            rows[-1] = _visible("[y/N]", 0, width)
        return max(1, height - len(rows)), rows

    @property
    def body_height(self):
        return self._layout()[0]

    def _help_rows(self, width):
        automatic = (
            "on" if getattr(self.session, "autosave_enabled", False) else "off"
        ) if getattr(self.session, "autosave_supported", False) else "unavailable (manual save)"
        commands = [
            "Commands — q / Esc / Ctrl+/ close",
            "Up/Down, PgUp/PgDn: scroll help",
            "Ctrl+/ (Ctrl+_) or F1: commands",
            f"Ctrl+A: toggle shared autosave ({automatic})",
            "Ctrl+W: toggle text and footer wrapping",
            "Ctrl+S: save (confirm overwrite on conflict)",
            "Ctrl+R: reload (confirm discarding changes)",
            "Ctrl+K: delete logical line",
            "Ctrl+L: clear all text (confirm with y)",
            "Ctrl+X: remove description" if getattr(self.session, "can_remove", False)
            else "Ctrl+X: removal unavailable here",
            "Ctrl+Q: disabled; close the pane to leave",
            "Arrows: move; Home/End: logical line start/end",
            "PgUp/PgDn: move by visible text height",
            "Mouse drag or Shift+arrows: select text",
            "Ctrl+Space, then arrows: anchor selection",
            "Ctrl+C: copy exact selection to clipboard",
            "Esc: clear selection; typing replaces it",
            "Ctrl+click: open HTTP(S) links",
            "Enter: newline; Tab: spaces; Backspace/Delete: erase",
        ]
        return [row for command in commands for row in self._text_rows(command, width)]

    def _draw_help(self):
        height, width = self.screen.getmaxyx()
        rows = self._help_rows(width)
        self.help_scroll = max(0, min(self.help_scroll, len(rows) - max(1, height)))
        for index, text in enumerate(rows[self.help_scroll:self.help_scroll + height]):
            self._write(index, 0, text, curses.A_BOLD if index == 0 else 0)

    def _render_line(self, screen_row, logical_row, start, end, width):
        line = self.session.buffer.lines[logical_row]
        links = links_in_line(line)
        selection = self.selection
        runs = []
        cells = 0
        left = self.first_column
        for index in range(start, end):
            character = line[index]
            length = _cell_width(character, cells)
            right = cells + length
            if length and cells >= left + width:
                break
            uri = next((uri for low, high, uri in links if low <= index < high), None)
            selected = bool(selection and selection[0] <= (logical_row, index) < selection[1])
            attributes = (curses.A_UNDERLINE if uri else 0) | (curses.A_REVERSE if selected else 0)
            if length == 0:
                if runs:
                    runs[-1][1] += character
                continue
            if right > left:
                column = max(0, cells - left)
                if character == "\t" or not character.isprintable() or cells < left or right > left + width:
                    text = " " * (min(right, left + width) - max(cells, left))
                else:
                    text = character
                if runs and runs[-1][2:] == [attributes, uri, selected]:
                    runs[-1][1] += text
                else:
                    runs.append([column, text, attributes, uri, selected])
            cells = right
        if not runs:
            self._write(screen_row, 0, "")
        fragments = []
        for column, text, attributes, uri, selected in runs:
            self._write(screen_row, column, text, attributes)
            if uri:
                fragments.append((screen_row, column, text, uri, selected))
        if selection and end == len(line) and selection[0] <= (logical_row, end) < selection[1]:
            column = cells - left
            if 0 <= column < width:
                self._write(screen_row, column, " ", curses.A_REVERSE)
        return fragments

    def _wrapped_rows(self, width):
        width = max(1, width)
        rows = []
        for line_index, line in enumerate(self.session.buffer.lines):
            start = 0
            while start < len(line):
                end = start
                cells = 0
                word_end = None
                while end < len(line):
                    length = _cell_width(line[end], cells)
                    if length and cells + length > width and end > start:
                        break
                    cells += length
                    if line[end].isspace():
                        word_end = end + 1
                    end += 1
                if end < len(line) and word_end is not None:
                    end = word_end
                rows.append((line_index, start, end))
                start = end
            if not line or _width(line[rows[-1][1]:]) >= width:
                rows.append((line_index, len(line), len(line)))
        return rows

    def _wrapped_cursor(self, rows):
        buffer = self.session.buffer
        cursor_row = 0
        start = 0
        for index, (line_index, row_start, unused) in enumerate(rows):
            if line_index == buffer.row and row_start <= buffer.column:
                cursor_row, start = index, row_start
            elif line_index > buffer.row:
                break
        return cursor_row, _width(buffer.lines[buffer.row][start:buffer.column])

    def _place_wrapped_cursor(self, rows, row_index, target):
        buffer = self.session.buffer
        row_index = max(0, min(len(rows) - 1, row_index))
        line_index, start, end = rows[row_index]
        limit = end
        if row_index + 1 < len(rows) and rows[row_index + 1][0] == line_index:
            limit = max(start, end - 1)
            while limit > start and unicodedata.combining(buffer.lines[line_index][limit]):
                limit -= 1
        cells = 0
        column = start
        for column in range(start, end):
            length = _cell_width(buffer.lines[line_index][column], cells)
            if cells + length > target:
                break
            cells += length
        else:
            column = end
        buffer.row = line_index
        buffer.column = min(column, limit)
        buffer.preferred_column = None

    def _move_vertical(self, delta):
        if not self.word_wrap:
            self.session.buffer.move_vertical(delta)
            return
        rows = self._wrapped_rows(self.screen.getmaxyx()[1])
        cursor_row, cursor_column = self._wrapped_cursor(rows)
        if self.preferred_visual_column is None:
            self.preferred_visual_column = cursor_column
        target = max(0, min(len(rows) - 1, cursor_row + delta))
        if target != cursor_row:
            self._place_wrapped_cursor(rows, target, self.preferred_visual_column)

    def _write(self, row, column, text, attributes=0):
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or column < 0 or column >= width:
            return
        try:
            self.screen.addstr(
                row, column, _visible(text, 0, width - column), attributes
            )
        except curses.error:
            pass

    def draw(self):
        screen = self.screen
        session = self.session
        if getattr(session, "pending_prompt", None) and not self.prompt and not self.help_open:
            self.prompt = session.pending_prompt
            session.pending_prompt = None
        buffer = session.buffer
        height, width = screen.getmaxyx()
        body_height, footer_rows = self._layout()
        rows = self._wrapped_rows(width) if self.word_wrap else None
        if self.word_wrap:
            cursor_row, cursor_column = self._wrapped_cursor(rows)
            self.first_column = 0
        else:
            cursor_row = buffer.row
            cursor_column = _width(buffer.lines[buffer.row][:buffer.column])
            self.first_column = min(self.first_column, cursor_column)
            if cursor_column >= self.first_column + max(1, width):
                self.first_column = cursor_column - max(1, width) + 1
        self.first_row = max(0, min(self.first_row, cursor_row))
        if cursor_row >= self.first_row + body_height:
            self.first_row = cursor_row - body_height + 1
        screen.erase()
        fragments = []
        if self.help_open:
            self._draw_help()
        else:
            for index in range(min(height, body_height)):
                line_index = self.first_row + index
                if line_index >= len(rows if self.word_wrap else buffer.lines):
                    break
                if self.word_wrap:
                    logical_row, start, end = rows[line_index]
                else:
                    logical_row, start = line_index, 0
                    end = len(buffer.lines[line_index])
                fragments.extend(self._render_line(index, logical_row, start, end, width))
            attributes = curses.A_BOLD if self.prompt or session.error else curses.A_DIM
            for index, footer in enumerate(footer_rows):
                self._write(height - len(footer_rows) + index, 0, footer.ljust(width), attributes)
            if height == 1 and self.prompt:
                fragments = []
        if fragments != self._link_fragments:
            # Curses cannot see OSC 8 metadata, so its text diff cannot clear stale links.
            screen.redrawwin()
        self._link_fragments = fragments
        if height > 0 and width > 0:
            screen.move(
                0 if self.help_open else min(body_height - 1, cursor_row - self.first_row),
                0 if self.help_open else max(0, min(width - 1, cursor_column - self.first_column)),
            )
        screen.refresh()
        overlay = hyperlink_overlay(fragments)
        if overlay:
            self.raw_writer(overlay)

    def _after_edit(self, previous):
        if self.session.buffer.content != previous and hasattr(self.session, "autosave"):
            self.session.autosave()

    def _handle_help(self, key):
        if key in ("q", "Q", "\x1b", "\x1f", curses.KEY_F1):
            self.help_open = False
        elif key in (curses.KEY_DOWN, "j"):
            self.help_scroll += 1
        elif key in (curses.KEY_UP, "k"):
            self.help_scroll = max(0, self.help_scroll - 1)
        elif key == curses.KEY_NPAGE:
            self.help_scroll += max(1, self.screen.getmaxyx()[0])
        elif key == curses.KEY_PPAGE:
            self.help_scroll = max(0, self.help_scroll - max(1, self.screen.getmaxyx()[0]))
        elif key == curses.KEY_HOME:
            self.help_scroll = 0
        elif key == curses.KEY_END:
            self.help_scroll = len(self._help_rows(self.screen.getmaxyx()[1]))

    def handle_key(self, key):
        buffer = self.session.buffer
        previous = buffer.content
        self.notice = ""
        if self.help_open:
            self._handle_help(key)
            return
        if self.prompt:
            if key == "\x17":
                self.word_wrap = not self.word_wrap
            elif isinstance(key, str) and key.lower() == "y":
                if buffer.editing_error:
                    self.notice = buffer.editing_error
                    return
                if self.prompt == "reload":
                    self.session.reload()
                elif self.prompt == "clear":
                    buffer.clear()
                    self._after_edit(previous)
                else:
                    self.session.save(force=True)
                self.clear_selection()
                self.prompt = None
            elif key in ("n", "N", "\x1b", "\n", "\r", curses.KEY_ENTER):
                self.prompt = None
            return
        if buffer.editing_error and (
            key in (
                curses.KEY_ENTER, "\n", "\r", "\t", curses.KEY_BACKSPACE,
                "\x7f", "\b", curses.KEY_DC, "\x0b", "\x0c",
            )
            or isinstance(key, str) and key.isprintable()
        ):
            self.notice = buffer.editing_error
            return
        shifted = {
            getattr(curses, "KEY_SLEFT", -10): curses.KEY_LEFT,
            getattr(curses, "KEY_SRIGHT", -11): curses.KEY_RIGHT,
            getattr(curses, "KEY_SR", -12): curses.KEY_UP,
            getattr(curses, "KEY_SF", -13): curses.KEY_DOWN,
            getattr(curses, "KEY_SHOME", -14): curses.KEY_HOME,
            getattr(curses, "KEY_SEND", -15): curses.KEY_END,
        }
        navigation = {
            curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN,
            curses.KEY_HOME, curses.KEY_END, curses.KEY_PPAGE, curses.KEY_NPAGE,
        }
        if key in shifted:
            if self.selection_anchor is None:
                self.selection_anchor = (buffer.row, buffer.column)
            key = shifted[key]
        elif key in navigation and not self.selection_active:
            self.clear_selection()
        if key not in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.preferred_visual_column = None
        if key == "\x13":
            if self.session.conflict:
                self.prompt = "overwrite"
            elif not self.session.save() and self.session.conflict:
                self.prompt = "overwrite"
        elif key == "\x01":
            self.session.toggle_autosave()
        elif key in ("\x1f", curses.KEY_F1):
            self.help_open = True
            self.help_scroll = 0
        elif key == "\x03":
            self.copy_selection()
        elif key == "\x00":
            self.selection_anchor = (buffer.row, buffer.column)
            self.selection_active = True
        elif key == "\x1b":
            self.clear_selection()
        elif key == "\x12":
            if buffer.modified:
                self.prompt = "reload"
            else:
                self.session.reload()
                self.clear_selection()
        elif key == "\x0b":
            if not self._delete_selection():
                buffer.delete_line()
        elif key == "\x0c":
            self.prompt = "clear"
        elif key == "\x17":
            self.word_wrap = not self.word_wrap
            self.first_row = 0
            self.first_column = 0
            buffer.preferred_column = None
        elif key == "\x18":
            if self.session.can_remove and self.session.remove():
                self.running = False
        elif key in (curses.KEY_ENTER, "\n", "\r"):
            self._delete_selection()
            buffer.insert("\n")
        elif key == "\t":
            self._delete_selection()
            buffer.insert(" " * (4 - _width(buffer.lines[buffer.row][:buffer.column]) % 4))
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if not self._delete_selection():
                buffer.backspace()
        elif key == curses.KEY_DC:
            if not self._delete_selection():
                buffer.delete()
        elif key == curses.KEY_LEFT:
            buffer.move_horizontal(-1)
        elif key == curses.KEY_RIGHT:
            buffer.move_horizontal(1)
        elif key == curses.KEY_UP:
            self._move_vertical(-1)
        elif key == curses.KEY_DOWN:
            self._move_vertical(1)
        elif key == curses.KEY_HOME:
            buffer.move_line_boundary()
        elif key == curses.KEY_END:
            buffer.move_line_boundary(end=True)
        elif key == curses.KEY_PPAGE:
            self._move_vertical(-self.body_height)
        elif key == curses.KEY_NPAGE:
            self._move_vertical(self.body_height)
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        elif isinstance(key, tuple) and key[0] == curses.KEY_MOUSE:
            self._handle_mouse(key[1:])
        elif isinstance(key, str) and key.isprintable():
            self._delete_selection()
            buffer.insert(key)
        self._after_edit(previous)

    def _handle_mouse(self, event=None):
        if event is None:
            try:
                event = curses.getmouse()
            except curses.error:
                return
        unused, x, y, unused, buttons = event
        height, width = self.screen.getmaxyx()
        clicked = buttons & curses.BUTTON1_CLICKED
        pressed = buttons & curses.BUTTON1_PRESSED
        released = buttons & curses.BUTTON1_RELEASED
        motion = buttons & getattr(curses, "REPORT_MOUSE_POSITION", 0)
        control = buttons & getattr(curses, "BUTTON_CTRL", 0)
        was_dragging = self.mouse_dragging
        if released:
            self._control_mouse_down = False
            self.mouse_dragging = False
            if control:
                return
        if control and (pressed or clicked):
            if self._control_mouse_down or self.mouse_dragging or motion:
                if self.mouse_dragging:
                    self.clear_selection()
                return
            self._control_mouse_down = bool(pressed)
        elif control:
            return
        if not (clicked or pressed or (was_dragging and (motion or released))):
            return
        if not (0 <= x < width and 0 <= y < min(height, self.body_height)):
            if released:
                self.mouse_dragging = False
            return
        buffer = self.session.buffer
        if self.word_wrap:
            self._place_wrapped_cursor(self._wrapped_rows(width), self.first_row + y, x)
        else:
            buffer.row = min(len(buffer.lines) - 1, self.first_row + y)
            target = self.first_column + x
            column = 0
            buffer.column = len(buffer.lines[buffer.row])
            for index, character in enumerate(buffer.lines[buffer.row]):
                length = _cell_width(character, column)
                if column + length > target:
                    buffer.column = index
                    break
                column += length
            buffer.preferred_column = None
        if control:
            self.clear_selection()
            uri = next((
                uri for low, high, uri in links_in_line(buffer.lines[buffer.row])
                if low <= buffer.column < high
            ), None)
            if uri:
                self.open_url(uri)
            return
        if clicked:
            self.clear_selection()
        elif pressed and not self.mouse_dragging:
            self.selection_anchor = (buffer.row, buffer.column)
            self.selection_active = False
            self.mouse_dragging = True
        elif released:
            self.mouse_dragging = False

    def run(self):
        self.screen.keypad(True)
        self.screen.timeout(100)
        curses.raw()
        curses.noecho()
        attributes = curses.A_NORMAL
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, -1, -1)
            attributes = curses.color_pair(1)
        except (AttributeError, curses.error):
            pass
        self.screen.bkgd(" ", attributes)
        self.screen.attrset(attributes)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass
        try:
            mouse_prefix = curses.tigetstr("kmous")
        except curses.error:
            mouse_prefix = None
        # Legacy ncurses drops batched X10 presses; decode SGR outside its mouse queue.
        mouse_input = (
            MouseInput(self.screen.get_wch)
            if mouse_prefix != b"\x1b[<" else None
        )
        next_poll = 0
        self.raw_writer("\x1b[?1003h")
        if mouse_input is not None:
            self.raw_writer("\x1b[?1006h")
        try:
            while self.running and not self.session.closed:
                now = time.monotonic()
                if hasattr(self.session, "pump"):
                    self.session.pump()
                    if self.session.closed:
                        break
                if now >= next_poll:
                    self.session.tick()
                    next_poll = time.monotonic() + 0.2
                    if self.session.closed:
                        break
                self.draw()
                try:
                    key = mouse_input.read() if mouse_input is not None else self.screen.get_wch()
                except curses.error:
                    continue
                if key is not None:
                    self.handle_key(key)
        finally:
            self.raw_writer("\x1b]8;;\x1b\\\x1b[?1003l")
            if mouse_input is not None:
                self.raw_writer("\x1b[?1006l")


def run_editor(file_path: Path, poll) -> None:
    from .editor_async import AsyncEditorSession

    locale.setlocale(locale.LC_ALL, "")
    session = AsyncEditorSession(file_path, poll)
    try:
        curses.wrapper(lambda screen: EditorScreen(screen, session).run())
    finally:
        session.shutdown()
