import copy
import dataclasses
import hashlib
import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.app import App
from herdr_description.api import HerdrError


class FakeHerdr:
    def __init__(self, socket_path="/tmp/herdr-description-test.sock"):
        self.socket_path = socket_path
        self.calls = []
        self.workspaces = []
        self.tabs = []
        self.panes = []
        self.trees = {}
        self.next_pane = 1
        self.next_tab = 1
        self.focused_pane_id = None
        self.tab_focus = {}
        self.move_events = []
        self.reported_titles = {}
        self.add_tab("space-1", "tab-1", "shell-1")

    def add_tab(self, workspace_id, tab_id, pane_id):
        if not any(item["workspace_id"] == workspace_id for item in self.workspaces):
            self.workspaces.append({"workspace_id": workspace_id})
        self.tabs.append({"workspace_id": workspace_id, "tab_id": tab_id})
        self.panes.append(
            {
                "workspace_id": workspace_id,
                "tab_id": tab_id,
                "pane_id": pane_id,
                "label": "shell",
            }
        )
        self.trees[tab_id] = {"type": "pane", "pane_id": pane_id}
        self.tab_focus[tab_id] = pane_id
        if self.focused_pane_id is None:
            self.focused_pane_id = pane_id

    def add_pane(self, tab_id, pane_id):
        tab = next(item for item in self.tabs if item["tab_id"] == tab_id)
        self.panes.append(
            {
                "workspace_id": tab["workspace_id"],
                "tab_id": tab_id,
                "pane_id": pane_id,
                "label": "shell",
            }
        )
        self.trees[tab_id] = {
            "type": "split", "direction": "right", "ratio": 0.5,
            "first": self.trees[tab_id],
            "second": {"type": "pane", "pane_id": pane_id},
        }

    def rectangles(self, node, x=0, y=0, width=200, height=120):
        if node is None:
            return []
        if node["type"] == "pane":
            return [{"pane_id": node["pane_id"], "rect": {
                "x": x, "y": y, "width": width, "height": height,
            }}]
        ratio = node.get("ratio", 0.5)
        if node["direction"] == "right":
            return self.rectangles(node["first"], x, y, width * ratio, height) + self.rectangles(
                node["second"], x + width * ratio, y, width * (1 - ratio), height
            )
        return self.rectangles(node["first"], x, y, width, height * ratio) + self.rectangles(
            node["second"], x, y + height * ratio, width, height * (1 - ratio)
        )

    def snapshot(self):
        focused = next((pane for pane in self.panes if pane["pane_id"] == self.focused_pane_id), {})
        return copy.deepcopy({
            "workspaces": self.workspaces, "tabs": self.tabs, "panes": self.panes,
            "focused_workspace_id": focused.get("workspace_id"),
            "focused_tab_id": focused.get("tab_id"),
            "focused_pane_id": self.focused_pane_id,
            "layouts": [{"tab_id": tab["tab_id"], "panes": self.rectangles(self.trees[tab["tab_id"]])}
                        for tab in self.tabs],
        })

    def split(self, node, target_id, pane_id, direction, ratio=0.5):
        if node["type"] == "pane":
            if node["pane_id"] == target_id:
                return {
                    "type": "split", "direction": direction, "ratio": ratio,
                    "first": node,
                    "second": {"type": "pane", "pane_id": pane_id},
                }
            return node
        return {
            **node,
            "first": self.split(node["first"], target_id, pane_id, direction, ratio),
            "second": self.split(node["second"], target_id, pane_id, direction, ratio),
        }

    def prune(self, node, pane_id):
        if node["type"] == "pane":
            return None if node["pane_id"] == pane_id else node
        first = self.prune(node["first"], pane_id)
        second = self.prune(node["second"], pane_id)
        if first is None or second is None:
            return first or second
        return {**node, "first": first, "second": second}

    def remove_pane(self, pane_id):
        pane = next(pane for pane in self.panes if pane["pane_id"] == pane_id)
        self.panes.remove(pane)
        self.trees[pane["tab_id"]] = self.prune(self.trees[pane["tab_id"]], pane_id)
        if self.focused_pane_id == pane_id:
            remaining = [item for item in self.panes if item["tab_id"] == pane["tab_id"]]
            self.focused_pane_id = (remaining or self.panes or [{}])[0].get("pane_id")

    def remove_tab(self, tab_id):
        for pane in list(self.panes):
            if pane["tab_id"] == tab_id:
                self.remove_pane(pane["pane_id"])
        self.tabs = [tab for tab in self.tabs if tab["tab_id"] != tab_id]
        self.trees.pop(tab_id)
        self.tab_focus.pop(tab_id, None)

    def remove_space(self, workspace_id):
        for tab in list(self.tabs):
            if tab["workspace_id"] == workspace_id:
                self.remove_tab(tab["tab_id"])
        self.workspaces = [
            space for space in self.workspaces if space["workspace_id"] != workspace_id
        ]

    def move_pane(self, pane_id, tab_id, destination_id=None):
        pane = copy.deepcopy(next(pane for pane in self.panes if pane["pane_id"] == pane_id))
        tab = next(tab for tab in self.tabs if tab["tab_id"] == tab_id)
        self.remove_pane(pane_id)
        pane_id = destination_id or pane_id
        pane.update(workspace_id=tab["workspace_id"], tab_id=tab_id)
        pane["pane_id"] = pane_id
        self.panes.append(pane)
        self.trees[tab_id] = {
            "type": "split", "direction": "right", "ratio": 0.5,
            "first": self.trees[tab_id],
            "second": {"type": "pane", "pane_id": pane_id},
        }
        self.focused_pane_id = pane_id

    def native_move(self, pane_id, destination, focus):
        pane = next(pane for pane in self.panes if pane["pane_id"] == pane_id)
        previous = copy.deepcopy(pane)
        selected = next(
            (item for item in self.panes if item["pane_id"] == self.focused_pane_id), {}
        )
        selected_tab = selected.get("tab_id")
        if selected_tab:
            self.tab_focus[selected_tab] = self.focused_pane_id
        if destination["type"] == "tab":
            tab = next(tab for tab in self.tabs if tab["tab_id"] == destination["tab_id"])
            if tab["tab_id"] == pane["tab_id"]:
                return {"changed": False, "reason": "same_tab", "pane": copy.deepcopy(pane)}
            target = destination.get("target_pane_id") or self.tab_focus[tab["tab_id"]]
            if not any(
                item["pane_id"] == target and item["tab_id"] == tab["tab_id"]
                for item in self.panes
            ):
                raise HerdrError("Destination pane is not in the target tab")
        elif destination["type"] == "new_tab":
            workspace_id = destination.get("workspace_id", pane["workspace_id"])
            if not any(item["workspace_id"] == workspace_id for item in self.workspaces):
                raise HerdrError("Unknown destination workspace")
            while any(tab["tab_id"] == "moved-tab-{}".format(self.next_tab) for tab in self.tabs):
                self.next_tab += 1
            tab = {
                "tab_id": "moved-tab-{}".format(self.next_tab),
                "workspace_id": workspace_id,
                "label": destination.get("label", ""),
            }
            self.next_tab += 1
            self.tabs.append(tab)
            target = None
        else:
            raise AssertionError("Unsupported pane.move destination")
        old_tab = pane["tab_id"]
        self.trees[old_tab] = self.prune(self.trees[old_tab], pane_id)
        pane.update(workspace_id=tab["workspace_id"], tab_id=tab["tab_id"])
        if target:
            self.trees[tab["tab_id"]] = self.split(
                self.trees[tab["tab_id"]], target, pane_id,
                destination.get("split", "right"), destination.get("ratio", 0.5),
            )
        else:
            self.trees[tab["tab_id"]] = {"type": "pane", "pane_id": pane_id}
            self.tab_focus[tab["tab_id"]] = pane_id
        remaining = [item for item in self.panes if item["tab_id"] == old_tab]
        closed_tab = None
        closed_workspace = None
        if not remaining:
            closed_tab = old_tab
            self.remove_tab(old_tab)
            if not any(item["workspace_id"] == previous["workspace_id"] for item in self.tabs):
                closed_workspace = previous["workspace_id"]
                self.workspaces = [
                    item for item in self.workspaces if item["workspace_id"] != closed_workspace
                ]
        elif self.tab_focus.get(old_tab) == pane_id:
            self.tab_focus[old_tab] = remaining[0]["pane_id"]
        if focus:
            self.focused_pane_id = pane_id
            self.tab_focus[tab["tab_id"]] = pane_id
        elif self.focused_pane_id == pane_id:
            self.focused_pane_id = self.tab_focus.get(selected_tab) or self.tab_focus[tab["tab_id"]]
        event = {
            "type": "pane_moved", "previous_pane_id": pane_id,
            "previous_workspace_id": previous["workspace_id"],
            "previous_tab_id": old_tab, "pane": copy.deepcopy(pane),
        }
        if closed_tab:
            event["closed_tab_id"] = closed_tab
        if closed_workspace:
            event["closed_workspace_id"] = closed_workspace
        self.move_events.append(event)
        return {"changed": True, "pane": copy.deepcopy(pane), "tab": copy.deepcopy(tab)}

    def call(self, method, **arguments):
        if method == "layout.apply":
            raise AssertionError("Applying a replacement layout can destroy shell panes")
        self.calls.append((method, copy.deepcopy(arguments)))
        if method == "pane.move":
            return self.native_move(
                arguments["pane_id"], arguments["destination"], arguments.get("focus", True)
            )
        if method == "plugin.pane.open":
            if arguments.get("placement") in ("split", "zoomed") and "workspace_id" in arguments:
                raise AssertionError("Split and zoomed plugin panes accept target_pane_id, not workspace_id")
            if arguments["entrypoint"] not in ("section", "descriptions"):
                return {}
            target = next(
                pane for pane in self.panes
                if pane["pane_id"] == arguments["target_pane_id"]
            )
            pane_id = "plugin-{}".format(self.next_pane)
            self.next_pane += 1
            pane = {
                "workspace_id": target["workspace_id"],
                "tab_id": target["tab_id"],
                "pane_id": pane_id,
                "label": "", "cwd": arguments.get("cwd", "/tmp"),
            }
            self.panes.append(pane)
            self.trees[target["tab_id"]] = self.split(
                self.trees[target["tab_id"]], target["pane_id"], pane_id, arguments.get("direction", "right")
            )
            if arguments.get("focus"):
                self.focused_pane_id = pane_id
                self.tab_focus[target["tab_id"]] = pane_id
            return {"plugin_pane": {"pane": copy.deepcopy(pane)}}
        if method in ("workspace.rename", "tab.rename"):
            kind = method.split(".", 1)[0]
            items = self.workspaces if kind == "workspace" else self.tabs
            item = next(item for item in items if item[kind + "_id"] == arguments[kind + "_id"])
            item["label"] = arguments["label"]
        elif method == "pane.rename":
            pane = next(
                pane for pane in self.panes if pane["pane_id"] == arguments["pane_id"]
            )
            pane["label"] = arguments["label"]
        elif method == "pane.report_metadata":
            pane = next(
                pane for pane in self.panes if pane["pane_id"] == arguments["pane_id"]
            )
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
        elif method == "layout.export":
            return {"layout": {"root": copy.deepcopy(self.trees[arguments["tab_id"]])}}
        elif method == "layout.set_split_ratio":
            if not 0.1 <= arguments["ratio"] <= 0.9:
                raise AssertionError("Herdr split ratios must be in 0.1..0.9")
            node = self.trees[arguments["tab_id"]]
            for side in arguments["path"]:
                node = node["second" if side else "first"]
            if node["type"] != "split":
                raise AssertionError("Resize path does not address a split")
            node["ratio"] = arguments["ratio"]
        elif method == "pane.swap":
            source = arguments["source_pane_id"]
            target = arguments["target_pane_id"]
            def swap(node):
                if node["type"] == "pane":
                    node["pane_id"] = {source: target, target: source}.get(node["pane_id"], node["pane_id"])
                else:
                    swap(node["first"])
                    swap(node["second"])
            for root in self.trees.values():
                swap(root)
            self.focused_pane_id = source
        elif method == "pane.focus":
            if not any(pane["pane_id"] == arguments["pane_id"] for pane in self.panes):
                raise AssertionError("Cannot focus a nonexistent pane")
            self.focused_pane_id = arguments["pane_id"]
            tab_id = next(
                pane["tab_id"] for pane in self.panes if pane["pane_id"] == arguments["pane_id"]
            )
            self.tab_focus[tab_id] = arguments["pane_id"]
        elif method == "pane.close":
            self.remove_pane(arguments["pane_id"])
        elif method == "tab.close":
            workspace_id = next(
                tab["workspace_id"] for tab in self.tabs if tab["tab_id"] == arguments["tab_id"]
            )
            self.remove_tab(arguments["tab_id"])
            if not any(tab["workspace_id"] == workspace_id for tab in self.tabs):
                self.workspaces = [
                    workspace for workspace in self.workspaces
                    if workspace["workspace_id"] != workspace_id
                ]
        elif method == "workspace.close":
            self.remove_space(arguments["workspace_id"])
        elif method == "notification.show":
            pass
        else:
            raise AssertionError("Unexpected API call: " + method)
        return {}


