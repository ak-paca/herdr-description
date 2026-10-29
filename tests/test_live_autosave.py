import hashlib
import unittest
from pathlib import Path

from herdr_description.app import App
from herdr_description.config import load_config, toggle_autosave, write_config
from tests.test_app import AppHarness, FakeHerdr


class LiveAutosaveTests(AppHarness, unittest.TestCase):
    def editor_poll(self, record, app=None, **payload):
        return self.poll(record, app=app, editor="simple", **payload)

    def save(self, record, content, app=None, base=None, reason="autosave", **extra):
        base = record["content"] if base is None else base
        return self.editor_poll(record, app=app, modified=True, save={
            "content": content,
            "expected_digest": hashlib.sha256(base.encode("utf-8")).hexdigest(),
            "reason": reason,
            **extra,
        })

    def shared_config_app(self, socket_path):
        app = App(self.app.config_dir, self.root / "main" / "state", api=FakeHerdr(socket_path))
        self.addCleanup(app.store.close)
        return app

    def assert_mode(self, response, enabled):
        self.assertIs(response["autosave_supported"], True)
        self.assertIs(response["autosave_enabled"], enabled)

    def test_each_new_description_starts_with_exact_header_template(self):
        for kind in ("space", "tab", "pane"):
            with self.subTest(kind=kind):
                record = self.create(kind, pane_id="shell-1")
                self.assertEqual(record["content"], "-- ")
                self.assertEqual(Path(record["file_path"]).read_bytes(), b"-- ")
                response = self.editor_poll(record)
                self.assertEqual(response["record"]["content"], "-- ")
                self.assertFalse(response["saved"])
                self.assertFalse(self.section(record)["modified"])
                self.assert_mode(response, True)

    def test_editing_existing_empty_and_nonempty_descriptions_preserves_content(self):
        for kind in ("space", "tab", "pane"):
            for index, content in enumerate(("", "Existing notes\n", "-- Existing name\nBody")):
                with self.subTest(kind=kind, content=content):
                    app = self.make_app(FakeHerdr(), kind + str(index))
                    existing = self.seed(kind, app=app)
                    path = app.store.file_path(existing["description_id"])
                    path.write_text(content, encoding="utf-8")
                    opened = self.create(kind, app=app, pane_id="shell-1")
                    self.assertEqual(opened["description_id"], existing["description_id"])
                    self.assertEqual(opened["content"], content)
                    self.assertEqual(path.read_bytes(), content.encode("utf-8"))

    def test_global_mode_reloads_in_existing_apps_and_other_named_sessions(self):
        record = self.create("tab")
        other = self.shared_config_app("/tmp/herdr-other-named-session.sock")
        other_record = self.create("tab", app=other)
        reopened = App(self.app.config_dir, self.root / "main" / "state", api=self.api)
        self.addCleanup(reopened.store.close)
        self.assertNotEqual(self.app.session, other.session)
        for app, item in ((self.app, record), (other, other_record), (reopened, record)):
            self.assert_mode(self.editor_poll(item, app=app), True)
        self.assert_mode(self.editor_poll(record, toggle_autosave=True), False)
        for app, item in ((self.app, record), (other, other_record), (reopened, record)):
            self.assert_mode(self.editor_poll(item, app=app), False)
        self.assertFalse(load_config(self.app.config_dir).autosave_enabled)
        restarted = self.shared_config_app("/tmp/herdr-third-named-session.sock")
        restarted_record = self.create("tab", app=restarted)
        self.assert_mode(self.editor_poll(restarted_record, app=restarted), False)
        self.assert_mode(self.editor_poll(other_record, app=other, toggle_autosave=True), True)
        self.assert_mode(self.editor_poll(record, app=reopened), True)

    def test_handwritten_global_mode_change_is_visible_on_next_poll(self):
        record = self.create("tab")
        self.assert_mode(self.editor_poll(record), True)
        config = load_config(self.app.config_dir)
        config.autosave_enabled = False
        write_config(self.app.config_dir, config)
        self.assert_mode(self.editor_poll(record), False)

    def test_each_autosave_persists_to_same_file_without_success_notifications(self):
        record = self.create("pane")
        path = Path(record["file_path"])
        pane_id = self.native_pane(record)["pane_id"]
        previous = record["content"]
        revision = record["revision"]
        for content in ("-- A", "-- AB", "-- ABC\nNotes"):
            with self.subTest(content=content):
                response = self.save(record, content, base=previous)
                self.assertTrue(response["saved"])
                self.assert_mode(response, True)
                self.assertEqual(response["record"]["description_id"], record["description_id"])
                self.assertEqual(response["record"]["file_path"], str(path))
                self.assertEqual(path.read_bytes(), content.encode("utf-8"))
                self.assertEqual(self.stored(record).content, content)
                self.assertGreater(self.stored(record).revision, revision)
                self.assertFalse(self.section(record)["modified"])
                self.assertEqual(self.api.focused_pane_id, pane_id)
                previous = content
                revision = self.stored(record).revision
        self.assertEqual(self.methods("notification.show"), [])

    def test_disabling_autosave_suppresses_stale_editor_save_and_preserves_dirty_state(self):
        record = self.create("tab")
        other = self.shared_config_app("/tmp/herdr-other-named-session.sock")
        other_record = self.create("tab", app=other)
        self.assert_mode(self.editor_poll(record), True)
        self.editor_poll(other_record, app=other, toggle_autosave=True)
        response = self.save(record, "local draft", force=True)
        self.assertTrue(response["autosave_skipped"])
        self.assertFalse(response["saved"])
        self.assert_mode(response, False)
        self.assertEqual(Path(record["file_path"]).read_bytes(), b"-- ")
        self.assertEqual(self.stored(record).content, record["content"])
        self.assertEqual(self.stored(record).revision, record["revision"])
        self.assertTrue(self.section(record)["modified"])
        self.assertEqual(self.methods("notification.show"), [])

    def test_manual_and_blur_saves_still_commit_when_autosave_is_disabled(self):
        for reason in ("manual", "blur"):
            with self.subTest(reason=reason):
                app = self.make_app(FakeHerdr(), reason)
                record = self.create("tab", app=app)
                toggle_autosave(app.config_dir)
                self.editor_poll(record, app=app, modified=True)
                if reason == "manual":
                    app.action("save")
                else:
                    self.focus("shell-1", app)
                request = self.editor_poll(record, app=app, modified=True)["save_request"]
                self.assertEqual(request["reason"], reason)
                response = self.save(record, reason + " saved", app=app, reason=reason, request_id=request["id"])
                self.assertTrue(response["saved"])
                self.assertFalse(response["write_requested"])
                self.assert_mode(response, False)
                self.assertEqual(self.stored(record, app).content, reason + " saved")
                self.assertEqual(app.api.focused_pane_id, "shell-1")
                self.assertEqual(
                    [item["title"] for item in self.methods("notification.show", app.api)],
                    ["Tab description saved"],
                )

    def test_autosave_conflict_preserves_external_file_and_advertises_mode(self):
        record = self.create("space")
        path = Path(record["file_path"])
        path.write_text("external edit", encoding="utf-8")
        response = self.save(record, "local draft")
        self.assertTrue(response["conflict"])
        self.assertFalse(response.get("saved", False))
        self.assert_mode(response, True)
        self.assertEqual(path.read_text(encoding="utf-8"), "external edit")
        self.assertFalse(any(item["title"].endswith("description saved") for item in self.methods("notification.show")))

    def test_autosave_cannot_redirect_a_write_to_another_description(self):
        record = self.create("tab")
        other = self.create("space")
        response = self.save(
            record, "tab only", description_id=other["description_id"], file_path=other["file_path"],
        )
        self.assertTrue(response["saved"])
        self.assertEqual(self.stored(record).content, "tab only")
        self.assertEqual(self.stored(other).content, other["content"])
        self.assertEqual(Path(other["file_path"]).read_bytes(), b"-- ")
