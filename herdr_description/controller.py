import dataclasses
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .api import Herdr, HerdrError
from .config import load_config
from .documents import DocumentConflictError, DocumentStore
from .layout import ColumnLayout
from .runtime import Runtime
from .storage import DescriptionStoreError


PLUGIN_ID = "herdr-description"


def context(snapshot):
    supplied = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    return (
        supplied.get("workspace_id") or snapshot.get("focused_workspace_id"),
        supplied.get("tab_id") or snapshot.get("focused_tab_id"),
        supplied.get("focused_pane_id") or snapshot.get("focused_pane_id"),
    )


class App:
    def __init__(self, config_dir, state_dir, api=None):
        self.config_dir = Path(config_dir)
        self.config = load_config(self.config_dir)
        self.api = api or Herdr()
        self.runtime = Runtime(state_dir, self.api.socket_path)
        self.store = DocumentStore(state_dir)
        self.session = self.runtime.session_id

    def identity(self, workspace_id):
        identity = self.runtime.identity(workspace_id)
        if identity is None:
            identity = {"id": str(uuid.uuid4())}
            self.runtime.put_identity(workspace_id, identity)
        return identity["id"]

    def target(self, workspace_id, tab_id, kind, pane_id=None):
        space = self.identity(workspace_id)
        if kind == "space":
            return space
        if kind == "tab":
            return space + "/" + tab_id
        if kind == "pane" and pane_id:
            return space + "/" + pane_id
        raise ValueError("A pane description requires a source pane.")

    def owned_ids(self, snapshot=None):
        ids = set()
        for view in self.runtime.views():
            if snapshot is not None:
                if view.get("format") == 2:
                    panes = [
                        self.section_pane(snapshot, view, section)
                        for section in view["sections"].values()
                    ]
                else:
                    panes = [self.legacy_pane(snapshot, view)]
                ids.update(pane["pane_id"] for pane in panes if pane)
                continue
            if view.get("format") == 2:
                ids.update(section.get("pane_id") for section in view["sections"].values())
            else:
                ids.add(view.get("pane_id"))
        return ids

    def source_panes(self, snapshot, tab_id):
        owned = self.owned_ids(snapshot)
        panes = [
            pane for pane in snapshot["panes"]
            if pane["tab_id"] == tab_id and pane["pane_id"] not in owned
        ]
        layout = next(
            (item for item in snapshot.get("layouts", []) if item["tab_id"] == tab_id), {}
        )
        positions = {
            pane["pane_id"]: (pane["rect"].get("y", 0), pane["rect"]["x"])
            for pane in layout.get("panes", [])
        }
        panes.sort(key=lambda pane: positions.get(pane["pane_id"], (0, 0)))
        return [
            {
                "pane_id": pane["pane_id"],
                "label": pane.get("label") or pane.get("title") or "",
                **({"terminal_id": pane["terminal_id"]} if pane.get("terminal_id") else {}),
            }
            for pane in panes
        ]

    def refresh_views(self, snapshot):
        for view in self.runtime.views():
            panes = self.source_panes(snapshot, view["tab_id"])
            ids = {pane["pane_id"] for pane in panes}
            focused = snapshot.get("focused_pane_id")
            if focused not in ids:
                focused = view.get("focused_source_pane")
            if focused not in ids:
                focused = view.get("return_pane") if view.get("return_pane") in ids else None
            changed = view.get("panes") != panes or view.get("focused_source_pane") != focused
            for section in view.get("sections", {}).values():
                editor_focused = snapshot.get("focused_pane_id") == section.get("pane_id")
                if section.get("editor") == "simple" and section.get("editor_focused") and not editor_focused:
                    self.request_save(section, "blur")
                    changed = True
                if section.get("editor_focused") != editor_focused:
                    section["editor_focused"] = editor_focused
                    changed = True
            if changed:
                if view.get("panes") and not panes:
                    view["last_source_panes"] = view["panes"]
                elif panes:
                    view.pop("last_source_panes", None)
                view.update(panes=panes, focused_source_pane=focused)
                self.runtime.put_view(view)

    def request_save(self, section, reason):
        section["request"] = {
            "id": time.time_ns(), "action": "save", "reason": reason,
            "return_focus": reason == "manual",
        }

    def descriptions(self, workspace_id, tab_id, snapshot=None):
        identity = self.runtime.identity(workspace_id)
        if identity is None:
            return []
        space = identity["id"]
        result = []
        view = self.runtime.view(tab_id) or {}
        for kind, target in (("space", space), ("tab", space + "/" + tab_id)):
            record = self.store.get(self.session, kind, target)
            if record:
                result.append({
                    **dataclasses.asdict(record),
                    "title": kind.capitalize() + " description", "focused": False,
                })
        panes = self.source_panes(snapshot, tab_id) if snapshot else view.get("panes", [])
        focused = view.get("focused_source_pane")
        if snapshot and snapshot.get("focused_pane_id") in {pane["pane_id"] for pane in panes}:
            focused = snapshot["focused_pane_id"]
        for index, pane in enumerate(panes, 1):
            record = self.store.get(self.session, "pane", space + "/" + pane["pane_id"])
            if record and record.tab_id == space + "/" + tab_id:
                label = "".join(char if char.isprintable() else " " for char in pane["label"])
                result.append({
                    **dataclasses.asdict(record),
                    "pane_id": pane["pane_id"],
                    "title": "Pane {} description{}".format(index, " · " + label if label else ""),
                    "focused": pane["pane_id"] == focused,
                })
        return result

    def selected_source(self, snapshot, tab_id, pane_id):
        ids = {pane["pane_id"] for pane in self.source_panes(snapshot, tab_id)}
        if pane_id in ids:
            return pane_id
        view = self.runtime.view(tab_id) or {}
        for candidate in (view.get("focused_source_pane"), view.get("return_pane")):
            if candidate in ids:
                return candidate
        raise HerdrError("Focus a terminal pane before editing its description.")

    def legacy_pane(self, snapshot, view):
        if not view or view.get("format") == 2:
            return None
        return next((
            pane for pane in snapshot["panes"]
            if pane["pane_id"] == view.get("pane_id")
            and pane["tab_id"] == view["tab_id"] and pane.get("label") == view["label"]
        ), None)

    def section_pane(self, snapshot, view, section):
        for pane in snapshot["panes"]:
            if pane["pane_id"] != section.get("pane_id") or pane["tab_id"] != view["tab_id"]:
                continue
            token = pane.get("tokens", {}).get("herdr_description")
            if token == section["marker"]:
                return pane
            cwd = pane.get("cwd")
            if (
                cwd and Path(cwd).resolve() == Path(section["cwd"]).resolve()
            ):
                return pane
        return None

    def restore_focus(self, view, only_if_description_focused=False):
        snapshot = self.api.snapshot()
        if only_if_description_focused and snapshot.get("focused_pane_id") not in self.owned_ids():
            return
        sources = self.source_panes(snapshot, view["tab_id"])
        ids = {pane["pane_id"] for pane in sources}
        target = view.get("return_pane")
        if target not in ids:
            target = sources[0]["pane_id"] if sources else None
        if target:
            self.api.call("pane.focus", pane_id=target)

    def notify(self, message, title=None):
        try:
            self.api.call("notification.show", title=title or PLUGIN_ID, body=message, sound="none")
        except (HerdrError, OSError):
            print(message, file=sys.stderr)

    def tree_leaves(self, node, path=()):
        if node["type"] == "pane":
            return [(node["pane_id"], list(path))]
        return self.tree_leaves(node["first"], path + (False,)) + self.tree_leaves(
            node["second"], path + (True,)
        )

    def section_title(self, record):
        return ("> " if record.get("focused") else "") + record["title"]

    def arrange(self, view, wanted, original_focus):
        snapshot = self.api.snapshot()
        if not self.source_panes(snapshot, view["tab_id"]):
            return
        description_ids = self.layout_description_ids(view, snapshot)
        ColumnLayout(self.api, self.runtime, self.config.panel_width_percent).ensure(
            view, description_ids,
        )
        root = self.api.call("layout.export", tab_id=view["tab_id"])["layout"]["root"]
        desired = [
            view["sections"][record["description_id"]]["pane_id"]
            for record in wanted if record["description_id"] in view["sections"]
        ]
        current = [pane for pane, _ in self.tree_leaves(root) if pane in desired]
        swapped = False
        try:
            for index, pane_id in enumerate(desired):
                if index >= len(current) or pane_id not in current:
                    break
                if current[index] != pane_id:
                    other = current.index(pane_id)
                    self.api.call(
                        "pane.swap", source_pane_id=pane_id, target_pane_id=current[index]
                    )
                    current[index], current[other] = current[other], current[index]
                    swapped = True
        finally:
            if swapped and original_focus:
                self.api.call("pane.focus", pane_id=original_focus)
        root = self.api.call("layout.export", tab_id=view["tab_id"])["layout"]["root"]
        owned = set(desired)

        def equalize(node, path):
            if node["type"] == "pane":
                return {node["pane_id"]}
            first = equalize(node["first"], path + [False])
            second = equalize(node["second"], path + [True])
            if first | second <= owned and node.get("direction") == "down":
                ratio = len(first) / (len(first) + len(second))
                if abs(node.get("ratio", 0.5) - ratio) > 0.001:
                    self.api.call("layout.set_split_ratio", tab_id=view["tab_id"], path=path, ratio=ratio)
            return first | second

        equalize(root, [])

    def layout_description_ids(self, view, snapshot):
        tab_ids = {view["tab_id"]}
        pending = view.get("layout_pending") or {}
        if pending.get("move_descriptions"):
            tab_ids.update(
                tab["tab_id"] for tab in snapshot["tabs"]
                if tab["workspace_id"] == view["workspace_id"] and (
                    tab["tab_id"] == pending.get("staging_tab_id")
                    or tab.get("label") == pending.get("staging_label")
                )
            )
        ids = set()
        for section in view.get("sections", {}).values():
            pane = next((
                pane for pane in snapshot["panes"]
                if pane["pane_id"] == section.get("pane_id")
                and pane["workspace_id"] == view["workspace_id"] and pane["tab_id"] in tab_ids
            ), None)
            if pane and self.section_pane(snapshot, {**view, "tab_id": pane["tab_id"]}, section):
                ids.add(pane["pane_id"])
        return ids

    def recover_layouts(self):
        for view in self.runtime.views():
            if not view.get("layout_pending"):
                continue
            snapshot = self.api.snapshot()
            description_ids = self.layout_description_ids(view, snapshot)
            ColumnLayout(self.api, self.runtime, self.config.panel_width_percent).ensure(
                view, description_ids,
            )

    def open_section(self, snapshot, view, record):
        existing = [
            section["pane_id"] for section in view["sections"].values()
            if section.get("pane_id") and self.section_pane(snapshot, view, section)
        ]
        if existing:
            root = self.api.call("layout.export", tab_id=view["tab_id"])["layout"]["root"]
            leaves = [(pane, path) for pane, path in self.tree_leaves(root) if pane in existing]
            target = min(leaves, key=lambda item: len(item[1]))[0]
            direction = "down"
        else:
            sources = self.source_panes(snapshot, view["tab_id"])
            if not sources:
                raise HerdrError("Add a terminal pane before opening descriptions.")
            layout = next(
                (item for item in snapshot.get("layouts", []) if item["tab_id"] == view["tab_id"]), {}
            )
            positions = {pane["pane_id"]: pane["rect"] for pane in layout.get("panes", [])}
            target = max(
                sources, key=lambda pane: (
                    positions.get(pane["pane_id"], {}).get("x", 0)
                    + positions.get(pane["pane_id"], {}).get("width", 0),
                    positions.get(pane["pane_id"], {}).get("height", 0),
                ),
            )["pane_id"]
            direction = "right"
        marker = uuid.uuid4().hex
        cwd = self.runtime.directory / "panes" / marker
        cwd.mkdir(parents=True, mode=0o700)
        section = {
            "pane_id": None, "marker": marker, "cwd": str(cwd),
            "kind": record["kind"], "target_id": record["target_id"],
            "request": None, "retired": False, "discard": False,
            "editor": "simple", "editor_focused": False,
            "label": self.section_title(record),
        }
        section_id = record["description_id"]
        view["sections"][section_id] = section
        self.runtime.put_view(view)
        try:
            pane = self.api.call(
                "plugin.pane.open", plugin_id=PLUGIN_ID, entrypoint="section",
                placement="split", target_pane_id=target, direction=direction,
                focus=False, cwd=str(cwd),
                env={
                    "DESCRIPTION_TAB_ID": view["tab_id"],
                    "DESCRIPTION_SECTION_ID": section_id,
                    "DESCRIPTION_VIEW_LABEL": marker,
                    "DESCRIPTION_FILE": record["file_path"],
                },
            )["plugin_pane"]["pane"]
        except (HerdrError, OSError):
            del view["sections"][section_id]
            self.runtime.put_view(view)
            raise
        section["pane_id"] = pane["pane_id"]
        self.runtime.put_view(view)
        self.api.call("pane.rename", pane_id=pane["pane_id"], label=section["label"])
        self.api.call(
            "pane.report_metadata", pane_id=pane["pane_id"], source=PLUGIN_ID,
            tokens={"herdr_description": marker},
        )
        if direction == "right":
            root = self.api.call("layout.export", tab_id=view["tab_id"])["layout"]["root"]
            path = next(path for pane_id, path in self.tree_leaves(root) if pane_id == pane["pane_id"])
            self.api.call(
                "layout.set_split_ratio", tab_id=view["tab_id"], path=path[:-1],
                ratio=1 - self.config.panel_width_percent / 100,
            )

    def sync_panel(self, snapshot, workspace_id, tab_id, return_pane=None):
        wanted = self.descriptions(workspace_id, tab_id, snapshot)
        view = self.runtime.view(tab_id)
        if view and view.get("format") != 2:
            if self.legacy_pane(snapshot, view):
                return view
            self.runtime.remove_view(tab_id)
            view = None
        if not wanted and view is None:
            return None
        if view is None:
            sources = self.source_panes(snapshot, tab_id)
            source = return_pane or (sources[0]["pane_id"] if sources else None)
            view = {
                "format": 2, "workspace_id": workspace_id, "tab_id": tab_id,
                "sections": {}, "return_pane": source,
                "focused_source_pane": source, "panes": sources,
            }
            self.runtime.put_view(view)
        desired_ids = {record["description_id"] for record in wanted}
        view.pop("needs_layout", None)
        for section_id, section in list(view["sections"].items()):
            pane = self.section_pane(snapshot, view, section)
            if pane is None:
                del view["sections"][section_id]
            elif section_id not in desired_ids:
                section["retired"] = True
        self.runtime.put_view(view)
        for record in wanted:
            section_id = record["description_id"]
            if section_id not in view["sections"]:
                self.open_section(snapshot, view, record)
                snapshot = self.api.snapshot()
            else:
                section = view["sections"][section_id]
                section.update(kind=record["kind"], target_id=record["target_id"], retired=False)
                title = self.section_title(record)
                if section.get("label") != title:
                    self.api.call("pane.rename", pane_id=section["pane_id"], label=title)
                    section["label"] = title
        self.runtime.put_view(view)
        if wanted:
            self.arrange(view, wanted, snapshot.get("focused_pane_id"))
        if not view["sections"]:
            self.runtime.remove_view(tab_id)
            return None
        return view

    def sync_active_panel(self, snapshot):
        workspace_id = snapshot.get("focused_workspace_id")
        tab_id = snapshot.get("focused_tab_id")
        if workspace_id and tab_id:
            self.sync_panel(snapshot, workspace_id, tab_id, snapshot.get("focused_pane_id"))

    def discard_record(self, description_id):
        for view in self.runtime.views():
            if view.get("format") == 2 and description_id in view["sections"]:
                view["sections"][description_id].update(discard=True, retired=True)
                self.runtime.put_view(view)

    def close_editor(self, tab_id, section_id, marker):
        with self.runtime.lock():
            self.recover_layouts()
            view = self.runtime.view(tab_id)
            section = (view or {}).get("sections", {}).get(section_id)
            if section is None or section["marker"] != marker:
                return
            self.close_section(view, section_id)

    def close_section(self, view, section_id):
        section = view["sections"][section_id]
        snapshot = self.api.snapshot()
        pane = self.section_pane(snapshot, view, section)
        if pane and snapshot.get("focused_pane_id") == pane["pane_id"]:
            view["return_pane"] = view.get("focused_source_pane") or view["return_pane"]
            self.restore_focus(view)
        del view["sections"][section_id]
        view["needs_layout"] = True
        self.runtime.put_view(view)
        if pane:
            self.api.call("pane.close", pane_id=pane["pane_id"])

    def action(self, action):
        if action == "restart-editors":
            return self.restart_editors()
        with self.runtime.lock():
            self.recover_layouts()
            snapshot = self.api.snapshot()
            self.refresh_views(snapshot)
            workspace_id, tab_id, pane_id = context(snapshot)
            if not workspace_id or not tab_id:
                raise HerdrError("Select a space and tab first.")
            if action in ("close-tab", "close-space", "close-pane"):
                source_id = self.selected_source(snapshot, tab_id, pane_id) if action == "close-pane" else None
                self.api.call(
                    "plugin.pane.open", plugin_id=PLUGIN_ID,
                    entrypoint="confirm-close", placement="overlay",
                    target_pane_id=pane_id, focus=True,
                    env={
                        "DESCRIPTION_CLOSE_KIND": action.removeprefix("close-"),
                        "DESCRIPTION_WORKSPACE_ID": workspace_id, "DESCRIPTION_TAB_ID": tab_id,
                        "DESCRIPTION_PANE_ID": source_id or "",
                    },
                )
                return
            view = self.runtime.view(tab_id)
            if action == "save":
                if not view:
                    raise HerdrError("Focus a description editor before saving.")
                if view.get("format") != 2:
                    self.runtime.request(tab_id, "save")
                    return
                selected = next((
                    section for section in view["sections"].values()
                    if section.get("pane_id") == pane_id
                ), None)
                if selected is None:
                    raise HerdrError("Focus the description editor you want to save.")
                view["return_pane"] = view.get("focused_source_pane") or view["return_pane"]
                self.request_save(selected, "manual")
                self.runtime.put_view(view)
                return
            if action.startswith(("edit-", "remove-")):
                operation, kind = action.split("-", 1)
                source = self.selected_source(snapshot, tab_id, pane_id) if kind == "pane" else None
                target = self.target(workspace_id, tab_id, kind, source)
                record = (
                    self.store.get_mapping(self.session, kind, target)
                    if operation == "remove" else self.store.get(self.session, kind, target)
                )
                if operation == "remove":
                    if record:
                        self.store.delete(self.session, kind, target)
                        self.discard_record(record.description_id)
                        self.sync_panel(snapshot, workspace_id, tab_id)
                    return
                if record is None:
                    record = self.store.create(
                        self.session, kind, target, space_id=self.identity(workspace_id),
                        tab_id=self.target(workspace_id, tab_id, "tab") if kind == "pane" else None,
                    )
                view = self.sync_panel(snapshot, workspace_id, tab_id, pane_id)
                previous = pane_id if pane_id not in self.owned_ids() else (
                    source or view.get("focused_source_pane") or view["return_pane"]
                )
                view["return_pane"] = previous
                if view.get("format") != 2:
                    self.runtime.put_view(view)
                    self.runtime.request(
                        tab_id, "edit", kind=kind, target_id=target,
                        selected_id=record.description_id, return_pane=previous,
                    )
                    self.api.call("pane.focus", pane_id=view["pane_id"])
                else:
                    self.runtime.put_view(view)
                    self.api.call("pane.focus", pane_id=view["sections"][record.description_id]["pane_id"])
                return
            raise ValueError("Unknown action: " + action)

    def restart_editors(self, timeout=5.0, poll_interval=0.05):
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("Invalid editor restart timeout.")

        def locate(target, snapshot=None):
            view = self.runtime.view(target["tab_id"])
            if view is None:
                return None, None, None
            if target["section_id"] is None:
                if view.get("format") == 2:
                    return None, None, None
                state = view
                matches = view.get("pane_id") == target["pane_id"] and view.get("label") == target["marker"]
                pane = self.legacy_pane(snapshot, view) if snapshot else None
            else:
                state = view.get("sections", {}).get(target["section_id"])
                if state is None:
                    return None, None, None
                matches = state.get("pane_id") == target["pane_id"] and state.get("marker") == target["marker"]
                pane = self.section_pane(snapshot, view, state) if snapshot else None
            if not matches:
                raise HerdrError("An editor changed while restarting; it was left open.")
            return view, state, pane

        targets = []
        with self.runtime.lock():
            self.recover_layouts()
            snapshot = self.api.snapshot()
            for view in self.runtime.views():
                if view.get("format") == 2:
                    candidates = [
                        (section_id, section, self.section_pane(snapshot, view, section), section["marker"])
                        for section_id, section in view["sections"].items()
                    ]
                else:
                    candidates = [(None, view, self.legacy_pane(snapshot, view), view["label"])]
                for section_id, state, pane, marker in candidates:
                    if pane is None:
                        if any(item["pane_id"] == state.get("pane_id") for item in snapshot["panes"]):
                            raise HerdrError("Cannot verify a description pane's ownership; it was left open.")
                        continue
                    if not self.source_panes(snapshot, view["tab_id"]):
                        raise HerdrError("Add a regular terminal pane before restarting this tab's descriptions.")
                    targets.append({
                        "tab_id": view["tab_id"], "section_id": section_id,
                        "pane_id": pane["pane_id"], "marker": marker,
                        "live": pane.get("tokens", {}).get("herdr_description") == marker,
                    })
            focused = snapshot.get("focused_pane_id")
            active = next((target for target in targets if target["pane_id"] == focused), None)
            if active:
                self.restore_focus(self.runtime.view(active["tab_id"]))
            self.refresh_views(self.api.snapshot())
            for target in targets:
                if target["live"]:
                    view, state, unused = locate(target)
                    self.request_save(state, "restart")
                    target["request_id"] = state["request"]["id"]
                    self.runtime.put_view(view)

        deadline = time.monotonic() + timeout
        while True:
            pending = False
            with self.runtime.lock():
                for target in targets:
                    if not target["live"]:
                        continue
                    view, state, unused = locate(target)
                    if state is None:
                        continue
                    request = state.get("request")
                    if request is not None:
                        if request.get("id") != target["request_id"]:
                            raise HerdrError("Another editor action interrupted the restart; editors were left open.")
                        pending = True
                    elif state.get("modified"):
                        raise HerdrError("An editor changed after saving; save it and retry the restart.")
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrError("An editor did not acknowledge saving. Resolve its save error or close it manually, then retry. No editors were restarted.")
            time.sleep(min(poll_interval, remaining))

        with self.runtime.lock():
            snapshot = self.api.snapshot()
            for target in targets:
                view, state, pane = locate(target, snapshot)
                if state is None:
                    continue
                if pane is None or not self.source_panes(snapshot, view["tab_id"]):
                    raise HerdrError("Pane ownership or the tab layout changed; editors were left open.")
                if target["live"] and (state.get("request") or state.get("modified")):
                    raise HerdrError("An editor changed after saving; save it and retry the restart.")
                if snapshot.get("focused_pane_id") == pane["pane_id"]:
                    raise HerdrError("An editor regained focus. Focus a work pane and retry the restart.")
            for target in targets:
                snapshot = self.api.snapshot()
                view, state, pane = locate(target, snapshot)
                if state is None:
                    continue
                if pane is None or not self.source_panes(snapshot, view["tab_id"]):
                    raise HerdrError("A description pane changed during restart; remaining panes were kept.")
                if snapshot.get("focused_pane_id") == pane["pane_id"]:
                    raise HerdrError("An editor regained focus; remaining panes were kept.")
                self.api.call("pane.close", pane_id=pane["pane_id"])
            snapshot = self.api.snapshot()
            self.restore(snapshot, verify_identities=False)
            snapshot = self.api.snapshot()
            self.refresh_views(snapshot)
            self.sync_active_panel(snapshot)
            self.notify("Description editors restarted. Saved files and mappings were preserved.")

    def section_tick(self, tab_id, section_id, payload):
        with self.runtime.lock():
            self.recover_layouts()
            view = self.runtime.view(tab_id)
            section = (view or {}).get("sections", {}).get(section_id)
            if section is None:
                return {"error": "Editor is detached; your unsaved text is retained."} if payload.get("modified") else {"closed": True}
            marker = os.environ.get("DESCRIPTION_VIEW_LABEL")
            if marker and marker != section["marker"]:
                return {"error": "Editor was replaced; your unsaved text is retained."} if payload.get("modified") else {"closed": True}
            state_before = json.dumps(view, sort_keys=True)
            simple = payload.get("editor") == "simple"
            if simple:
                section["editor"] = "simple"
            if section.get("discard"):
                self.close_section(view, section_id)
                return {"closed": True}
            if payload.get("remove") is True:
                record = self.store.get_mapping(self.session, section["kind"], section["target_id"])
                if record and record.description_id == section_id:
                    self.store.delete(self.session, record.kind, record.target_id)
                    self.discard_record(section_id)
                self.close_section(view, section_id)
                snapshot = self.api.snapshot()
                if any(tab["tab_id"] == tab_id for tab in snapshot["tabs"]):
                    self.sync_panel(snapshot, view["workspace_id"], tab_id)
                return {"closed": True}
            record = self.store.get(self.session, section["kind"], section["target_id"])
            if record is None or record.description_id != section_id:
                if payload.get("modified"):
                    return {"error": "Description was removed; unsaved text is retained."}
                self.close_section(view, section_id)
                return {"closed": True}
            retired = section.get("retired", False) or (
                record.kind == "pane"
                and record.tab_id != self.target(view["workspace_id"], tab_id, "tab")
            )
            if simple and payload.get("modified") and not section.get("request"):
                snapshot = self.api.snapshot()
                section["editor_focused"] = snapshot.get("focused_pane_id") == section["pane_id"]
                if not section["editor_focused"]:
                    self.request_save(section, "blur")
            request = section.get("request") or {}
            if payload.get("ack_request") == request.get("id") and request:
                section["request"] = None
            saved = False
            warning = None
            save = payload.get("save") if simple else None
            if save is not None:
                if not isinstance(save, dict):
                    raise ValueError("Invalid editor save request.")
                try:
                    record = self.store.save_file(
                        self.session, record.kind, record.target_id, save["content"],
                        description_id=section_id, expected_digest=save["expected_digest"],
                        force=save.get("force", False),
                    )
                except DocumentConflictError as error:
                    self.runtime.put_view(view)
                    self.notify("Could not save {} description: file changed elsewhere.".format(record.kind))
                    return {"error": str(error), "conflict": True}
                except (OSError, DescriptionStoreError) as error:
                    self.runtime.put_view(view)
                    self.notify("Could not save {} description: {}".format(record.kind, error))
                    return {"error": str(error)}
                saved = True
                requested_id = save.get("request_id")
                if requested_id is None or requested_id == request.get("id"):
                    section["request"] = None
                self.notify("", title="{} description saved".format(record.kind.capitalize()))
            elif payload.get("saved"):
                saved = True
                section["request"] = None
            if saved:
                self.runtime.put_view(view)
                return_focus = payload.get("return_focus", False)
                if save is not None:
                    return_focus = request.get("return_focus", False)
                if request.get("reason") == "restart":
                    return_focus = False
                if return_focus:
                    view["return_pane"] = view.get("focused_source_pane") or view["return_pane"]
                    self.runtime.put_view(view)
                    try:
                        self.restore_focus(view)
                    except (HerdrError, OSError) as error:
                        warning = "Saved, but could not restore focus: " + str(error)
            modified = bool(payload.get("modified")) and not saved
            section["modified"] = modified
            if json.dumps(view, sort_keys=True) != state_before:
                self.runtime.put_view(view)
            if retired and not modified:
                self.close_section(view, section_id)
                return {"closed": True, "saved": saved}
            presented = next((
                item for item in self.descriptions(view["workspace_id"], tab_id)
                if item["description_id"] == section_id
            ), {**dataclasses.asdict(record), "title": record.kind.capitalize() + " description", "focused": False})
            return {
                "record": presented, "saved": saved, "removable": True,
                "write_requested": (section.get("request") or {}).get("action") == "save",
                "save_request": section.get("request"),
                "editor_focused": section.get("editor_focused", False),
                "warning": warning,
            }

    def tick(self, tab_id, payload):
        with self.runtime.lock():
            self.recover_layouts()
            view = self.runtime.view(tab_id)
            if view is None or view.get("format") == 2:
                return {"error": "This combined editor was replaced; export local edits and close it."} if payload.get("changes") else {"closed": True}
            request = view.get("request") or {}
            action = request.get("action")
            saved = []
            errors = []
            if action == "save":
                for change in payload.get("changes", []):
                    target = change.get("target_id") or self.target(view["workspace_id"], tab_id, change["kind"])
                    try:
                        record = self.store.save(
                            self.session, change["kind"], target, change["content"],
                            description_id=change["description_id"], expected_revision=change["revision"],
                        )
                        saved.append(dataclasses.asdict(record))
                    except DescriptionStoreError as error:
                        errors.append(str(error))
                if errors:
                    return {"error": "; ".join(errors), "saved": saved}
            view["request"] = None
            self.runtime.put_view(view)
            if action == "save" and request.get("return_focus", True):
                self.restore_focus(view)
            descriptions = self.descriptions(view["workspace_id"], tab_id)
            existing = {record["description_id"] for record in descriptions}
            if any(change["description_id"] not in existing for change in payload.get("changes", [])):
                return {"error": "Description moved or was removed; export local edits before closing.", "saved": saved}
            if not descriptions:
                return {"closed": True, "saved": saved}
            return {
                "descriptions": descriptions, "saved": saved, "discarded": [],
                "action": action, "kind": view.get("kind"),
                "selected_id": view.get("selected_id"),
            }

    def event(self, event, startup=False, verify_identities=True):
        with self.runtime.lock():
            self.recover_layouts()
            snapshot = self.api.snapshot()
            data = event.get("data", event)
            event_type = data.get("type", "")
            workspace_id = data.get("workspace_id")
            close_tabs = set()
            moved_pane_id = None
            if event_type in ("pane_closed", "pane_exited"):
                for view in self.runtime.views():
                    sources = view.get("panes", []) + view.get("last_source_panes", [])
                    source_ids = {pane["pane_id"] for pane in sources}
                    source_ids.update((view.get("return_pane"), view.get("focused_source_pane")))
                    if view["workspace_id"] == workspace_id and data.get("pane_id") in source_ids:
                        close_tabs.add(view["tab_id"])
            if event_type == "pane_moved":
                close_tabs.add(data.get("previous_tab_id"))
                moved_pane_id = data.get("previous_pane_id")
                previous = self.runtime.identity(data.get("previous_workspace_id"))
                pane = data.get("pane")
                if pane:
                    pane = next((
                        current for current in snapshot["panes"]
                        if current["pane_id"] == pane["pane_id"]
                        or (pane.get("terminal_id") and current.get("terminal_id") == pane["terminal_id"])
                    ), pane)
                if previous and pane:
                    old_target = previous["id"] + "/" + data["previous_pane_id"]
                    target = self.target(pane["workspace_id"], pane["tab_id"], "pane", pane["pane_id"])
                    space = self.identity(pane["workspace_id"])
                    tab = self.target(pane["workspace_id"], pane["tab_id"], "tab")
                    current = self.store.get_mapping(self.session, "pane", old_target)
                    moved = None
                    if current and (
                        current.target_id != target or current.space_id != space or current.tab_id != tab
                    ):
                        moved = self.store.move_pane(
                            self.session, old_target, target, space_id=space, tab_id=tab,
                        )
                    if moved:
                        for existing in self.runtime.views():
                            section = existing.get("sections", {}).get(moved.description_id)
                            if section:
                                section["target_id"] = moved.target_id
                                self.runtime.put_view(existing)
                if data.get("closed_workspace_id"):
                    event_type, workspace_id = "workspace_closed", data["closed_workspace_id"]
                elif data.get("closed_tab_id"):
                    event_type, workspace_id = "tab_closed", data["previous_workspace_id"]
                    data["tab_id"] = data["closed_tab_id"]
            if event_type == "workspace_closed" and workspace_id:
                identity = self.runtime.identity(workspace_id)
                if identity:
                    self.store.delete_space(self.session, identity["id"])
                    self.runtime.forget_identity(workspace_id)
                for view in self.runtime.views():
                    if view["workspace_id"] == workspace_id:
                        self.runtime.remove_view(view["tab_id"])
            if event_type == "tab_closed" and workspace_id:
                identity = self.runtime.identity(workspace_id)
                if identity:
                    self.store.delete_tab(self.session, identity["id"] + "/" + data["tab_id"])
                self.runtime.remove_view(data["tab_id"])
            if event_type in ("pane_closed", "pane_exited"):
                if workspace_id:
                    identity = self.runtime.identity(workspace_id)
                    if identity:
                        target = identity["id"] + "/" + data["pane_id"]
                        record = self.store.get_mapping(self.session, "pane", target)
                        if record:
                            self.store.delete(self.session, "pane", target)
                            self.discard_record(record.description_id)
                for view in self.runtime.views():
                    if view.get("format") == 2:
                        previous = len(view["sections"])
                        view["sections"] = {
                            key: section for key, section in view["sections"].items()
                            if section.get("pane_id") != data["pane_id"]
                        }
                        if len(view["sections"]) != previous:
                            view["needs_layout"] = True
                        self.runtime.put_view(view)
                    elif view.get("pane_id") == data["pane_id"]:
                        self.runtime.remove_view(view["tab_id"])
            if startup:
                self.restore(snapshot, verify_identities=verify_identities)
                snapshot = self.api.snapshot()
            snapshot = self.close_description_only_tabs(
                snapshot, None if startup else close_tabs, moved_pane_id,
            )
            self.refresh_views(snapshot)
            self.sync_active_panel(snapshot)

    def close_description_only_tabs(self, snapshot, tab_ids=None, moved_pane_id=None):
        for view in self.runtime.views():
            tab_id = view["tab_id"]
            workspace_id = view["workspace_id"]
            if tab_ids is not None and tab_id not in tab_ids:
                continue

            def description_only(current):
                tab_exists = any(
                    tab["tab_id"] == tab_id and tab["workspace_id"] == workspace_id
                    for tab in current["tabs"]
                )
                panes = [pane for pane in current["panes"] if pane["tab_id"] == tab_id]
                sources = view.get("panes", []) + view.get("last_source_panes", [])
                for source in sources:
                    if source["pane_id"] == moved_pane_id:
                        continue
                    if any(
                        pane["pane_id"] == source["pane_id"]
                        or (source.get("terminal_id") and pane.get("terminal_id") == source["terminal_id"])
                        for pane in current["panes"]
                    ):
                        return False
                owned = self.owned_ids(current)
                return tab_exists and bool(panes) and all(
                    pane["workspace_id"] == workspace_id and pane["pane_id"] in owned
                    for pane in panes
                )

            if not description_only(snapshot):
                continue
            snapshot = self.api.snapshot()
            if not description_only(snapshot):
                continue
            self.api.call("tab.close", tab_id=tab_id)
            snapshot = self.api.snapshot()
            self.cleanup_closed("tab", workspace_id, tab_id, None, snapshot)
        return snapshot

    def restore(self, snapshot, verify_identities=True):
        views = self.runtime.views()
        proved = set()
        for view in views:
            if self.legacy_pane(snapshot, view) or any(
                self.section_pane(snapshot, view, section)
                for section in view.get("sections", {}).values()
            ):
                proved.add(view["workspace_id"])
        if verify_identities:
            for workspace_id in self.runtime.identities():
                if workspace_id not in proved:
                    self.runtime.forget_identity(workspace_id)
        for view in views:
            if view.get("format") != 2:
                pane = self.legacy_pane(snapshot, view)
                if pane and pane.get("tokens", {}).get("herdr_description") == view["label"]:
                    self.notify("Save and close the old combined description editor, then run Restore descriptions.")
                    continue
                if pane:
                    if not self.source_panes(snapshot, view["tab_id"]):
                        continue
                    self.api.call("pane.close", pane_id=pane["pane_id"])
                self.runtime.remove_view(view["tab_id"])
                continue
            for section_id, section in list(view["sections"].items()):
                pane = self.section_pane(snapshot, view, section)
                if pane and pane.get("tokens", {}).get("herdr_description") == section["marker"]:
                    continue
                if pane:
                    if not self.source_panes(snapshot, view["tab_id"]):
                        continue
                    self.api.call("pane.close", pane_id=pane["pane_id"])
                del view["sections"][section_id]
            self.runtime.put_view(view)

    def close_plan(self, snapshot, kind, workspace_id, tab_id, pane_id=None):
        workspace = next((item for item in snapshot["workspaces"] if item["workspace_id"] == workspace_id), None)
        tab = next((item for item in snapshot["tabs"] if item["tab_id"] == tab_id and item["workspace_id"] == workspace_id), None)
        if workspace is None or (kind != "space" and tab is None):
            raise HerdrError("The requested space or tab has already closed.")
        if kind == "pane" and pane_id not in {item["pane_id"] for item in self.source_panes(snapshot, tab_id)}:
            raise HerdrError("The requested source pane has already closed.")
        tabs = [item for item in snapshot["tabs"] if item["workspace_id"] == workspace_id]
        closes_tab = kind == "tab" or (
            kind == "pane" and len(self.source_panes(snapshot, tab_id)) == 1
        )
        scope = "space" if kind == "space" or (closes_tab and len(tabs) == 1) else (
            "tab" if closes_tab else kind
        )
        identity = self.runtime.identity(workspace_id)
        records = self.store.list_mappings(self.session, space_id=identity["id"]) if identity else []
        if scope == "tab":
            target = identity["id"] + "/" + tab_id if identity else ""
            records = [record for record in records if (record.kind == "tab" and record.target_id == target) or (record.kind == "pane" and record.tab_id == target)]
        elif scope == "pane":
            target = identity["id"] + "/" + pane_id if identity else ""
            records = [record for record in records if record.kind == "pane" and record.target_id == target]
        counts = {item: sum(record.kind == item for record in records) for item in ("space", "tab", "pane")}
        if scope == "space":
            warning = "All descriptions in this space will be deleted: the space description, all tab descriptions, and all pane descriptions in those tabs."
            if kind == "tab":
                warning = "Closing the last tab also closes its space. " + warning
            elif kind == "pane":
                warning = "Closing the last work pane also closes its tab and this space. " + warning
        elif scope == "tab":
            warning = "The tab description and all pane descriptions in this tab will be deleted."
            if kind == "pane":
                warning = "Closing the last work pane also closes its tab. " + warning
        else:
            warning = "This pane's description will be deleted. If closing it also closes its tab or space, all descriptions inside that container will also be deleted."
        warning += "\nFiles and mappings to remove: {} space, {} tab, {} pane ({} total).".format(counts["space"], counts["tab"], counts["pane"], len(records))
        return {"scope": scope, "counts": counts, "records": records, "warning": warning}

    def cleanup_closed(self, kind, workspace_id, tab_id, pane_id, snapshot):
        identity = self.runtime.identity(workspace_id)
        if identity is None:
            return
        space = identity["id"]
        space_exists = any(item["workspace_id"] == workspace_id for item in snapshot["workspaces"])
        tab_exists = any(item["tab_id"] == tab_id for item in snapshot["tabs"])
        pane_exists = any(item["pane_id"] == pane_id for item in snapshot["panes"])
        if (kind == "space" and space_exists) or (kind == "tab" and tab_exists) or (kind == "pane" and pane_exists):
            raise HerdrError("Herdr did not close the requested {}. Description files were kept.".format(kind))
        records = self.store.list_mappings(self.session, space_id=space)
        if not space_exists:
            for record in records:
                self.discard_record(record.description_id)
            self.store.delete_space(self.session, space)
            self.runtime.forget_identity(workspace_id)
            for view in self.runtime.views():
                if view["workspace_id"] == workspace_id:
                    self.runtime.remove_view(view["tab_id"])
        elif not tab_exists:
            target = space + "/" + tab_id
            for record in records:
                if (record.kind == "tab" and record.target_id == target) or (record.kind == "pane" and record.tab_id == target):
                    self.discard_record(record.description_id)
            self.store.delete_tab(self.session, target)
            self.runtime.remove_view(tab_id)
        else:
            target = space + "/" + pane_id
            for record in records:
                if record.kind == "pane" and record.target_id == target:
                    self.discard_record(record.description_id)
                    self.store.delete(self.session, "pane", target)

    def confirm_close(self):
        workspace_id = os.environ["DESCRIPTION_WORKSPACE_ID"]
        tab_id = os.environ["DESCRIPTION_TAB_ID"]
        pane_id = os.environ.get("DESCRIPTION_PANE_ID")
        kind = os.environ["DESCRIPTION_CLOSE_KIND"]
        if kind not in ("space", "tab", "pane"):
            raise ValueError("Unknown close target.")
        with self.runtime.lock():
            self.recover_layouts()
            plan = self.close_plan(self.api.snapshot(), kind, workspace_id, tab_id, pane_id)
        print("\nClose {}? Running processes may stop.".format(kind))
        print("Notice: " + plan["warning"])
        if input("\nType yes to close and delete these descriptions, or press Enter to cancel: ").strip().lower() != "yes":
            return
        with self.runtime.lock():
            self.recover_layouts()
            self.close_plan(self.api.snapshot(), kind, workspace_id, tab_id, pane_id)
            if kind == "space":
                self.api.call("workspace.close", workspace_id=workspace_id)
            elif kind == "tab":
                self.api.call("tab.close", tab_id=tab_id)
            else:
                self.api.call("pane.close", pane_id=pane_id)
            snapshot = self.close_description_only_tabs(self.api.snapshot(), {tab_id})
            self.cleanup_closed(kind, workspace_id, tab_id, pane_id, snapshot)