class AppHarness:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": "{}"})
        environment.start()
        self.addCleanup(environment.stop)
        self.api = FakeHerdr()
        self.app = self.make_app(self.api)

    def make_app(self, api, name="main"):
        app = App(self.root / name / "config", self.root / name / "state", api=api)
        self.addCleanup(app.store.close)
        return app

    def methods(self, method, api=None):
        return [arguments for name, arguments in (api or self.api).calls if name == method]

    def focus(self, pane_id, app=None):
        app = app or self.app
        app.api.focused_pane_id = pane_id
        app.event({"type": "pane_focused", "pane_id": pane_id})

    def seed(self, kind, tab_id="tab-1", pane_id="shell-1", app=None):
        app = app or self.app
        tab = next(tab for tab in app.api.tabs if tab["tab_id"] == tab_id)
        space = tab["workspace_id"]
        return dataclasses.asdict(app.store.create(
            app.session, kind, app.target(space, tab_id, kind, pane_id),
            space_id=app.identity(space),
            tab_id=app.target(space, tab_id, "tab") if kind == "pane" else None,
        ))

    def create(self, kind, app=None, pane_id=None):
        app = app or self.app
        if pane_id:
            self.focus(pane_id, app)
        snapshot = app.api.snapshot()
        source = app.selected_source(snapshot, snapshot["focused_tab_id"], snapshot["focused_pane_id"])
        app.action("edit-" + kind)
        return next(
            record for record in app.descriptions(snapshot["focused_workspace_id"], snapshot["focused_tab_id"])
            if record["kind"] == kind and (kind != "pane" or record["pane_id"] == source)
        )

    def section(self, record, tab_id="tab-1", app=None):
        return (app or self.app).runtime.view(tab_id)["sections"][record["description_id"]]

    def native_pane(self, record, tab_id="tab-1", app=None):
        app = app or self.app
        pane_id = self.section(record, tab_id, app)["pane_id"]
        return next(pane for pane in app.api.panes if pane["pane_id"] == pane_id)

    def poll(self, record, tab_id="tab-1", app=None, **payload):
        return (app or self.app).section_tick(tab_id, record["description_id"], payload)

    def write(self, record, content, tab_id="tab-1", app=None, return_focus=False):
        app = app or self.app
        app.store.file_path(record["description_id"]).write_text(content, encoding="utf-8")
        return self.poll(
            record, tab_id, app, saved=True, modified=False, return_focus=return_focus,
        )

    def stored(self, record, app=None):
        app = app or self.app
        return app.store.get(app.session, record["kind"], record["target_id"])

    def closed_event(self, pane_id, tab_id="tab-1", app=None, event_type="pane_exited"):
        app = app or self.app
        tab = next(tab for tab in app.api.tabs if tab["tab_id"] == tab_id)
        app.event({
            "type": event_type, "workspace_id": tab["workspace_id"],
            "tab_id": tab_id, "pane_id": pane_id,
        })

    def legacy_editor(self):
        record = self.seed("tab")
        pane = self.api.call(
            "plugin.pane.open", entrypoint="descriptions", placement="split",
            target_pane_id="shell-1", focus=False, direction="right",
        )["plugin_pane"]["pane"]
        label = "herdr-description:legacy"
        self.api.call("pane.rename", pane_id=pane["pane_id"], label=label)
        self.api.call("pane.report_metadata", pane_id=pane["pane_id"], tokens={"herdr_description": label})
        self.app.runtime.put_view({
            "workspace_id": "space-1", "tab_id": "tab-1", "pane_id": pane["pane_id"],
            "label": label, "return_pane": "shell-1", "focused_source_pane": "shell-1",
            "panes": [{"pane_id": "shell-1", "label": "shell"}], "request": None, "kind": "tab",
        })
        return record


