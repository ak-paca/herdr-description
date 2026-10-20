import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_description.documents import DocumentConflictError, DocumentStore
from herdr_description.storage import (
    DescriptionNotFoundError,
    DescriptionStore,
    RevisionConflictError,
    StaleDescriptionError,
)


class DocumentStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.directory.name) / "state"
        self.store = DocumentStore(self.state_dir)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_create_immediately_creates_private_file_and_mapping(self):
        description = self.store.create("session", "space", "space", content="note")
        path = Path(description.file_path)
        self.assertEqual(path, self.store.file_path(description.description_id))
        self.assertEqual(path.read_text(), "note")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(self.store.get("session", "space", "space"), description)

    def test_migrates_legacy_content_once_and_preserves_file_after_reopen(self):
        with DescriptionStore(self.state_dir) as metadata:
            legacy = metadata.create(
                "session", "tab", "tab", space_id="space", content="legacy\nПривет\n"
            )
        migrated = self.store.get("session", "tab", "tab")
        self.assertEqual(migrated.description_id, legacy.description_id)
        self.assertEqual(migrated.content, "legacy\nПривет\n")
        Path(migrated.file_path).write_text("saved in Vim\n")
        self.store.close()
        self.store = DocumentStore(self.state_dir)
        restored = self.store.get("session", "tab", "tab")
        self.assertEqual(restored.file_path, migrated.file_path)
        self.assertEqual(restored.content, "saved in Vim\n")

    def test_existing_file_wins_before_migration_registration(self):
        with DescriptionStore(self.state_dir) as metadata:
            legacy = metadata.create("session", "space", "space", content="stale")
        path = self.store.file_path(legacy.description_id)
        path.write_text("existing file")
        description = self.store.get("session", "space", "space")
        self.assertEqual(description.content, "existing file")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_direct_writes_are_read_by_get_list_and_export(self):
        description = self.store.create("session", "space", "space")
        Path(description.file_path).write_bytes(b"saved\r\nexactly\r\n")
        self.assertEqual(
            self.store.get("session", "space", "space").content, "saved\r\nexactly\r\n"
        )
        self.assertEqual(
            self.store.list_descriptions("session")[0].content, "saved\r\nexactly\r\n"
        )
        self.assertEqual(
            self.store.export_descriptions()[0].content, "saved\r\nexactly\r\n"
        )

    def test_missing_registered_file_is_not_regenerated_from_old_metadata(self):
        description = self.store.create(
            "session", "space", "space", content="stale database text"
        )
        path = Path(description.file_path)
        path.unlink()
        self.store.close()
        self.store = DocumentStore(self.state_dir)
        with self.assertRaisesRegex(FileNotFoundError, "Description file is missing"):
            self.store.get("session", "space", "space")
        self.assertFalse(path.exists())
        self.assertTrue(self.store.delete("session", "space", "space"))

    def test_missing_file_mapping_can_be_read_and_removed(self):
        description = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        Path(description.file_path).unlink()
        mapping = self.store.get_mapping("session", "pane", "pane")
        self.assertEqual(mapping.description_id, description.description_id)
        self.assertEqual(mapping.tab_id, "tab")
        self.assertEqual(
            self.store.list_mappings("session", space_id="space", tab_id="tab"),
            [mapping],
        )
        self.assertTrue(
            self.store.delete(
                "session", "pane", "pane", description_id=mapping.description_id
            )
        )
        self.assertIsNone(self.store.get_mapping("session", "pane", "pane"))
        self.assertEqual(self.store.list_mappings("session"), [])
        self.assertFalse(Path(description.file_path).exists())

    def test_mapping_access_does_not_migrate_legacy_descriptions(self):
        with DescriptionStore(self.state_dir) as metadata:
            legacy = metadata.create(
                "session", "tab", "tab", space_id="space", content="legacy"
            )
        self.assertEqual(self.store.get_mapping("session", "tab", "tab"), legacy)
        self.assertEqual(self.store.list_mappings("session"), [legacy])
        self.assertFalse(self.store.file_path(legacy.description_id).exists())
        self.assertTrue(self.store.delete("session", "tab", "tab"))

    def test_mapping_filters_preserve_space_tab_and_session_boundaries(self):
        pane = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        self.store.create(
            "session", "pane", "other-tab", space_id="space", tab_id="other"
        )
        self.store.create(
            "session", "pane", "other-space", space_id="other", tab_id="tab"
        )
        self.store.create(
            "other", "pane", "pane", space_id="space", tab_id="tab"
        )
        Path(pane.file_path).unlink()
        matches = self.store.list_mappings(
            "session", space_id="space", tab_id="tab"
        )
        self.assertEqual(
            [item.description_id for item in matches], [pane.description_id]
        )
        self.assertEqual(
            matches[0], self.store.get_mapping("session", "pane", "pane")
        )

    def test_mapping_access_preserves_identifier_validation(self):
        for identifier in (None, "", "\0", 1):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    self.store.get_mapping(identifier, "space", "space")
                with self.assertRaises(ValueError):
                    self.store.get_mapping("session", "space", identifier)
                with self.assertRaises(ValueError):
                    self.store.list_mappings(identifier)
        for identifier in ("", "\0", 1):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    self.store.list_mappings("session", space_id=identifier)
                with self.assertRaises(ValueError):
                    self.store.list_mappings("session", tab_id=identifier)
        with self.assertRaises(ValueError):
            self.store.get_mapping("session", "invalid", "space")

    def test_cascade_deletion_removes_mappings_with_missing_files(self):
        for scope in ("tab", "space"):
            with self.subTest(scope=scope):
                space = self.store.create("session", "space", "space")
                tab = self.store.create("session", "tab", "tab", space_id="space")
                pane = self.store.create(
                    "session", "pane", "pane", space_id="space", tab_id="tab"
                )
                for document in (space, tab, pane):
                    Path(document.file_path).unlink()
                if scope == "tab":
                    self.assertEqual(self.store.delete_tab("session", "tab"), 2)
                    self.assertIsNone(
                        self.store.get_mapping("session", "tab", "tab")
                    )
                    self.assertIsNone(
                        self.store.get_mapping("session", "pane", "pane")
                    )
                    self.assertIsNotNone(
                        self.store.get_mapping("session", "space", "space")
                    )
                    self.store.delete("session", "space", "space")
                else:
                    self.assertEqual(self.store.delete_space("session", "space"), 3)
                self.assertEqual(self.store.list_mappings("session"), [])

    def test_export_migrates_records_from_all_sessions(self):
        with DescriptionStore(self.state_dir) as metadata:
            metadata.create("one", "space", "space", content="one")
            metadata.create("two", "space", "space", content="two")
        descriptions = self.store.export_descriptions()
        self.assertEqual([item.content for item in descriptions], ["one", "two"])
        self.assertTrue(all(Path(item.file_path).is_file() for item in descriptions))

    def test_delete_removes_only_requested_mapping_and_file(self):
        first = self.store.create("session", "space", "first", content="first")
        second = self.store.create("session", "space", "second", content="second")
        self.assertTrue(self.store.delete("session", "space", "first"))
        self.assertFalse(Path(first.file_path).exists())
        self.assertEqual(Path(second.file_path).read_text(), "second")
        self.assertIsNone(self.store.get("session", "space", "first"))
        self.assertFalse(self.store.delete("session", "space", "first"))
        Path(first.file_path).write_text("old editor saved after removal")
        self.assertIsNone(self.store.get("session", "space", "first"))
        replacement = self.store.create("session", "space", "first")
        self.assertNotEqual(replacement.file_path, first.file_path)
        self.assertEqual(replacement.content, "")

    def test_delete_tab_preserves_space_and_other_tab_files(self):
        space = self.store.create("session", "space", "space")
        tab = self.store.create("session", "tab", "tab", space_id="space")
        pane = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        other = self.store.create(
            "session", "pane", "other", space_id="space", tab_id="other-tab"
        )
        self.assertEqual(self.store.delete_tab("session", "tab"), 2)
        self.assertFalse(Path(tab.file_path).exists())
        self.assertFalse(Path(pane.file_path).exists())
        self.assertTrue(Path(space.file_path).exists())
        self.assertTrue(Path(other.file_path).exists())

    def test_delete_space_preserves_other_spaces_and_sessions(self):
        first = self.store.create("session", "space", "space")
        tab = self.store.create("session", "tab", "tab", space_id="space")
        pane = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        other_space = self.store.create("session", "space", "other")
        other_session = self.store.create("other-session", "space", "space")
        self.assertEqual(self.store.delete_space("session", "space"), 3)
        for item in (first, tab, pane):
            self.assertFalse(Path(item.file_path).exists())
        for item in (other_space, other_session):
            self.assertTrue(Path(item.file_path).exists())

    def test_invalid_space_deletion_does_not_delete_session(self):
        description = self.store.create("session", "space", "space")
        with self.assertRaises(ValueError):
            self.store.delete_space("session", None)
        self.assertEqual(self.store.get("session", "space", "space"), description)

    def test_independent_stores_observe_plaintext_and_revision_checks(self):
        description = self.store.create("session", "space", "space")
        with DocumentStore(self.state_dir) as other:
            Path(description.file_path).write_text("native Vim write")
            self.assertEqual(
                other.get("session", "space", "space").content, "native Vim write"
            )
            self.store.save(
                "session",
                "space",
                "space",
                "legacy explicit write",
                description_id=description.description_id,
                expected_revision=description.revision,
            )
            with self.assertRaises(RevisionConflictError):
                other.save(
                    "session",
                    "space",
                    "space",
                    "stale",
                    description_id=description.description_id,
                    expected_revision=description.revision,
                )

    def test_move_pane_preserves_file_identity_and_live_content(self):
        description = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab", content="old"
        )
        Path(description.file_path).write_text("current")
        moved = self.store.move_pane(
            "session", "pane", "new-pane", space_id="new-space", tab_id="new-tab"
        )
        self.assertEqual(moved.file_path, description.file_path)
        self.assertEqual(moved.description_id, description.description_id)
        self.assertEqual(moved.content, "current")
        self.assertEqual(moved.space_id, "new-space")
        self.assertEqual(moved.tab_id, "new-tab")
        self.assertIsNone(self.store.get("session", "pane", "pane"))
        self.assertEqual(self.store.get("session", "pane", "new-pane"), moved)

    def test_legacy_save_checks_revision_and_writes_plaintext(self):
        description = self.store.create("session", "space", "space", content="old")
        saved = self.store.save(
            "session",
            "space",
            "space",
            "explicit save\n",
            description_id=description.description_id,
            expected_revision=description.revision,
        )
        self.assertEqual(saved.revision, description.revision + 1)
        self.assertEqual(Path(saved.file_path).read_text(), "explicit save\n")
        with self.assertRaises(RevisionConflictError):
            self.store.save(
                "session",
                "space",
                "space",
                "outdated editor",
                description_id=description.description_id,
                expected_revision=description.revision,
            )
        self.assertEqual(Path(saved.file_path).read_text(), "explicit save\n")

    def test_legacy_save_cannot_recreate_removed_description(self):
        description = self.store.create("session", "space", "space")
        self.store.delete("session", "space", "space")
        replacement = self.store.create("session", "space", "space")
        with self.assertRaises(StaleDescriptionError):
            self.store.save(
                "session",
                "space",
                "space",
                "stale text",
                description_id=description.description_id,
                expected_revision=description.revision,
            )
        self.assertEqual(Path(replacement.file_path).read_text(), "")
        self.assertFalse(Path(description.file_path).exists())

    def test_save_file_preserves_exact_utf8_and_updates_only_file_authority(self):
        description = self.store.create("session", "space", "space", content="old")
        content = "Привет 🌱\r\nline two\nlast line"
        saved = self.store.save_file(
            "session",
            "space",
            "space",
            content,
            description_id=description.description_id,
            expected_digest=hashlib.sha256(b"old").hexdigest(),
        )
        self.assertEqual(saved.content, content)
        self.assertEqual(Path(saved.file_path).read_bytes(), content.encode("utf-8"))
        self.assertEqual(stat.S_IMODE(Path(saved.file_path).stat().st_mode), 0o600)
        self.assertEqual(saved.revision, description.revision + 1)
        self.assertGreaterEqual(saved.updated_at, description.updated_at)
        self.assertEqual(saved.description_id, description.description_id)
        self.assertEqual(saved.file_path, description.file_path)
        with DescriptionStore(self.state_dir) as metadata:
            self.assertEqual(metadata.get("session", "space", "space").content, "old")
        self.store.close()
        self.store = DocumentStore(self.state_dir)
        self.assertEqual(self.store.get("session", "space", "space"), saved)

    def test_save_file_detects_external_changes_without_touching_other_files(self):
        description = self.store.create("session", "space", "space", content="old")
        other = self.store.create("session", "space", "other", content="unrelated")
        Path(description.file_path).write_bytes("external\r\n変更".encode("utf-8"))
        with self.assertRaises(DocumentConflictError) as failure:
            self.store.save_file(
                "session",
                "space",
                "space",
                "editor changes",
                description_id=description.description_id,
                expected_digest=hashlib.sha256(b"old").hexdigest(),
            )
        self.assertEqual(
            failure.exception.actual_digest,
            hashlib.sha256("external\r\n変更".encode("utf-8")).hexdigest(),
        )
        current = self.store.get("session", "space", "space")
        self.assertEqual(current.content, "external\r\n変更")
        self.assertEqual(current.revision, description.revision)
        self.assertEqual(current.updated_at, description.updated_at)
        self.assertEqual(self.store.get("session", "space", "other"), other)
        self.assertEqual(Path(other.file_path).read_text(), "unrelated")

    def test_save_file_uses_current_file_digest_instead_of_legacy_metadata(self):
        description = self.store.create("session", "space", "space", content="old")
        Path(description.file_path).write_bytes(b"external\r\n")
        saved = self.store.save_file(
            "session",
            "space",
            "space",
            "edited external text",
            description_id=description.description_id,
            expected_digest=hashlib.sha256(b"external\r\n").hexdigest(),
        )
        self.assertEqual(saved.content, "edited external text")
        self.assertEqual(saved.revision, description.revision + 1)

    def test_save_file_conflicts_between_independent_editors(self):
        description = self.store.create("session", "space", "space", content="old")
        with DocumentStore(self.state_dir) as other:
            first = self.store.save_file(
                "session",
                "space",
                "space",
                "first editor",
                description_id=description.description_id,
                expected_digest=hashlib.sha256(b"old").hexdigest(),
            )
            with self.assertRaises(DocumentConflictError):
                other.save_file(
                    "session",
                    "space",
                    "space",
                    "second editor",
                    description_id=description.description_id,
                    expected_digest=hashlib.sha256(b"old").hexdigest(),
                )
            self.assertEqual(other.get("session", "space", "space"), first)

    def test_save_file_forced_overwrite_still_checks_description_identity(self):
        description = self.store.create("session", "space", "space", content="old")
        Path(description.file_path).write_text("external")
        saved = self.store.save_file(
            "session",
            "space",
            "space",
            "confirmed editor changes",
            description_id=description.description_id,
            expected_digest=hashlib.sha256(b"old").hexdigest(),
            force=True,
        )
        self.assertEqual(saved.content, "confirmed editor changes")
        self.assertEqual(saved.revision, description.revision + 1)
        self.store.delete("session", "space", "space")
        replacement = self.store.create("session", "space", "space", content="new")
        with self.assertRaises(StaleDescriptionError):
            self.store.save_file(
                "session",
                "space",
                "space",
                "obsolete editor",
                description_id=description.description_id,
                expected_digest=hashlib.sha256(b"old").hexdigest(),
                force=True,
            )
        self.assertEqual(self.store.get("session", "space", "space"), replacement)
        self.assertFalse(Path(description.file_path).exists())

    def test_save_file_same_content_retry_does_not_rewrite_or_change_revision(self):
        description = self.store.create("session", "space", "space", content="old")
        arguments = {
            "description_id": description.description_id,
            "expected_digest": hashlib.sha256(b"old").hexdigest(),
        }
        saved = self.store.save_file(
            "session", "space", "space", "saved", **arguments
        )
        file_stat = Path(saved.file_path).stat()
        retried = self.store.save_file(
            "session", "space", "space", "saved", **arguments
        )
        self.assertEqual(retried, saved)
        current_stat = Path(saved.file_path).stat()
        self.assertEqual(current_stat.st_ino, file_stat.st_ino)
        self.assertEqual(current_stat.st_mtime_ns, file_stat.st_mtime_ns)

    def test_save_file_deleted_mapping_cannot_be_resurrected(self):
        description = self.store.create("session", "space", "space")
        self.store.delete("session", "space", "space")
        for force in (False, True):
            with self.subTest(force=force):
                with self.assertRaises(DescriptionNotFoundError):
                    self.store.save_file(
                        "session",
                        "space",
                        "space",
                        "obsolete editor",
                        description_id=description.description_id,
                        expected_digest=hashlib.sha256(b"").hexdigest(),
                        force=force,
                    )
                self.assertIsNone(self.store.get("session", "space", "space"))
                self.assertFalse(Path(description.file_path).exists())

    def test_save_file_recreated_mapping_cannot_be_changed_by_old_editor(self):
        description = self.store.create("session", "space", "space")
        self.store.delete("session", "space", "space")
        replacement = self.store.create("session", "space", "space", content="new")
        with self.assertRaises(StaleDescriptionError):
            self.store.save_file(
                "session",
                "space",
                "space",
                "obsolete editor",
                description_id=description.description_id,
                expected_digest=hashlib.sha256(b"").hexdigest(),
            )
        self.assertEqual(self.store.get("session", "space", "space"), replacement)
        self.assertFalse(Path(description.file_path).exists())

    def test_save_file_cannot_write_another_description_identity(self):
        description = self.store.create("session", "space", "space", content="one")
        other = self.store.create("session", "space", "other", content="two")
        with self.assertRaises(StaleDescriptionError):
            self.store.save_file(
                "session",
                "space",
                "space",
                "wrong target",
                description_id=other.description_id,
                expected_digest=hashlib.sha256(b"one").hexdigest(),
            )
        self.assertEqual(self.store.get("session", "space", "space"), description)
        self.assertEqual(self.store.get("session", "space", "other"), other)

    def test_save_file_missing_file_is_never_recreated(self):
        description = self.store.create("session", "space", "space", content="old")
        Path(description.file_path).unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.save_file(
                "session",
                "space",
                "space",
                "editor changes",
                description_id=description.description_id,
                expected_digest=hashlib.sha256(b"old").hexdigest(),
                force=True,
            )
        self.assertFalse(Path(description.file_path).exists())

    def test_save_file_invalid_digest_cannot_modify_file_even_when_forced(self):
        description = self.store.create("session", "space", "space", content="old")
        for digest in (None, 123, "", "0" * 63, "0" * 65, "g" * 64, " " * 64):
            for force in (False, True):
                with self.subTest(digest=digest, force=force):
                    with self.assertRaisesRegex(ValueError, "expected_digest"):
                        self.store.save_file(
                            "session",
                            "space",
                            "space",
                            "new",
                            description_id=description.description_id,
                            expected_digest=digest,
                            force=force,
                        )
        self.assertEqual(self.store.get("session", "space", "space"), description)

    def test_save_file_requires_boolean_force(self):
        description = self.store.create("session", "space", "space", content="old")
        for force in ("false", "true", 1, 0, None):
            with self.subTest(force=force):
                with self.assertRaisesRegex(ValueError, "force"):
                    self.store.save_file(
                        "session",
                        "space",
                        "space",
                        "new",
                        description_id=description.description_id,
                        expected_digest=hashlib.sha256(b"old").hexdigest(),
                        force=force,
                    )
        self.assertEqual(self.store.get("session", "space", "space"), description)

    def test_save_file_accepts_uppercase_digest(self):
        description = self.store.create("session", "space", "space", content="old")
        saved = self.store.save_file(
            "session",
            "space",
            "space",
            "new",
            description_id=description.description_id,
            expected_digest=hashlib.sha256(b"old").hexdigest().upper(),
        )
        self.assertEqual(saved.content, "new")

    def test_save_file_disk_write_error_preserves_previous_file_and_metadata(self):
        description = self.store.create("session", "space", "space", content="old")
        with patch(
            "herdr_description.documents.os.replace",
            side_effect=OSError("disk failure"),
        ):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.store.save_file(
                    "session",
                    "space",
                    "space",
                    "new",
                    description_id=description.description_id,
                    expected_digest=hashlib.sha256(b"old").hexdigest(),
                )
        self.assertEqual(self.store.get("session", "space", "space"), description)
        self.assertEqual(
            list(self.store.documents_dir.iterdir()), [Path(description.file_path)]
        )

    def test_save_file_fsync_error_does_not_report_success(self):
        description = self.store.create("session", "space", "space", content="old")
        with patch(
            "herdr_description.documents.os.fsync",
            side_effect=OSError("fsync failure"),
        ):
            with self.assertRaisesRegex(OSError, "fsync failure"):
                self.store.save_file(
                    "session",
                    "space",
                    "space",
                    "new",
                    description_id=description.description_id,
                    expected_digest=hashlib.sha256(b"old").hexdigest(),
                )
        self.assertEqual(self.store.get("session", "space", "space"), description)

    def test_save_file_directory_fsync_error_after_replace_does_not_report_success(self):
        description = self.store.create("session", "space", "space", content="old")
        with patch(
            "herdr_description.documents.os.fsync",
            side_effect=[None, OSError("directory fsync failure")],
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                self.store.save_file(
                    "session",
                    "space",
                    "space",
                    "new",
                    description_id=description.description_id,
                    expected_digest=hashlib.sha256(b"old").hexdigest(),
                )
        current = self.store.get("session", "space", "space")
        self.assertEqual(current.content, "new")
        self.assertEqual(current.revision, description.revision)
        self.assertEqual(current.updated_at, description.updated_at)
        self.assertEqual(
            list(self.store.documents_dir.iterdir()), [Path(description.file_path)]
        )

    def test_file_path_rejects_noncanonical_ids_and_traversal(self):
        for value in ("../outside", "/tmp/outside", "bad", "", None, "a/b"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.file_path(value)

    def test_preexisting_symlink_is_not_used_for_migration(self):
        with DescriptionStore(self.state_dir) as metadata:
            legacy = metadata.create("session", "space", "space", content="stale")
        outside = Path(self.directory.name) / "outside"
        outside.write_text("outside text")
        os.symlink(str(outside), str(self.store.file_path(legacy.description_id)))
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.store.get("session", "space", "space")
        self.assertEqual(outside.read_text(), "outside text")


if __name__ == "__main__":
    unittest.main()
