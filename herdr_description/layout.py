import math
import uuid

from .api import HerdrError


def leaves(node):
    if node["type"] == "pane":
        return [node["pane_id"]]
    return leaves(node["first"]) + leaves(node["second"])


def normalize(node):
    if not isinstance(node, dict):
        raise HerdrError("The tab layout is unavailable.")
    if node.get("type") == "pane":
        pane_id = node.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            raise HerdrError("The tab contains an invalid pane identity.")
        return {"type": "pane", "pane_id": pane_id}
    ratio = node.get("ratio")
    if (
        node.get("type") != "split" or node.get("direction") not in ("right", "down")
        or not isinstance(ratio, (int, float)) or isinstance(ratio, bool)
        or not math.isfinite(ratio) or not 0.1 <= ratio <= 0.9
    ):
        raise HerdrError("The tab contains an unsupported split.")
    result = {
        "type": "split", "direction": node["direction"], "ratio": ratio,
        "first": normalize(node.get("first")), "second": normalize(node.get("second")),
    }
    ids = leaves(result)
    if len(ids) != len(set(ids)):
        raise HerdrError("The tab contains duplicate pane identities.")
    return result


def prune(node, removed):
    if node["type"] == "pane":
        return None if node["pane_id"] in removed else node
    first, second = prune(node["first"], removed), prune(node["second"], removed)
    if first is None or second is None:
        return first or second
    return {**node, "first": first, "second": second}


def same_tree(first, second):
    if first["type"] != second["type"]:
        return False
    if first["type"] == "pane":
        return first["pane_id"] == second["pane_id"]
    return (
        first["direction"] == second["direction"]
        and abs(first["ratio"] - second["ratio"]) < 0.000001
        and same_tree(first["first"], second["first"])
        and same_tree(first["second"], second["second"])
    )


def column_parent(node, description_ids):
    if node["type"] == "pane":
        return None
    first, second = set(leaves(node["first"])), set(leaves(node["second"]))
    if node["direction"] == "right" and not first & description_ids and description_ids <= second:
        return node
    return column_parent(node["first"], description_ids) or column_parent(
        node["second"], description_ids
    )


def description_column(ids):
    if len(ids) == 1:
        return {"type": "pane", "pane_id": ids[0]}
    middle = len(ids) // 2
    return {
        "type": "split", "direction": "down", "ratio": middle / len(ids),
        "first": description_column(ids[:middle]),
        "second": description_column(ids[middle:]),
    }