class AppTest(AppHarness, unittest.TestCase):
    def test_edit_opens_one_native_editor_per_description_and_reuses_it(self):
        tab = self.create("tab")
        first = self.native_pane(tab)["pane_id"]
        self.app.action("edit-tab")
        self.assertEqual(self.api.focused_pane_id, first)
        space = self.create("space")
        pane = self.create("pane", pane_id="shell-1")
        self.assertEqual(len(self.app.runtime.view("tab-1")["sections"]), 3)
        self.assertEqual(
            {self.native_pane(record)["label"] for record in (tab, space, pane)},
            {"Tab description", "Space description", "> Pane 1 description · shell"},
        )
        self.assertEqual(len(self.methods("plugin.pane.open")), 3)
        self.assertEqual(self.api.focused_pane_id, self.native_pane(pane)["pane_id"])
        self.assertEqual(self.app.runtime.view("tab-1")["return_pane"], "shell-1")

    def test_editor_ownership_uses_private_unique_cwd_and_marker(self):
        records = [self.create("tab"), self.create("space")]
        sections = [self.section(record) for record in records]
        self.assertEqual(len({section["cwd"] for section in sections}), 2)
        self.assertEqual(len({section["marker"] for section in sections}), 2)
        for record, section in zip(records, sections):
            self.assertEqual(Path(section["cwd"]).stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.native_pane(record)["cwd"], section["cwd"])
            self.assertEqual(self.native_pane(record)["tokens"]["herdr_description"], section["marker"])
        for call in self.methods("plugin.pane.open"):
            self.assertEqual(call["entrypoint"], "section")
            self.assertEqual(call["placement"], "split")
            self.assertNotIn("workspace_id", call)
            self.assertFalse(call["focus"])
            section_id = call["env"]["DESCRIPTION_SECTION_ID"]
            self.assertEqual(
                Path(call["env"]["DESCRIPTION_FILE"]), self.app.store.file_path(section_id)
            )
            self.assertTrue(Path(call["env"]["DESCRIPTION_FILE"]).is_file())

    def test_native_titles_follow_source_focus_and_label_without_reopening_editors(self):
        self.api.add_pane("tab-1", "shell-2")
        first = self.create("pane", pane_id="shell-1")
        second = self.create("pane", pane_id="shell-2")
        ids = [self.native_pane(record)["pane_id"] for record in (first, second)]
        self.api.calls.clear()
        self.api.focused_pane_id = "shell-1"
        next(pane for pane in self.api.panes if pane["pane_id"] == "shell-1")["label"] = "review"
        self.app.event({"type": "pane_focused", "pane_id": "shell-1"})
        self.assertTrue(self.native_pane(first)["label"].startswith("> Pane "))
        self.assertIn("review", self.native_pane(first)["label"])
        self.assertFalse(self.native_pane(second)["label"].startswith("> "))
        self.assertEqual([self.native_pane(record)["pane_id"] for record in (first, second)], ids)
        self.assertEqual(self.methods("plugin.pane.open"), [])

    def test_column_splits_rightmost_shell_and_sections_split_down(self):
        self.api.add_pane("tab-1", "shell-right")
        original_sources = copy.deepcopy(self.api.trees["tab-1"])
        self.create("tab")
        self.create("space")
        calls = self.methods("plugin.pane.open")
        self.assertEqual(calls[0]["target_pane_id"], "shell-right")
        self.assertEqual([call["direction"] for call in calls], ["right", "down"])
        self.assertEqual(self.api.trees["tab-1"]["first"], original_sources)
        self.assertEqual(
            {pane["pane_id"] for pane in self.api.panes if pane["label"] == "shell"},
            {"shell-1", "shell-right"},
        )

    def test_sections_order_space_tab_then_sources_and_swap_restores_focus(self):
        self.api.add_pane("tab-1", "shell-2")
        second = self.create("pane", pane_id="shell-2")
        first = self.create("pane", pane_id="shell-1")
        tab = self.create("tab")
        space = self.create("space")
        desired = [space, tab, first, second]
        ids = [self.native_pane(record)["pane_id"] for record in desired]
        actual = [item["pane_id"] for item in self.api.rectangles(self.api.trees["tab-1"]) if item["pane_id"] in ids]
        self.assertEqual(actual, ids)
        self.assertTrue(self.methods("pane.swap"))
        self.assertEqual(self.api.focused_pane_id, ids[0])
        self.focus("shell-2")
        snapshot = self.api.snapshot()
        snapshot["panes"].reverse()
        self.assertEqual(
            [record["description_id"] for record in self.app.descriptions("space-1", "tab-1", snapshot)],
            [record["description_id"] for record in desired],
        )
        self.assertEqual(self.api.focused_pane_id, "shell-2")

    def test_many_sections_have_equal_height_and_native_legal_split_ratios(self):
        for index in range(2, 13):
            self.api.add_pane("tab-1", "shell-{}".format(index))
        records = [self.seed("space"), self.seed("tab")]
        records += [self.seed("pane", pane_id="shell-{}".format(index)) for index in range(1, 13)]
        self.focus("shell-1")
        ids = {self.native_pane(record)["pane_id"] for record in records}
        rectangles = [item["rect"] for item in self.api.rectangles(self.api.trees["tab-1"]) if item["pane_id"] in ids]
        self.assertEqual(len(rectangles), 14)
        for rect in rectangles:
            self.assertAlmostEqual(rect["height"], 120 / 14)
            self.assertAlmostEqual(rect["x"], rectangles[0]["x"])
            self.assertAlmostEqual(rect["width"], rectangles[0]["width"])
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_background_insertion_reorders_without_focus_theft(self):
        pane = self.create("pane")
        old_id = self.native_pane(pane)["pane_id"]
        self.seed("space")
        self.focus("shell-1")
        self.assertTrue(self.methods("pane.swap"))
        self.assertEqual(self.native_pane(pane)["pane_id"], old_id)
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_poll_never_saves_even_with_legacy_force_quit_and_autosave_fields(self):
        record = self.create("tab")
        for extra in ({}, {"force": True}, {"quit": True}, {"autosave": True}, {"write": True}):
            with self.subTest(extra=extra):
                result = self.poll(record, modified=True, content="unsaved", **extra)
                self.assertFalse(result["saved"])
                self.assertEqual(self.stored(record).content, record["content"])
                self.assertEqual(self.stored(record).revision, record["revision"])
                self.assertEqual(
                    self.app.store.file_path(record["description_id"]).read_bytes(), record["content"].encode("utf-8")
                )
        self.assertTrue(self.section(record)["modified"])

    def test_enabled_autosave_does_not_persist_unstructured_poll_content(self):
        config_dir = self.root / "old-config" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(json.dumps({"autosave_enabled": True, "autosave_interval_ms": 250}) + "\n")
        app = self.make_app(FakeHerdr(), "old-config")
        record = self.create("tab", app=app)
        self.poll(record, app=app, modified=True, content="draft", force=True)
        self.assertEqual(self.stored(record, app).content, record["content"])

    def test_vim_write_saves_only_this_record_and_keeps_focus(self):
        tab = self.create("tab")
        space = self.create("space")
        self.poll(space, modified=True, content="unsaved space")
        self.focus(self.native_pane(tab)["pane_id"])
        result = self.write(tab, "saved tab\n")
        self.assertTrue(result["saved"])
        self.assertEqual(self.stored(tab).content, "saved tab\n")
        self.assertEqual(self.stored(space).content, space["content"])
        self.assertEqual(self.api.focused_pane_id, self.native_pane(tab)["pane_id"])
        self.assertTrue(self.section(space)["modified"])

    def test_each_scope_has_independent_real_file_preserved_when_controller_reopens(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        paths = [self.app.store.file_path(record["description_id"]) for record in records]
        self.assertEqual(len(set(paths)), 3)
        for record, path in zip(records, paths):
            self.assertEqual(Path(record["file_path"]), path)
            path.write_text(record["kind"] + " notes\n", encoding="utf-8")
        reopened = self.make_app(self.api)
        for record, path in zip(records, paths):
            self.assertEqual(reopened.store.file_path(record["description_id"]), path)
            self.assertEqual(self.stored(record, reopened).content, record["kind"] + " notes\n")

    def test_save_acknowledgment_cannot_persist_payload_content(self):
        record = self.create("tab")
        path = self.app.store.file_path(record["description_id"])
        path.write_text("native Vim wrote this\n", encoding="utf-8")
        self.app.action("save")
        response = self.poll(
            record, saved=True, modified=False, content="never use RPC content", return_focus=True
        )
        self.assertTrue(response["saved"])
        self.assertFalse(response["write_requested"])
        self.assertEqual(path.read_text(encoding="utf-8"), "native Vim wrote this\n")
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_save_queues_only_selected_section_then_returns_to_latest_source(self):
        self.api.add_pane("tab-1", "shell-2")
        tab = self.create("tab")
        space = self.create("space")
        self.focus("shell-2")
        self.focus(self.native_pane(tab)["pane_id"])
        self.app.action("save")
        self.assertTrue(self.poll(tab)["write_requested"])
        self.assertFalse(self.poll(space)["write_requested"])
        self.assertEqual(self.api.focused_pane_id, self.native_pane(tab)["pane_id"])
        self.assertFalse(self.write(tab, "manual", return_focus=True)["write_requested"])
        self.assertEqual(self.api.focused_pane_id, "shell-2")

    def test_save_requires_focused_description(self):
        self.create("tab")
        self.focus("shell-1")
        with self.assertRaises(HerdrError):
            self.app.action("save")

    def test_focus_failure_after_write_reports_saved_record_and_clears_request(self):
        record = self.create("tab")
        self.app.action("save")
        original_call = self.api.call

        def fail_focus(method, **arguments):
            if method == "pane.focus":
                raise HerdrError("Focus RPC unavailable")
            return original_call(method, **arguments)

        with patch.object(self.api, "call", side_effect=fail_focus):
            response = self.write(record, "committed", return_focus=True)
        self.assertTrue(response["saved"])
        self.assertIn("warning", response)
        self.assertEqual(self.stored(record).content, "committed")
        self.assertFalse(self.poll(record)["write_requested"])

    def test_unchanged_poll_does_not_rewrite_runtime_state(self):
        record = self.create("tab")
        self.poll(record, modified=True)
        with patch.object(self.app.runtime, "put_view", wraps=self.app.runtime.put_view) as write:
            self.poll(record, modified=True)
            self.poll(record, modified=True)
        write.assert_not_called()

    def test_unacknowledged_save_retains_request_file_and_editor_focus(self):
        record = self.create("tab")
        self.write(record, "first writer")
        self.app.action("save")
        response = self.poll(record, modified=True, content="unsaved draft", return_focus=True)
        self.assertFalse(response["saved"])
        self.assertEqual(self.stored(record).content, "first writer")
        self.assertEqual(self.api.focused_pane_id, self.native_pane(record)["pane_id"])
        self.assertTrue(self.poll(record, modified=True)["write_requested"])

    def test_rpc_cannot_write_content_or_choose_another_description_file(self):
        record = self.create("tab")
        other = self.create("space")
        self.poll(
            record, write=True, modified=True, description_id=other["description_id"],
            revision=record["revision"], content="wrong",
        )
        self.assertEqual(self.stored(record).content, record["content"])
        self.assertEqual(self.stored(other).content, other["content"])

    def test_shared_space_editors_open_same_real_file_and_poll_external_changes(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space = self.create("space")
        self.focus("shell-2")
        self.assertNotEqual(self.native_pane(space)["pane_id"], self.native_pane(space, "tab-2")["pane_id"])
        calls = self.methods("plugin.pane.open")
        self.assertEqual(calls[0]["env"]["DESCRIPTION_FILE"], calls[1]["env"]["DESCRIPTION_FILE"])
        self.write(space, "first tab")
        self.assertEqual(self.poll(space, "tab-2")["record"]["content"], "first tab")
        path = self.app.store.file_path(space["description_id"])
        path.write_text("changed in external editor\n", encoding="utf-8")
        for tab_id in ("tab-1", "tab-2"):
            self.assertEqual(
                self.poll(space, tab_id, modified=True, content="local unsaved draft")["record"]["content"],
                "changed in external editor\n",
            )
        self.assertEqual(path.read_text(encoding="utf-8"), "changed in external editor\n")

    def test_remove_discards_only_selected_scope_and_never_saves_other_buffers(self):
        space = self.create("space")
        tab = self.create("tab")
        pane = self.create("pane", pane_id="shell-1")
        tab_pane_id = self.native_pane(tab)["pane_id"]
        self.poll(space, modified=True, content="space draft")
        self.poll(pane, modified=True, content="pane draft")
        self.app.action("remove-tab")
        self.assertIsNone(self.stored(tab))
        self.assertEqual(self.poll(tab, modified=True, write=True, content="deleted"), {"closed": True})
        self.assertIn({"pane_id": tab_pane_id}, self.methods("pane.close"))
        self.closed_event(tab_pane_id)
        self.assertEqual(self.stored(space).content, space["content"])
        self.assertEqual(self.stored(pane).content, pane["content"])
        self.assertTrue(self.section(space)["modified"])
        self.assertTrue(self.section(pane)["modified"])
        self.assertEqual(len(self.app.runtime.view("tab-1")["sections"]), 2)

    def test_focus_highlight_follows_source_and_survives_editor_focus(self):
        self.api.add_pane("tab-1", "shell-2")
        first = self.create("pane", pane_id="shell-1")
        second = self.create("pane", pane_id="shell-2")
        self.focus("shell-1")
        self.assertTrue(self.poll(first)["record"]["focused"])
        self.assertFalse(self.poll(second)["record"]["focused"])
        self.focus(self.native_pane(second)["pane_id"])
        self.assertTrue(self.poll(first, modified=True)["record"]["focused"])
        self.app.action("edit-pane")
        self.assertEqual(self.api.focused_pane_id, self.native_pane(first)["pane_id"])

    def test_sessions_with_identical_host_ids_are_isolated(self):
        first = self.create("tab")
        other = App(self.app.config_dir, self.root / "main" / "state", api=FakeHerdr("/tmp/other.sock"))
        self.addCleanup(other.store.close)
        second = self.create("tab", app=other)
        self.write(first, "first session")
        self.assertNotEqual(first["description_id"], second["description_id"])
        self.assertEqual(self.stored(second, other).content, second["content"])

    def test_tab_close_cascades_panes_and_preserves_space_and_other_tab(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space = self.create("space")
        tab = self.create("tab")
        pane = self.create("pane", pane_id="shell-1")
        other = self.seed("tab", tab_id="tab-2")
        self.api.remove_tab("tab-1")
        self.app.event({"type": "tab_closed", "workspace_id": "space-1", "tab_id": "tab-1"})
        self.assertIsNone(self.stored(tab))
        self.assertIsNone(self.stored(pane))
        self.assertIsNotNone(self.stored(space))
        self.assertIsNotNone(self.stored(other))
        self.assertIsNone(self.app.runtime.view("tab-1"))

    def test_space_close_cascades_only_that_space(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        other = self.seed("pane", tab_id="tab-2", pane_id="shell-2")
        self.api.remove_space("space-1")
        self.app.event({"type": "workspace_closed", "workspace_id": "space-1"})
        for record in records:
            self.assertIsNone(self.stored(record))
        self.assertIsNotNone(self.stored(other))
        self.assertIsNone(self.app.runtime.identity("space-1"))

    def test_pane_move_retains_old_dirty_editor_and_saved_record_until_clean(self):
        self.api.add_pane("tab-1", "remains")
        self.api.add_tab("space-1", "tab-2", "shell-2")
        record = self.create("pane", pane_id="shell-1")
        old_id = self.native_pane(record)["pane_id"]
        path = self.app.store.file_path(record["description_id"])
        self.write(record, "before move")
        self.api.move_pane("shell-1", "tab-2")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "pane": {"workspace_id": "space-1", "tab_id": "tab-2", "pane_id": "shell-1"},
        })
        self.assertFalse(self.poll(record, modified=True, write=True, content="old dirty editor")["saved"])
        self.assertEqual(self.stored(record).content, "before move")
        self.assertEqual(self.app.store.file_path(record["description_id"]), path)
        self.assertEqual(path.read_text(encoding="utf-8"), "before move")
        self.assertNotIn({"pane_id": old_id}, self.methods("pane.close"))
        self.assertTrue(self.poll(record)["closed"])
        self.assertEqual(self.poll(record, "tab-2")["record"]["description_id"], record["description_id"])

    def test_move_survives_source_space_closure_and_new_pane_id(self):
        self.api.add_tab("space-2", "tab-2", "shell-2")
        old_tab = self.create("tab")
        old_space = self.create("space")
        record = self.create("pane", pane_id="shell-1")
        self.write(record, "portable notes")
        path = self.app.store.file_path(record["description_id"])
        self.api.move_pane("shell-1", "tab-2", "moved-shell")
        self.api.remove_space("space-1")
        self.app.event({
            "type": "pane_moved", "previous_pane_id": "shell-1",
            "previous_workspace_id": "space-1", "previous_tab_id": "tab-1",
            "closed_workspace_id": "space-1", "closed_tab_id": "tab-1",
            "pane": {"workspace_id": "space-2", "tab_id": "tab-2", "pane_id": "moved-shell"},
        })
        moved = self.app.descriptions("space-2", "tab-2")
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["description_id"], record["description_id"])
        self.assertEqual(moved[0]["content"], "portable notes")
        self.assertEqual(Path(moved[0]["file_path"]), path)
        self.assertEqual(path.read_text(encoding="utf-8"), "portable notes")
        for original in (old_tab, old_space, record):
            self.assertIsNone(self.stored(original))

    def test_restore_preserves_live_editors_unsaved_state_and_store(self):
        record = self.create("tab")
        self.poll(record, modified=True)
        before = self.app.runtime.view("tab-1")
        stored = self.app.store.export_descriptions()
        self.api.calls.clear()
        self.app.restore(self.api.snapshot())
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.app.runtime.view("tab-1"), before)
        self.assertEqual(self.app.store.export_descriptions(), stored)

    def test_restore_replaces_proven_shell_preserving_identity_and_content(self):
        record = self.create("tab")
        self.write(record, "survives restart")
        old = self.native_pane(record)
        old_id = old["pane_id"]
        old.pop("tokens")
        records = self.app.store.export_descriptions()
        identity = self.app.runtime.identity("space-1")
        self.api.calls.clear()
        self.app.event({}, startup=True)
        self.assertEqual(self.methods("pane.close"), [{"pane_id": old_id}])
        self.assertNotEqual(self.native_pane(record)["pane_id"], old_id)
        self.assertEqual(self.app.runtime.identity("space-1"), identity)
        self.assertEqual(self.app.store.export_descriptions(), records)

    def test_manual_restore_after_plugin_disable_and_pane_closure_preserves_notes(self):
        record = self.create("tab")
        self.write(record, "restart safely")
        old_id = self.native_pane(record)["pane_id"]
        path = self.app.store.file_path(record["description_id"])
        identity = self.app.runtime.identity("space-1")
        records = self.app.store.export_descriptions()
        self.api.remove_pane(old_id)
        self.app.event({}, startup=True, verify_identities=False)
        self.assertEqual(self.app.runtime.identity("space-1"), identity)
        self.assertEqual(self.app.store.export_descriptions(), records)
        self.assertNotEqual(self.native_pane(record)["pane_id"], old_id)
        self.assertEqual(self.app.store.file_path(record["description_id"]), path)
        self.assertEqual(path.read_text(encoding="utf-8"), "restart safely")

    def test_restore_requires_matching_pane_tab_and_private_cwd_without_live_token(self):
        for field, value in (("cwd", "/tmp"), ("tab_id", "other-tab"), ("pane_id", "other-pane")):
            with self.subTest(field=field):
                api = FakeHerdr()
                app = self.make_app(api, field)
                record = self.create("tab", app=app)
                pane = self.native_pane(record, app=app)
                pane.pop("tokens")
                pane[field] = value
                records = app.store.export_descriptions()
                api.calls.clear()
                app.restore(api.snapshot())
                self.assertEqual(self.methods("pane.close", api), [])
                self.assertIn(pane, api.panes)
                self.assertEqual(app.store.export_descriptions(), records)
                self.assertIsNone(app.runtime.identity("space-1"))

    def test_restore_preserves_association_when_display_title_has_changed(self):
        record = self.create("tab")
        pane = self.native_pane(record)
        old_id = pane["pane_id"]
        pane.pop("tokens")
        pane["label"] = "Old description title"
        identity = self.app.runtime.identity("space-1")
        records = self.app.store.export_descriptions()
        self.app.event({}, startup=True)
        self.assertEqual(self.app.runtime.identity("space-1"), identity)
        self.assertEqual(self.app.store.export_descriptions(), records)
        self.assertNotEqual(self.native_pane(record)["pane_id"], old_id)
        self.assertEqual(self.native_pane(record)["label"], "Tab description")

    def test_restore_keeps_last_native_pane_in_tab(self):
        record = self.create("tab")
        pane = self.native_pane(record)
        pane.pop("tokens")
        self.api.remove_pane("shell-1")
        self.api.calls.clear()
        self.app.restore(self.api.snapshot())
        self.assertEqual(self.methods("pane.close"), [])
        self.assertEqual(self.api.panes, [pane])

    def test_legacy_live_editor_only_explicit_save_can_persist(self):
        record = self.legacy_editor()
        self.app.restore(self.api.snapshot())
        self.assertTrue(self.methods("notification.show"))
        self.assertEqual(self.methods("pane.close"), [])
        change = {**record, "content": "legacy unsaved"}
        for extra in ({}, {"force": True}, {"quit": True}):
            response = self.app.tick("tab-1", {"changes": [change], **extra})
            self.assertEqual(response["saved"], [])
            self.assertEqual(self.stored(record).content, "")
        self.app.action("save")
        self.assertEqual(len(self.app.tick("tab-1", {"changes": [change]})["saved"]), 1)
        self.assertEqual(self.stored(record).content, "legacy unsaved")

    def test_legacy_restored_shell_migrates_without_changing_saved_records(self):
        record = self.legacy_editor()
        view = self.app.runtime.view("tab-1")
        next(pane for pane in self.api.panes if pane["pane_id"] == view["pane_id"]).pop("tokens")
        records = self.app.store.export_descriptions()
        self.app.event({}, startup=True)
        self.assertEqual(self.app.runtime.view("tab-1")["format"], 2)
        self.assertEqual(self.native_pane(record)["label"], "Tab description")
        self.assertEqual(self.app.store.export_descriptions(), records)

    def test_replaced_editor_cannot_update_metadata_or_close_replacement(self):
        record = self.create("tab")
        pane_id = self.native_pane(record)["pane_id"]
        with patch.dict(os.environ, {"DESCRIPTION_VIEW_LABEL": "obsolete-marker"}):
            self.assertIn("error", self.poll(record, modified=True, write=True, content="obsolete editor"))
            self.assertEqual(self.poll(record), {"closed": True})
        self.app.close_editor("tab-1", record["description_id"], "obsolete-marker")
        self.assertNotIn({"pane_id": pane_id}, self.methods("pane.close"))
        self.assertEqual(self.stored(record).content, record["content"])

    def test_supervisor_closes_only_owned_pane_and_preserves_saved_description(self):
        record = self.create("tab")
        section = self.section(record)
        self.write(record, "persisted")
        self.app.close_editor("tab-1", record["description_id"], section["marker"])
        self.assertIn({"pane_id": section["pane_id"]}, self.methods("pane.close"))
        self.assertEqual(self.stored(record).content, "persisted")
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_cancel_close_warns_about_pane_descriptions_and_preserves_them(self):
        record = self.create("pane")
        output = io.StringIO()
        with patch.dict(os.environ, {
            "DESCRIPTION_WORKSPACE_ID": "space-1", "DESCRIPTION_TAB_ID": "tab-1", "DESCRIPTION_CLOSE_KIND": "tab",
        }), patch("builtins.input", return_value=""), patch("sys.stdout", output):
            self.app.confirm_close()
        self.assertIn("descriptions in this space will be deleted", output.getvalue())
        self.assertIsNotNone(self.stored(record))
        self.assertEqual(self.methods("tab.close"), [])


