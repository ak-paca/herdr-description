import base64
import curses
import re
import sys
import unicodedata
from collections import deque
from urllib.parse import urlsplit


class MouseInput:
    def __init__(self, read_character):
        self.read_character = read_character
        self.pending = deque()
        self.prefix = ""
        self.escape_reported = False
        self.discarding = False

    def read(self):
        for unused in range(128):
            try:
                key = self.pending.popleft() if self.pending else self.read_character()
            except curses.error:
                if self.prefix == "\x1b" and not self.escape_reported:
                    self.escape_reported = True
                    return "\x1b"
                raise
            if key == curses.KEY_RESIZE and self.prefix:
                return key
            if not self.prefix:
                if key != "\x1b":
                    return key
                self.prefix = key
                self.escape_reported = False
            elif self.prefix == "\x1b":
                if key == "\x1b":
                    self.escape_reported = False
                    return None
                if key == "[":
                    self.prefix += key
                else:
                    self.prefix = ""
                    if self.escape_reported:
                        return key
                    self.pending.appendleft(key)
                    return "\x1b"
            elif self.prefix == "\x1b[":
                if key == "<":
                    self.prefix += key
                else:
                    self.prefix = ""
                    self.pending.extend(("[", key))
                    if not self.escape_reported:
                        return "\x1b"
            elif key == "\x1b":
                self.prefix = key
                self.escape_reported = False
                self.discarding = False
            elif isinstance(key, str) and key in ("M", "m"):
                packet = self.prefix + key
                invalid = self.discarding
                self.prefix = ""
                self.discarding = False
                return None if invalid else self._mouse(packet)
            elif isinstance(key, str) and key in "0123456789;" and len(self.prefix) < 64:
                self.prefix += key
            else:
                self.discarding = True
        return None

    def _mouse(self, packet):
        match = re.fullmatch(r"\x1b\[<(\d{1,6});(\d{1,6});(\d{1,6})([Mm])", packet)
        if not match:
            return None
        code, x, y = (int(value) for value in match.groups()[:3])
        if code >= 64 or x < 1 or y < 1:
            return None
        button = code & 3
        if button not in (0, 3):
            return None
        buttons = 0
        if match[4] == "m" or button == 3:
            buttons |= curses.BUTTON1_RELEASED
        elif code & 32:
            buttons |= curses.REPORT_MOUSE_POSITION
        else:
            buttons |= curses.BUTTON1_PRESSED
        if code & 16:
            buttons |= curses.BUTTON_CTRL
        if code & 4:
            buttons |= curses.BUTTON_SHIFT
        if code & 8:
            buttons |= curses.BUTTON_ALT
        return (curses.KEY_MOUSE, 0, x - 1, y - 1, 0, buttons)


def links_in_line(line):
    links = []
    for match in re.finditer(r"https?://[^\s<>\"`\x00-\x1f\x7f-\x9f]+", line, re.IGNORECASE):
        uri = match.group()
        opening = line[match.start() - 1] if match.start() else ""
        if opening == "'":
            end = uri.rfind("'")
            if end >= 0 and all(character in ".,;:!?" for character in uri[end + 1:]):
                uri = uri[:end]
        closing = {"(": ")", "[": "]", "{": "}"}.get(opening)
        if closing:
            depth = 0
            for index, character in enumerate(uri):
                if character == opening:
                    depth += 1
                elif character == closing:
                    if depth == 0:
                        if all(character in ".,;:!?" for character in uri[index + 1:]):
                            uri = uri[:index]
                        break
                    depth -= 1
        try:
            parsed = urlsplit(uri)
            valid = parsed.scheme.lower() in ("http", "https") and parsed.hostname
        except ValueError:
            valid = False
        if valid and not any(unicodedata.category(char).startswith("C") for char in uri):
            links.append((match.start(), match.start() + len(uri), uri))
    return links


def terminal_write(text):
    if sys.stdout.isatty():
        sys.stdout.write(text)
        sys.stdout.flush()


def copy_sequence(text):
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\x1b]52;c;" + encoded + "\x1b\\"


def hyperlink_overlay(fragments):
    if not fragments:
        return ""
    output = ["\x1b7", "\x1b]8;;\x1b\\"]
    for row, column, text, uri, selected in fragments:
        output.extend((
            f"\x1b[{row + 1};{column + 1}H", "\x1b[0;4" + (";7" if selected else "") + "m",
            "\x1b]8;;" + uri + "\x1b\\", text, "\x1b]8;;\x1b\\",
        ))
    output.extend(("\x1b[0m", "\x1b8"))
    return "".join(output)
