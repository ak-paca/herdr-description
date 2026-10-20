import curses
import hashlib
import locale
import time
import unicodedata
from collections import deque
from pathlib import Path


class TextBuffer:
    def __init__(self, content=""):
        self.lines = content.split("\n")
        self.baseline = content
        self.row = 0
        self.column = 0
        self.preferred_column = None

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
        line = self.lines[self.row]
        if self.column < len(line):
            self.lines[self.row] = line[:self.column] + line[self.column + 1:]
        elif self.row + 1 < len(self.lines):
            self.lines[self.row:self.row + 2] = [line + self.lines[self.row + 1]]
        self.preferred_column = None

    def delete_line(self):
        del self.lines[self.row]
        if not self.lines:
            self.lines = [""]
        self.row = min(self.row, len(self.lines) - 1)
        self.column = 0
        self.preferred_column = None

    def clear(self):
        self.lines = [""]
        self.row = 0
        self.column = 0
        self.preferred_column = None

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
        self.title = "Description"
        self.focused = False
        self.error = ""
        self.conflict = False
        self.closed = False
        self.saved = False
        self.can_remove = False
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
                self.save(request_id=request_id)
            else:
                self._exchange({"ack_request": request_id, "modified": False})

    def save(self, request_id=None, force=False):
        if self.closed:
            return False
        if not self.buffer.modified:
            return True
        content = self.buffer.content
        request = {
            "content": content,
            "expected_digest": self.buffer.digest,
            "request_id": request_id,
        }
        if force:
            request["force"] = True
        response = self._exchange(
            {"modified": True, "save": request},
            saved_content=content,
        )
        if not response.get("saved") and not response.get("error") and not self.closed:
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
            return "Unsaved · saves when focus leaves"
        return "Saved" if self.saved else "Ready · type to edit"


def _cell_width(character, column=0):
    if character == "\t":
        return 4 - column % 4
    if unicodedata.combining(character):
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
    def __init__(self, screen, session):
        self.screen = screen
        self.session = session
        self.first_row = 0
        self.first_column = 0
        self.prompt = None
        self.running = True
        self.word_wrap = False
        self.preferred_visual_column = None

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
        buffer = session.buffer
        height, width = screen.getmaxyx()
        body_height = max(1, height - 1)
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
        for index in range(min(height, body_height)):
            line_index = self.first_row + index
            if line_index >= len(rows if self.word_wrap else buffer.lines):
                break
            if self.word_wrap:
                logical_row, start, end = rows[line_index]
                text = buffer.lines[logical_row][start:end]
            else:
                text = buffer.lines[line_index]
            self._write(
                index, 0,
                _visible(text, self.first_column, width),
            )
        if self.prompt == "reload":
            footer = "Discard unsaved edits and reload? [y/N]"
        elif self.prompt == "overwrite":
            footer = "Overwrite the externally changed file? [y/N]"
        elif self.prompt == "clear":
            footer = "Clear ALL text? [y/N]"
        else:
            wrap = "on" if self.word_wrap else "off"
            footer = session.status + " | "
            if getattr(session, "can_remove", False):
                footer += "^X remove  "
            footer += f"^W wrap:{wrap}  ^K line  ^L all  ^S save  ^R reload"
        footer_attributes = curses.A_BOLD if self.prompt or session.error else curses.A_DIM
        if height > 1 or (height == 1 and self.prompt):
            self._write(height - 1, 0, footer.ljust(width), footer_attributes)
        if height > 0 and width > 0:
            screen.move(
                min(body_height - 1, cursor_row - self.first_row),
                max(0, min(width - 1, cursor_column - self.first_column)),
            )
        screen.refresh()

    def handle_key(self, key):
        buffer = self.session.buffer
        if key not in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.preferred_visual_column = None
        if self.prompt:
            if isinstance(key, str) and key.lower() == "y":
                if self.prompt == "reload":
                    self.session.reload()
                elif self.prompt == "clear":
                    buffer.clear()
                else:
                    self.session.save(force=True)
                self.prompt = None
            elif key in ("n", "N", "\x1b", "\n", "\r", curses.KEY_ENTER):
                self.prompt = None
            return
        if key == "\x13":
            if self.session.conflict:
                self.prompt = "overwrite"
            elif not self.session.save() and self.session.conflict:
                self.prompt = "overwrite"
        elif key == "\x12":
            if buffer.modified:
                self.prompt = "reload"
            else:
                self.session.reload()
        elif key == "\x0b":
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
            buffer.insert("\n")
        elif key == "\t":
            buffer.insert(" " * (4 - _width(buffer.lines[buffer.row][:buffer.column]) % 4))
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            buffer.backspace()
        elif key == curses.KEY_DC:
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
            self._move_vertical(-max(1, self.screen.getmaxyx()[0] - 1))
        elif key == curses.KEY_NPAGE:
            self._move_vertical(max(1, self.screen.getmaxyx()[0] - 1))
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        elif isinstance(key, str) and key.isprintable():
            buffer.insert(key)

    def _handle_mouse(self):
        try:
            unused, x, y, unused, buttons = curses.getmouse()
        except curses.error:
            return
        height, width = self.screen.getmaxyx()
        if not (buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)):
            return
        if not (0 <= x < width and 0 <= y < min(height, max(1, height - 1))):
            return
        if self.word_wrap:
            self._place_wrapped_cursor(
                self._wrapped_rows(width), self.first_row + y, x
            )
            return
        buffer = self.session.buffer
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
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
        except curses.error:
            pass
        next_poll = 0
        while self.running and not self.session.closed:
            now = time.monotonic()
            if now >= next_poll:
                self.session.tick()
                next_poll = time.monotonic() + 0.2
                if self.session.closed:
                    break
            self.draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self.handle_key(key)


def run_editor(file_path: Path, poll) -> None:
    locale.setlocale(locale.LC_ALL, "")
    session = EditorSession(file_path, poll)
    curses.wrapper(lambda screen: EditorScreen(screen, session).run())