class ColumnLayout:
    def __init__(self, api, runtime, width_percent):
        self.api = api
        self.runtime = runtime
        self.ratio = 1 - width_percent / 100

    def export(self, tab_id):
        return normalize(self.api.call("layout.export", tab_id=tab_id)["layout"]["root"])

    def ensure(self, view, description_ids):
        description_ids = set(description_ids)
        pending = view.get("layout_pending")
        if pending is not None and not any(
            tab["tab_id"] == view["tab_id"] for tab in self.api.snapshot()["tabs"]
        ):
            view.pop("layout_pending", None)
            self.runtime.put_view(view)
            return True
        if pending is None:
            if not description_ids:
                return False
            snapshot = self.api.snapshot()
            root = self.export(view["tab_id"])
            ids = set(leaves(root))
            if not description_ids < ids:
                raise HerdrError("Descriptions require at least one work terminal.")
            parent = column_parent(root, description_ids)
            if parent is root and set(leaves(root["second"])) == description_ids:
                return False
            source_tree = prune(root, description_ids)
            sources = set(leaves(source_tree))
            panes = {pane["pane_id"]: pane for pane in snapshot["panes"]}
            if ids != {
                pane["pane_id"] for pane in snapshot["panes"] if pane["tab_id"] == view["tab_id"]
            }:
                raise HerdrError("The tab changed while reading its layout; terminals were preserved.")
            if any(panes[pane_id]["workspace_id"] != view["workspace_id"] for pane_id in ids):
                raise HerdrError("The tab changed spaces; terminals were preserved.")
            pending = {
                "source_tree": source_tree,
                "description_tree": (
                    description_column([pane_id for pane_id in leaves(root) if pane_id in description_ids])
                    if parent is None else prune(root, sources)
                ),
                "move_descriptions": parent is None,
                "source_identities": {
                    pane_id: panes[pane_id].get("terminal_id") for pane_id in leaves(source_tree)
                },
                "description_identities": {
                    pane_id: panes[pane_id].get("terminal_id") for pane_id in description_ids
                },
                "anchor": leaves(source_tree)[0],
                "workspace_id": view["workspace_id"], "tab_id": view["tab_id"],
                "staging_label": "Description layout recovery " + uuid.uuid4().hex,
                "staging_tab_id": None,
                "original_focus": snapshot.get("focused_pane_id"),
                "original_focus_terminal": panes.get(
                    snapshot.get("focused_pane_id"), {}
                ).get("terminal_id"),
                "ratio": self.ratio,
            }
            view["layout_pending"] = pending
            self.runtime.put_view(view)
        try:
            self.rebuild(view, pending, description_ids)
            view.pop("layout_pending", None)
            self.runtime.put_view(view)
            return True
        except (HerdrError, OSError, KeyError, TypeError, ValueError) as error:
            raise HerdrError(
                "{} Work terminals were preserved. Retry the description action to recover; "
                "some terminals may remain in the temporary recovery tab.".format(error)
            ) from error
        finally:
            self.restore_focus(pending)

    def restore_focus(self, pending):
        pane_id = pending.get("original_focus")
        if not pane_id:
            return
        try:
            snapshot = self.api.snapshot()
            pane = next((pane for pane in snapshot["panes"] if pane["pane_id"] == pane_id), None)
            terminal_id = pending.get("original_focus_terminal")
            if pane and (terminal_id is None or pane.get("terminal_id") == terminal_id):
                if snapshot.get("focused_pane_id") != pane_id:
                    self.api.call("pane.focus", pane_id=pane_id)
        except (HerdrError, OSError, KeyError, TypeError, ValueError):
            pass

    def inspect(self, view, pending, description_ids):
        source_tree = normalize(pending["source_tree"])
        description_tree = normalize(pending["description_tree"])
        sources = set(leaves(source_tree))
        if (
            set(leaves(description_tree)) != description_ids or sources & description_ids
            or sources != set(pending["source_identities"])
            or description_ids != set(pending["description_identities"])
            or pending["anchor"] != leaves(source_tree)[0]
            or pending["tab_id"] != view["tab_id"]
            or pending["workspace_id"] != view["workspace_id"]
            or not 0.1 <= pending["ratio"] <= 0.9
        ):
            raise HerdrError("The saved layout or description ownership changed.")
        snapshot = self.api.snapshot()
        panes = {pane["pane_id"]: pane for pane in snapshot["panes"]}
        if len(panes) != len(snapshot["panes"]):
            raise HerdrError("Pane identities are ambiguous.")
        tabs = {tab["tab_id"]: tab for tab in snapshot["tabs"]}
        original = tabs.get(view["tab_id"])
        if original is None or original["workspace_id"] != view["workspace_id"]:
            raise HerdrError("The original tab closed or changed spaces.")
        stage = tabs.get(pending.get("staging_tab_id"))
        if stage is None:
            matches = [
                tab for tab in snapshot["tabs"]
                if tab.get("label") == pending["staging_label"]
                and tab["workspace_id"] == view["workspace_id"] and tab["tab_id"] != view["tab_id"]
            ]
            if len(matches) > 1:
                raise HerdrError("The temporary recovery tab is ambiguous.")
            stage = matches[0] if matches else None
        stage_id = stage["tab_id"] if stage else None
        if stage and stage["workspace_id"] != view["workspace_id"]:
            raise HerdrError("The temporary recovery tab changed spaces.")
        movable = (
            sources | description_ids if pending.get("move_descriptions") else sources
        ) - {pending["anchor"]}
        for identities in (pending["source_identities"], pending["description_identities"]):
            for pane_id, terminal_id in identities.items():
                pane = panes.get(pane_id)
                if pane is None or (
                    terminal_id is not None and pane.get("terminal_id") != terminal_id
                ):
                    raise HerdrError("A saved terminal closed or its pane identity was reused.")
                allowed = {view["tab_id"]}
                if pane_id in movable and stage_id:
                    allowed.add(stage_id)
                if pane["workspace_id"] != view["workspace_id"] or pane["tab_id"] not in allowed:
                    raise HerdrError("A saved terminal moved outside the original and recovery tabs.")
        for pane in snapshot["panes"]:
            if pane["tab_id"] == view["tab_id"] and pane["pane_id"] not in sources | description_ids:
                raise HerdrError("A new pane was added to the original tab.")
            if stage_id and pane["tab_id"] == stage_id and pane["pane_id"] not in movable:
                raise HerdrError("The temporary recovery tab contains an unrelated pane.")
        root = self.export(view["tab_id"])
        if set(leaves(root)) != {
            pane["pane_id"] for pane in snapshot["panes"] if pane["tab_id"] == view["tab_id"]
        }:
            raise HerdrError("The tab changed while reading its layout.")
        if not pending.get("move_descriptions") and not same_tree(prune(root, sources), description_tree):
            raise HerdrError("The description layout changed during recovery.")
        if stage_id and pending.get("staging_tab_id") != stage_id:
            pending["staging_tab_id"] = stage_id
            self.runtime.put_view(view)
        return panes, stage_id, root

    def rebuild(self, view, pending, description_ids):
        panes, stage_id, root = self.inspect(view, pending, description_ids)
        source_tree, description_tree = pending["source_tree"], pending["description_tree"]
        target = {
            "type": "split", "direction": "right", "ratio": pending["ratio"],
            "first": source_tree, "second": description_tree,
        }
        if same_tree(root, {**target, "ratio": root.get("ratio")}):
            self.finish(view, pending, target, root)
            return
        anchor = pending["anchor"]
        moving_descriptions = pending.get("move_descriptions")
        if not moving_descriptions:
            parent = column_parent(root, description_ids)
            if parent is None:
                raise HerdrError("The descriptions no longer share their original right column.")
            if anchor not in leaves(parent["first"]):
                self.api.call("pane.swap", source_pane_id=anchor, target_pane_id=leaves(parent["first"])[0])
        restored_tree = target if moving_descriptions else source_tree
        for pane_id in leaves(restored_tree)[1:]:
            panes, stage_id, root = self.inspect(view, pending, description_ids)
            if panes[pane_id]["tab_id"] != view["tab_id"]:
                continue
            if stage_id:
                staged = [item["pane_id"] for item in panes.values() if item["tab_id"] == stage_id]
                if not staged:
                    raise HerdrError("The temporary recovery tab has no destination terminal.")
                destination = {
                    "type": "tab", "tab_id": stage_id,
                    "target_pane_id": staged[0],
                    "split": "right", "ratio": 0.5,
                }
            else:
                destination = {
                    "type": "new_tab", "workspace_id": view["workspace_id"],
                    "label": pending["staging_label"],
                }
            result = self.api.call("pane.move", pane_id=pane_id, destination=destination, focus=False)
            if stage_id is None:
                pending["staging_tab_id"] = result["tab"]["tab_id"]
                self.runtime.put_view(view)
        panes, stage_id, root = self.inspect(view, pending, description_ids)
        base = {"type": "pane", "pane_id": anchor}
        if not moving_descriptions:
            base = {**target, "ratio": root.get("ratio"), "first": base}
        if not same_tree(root, base):
            raise HerdrError("The original tab did not retain the work anchor beside its descriptions.")

        def restore(node):
            if node["type"] == "pane":
                return
            pane_id, target_id = leaves(node["second"])[0], leaves(node["first"])[0]
            panes, stage_id, unused = self.inspect(view, pending, description_ids)
            if panes[pane_id]["tab_id"] != stage_id or panes[target_id]["tab_id"] != view["tab_id"]:
                raise HerdrError("A work terminal changed tabs during recovery.")
            self.api.call(
                "pane.move", pane_id=pane_id, focus=False,
                destination={
                    "type": "tab", "tab_id": view["tab_id"], "target_pane_id": target_id,
                    "split": node["direction"], "ratio": node["ratio"],
                },
            )
            restore(node["first"])
            restore(node["second"])

        restore(restored_tree)
        panes, stage_id, root = self.inspect(view, pending, description_ids)
        self.finish(view, pending, target, root)

    def finish(self, view, pending, target, root):
        if not same_tree(root, {**target, "ratio": root.get("ratio")}):
            raise HerdrError("The restored source layout differs from the saved layout.")
        if not same_tree(root, target):
            self.api.call("layout.set_split_ratio", tab_id=view["tab_id"], path=[], ratio=pending["ratio"])
        if not same_tree(self.export(view["tab_id"]), target):
            raise HerdrError("The final description column could not be verified.")