class SimpleEditorAppTest(AppHarness, unittest.TestCase):
    def editor_poll(self, record, tab_id="tab-1", **payload):
        return self.poll(record, tab_id, editor="simple", **payload)

    def save(self, record, content, request=None, base=None, tab_id="tab-1", force=False):
        base = record["content"] if base is None else base
        return self.editor_poll(record, tab_id, modified=True, save={
            "content": content,
            "expected_digest": hashlib.sha256(base.encode("utf-8")).hexdigest(),
            "request_id": request["id"] if request else None,
            "force": force,
        })

    def confirm(self, kind, answer="yes", pane_id="shell-1", tab_id="tab-1", workspace_id="space-1"):
        output = io.StringIO()
        with patch.dict(os.environ, {
            "DESCRIPTION_WORKSPACE_ID": workspace_id,
            "DESCRIPTION_TAB_ID": tab_id,
            "DESCRIPTION_PANE_ID": pane_id,
            "DESCRIPTION_CLOSE_KIND": kind,
        }), patch("builtins.input", return_value=answer), patch("sys.stdout", output):
            self.app.confirm_close()
        return output.getvalue()

    def assert_deleted(self, records):
        for record in records:
            self.assertIsNone(self.stored(record))
            self.assertFalse(Path(record["file_path"]).exists())
        deleted = {record["description_id"] for record in records}
        self.assertFalse(deleted & {
            record.description_id for record in self.app.store.export_descriptions()
        })

    def test_dirty_poll_without_save_payload_does_not_write(self):
        record = self.create("tab")
        for _ in range(3):
            response = self.editor_poll(record, modified=True, content="in progress")
            self.assertTrue(response["editor_focused"])
            self.assertFalse(response["saved"])
            self.assertFalse(response["write_requested"])
            self.assertEqual(self.stored(record).content, record["content"])
        self.assertEqual(self.methods("notification.show"), [])

    def test_blur_to_source_other_tab_space_and_description_saves_without_focus_theft(self):
        for destination in ("shell-1", "shell-2", "shell-3", "other-description"):
            with self.subTest(destination=destination):
                api = FakeHerdr()
                app = self.make_app(api, destination)
                api.add_tab("space-1", "tab-2", "shell-2")
                api.add_tab("space-2", "tab-3", "shell-3")
                other = self.create("space", app=app)
                record = self.create("tab", app=app)
                destination_id = self.native_pane(other, app=app)["pane_id"] if destination == "other-description" else destination
                self.poll(record, app=app, editor="simple", modified=True)
                self.focus(destination_id, app)
                for event in ("pane_focused", "tab_focused", "workspace_focused"):
                    app.event({"type": event})
                response = self.poll(record, app=app, editor="simple", modified=True)
                self.assertFalse(response["editor_focused"])
                self.assertTrue(response["write_requested"])
                self.assertEqual(response["save_request"]["reason"], "blur")
                request = response["save_request"]
                self.assertEqual(
                    self.poll(record, app=app, editor="simple", modified=True)["save_request"]["id"],
                    request["id"],
                )
                self.assertEqual(self.stored(record, app).content, record["content"])
                response = self.poll(record, app=app, editor="simple", modified=True, save={
                    "content": "draft " + destination,
                    "expected_digest": hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
                    "request_id": request["id"], "force": False,
                })
                self.assertTrue(response["saved"])
                self.assertFalse(response["write_requested"])
                self.assertEqual(self.stored(record, app).content, "draft " + destination)
                self.assertEqual(api.focused_pane_id, destination_id)
                self.assertEqual(self.methods("notification.show", api)[-1]["title"], "Tab description saved")
                self.assertEqual(len(self.methods("notification.show", api)), 1)

    def test_dirty_poll_catches_blur_when_focus_hook_is_missing(self):
        record = self.create("pane")
        self.editor_poll(record, modified=True)
        self.api.focused_pane_id = "shell-1"
        response = self.editor_poll(record, modified=True)
        self.assertFalse(response["editor_focused"])
        self.assertEqual(response["save_request"]["reason"], "blur")
        self.assertTrue(self.save(record, "recovered edit", response["save_request"])["saved"])
        self.assertEqual(self.stored(record).content, "recovered edit")
        self.assertEqual(self.api.focused_pane_id, "shell-1")
        self.assertEqual(self.methods("notification.show")[-1]["title"], "Pane description saved")

    def test_clean_blur_acknowledges_without_save_or_duplicate_notification(self):
        record = self.create("space")
        self.editor_poll(record, modified=True)
        self.focus("shell-1")
        request = self.editor_poll(record, modified=True)["save_request"]
        self.save(record, "saved", request)
        self.focus(self.native_pane(record)["pane_id"])
        self.editor_poll(record, modified=False)
        self.focus("shell-1")
        response = self.editor_poll(record, modified=False)
        if response["write_requested"]:
            response = self.editor_poll(record, modified=False, ack_request=response["save_request"]["id"])
        self.assertFalse(response["write_requested"])
        self.assertFalse(response["saved"])
        self.assertEqual(self.stored(record).content, "saved")
        self.assertEqual([item["title"] for item in self.methods("notification.show")], ["Space description saved"])

    def test_manual_save_returns_to_last_source_but_blur_save_does_not(self):
        self.api.add_pane("tab-1", "shell-2")
        record = self.create("tab")
        self.focus("shell-2")
        self.focus(self.native_pane(record)["pane_id"])
        self.editor_poll(record, modified=True)
        self.app.action("save")
        response = self.editor_poll(record, modified=True)
        self.assertEqual(response["save_request"]["reason"], "manual")
        self.assertTrue(response["save_request"]["return_focus"])
        response = self.save(record, "manual save", response["save_request"])
        self.assertTrue(response["saved"])
        self.assertEqual(self.api.focused_pane_id, "shell-2")
        self.assertFalse(response["write_requested"])

    def test_save_conflict_keeps_external_file_pending_request_and_editor_dirty(self):
        record = self.create("space")
        self.editor_poll(record, modified=True)
        self.focus("shell-1")
        request = self.editor_poll(record, modified=True)["save_request"]
        path = Path(record["file_path"])
        path.write_text("another editor", encoding="utf-8")
        response = self.save(record, "my local edits", request)
        self.assertTrue(response["conflict"])
        self.assertFalse(response.get("saved"))
        self.assertEqual(path.read_text(encoding="utf-8"), "another editor")
        self.assertEqual(self.section(record)["request"]["id"], request["id"])
        self.assertTrue(self.section(record)["modified"])
        self.assertFalse(any(item["title"].endswith("description saved") for item in self.methods("notification.show")))
        self.assertEqual(self.api.focused_pane_id, "shell-1")

    def test_failed_file_write_never_shows_success_notification_or_clears_request(self):
        record = self.create("tab")
        self.editor_poll(record, modified=True)
        self.focus("shell-1")
        request = self.editor_poll(record, modified=True)["save_request"]
        with patch.object(self.app.store, "save_file", side_effect=OSError("disk unavailable")):
            response = self.save(record, "not committed", request)
        self.assertIn("error", response)
        self.assertFalse(response.get("saved"))
        self.assertEqual(self.stored(record).content, record["content"])
        self.assertEqual(self.section(record)["request"]["id"], request["id"])
        self.assertFalse(any(item["title"].endswith("description saved") for item in self.methods("notification.show")))

    def test_save_content_is_bound_to_section_identity(self):
        record = self.create("tab")
        other = self.create("space")
        response = self.editor_poll(record, modified=True, description_id=other["description_id"], save={
            "content": "tab only", "description_id": other["description_id"],
            "expected_digest": hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
            "request_id": None, "force": False,
        })
        self.assertTrue(response["saved"])
        self.assertEqual(self.stored(record).content, "tab only")
        self.assertEqual(self.stored(other).content, other["content"])

    def test_remove_description_deletes_file_mapping_and_only_its_editor(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        removed_pane_id = self.native_pane(records[1])["pane_id"]
        self.app.action("remove-tab")
        self.assert_deleted([records[1]])
        self.assertEqual(self.save(records[1], "late editor contents"), {"closed": True})
        self.assert_deleted([records[1]])
        for record in (records[0], records[2]):
            self.assertIsNotNone(self.stored(record))
            self.assertTrue(Path(record["file_path"]).exists())
        self.assertNotIn(removed_pane_id, {pane["pane_id"] for pane in self.api.panes})
        self.assertIn("shell-1", {pane["pane_id"] for pane in self.api.panes})

    def test_guarded_pane_close_targets_source_from_its_editor(self):
        self.api.add_pane("tab-1", "remains")
        record = self.create("pane")
        self.app.action("close-pane")
        call = self.methods("plugin.pane.open")[-1]
        self.assertEqual(call["entrypoint"], "confirm-close")
        self.assertEqual(call["env"]["DESCRIPTION_CLOSE_KIND"], "pane")
        self.assertEqual(call["env"]["DESCRIPTION_PANE_ID"], "shell-1")
        self.assertIsNotNone(self.stored(record))
        self.confirm("pane")
        self.assert_deleted([record])
        self.assertEqual(self.editor_poll(record, modified=True), {"closed": True})
        self.assertEqual([pane["pane_id"] for pane in self.api.panes], ["remains"])

    def test_confirmed_pane_close_preserves_tab_space_and_sibling_pane_descriptions(self):
        self.api.add_pane("tab-1", "remains")
        space = self.create("space")
        tab = self.create("tab")
        removed = self.create("pane", pane_id="shell-1")
        other = self.create("pane", pane_id="remains")
        self.confirm("pane", pane_id="shell-1")
        self.assert_deleted([removed])
        for record in (space, tab, other):
            self.assertIsNotNone(self.stored(record))
            self.assertTrue(Path(record["file_path"]).exists())
        self.assertIn("remains", {pane["pane_id"] for pane in self.api.panes})
        self.assertNotIn("shell-1", {pane["pane_id"] for pane in self.api.panes})

    def test_cancel_space_close_warns_all_tabs_and_panes_without_deleting(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        records += [self.seed("tab", "tab-2"), self.seed("pane", "tab-2", "shell-2")]
        panes = copy.deepcopy(self.api.panes)
        before = self.app.store.export_descriptions()
        output = self.confirm("space", answer="")
        self.assertIn("all", output.lower())
        self.assertIn("tabs", output.lower())
        self.assertIn("all pane descriptions", output.lower())
        self.assertIn("deleted", output.lower())
        self.assertEqual(self.app.store.export_descriptions(), before)
        self.assertEqual(self.api.panes, panes)
        self.assertEqual(self.methods("workspace.close"), [])
        for record in records:
            self.assertTrue(Path(record["file_path"]).exists())

    def test_cancel_pane_close_keeps_every_description_and_native_pane(self):
        self.api.add_pane("tab-1", "remains")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        panes = copy.deepcopy(self.api.panes)
        output = self.confirm("pane", answer="")
        self.assertIn("pane", output.lower())
        self.assertIn("deleted", output.lower())
        self.assertEqual(self.api.panes, panes)
        for record in records:
            self.assertIsNotNone(self.stored(record))
            self.assertTrue(Path(record["file_path"]).exists())

    def test_confirmed_space_close_synchronously_removes_all_files_and_mappings(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        self.api.add_tab("space-2", "tab-3", "shell-3")
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        records += [self.seed("tab", "tab-2"), self.seed("pane", "tab-2", "shell-2")]
        other = self.seed("pane", "tab-3", "shell-3")
        self.confirm("space")
        self.assert_deleted(records)
        self.assertIsNotNone(self.stored(other))
        self.assertTrue(Path(other["file_path"]).exists())
        self.app.event({"type": "workspace_closed", "workspace_id": "space-1"})
        self.app.event({"type": "tab_closed", "workspace_id": "space-1", "tab_id": "tab-1"})
        self.assert_deleted(records)
        self.assertIsNotNone(self.stored(other))

    def test_confirmed_tab_close_removes_inner_panes_and_preserves_space_and_sibling_tab(self):
        self.api.add_tab("space-1", "tab-2", "shell-2")
        space = self.create("space")
        records = [self.create("tab"), self.create("pane", pane_id="shell-1")]
        other = self.seed("pane", "tab-2", "shell-2")
        output = self.confirm("tab")
        self.assertIn("pane", output.lower())
        self.assert_deleted(records)
        for record in (space, other):
            self.assertIsNotNone(self.stored(record))
            self.assertTrue(Path(record["file_path"]).exists())
        self.assertIn({"workspace_id": "space-1"}, self.api.workspaces)

    def test_last_tab_close_warns_about_space_and_deletes_its_description(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        output = self.confirm("tab")
        self.assertIn("space", output.lower())
        self.assert_deleted(records)
        self.assertEqual(self.api.workspaces, [])

    def test_failed_native_close_does_not_delete_files_mappings_or_work_panes(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        panes = copy.deepcopy(self.api.panes)
        before = self.app.store.export_descriptions()
        original_call = self.api.call

        def fail_close(method, **arguments):
            if method == "workspace.close":
                raise HerdrError("Cannot close workspace")
            return original_call(method, **arguments)

        with patch.object(self.api, "call", side_effect=fail_close), self.assertRaises(HerdrError):
            self.confirm("space")
        self.assertEqual(self.app.store.export_descriptions(), before)
        self.assertEqual(self.api.panes, panes)
        for record in records:
            self.assertTrue(Path(record["file_path"]).exists())

    def test_native_close_response_without_actual_removal_preserves_descriptions(self):
        records = [self.create("space"), self.create("tab"), self.create("pane", pane_id="shell-1")]
        panes = copy.deepcopy(self.api.panes)
        original_call = self.api.call

        def unchanged_close(method, **arguments):
            if method == "workspace.close":
                return {}
            return original_call(method, **arguments)

        with patch.object(self.api, "call", side_effect=unchanged_close), self.assertRaises(HerdrError):
            self.confirm("space")
        self.assertEqual(self.api.panes, panes)
        for record in records:
            self.assertIsNotNone(self.stored(record))
            self.assertTrue(Path(record["file_path"]).exists())


if __name__ == "__main__":
    unittest.main()
