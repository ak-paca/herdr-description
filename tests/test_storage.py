import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from herdr_description.storage import (
    DescriptionExistsError,
    DescriptionNotFoundError,
    DescriptionStore,
    RevisionConflictError,
    StaleDescriptionError,
)


class DescriptionStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.directory.name) / "descriptions"
        self.store = DescriptionStore(self.state_dir)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def save(self, description, content, store=None):
        return (store or self.store).save(
            description.session_id,
            description.kind,
            description.target_id,
            content,
            description_id=description.description_id,
            expected_revision=description.revision,
        )

    def test_empty_description_is_distinct_from_absent(self):
        self.assertIsNone(self.store.get("session", "space", "space"))
        created = self.store.create("session", "space", "space")
        self.assertEqual(created.content, "")
        self.assertEqual(created.space_id, "space")
        self.assertEqual(self.store.get("session", "space", "space"), created)
        with self.assertRaises(DescriptionExistsError):
            self.store.create("session", "space", "space")
        self.assertTrue(self.store.delete("session", "space", "space"))
        self.assertFalse(self.store.delete("session", "space", "space"))
        self.assertIsNone(self.store.get("session", "space", "space"))

    def test_content_and_mapping_survive_reopening(self):
        created = self.store.create(
            "session", "tab", "tab", space_id="space", content="initial"
        )
        saved = self.save(created, "Notes\nwith Unicode: Привет 👋\n")
        self.store.close()
        self.store = DescriptionStore(self.state_dir)
        restored = self.store.get("session", "tab", "tab")
        self.assertEqual(restored, saved)
        self.assertEqual(restored.revision, 2)
        self.assertEqual(restored.description_id, created.description_id)

    def test_pane_content_and_associations_survive_reopening(self):
        created = self.store.create(
            "session",
            "pane",
            "pane",
            space_id="space",
            tab_id="tab",
            content="initial",
        )
        self.assertEqual(created.tab_id, "tab")
        saved = self.save(created, "pane notes")
        self.store.close()
        self.store = DescriptionStore(self.state_dir)
        self.assertEqual(self.store.get("session", "pane", "pane"), saved)
        self.assertEqual(
            self.store.list_descriptions("session", space_id="space", tab_id="tab"),
            [saved],
        )
        self.assertEqual(self.store.list_descriptions("session", tab_id="other"), [])
        self.assertEqual(
            self.store.list_descriptions("session", space_id="other", tab_id="tab"),
            [],
        )
        with self.assertRaises(DescriptionExistsError):
            self.store.create(
                "session", "pane", "pane", space_id="space", tab_id="tab"
            )
        self.assertTrue(self.store.delete("session", "pane", "pane"))
        self.assertFalse(self.store.delete("session", "pane", "pane"))
        with self.assertRaises(DescriptionNotFoundError):
            self.save(saved, "late autosave")

    def test_independent_writers_reject_old_revision(self):
        created = self.store.create("session", "space", "space")
        with DescriptionStore(self.state_dir) as other:
            old = other.get("session", "space", "space")
            self.save(created, "first writer")
            with self.assertRaises(RevisionConflictError) as raised:
                self.save(old, "second writer", store=other)
            self.assertEqual(raised.exception.expected_revision, 1)
            self.assertEqual(raised.exception.actual_revision, 2)
            current = other.get("session", "space", "space")
            self.assertEqual(current.content, "first writer")
            self.save(current, "retry", store=other)
        self.assertEqual(self.store.get("session", "space", "space").content, "retry")

    def test_simultaneous_writers_have_one_winner(self):
        created = self.store.create("session", "space", "space")
        barrier = threading.Barrier(2)
        results = []

        def write(content):
            with DescriptionStore(self.state_dir) as store:
                barrier.wait(timeout=5)
                try:
                    results.append(self.save(created, content, store=store))
                except RevisionConflictError as error:
                    results.append(error)

        threads = [
            threading.Thread(target=write, args=("one",)),
            threading.Thread(target=write, args=("two",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sum(isinstance(result, RevisionConflictError) for result in results), 1
        )
        self.assertEqual(self.store.get("session", "space", "space").revision, 2)

    def test_deleted_description_cannot_be_resurrected(self):
        created = self.store.create("session", "space", "space")
        self.store.delete("session", "space", "space")
        with self.assertRaises(DescriptionNotFoundError):
            self.save(created, "late autosave")
        self.assertIsNone(self.store.get("session", "space", "space"))

    def test_recreated_description_rejects_old_editor_and_delete(self):
        old = self.store.create("session", "space", "space")
        self.store.delete("session", "space", "space")
        new = self.store.create("session", "space", "space", content="replacement")
        self.assertNotEqual(new.description_id, old.description_id)
        with self.assertRaises(StaleDescriptionError):
            self.save(old, "late autosave")
        with self.assertRaises(StaleDescriptionError):
            self.store.delete(
                "session", "space", "space", description_id=old.description_id
            )
        self.assertEqual(self.store.get("session", "space", "space"), new)

    def test_stale_delete_rejects_newer_content(self):
        created = self.store.create("session", "space", "space")
        updated = self.save(created, "new content")
        with self.assertRaises(RevisionConflictError):
            self.store.delete(
                "session",
                "space",
                "space",
                description_id=created.description_id,
                expected_revision=created.revision,
            )
        self.assertEqual(self.store.get("session", "space", "space"), updated)

    def test_removing_space_description_preserves_tab_description(self):
        self.store.create("session", "space", "space")
        tab = self.store.create("session", "tab", "tab", space_id="space")
        self.store.delete("session", "space", "space")
        self.assertEqual(self.store.list_descriptions("session"), [tab])

    def test_removing_tab_description_preserves_pane_description(self):
        self.store.create("session", "tab", "tab", space_id="space")
        pane = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        self.store.delete("session", "tab", "tab")
        self.assertEqual(self.store.list_descriptions("session"), [pane])

    def test_deleted_tab_cascades_panes_and_isolates_sessions(self):
        space = self.store.create("session", "space", "space")
        self.store.create("session", "tab", "tab", space_id="space")
        pane = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        self.store.create(
            "session", "pane", "pane2", space_id="space", tab_id="tab"
        )
        other_tab = self.store.create("session", "tab", "other-tab", space_id="space")
        other_pane = self.store.create(
            "session", "pane", "other-pane", space_id="space", tab_id="other-tab"
        )
        other_session = self.store.create(
            "other", "pane", "pane", space_id="space", tab_id="tab"
        )
        self.assertEqual(self.store.delete_tab("session", "tab"), 3)
        self.assertEqual(self.store.delete_tab("session", "tab"), 0)
        self.assertEqual(
            self.store.list_descriptions("session"), [other_pane, space, other_tab]
        )
        self.assertEqual(self.store.list_descriptions("other"), [other_session])
        with self.assertRaises(DescriptionNotFoundError):
            self.save(pane, "late autosave")

    def test_deleted_tab_cascades_panes_without_tab_description(self):
        self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        self.assertEqual(self.store.delete_tab("session", "tab"), 1)
        self.assertEqual(self.store.list_descriptions("session"), [])

    def test_deleted_space_cascades_tabs_and_isolates_sessions(self):
        self.store.create("session", "space", "space")
        tab = self.store.create("session", "tab", "tab", space_id="space")
        self.store.create("session", "tab", "tab2", space_id="space")
        self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        kept = self.store.create("session", "tab", "other-tab", space_id="other-space")
        other = self.store.create("other-session", "space", "space")
        self.assertEqual(self.store.delete_space("session", "space"), 4)
        self.assertEqual(self.store.delete_space("session", "space"), 0)
        self.assertEqual(self.store.list_descriptions("session"), [kept])
        self.assertEqual(self.store.list_descriptions("other-session"), [other])
        with self.assertRaises(DescriptionNotFoundError):
            self.save(tab, "late autosave")

    def test_listing_filters_space_and_never_changes_mapping(self):
        one = self.store.create("session", "tab", "tab", space_id="one")
        two = self.store.create("session", "space", "two")
        other = self.store.create("other", "tab", "tab", space_id="one")
        self.assertEqual(self.store.list_descriptions("session", space_id="one"), [one])
        self.assertEqual(self.store.list_descriptions("session"), [one, two])
        self.assertEqual(self.store.list_descriptions("other"), [other])
        self.assertEqual(self.store.list_descriptions("missing"), [])
        self.assertEqual(
            self.store.list_descriptions("session", space_id="missing"), []
        )

    def test_tab_mapping_exists_without_space_description(self):
        tab = self.store.create("session", "tab", "tab", space_id="space")
        self.assertEqual(self.store.list_descriptions("session", space_id="space"), [tab])
        self.assertEqual(self.store.delete_space("session", "space"), 1)

    def test_moving_pane_preserves_description_and_rejects_old_writes(self):
        for space_id in ("space", "other-space"):
            with self.subTest(space_id=space_id):
                created = self.store.create(
                    space_id,
                    "pane",
                    "old-pane",
                    space_id="space",
                    tab_id="tab",
                    content="work in progress",
                )
                moved = self.store.move_pane(
                    space_id,
                    "old-pane",
                    "new-pane",
                    space_id=space_id,
                    tab_id="other-tab",
                )
                self.assertEqual(moved.description_id, created.description_id)
                self.assertEqual(moved.content, created.content)
                self.assertEqual(moved.created_at, created.created_at)
                self.assertGreaterEqual(moved.updated_at, created.updated_at)
                self.assertEqual(moved.revision, created.revision + 1)
                self.assertEqual(moved.space_id, space_id)
                self.assertEqual(moved.tab_id, "other-tab")
                self.assertEqual(moved.target_id, "new-pane")
                self.assertIsNone(self.store.get(space_id, "pane", "old-pane"))
                self.assertEqual(
                    self.store.list_descriptions(space_id, tab_id="other-tab"), [moved]
                )
                self.assertEqual(
                    self.store.list_descriptions(space_id, tab_id="tab"), []
                )
                with self.assertRaises(DescriptionNotFoundError):
                    self.save(created, "late autosave")
                saved = self.save(moved, "continued")
                self.assertEqual(saved.content, "continued")
                self.assertEqual(self.store.delete_tab(space_id, "tab"), 0)
                self.assertEqual(self.store.delete_tab(space_id, "other-tab"), 1)

    def test_moving_pane_with_same_target_rejects_old_revision(self):
        created = self.store.create(
            "session", "pane", "pane", space_id="space", tab_id="tab"
        )
        moved = self.store.move_pane(
            "session", "pane", "pane", space_id="other-space", tab_id="other-tab"
        )
        self.assertEqual(moved.description_id, created.description_id)
        self.assertEqual(moved.target_id, created.target_id)
        self.assertEqual(moved.space_id, "other-space")
        self.assertEqual(moved.tab_id, "other-tab")
        self.assertEqual(moved.revision, 2)
        with self.assertRaises(RevisionConflictError):
            self.save(created, "late autosave")
        self.assertEqual(self.store.delete_space("session", "space"), 0)
        self.assertEqual(self.store.get("session", "pane", "pane"), moved)
        self.assertEqual(self.store.delete_space("session", "other-space"), 1)

    def test_moving_pane_rejects_destination_conflicts_without_changes(self):
        source = self.store.create(
            "session", "pane", "source", space_id="space", tab_id="tab"
        )
        destination = self.store.create(
            "session", "pane", "destination", space_id="other", tab_id="other-tab"
        )
        before = self.store.export_descriptions()
        with self.assertRaises(DescriptionExistsError):
            self.store.move_pane(
                "session", "source", "destination", space_id="other", tab_id="other-tab"
            )
        self.assertEqual(self.store.export_descriptions(), before)
        self.assertEqual(self.store.get("session", "pane", "source"), source)
        self.assertEqual(self.store.get("session", "pane", "destination"), destination)

    def test_moving_missing_pane_does_not_create_or_change_other_session(self):
        other = self.store.create(
            "other", "pane", "source", space_id="space", tab_id="tab"
        )
        self.assertIsNone(
            self.store.move_pane(
                "session", "source", "destination", space_id="space", tab_id="tab"
            )
        )
        self.assertEqual(self.store.export_descriptions(), [other])

    def test_export_includes_all_sessions_and_preserves_empty_descriptions(self):
        self.assertEqual(self.store.export_descriptions(), [])
        other = self.store.create("z-session", "space", "space", content="other session")
        tab = self.store.create("a-session", "tab", "tab", space_id="space", content="tab")
        space = self.store.create("a-session", "space", "space", content="")
        exported = self.store.export_descriptions()
        self.assertEqual(exported, [space, tab, other])
        exported.clear()
        self.assertEqual(self.store.export_descriptions(), [space, tab, other])
        self.store.close()
        self.store = DescriptionStore(self.state_dir)
        self.assertEqual(self.store.export_descriptions(), [space, tab, other])

    def test_invalid_inputs_do_not_create_descriptions(self):
        invalid = [
            ("", "space", "space", None),
            ("session", "unsupported", "target", None),
            ("session", "tab", "", "space"),
            ("session", "tab", "tab", None),
            ("session", "tab", "tab", ""),
            ("session", "space", "space", "other"),
            ("session\0", "space", "space", None),
        ]
        for session, kind, target, space in invalid:
            with self.subTest(session=session, kind=kind, target=target, space=space):
                with self.assertRaises(ValueError):
                    self.store.create(session, kind, target, space_id=space)
        with self.assertRaises(ValueError):
            self.store.create("session", "space", "space", content=None)
        self.assertEqual(self.store.list_descriptions("session"), [])

    def test_pane_requires_tab_and_space_associations(self):
        invalid = [
            ("pane", None, "tab"),
            ("pane", "", "tab"),
            ("pane", "space", None),
            ("pane", "space", ""),
            ("pane", "space", "tab\0"),
            ("space", None, "tab"),
            ("tab", "space", "tab"),
        ]
        for kind, space_id, tab_id in invalid:
            with self.subTest(kind=kind, space_id=space_id, tab_id=tab_id):
                with self.assertRaises(ValueError):
                    self.store.create(
                        "session", kind, "target", space_id=space_id, tab_id=tab_id
                    )
        with self.assertRaises(ValueError):
            self.store.list_descriptions("session", tab_id="")
        with self.assertRaises(ValueError):
            self.store.delete_tab("session", "")
        with self.assertRaises(ValueError):
            self.store.move_pane(
                "session", "pane", "pane2", space_id="space", tab_id=""
            )
        self.assertEqual(self.store.export_descriptions(), [])

    def test_parameterized_identifiers(self):
        session = "'; DROP TABLE descriptions; --"
        target = "../same name ' \"\n"
        created = self.store.create(session, "space", target, content="safe")
        self.assertEqual(self.store.get(session, "space", target), created)
        self.assertEqual(self.store.list_descriptions(session), [created])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_database_directory_and_files_are_private(self):
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        for path in self.state_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_committed_content_survives_abrupt_process_exit(self):
        program = """
import os
import sys
from herdr_description.storage import DescriptionStore
store = DescriptionStore(sys.argv[1])
item = store.create("crashed", "tab", "tab", space_id="space")
store.save("crashed", "tab", "tab", "last committed autosave",
           description_id=item.description_id, expected_revision=item.revision)
os._exit(23)
"""
        result = subprocess.run(
            [sys.executable, "-c", program, str(self.state_dir)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.store.close()
        self.store = DescriptionStore(self.state_dir)
        restored = self.store.get("crashed", "tab", "tab")
        self.assertEqual(restored.content, "last committed autosave")
        self.assertEqual(restored.space_id, "space")
        self.assertEqual(restored.revision, 2)


class DescriptionStoreMigrationTest(unittest.TestCase):
    def test_version_one_database_preserves_rows_and_indexes_after_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descriptions.sqlite3"
            with sqlite3.connect(str(path)) as connection:
                connection.executescript(
                    """
CREATE TABLE descriptions (
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('space', 'tab')),
    target_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    description_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, kind, target_id),
    CHECK (kind != 'space' OR target_id = space_id)
);
CREATE INDEX descriptions_by_space ON descriptions (session_id, space_id);
CREATE INDEX descriptions_by_revision ON descriptions (revision);
PRAGMA user_version = 1;
"""
                )
                old_rows = [
                    (
                        "session",
                        "space",
                        "space",
                        "space",
                        "space-uuid",
                        "",
                        1,
                        1.0,
                        1.0,
                    ),
                    (
                        "session",
                        "tab",
                        "tab",
                        "space",
                        "tab-uuid",
                        "saved notes\nПривет",
                        8,
                        2.0,
                        3.0,
                    ),
                ]
                connection.executemany(
                    "INSERT INTO descriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    old_rows,
                )
            with DescriptionStore(directory) as store:
                migrated = store.export_descriptions()
                self.assertEqual(len(migrated), 2)
                for row, item in zip(old_rows, migrated):
                    self.assertEqual(
                        (
                            item.session_id,
                            item.kind,
                            item.target_id,
                            item.space_id,
                            item.description_id,
                            item.content,
                            item.revision,
                            item.created_at,
                            item.updated_at,
                        ),
                        row,
                    )
                    self.assertIsNone(item.tab_id)
                pane = store.create(
                    "session", "pane", "pane", space_id="space", tab_id="tab"
                )
            with DescriptionStore(directory) as reopened:
                self.assertEqual(reopened.get("session", "pane", "pane"), pane)
                self.assertEqual(reopened.get("session", "tab", "tab"), migrated[1])
                self.assertEqual(reopened.get("session", "space", "space"), migrated[0])
            with sqlite3.connect(str(path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 2
                )
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(descriptions)")
                }
                self.assertTrue(
                    {
                        "descriptions_by_space",
                        "descriptions_by_tab",
                        "descriptions_by_revision",
                    }.issubset(indexes)
                )


if __name__ == "__main__":
    unittest.main()
