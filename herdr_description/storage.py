import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Union


class DescriptionStoreError(Exception):
    pass


class DescriptionExistsError(DescriptionStoreError):
    pass


class DescriptionNotFoundError(DescriptionStoreError):
    pass


class StaleDescriptionError(DescriptionStoreError):
    pass


class RevisionConflictError(DescriptionStoreError):
    def __init__(self, expected_revision: int, actual_revision: int):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Description revision changed from {} to {}".format(
                expected_revision, actual_revision
            )
        )


@dataclass(frozen=True)
class Description:
    session_id: str
    kind: str
    target_id: str
    space_id: str
    description_id: str
    content: str
    revision: int
    created_at: float
    updated_at: float
    tab_id: Optional[str] = None


class DescriptionStore:
    def __init__(self, state_dir: Union[str, Path], busy_timeout_ms: int = 5000):
        if not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a nonnegative integer")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.path = self.state_dir / "descriptions.sqlite3"
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute(
                "PRAGMA busy_timeout = {}".format(busy_timeout_ms)
            )
            mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode[0].lower() != "wal":
                raise DescriptionStoreError("The description database requires WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA fullfsync = ON")
            self._connection.execute("PRAGMA checkpoint_fullfsync = ON")
            self._initialize()
            self._restrict_database_files()
            self._sync_directory()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "DescriptionStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get(
        self, session_id: str, kind: str, target_id: str
    ) -> Optional[Description]:
        key = self._key(session_id, kind, target_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM descriptions "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                key,
            ).fetchone()
            return Description(**dict(row)) if row is not None else None

    def create(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        *,
        space_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        content: str = "",
    ) -> Description:
        key = self._key(session_id, kind, target_id)
        self._content(content)
        if kind == "space":
            if space_id is not None and space_id != target_id:
                raise ValueError("A space description must belong to its own space")
            space_id = target_id
        else:
            self._identifier(space_id, "space_id")
        if kind == "pane":
            self._identifier(tab_id, "tab_id")
        elif tab_id is not None:
            raise ValueError("Only a pane description can have a tab_id")
        now = time.time()
        description = Description(
            session_id=session_id,
            kind=kind,
            target_id=target_id,
            space_id=space_id,
            description_id=str(uuid.uuid4()),
            content=content,
            revision=1,
            created_at=now,
            updated_at=now,
            tab_id=tab_id,
        )
        with self._transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM descriptions "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                key,
            ).fetchone()
            if existing is not None:
                raise DescriptionExistsError(
                    "A description already exists for this {}".format(kind)
                )
            self._connection.execute(
                "INSERT INTO descriptions "
                "(session_id, kind, target_id, space_id, description_id, content, "
                "revision, created_at, updated_at, tab_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    description.session_id,
                    description.kind,
                    description.target_id,
                    description.space_id,
                    description.description_id,
                    description.content,
                    description.revision,
                    description.created_at,
                    description.updated_at,
                    description.tab_id,
                ),
            )
        return description

    def save(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        content: str,
        *,
        description_id: str,
        expected_revision: int,
    ) -> Description:
        key = self._key(session_id, kind, target_id)
        self._content(content)
        self._identifier(description_id, "description_id")
        self._revision(expected_revision)
        with self._transaction():
            current = self.get(*key)
            self._check_current(current, description_id, expected_revision)
            self._connection.execute(
                "UPDATE descriptions SET content = ?, revision = revision + 1, "
                "updated_at = ? "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                (content, time.time(), *key),
            )
            return self.get(*key)

    def list_descriptions(
        self,
        session_id: str,
        *,
        space_id: Optional[str] = None,
        tab_id: Optional[str] = None,
    ) -> List[Description]:
        self._identifier(session_id, "session_id")
        query = "SELECT * FROM descriptions WHERE session_id = ?"
        parameters = [session_id]
        if space_id is not None:
            self._identifier(space_id, "space_id")
            query += " AND space_id = ?"
            parameters.append(space_id)
        if tab_id is not None:
            self._identifier(tab_id, "tab_id")
            query += " AND tab_id = ?"
            parameters.append(tab_id)
        query += " ORDER BY space_id, kind, target_id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
            return [Description(**dict(row)) for row in rows]

    def export_descriptions(self) -> List[Description]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM descriptions "
                "ORDER BY session_id, space_id, kind, target_id"
            ).fetchall()
            return [Description(**dict(row)) for row in rows]

    def delete(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        *,
        description_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        key = self._key(session_id, kind, target_id)
        if description_id is not None:
            self._identifier(description_id, "description_id")
        if expected_revision is not None:
            self._revision(expected_revision)
        with self._transaction():
            current = self.get(*key)
            if current is None:
                return False
            self._check_current(current, description_id, expected_revision)
            self._connection.execute(
                "DELETE FROM descriptions "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                key,
            )
        return True

    def delete_space(self, session_id: str, space_id: str) -> int:
        self._identifier(session_id, "session_id")
        self._identifier(space_id, "space_id")
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM descriptions WHERE session_id = ? AND space_id = ?",
                (session_id, space_id),
            )
            return cursor.rowcount

    def delete_tab(self, session_id: str, tab_id: str) -> int:
        self._identifier(session_id, "session_id")
        self._identifier(tab_id, "tab_id")
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM descriptions WHERE session_id = ? "
                "AND ((kind = 'tab' AND target_id = ?) "
                "OR (kind = 'pane' AND tab_id = ?))",
                (session_id, tab_id, tab_id),
            )
            return cursor.rowcount

    def move_pane(
        self,
        session_id: str,
        old_target_id: str,
        new_target_id: str,
        *,
        space_id: str,
        tab_id: str,
    ) -> Optional[Description]:
        old_key = self._key(session_id, "pane", old_target_id)
        new_key = self._key(session_id, "pane", new_target_id)
        self._identifier(space_id, "space_id")
        self._identifier(tab_id, "tab_id")
        with self._transaction():
            current = self.get(*old_key)
            if current is None:
                return None
            if old_key != new_key and self.get(*new_key) is not None:
                raise DescriptionExistsError(
                    "A description already exists for the destination pane"
                )
            self._connection.execute(
                "UPDATE descriptions SET target_id = ?, space_id = ?, tab_id = ?, "
                "revision = revision + 1, updated_at = ? "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                (new_target_id, space_id, tab_id, time.time(), *old_key),
            )
            return self.get(*new_key)

    def _initialize(self) -> None:
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise DescriptionStoreError(
                    "Unsupported description database version: {}".format(version)
                )
            old_table = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'descriptions'"
            ).fetchone()
            indexes = []
            if version < 2 and old_table is not None:
                indexes = self._connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'descriptions' AND sql IS NOT NULL"
                ).fetchall()
                self._connection.execute(
                    "ALTER TABLE descriptions RENAME TO descriptions_v1"
                )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS descriptions ("
                "session_id TEXT NOT NULL, "
                "kind TEXT NOT NULL CHECK (kind IN ('space', 'tab', 'pane')), "
                "target_id TEXT NOT NULL, "
                "space_id TEXT NOT NULL, "
                "description_id TEXT NOT NULL UNIQUE, "
                "content TEXT NOT NULL, "
                "revision INTEGER NOT NULL CHECK (revision >= 1), "
                "created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, "
                "tab_id TEXT, "
                "PRIMARY KEY (session_id, kind, target_id), "
                "CHECK (kind != 'space' OR target_id = space_id), "
                "CHECK ((kind = 'pane' AND tab_id IS NOT NULL "
                "AND length(tab_id) > 0) OR (kind != 'pane' AND tab_id IS NULL)))"
            )
            if version < 2 and old_table is not None:
                self._connection.execute(
                    "INSERT INTO descriptions "
                    "(session_id, kind, target_id, space_id, description_id, content, "
                    "revision, created_at, updated_at) "
                    "SELECT session_id, kind, target_id, space_id, description_id, "
                    "content, revision, created_at, updated_at FROM descriptions_v1"
                )
                self._connection.execute("DROP TABLE descriptions_v1")
                for index in indexes:
                    self._connection.execute(index[0])
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS descriptions_by_space "
                "ON descriptions (session_id, space_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS descriptions_by_tab "
                "ON descriptions (session_id, tab_id)"
            )
            self._connection.execute("PRAGMA user_version = 2")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _restrict_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                if not suffix:
                    raise

    def _sync_directory(self) -> None:
        descriptor = os.open(
            str(self.state_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("{} must be a nonempty string without NUL".format(name))

    @classmethod
    def _key(cls, session_id: str, kind: str, target_id: str):
        cls._identifier(session_id, "session_id")
        cls._identifier(target_id, "target_id")
        if kind not in ("space", "tab", "pane"):
            raise ValueError("kind must be 'space', 'tab', or 'pane'")
        return session_id, kind, target_id

    @staticmethod
    def _content(value: str) -> None:
        if not isinstance(value, str):
            raise ValueError("content must be a string")

    @staticmethod
    def _revision(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("expected_revision must be a positive integer")

    @staticmethod
    def _check_current(
        current: Optional[Description],
        description_id: Optional[str],
        expected_revision: Optional[int],
    ) -> None:
        if current is None:
            raise DescriptionNotFoundError("The description has been removed")
        if description_id is not None and current.description_id != description_id:
            raise StaleDescriptionError("The description has been replaced")
        if expected_revision is not None and current.revision != expected_revision:
            raise RevisionConflictError(expected_revision, current.revision)
