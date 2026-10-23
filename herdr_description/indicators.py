from pathlib import Path

from .api import HerdrError
from .storage import DescriptionStoreError


MARKER = "✎"
PREFIX = MARKER + " "
METADATA_SOURCE = "herdr-description.indicator"


class NameIndicators:
    def __init__(self, api, runtime, store):
        self.api = api
        self.runtime = runtime
        self.store = store

    def toggle(self, snapshot, excluded_pane_ids=()):
        state = self.runtime.indicator_state()
        state["enabled"] = not state["enabled"]
        self.runtime.put_indicator_state(state)
        self.sync(snapshot, excluded_pane_ids)
        return state["enabled"]

    def undecorated_label(self, kind, host_id, label):
        entry = self.runtime.indicator_state()["entries"].get(self._key(kind, host_id))
        if entry and label == entry["original_label"]:
            return label
        if entry and label == entry["decorated_label"]:
            if kind == "pane":
                return entry.get("original_title") or entry["original_label"] or ""
            return entry["original_label"]
        if kind != "pane" and entry and entry.get("confirmed"):
            return self._undecorate(label, entry)
        return label

    @staticmethod
    def _undecorate(label, entry):
        if not isinstance(label, str):
            return label
        prefix, suffix = entry.get("prefix", ""), entry.get("suffix", " *")
        if prefix and not label.startswith(prefix) or suffix and not label.endswith(suffix):
            return label
        return label[len(prefix):len(label) - len(suffix) if suffix else None]

    def sync(self, snapshot, excluded_pane_ids=()):
        state = self.runtime.indicator_state()
        entries = state["entries"]
        excluded = set(excluded_pane_ids) | self._editor_ids(snapshot)
        entities = self._entities(snapshot)
        targets = self._targets(entities, excluded, state["enabled"])
        by_description = {target["record"].description_id: target for target in targets.values()}
        replaced = set()
        for key, entry in list(entries.items()):
            entity = entities.get(key)
            target = by_description.get(entry["description_id"])
            if (
                entity and entry["kind"] == "space"
                and (entry["space_id"] is None or entity["space_id"] is None)
                and entity["workspace_id"] == entry["workspace_id"]
                and entity["item"].get("label") == entry["decorated_label"]
            ):
                self._set_scope(entry, entity)
            if target and self._relocated(entry, target["entity"]):
                entity = target["entity"]
                new_key = self._key(entry["kind"], entity["host_id"])
                if new_key != key:
                    entries.pop(key)
                    entries[new_key] = entry
                self._set_scope(entry, entity)
            elif not entity or not self._same_scope(entry, entity):
                if entity and target and target["entity"] is entity:
                    replaced.add(key)
                    continue
                entries.pop(key)

        targets = self._inherit_targets(entities, targets, replaced)
        failures = []
        changed = False
        for key in sorted(set(entries) | set(targets)):
            if key in replaced:
                continue
            entity = entities.get(key)
            if entity is None:
                continue
            target = targets.get(key)
            entry = entries.get(key)
            if target and target["unavailable"] and not (
                entity["kind"] == "space" and entry is not None
                and entry.get("prefix", "").isspace() and entry.get("suffix") == ""
            ):
                continue
            wanted = state["enabled"] and target and target["nonempty"]
            if wanted:
                changed |= self._apply(state, entity, target["record"], entry, failures)
            elif entry:
                changed |= self._remove(state, entity, entry, failures)
        self.runtime.put_indicator_state(state)
        if failures:
            raise HerdrError("Could not update description name indicators: " + "; ".join(failures))
        return changed

    def _entities(self, snapshot):
        result = {}
        for kind, collection, identifier in (
            ("space", "workspaces", "workspace_id"),
            ("tab", "tabs", "tab_id"),
            ("pane", "panes", "pane_id"),
        ):
            for item in snapshot.get(collection, []):
                workspace_id = item.get("workspace_id")
                identity = self.runtime.identity(workspace_id)
                if not identity and kind != "space":
                    continue
                host_id = item[identifier]
                entity = {
                    "kind": kind, "host_id": host_id, "workspace_id": workspace_id,
                    "space_id": identity["id"] if identity else None, "terminal_id": item.get("terminal_id"),
                    "item": item,
                }
                result[self._key(kind, host_id)] = entity
        return result

    def _targets(self, entities, excluded, enabled):
        result = {}
        mappings = self.store.list_mappings(self.runtime.session_id)
        by_target = {}
        for key, entity in entities.items():
            kind = entity["kind"]
            if entity["space_id"] is None:
                continue
            if kind == "pane" and entity["host_id"] in excluded:
                continue
            target_id = entity["space_id"]
            if kind != "space":
                target_id += "/" + entity["host_id"]
            by_target[(kind, target_id)] = (key, entity)
        for record in mappings:
            located = by_target.get((record.kind, record.target_id))
            if not located:
                continue
            key, entity = located
            if record.space_id != entity["space_id"]:
                continue
            if record.kind == "pane" and record.tab_id != (
                entity["space_id"] + "/" + entity["item"]["tab_id"]
            ):
                continue
            target = {"entity": entity, "record": record, "unavailable": False, "nonempty": False}
            if enabled:
                try:
                    document = self.store.get(self.runtime.session_id, record.kind, record.target_id)
                    target["nonempty"] = bool(document and document.content.strip())
                except (OSError, UnicodeError, DescriptionStoreError):
                    target["unavailable"] = True
            result[key] = target
        return result

    def _inherit_targets(self, entities, targets, excluded):
        result = {}
        for key, target in targets.items():
            if key in excluded:
                continue
            source = target["entity"]
            ancestors = {key, self._key("space", source["workspace_id"])}
            if source["kind"] == "pane":
                ancestors.add(self._key("tab", source["item"]["tab_id"]))
            for ancestor in ancestors:
                entity = entities.get(ancestor)
                if entity is None or entity["space_id"] != source["space_id"]:
                    continue
                if ancestor not in result:
                    result[ancestor] = {**target, "entity": entity}
                    continue
                combined = result[ancestor]
                if target["nonempty"] and not combined["nonempty"]:
                    combined["record"] = target["record"]
                combined["nonempty"] |= target["nonempty"]
                combined["unavailable"] |= target["unavailable"]
        for target in result.values():
            target["unavailable"] = target["unavailable"] and not target["nonempty"]
        return result

    def _editor_ids(self, snapshot):
        panes = {pane["pane_id"]: pane for pane in snapshot.get("panes", [])}
        result = {
            pane_id for pane_id, pane in panes.items()
            if pane.get("tokens", {}).get("herdr_description")
        }
        for view in self.runtime.views():
            sections = view.get("sections", {}).values() if view.get("format") == 2 else [view]
            for section in sections:
                pane = panes.get(section.get("pane_id"))
                if not pane or pane.get("tab_id") != view.get("tab_id"):
                    continue
                if view.get("format") != 2:
                    if pane.get("label") == view.get("label"):
                        result.add(pane["pane_id"])
                elif pane.get("cwd") and section.get("cwd") and (
                    Path(pane["cwd"]).resolve() == Path(section["cwd"]).resolve()
                ):
                    result.add(pane["pane_id"])
        return result

    @staticmethod
    def _key(kind, host_id):
        return kind + ":" + host_id

    @staticmethod
    def _set_scope(entry, entity):
        for name in ("kind", "host_id", "workspace_id", "space_id", "terminal_id"):
            entry[name] = entity[name]

    @staticmethod
    def _same_scope(entry, entity):
        return (
            entry["workspace_id"] == entity["workspace_id"]
            and entry["space_id"] == entity["space_id"]
            and (entry["kind"] != "pane" or entry.get("terminal_id") == entity["terminal_id"])
        )

    @staticmethod
    def _relocated(entry, entity):
        return (
            entry["kind"] == "pane"
            and entity["kind"] == "pane"
            and entry.get("terminal_id")
            and entry["terminal_id"] == entity["terminal_id"]
        )

    def _apply(self, state, entity, record, entry, failures):
        if entity["kind"] == "pane":
            return self._apply_pane(state, entity, record, entry, failures)
        item = entity["item"]
        current = item.get("label")
        if current is None:
            failures.append(entity["kind"] + " " + entity["host_id"] + " has no visible name")
            return False
        original = current
        if entry:
            if current == entry["decorated_label"] or current == entry["original_label"] or (
                entry.get("pending") and current == entry.get("before_label")
            ):
                original = entry["original_label"]
            elif entry.get("confirmed"):
                original = self._undecorate(current, entry)
        display = original or ""
        suffix = " " + MARKER if entity["kind"] == "space" else MARKER
        decorated = display + suffix if display else MARKER
        if entry is None:
            entry = {"confirmed": False}
            self._set_scope(entry, entity)
            state["entries"][self._key(entity["kind"], entity["host_id"])] = entry
        entry.update(
            description_id=record.description_id if record else None, original_label=original,
            decorated_label=decorated, prefix="", suffix=suffix,
        )
        if current == decorated:
            entry.update(confirmed=True, pending=False)
            return False
        entry.update(pending=True, before_label=current)
        self.runtime.put_indicator_state(state)
        if not self._rename(entity, decorated, failures):
            return False
        entry.update(confirmed=True, pending=False)
        return True

    def _remove(self, state, entity, entry, failures):
        if entity["kind"] == "pane":
            self.runtime.put_indicator_state(state)
            if not self._metadata(entity, failures, clear_title=True):
                return False
            state["entries"].pop(self._key(entity["kind"], entity["host_id"]))
            return True
        current = entity["item"].get("label")
        original = entry["original_label"]
        if current != entry["decorated_label"] and not (
            entry.get("confirmed") and entry.get("pending") and current == entry.get("before_label")
        ):
            undecorated = self._undecorate(current, entry)
            if current == original or not entry.get("confirmed") or current == undecorated:
                state["entries"].pop(self._key(entity["kind"], entity["host_id"]))
                return False
            original = undecorated
        entry.update(pending=True, before_label=current)
        self.runtime.put_indicator_state(state)
        if not self._rename(entity, original, failures):
            return False
        state["entries"].pop(self._key(entity["kind"], entity["host_id"]))
        return True

    def _apply_pane(self, state, entity, record, entry, failures):
        item = entity["item"]
        current = item.get("title")
        original = current
        if entry:
            if current == entry["decorated_label"] or (
                entry.get("pending") and current == entry.get("before_title")
            ):
                original = entry.get("original_title")
        display = original or item.get("label") or ""
        decorated = PREFIX + display if display else MARKER
        if entry is None:
            entry = {"confirmed": False}
            self._set_scope(entry, entity)
            state["entries"][self._key("pane", entity["host_id"])] = entry
        entry.update(
            description_id=record.description_id, original_title=original,
            original_label=item.get("label"), decorated_label=decorated, prefix=PREFIX, suffix="",
        )
        if current == decorated:
            entry.update(confirmed=True, pending=False)
            return False
        entry.update(pending=True, before_title=current)
        self.runtime.put_indicator_state(state)
        if not self._metadata(entity, failures, title=decorated):
            return False
        entry.update(confirmed=True, pending=False)
        return True

    def _metadata(self, entity, failures, **values):
        try:
            self.api.call(
                "pane.report_metadata", pane_id=entity["host_id"],
                source=METADATA_SOURCE, **values,
            )
        except (HerdrError, OSError, ValueError) as error:
            failures.append("pane " + entity["host_id"] + ": " + str(error))
            return False
        return True

    def _rename(self, entity, label, failures):
        kind = entity["kind"]
        method, identifier = (
            ("workspace.rename", "workspace_id") if kind == "space"
            else (kind + ".rename", kind + "_id")
        )
        try:
            self.api.call(method, **{identifier: entity["host_id"], "label": label})
        except (HerdrError, OSError, ValueError) as error:
            failures.append(kind + " " + entity["host_id"] + ": " + str(error))
            return False
        return True
