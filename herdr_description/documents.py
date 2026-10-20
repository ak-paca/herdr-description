import hashlib
import os
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Union

from .storage import Description, DescriptionStore, DescriptionStoreError


class DocumentConflictError(DescriptionStoreError):
    def __init__(self, expected_digest: str, actual_digest: str):
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            "Description file changed since editing began. Reload it or "
            "confirm overwriting the saved changes."
        )


@dataclass(frozen=True)
class Document(Description):
    file_path: str = ""


class DocumentStore:
    def __init__(self, state_dir: Union[str, Path], busy_timeout_ms: int = 5000):
        self._metadata = DescriptionStore(state_dir, busy_timeout_ms)
        self.state_dir = self._metadata.state_dir
        self.path = self._metadata.path
        self.documents_dir = self.state_dir / "descriptions"
        try:
            self.documents_dir.mkdir(mode=0o700, exist_ok=True)
            self.documents_dir.chmod(0o700)
            self._sync_directory(self.state_dir)
            with self._metadata._transaction():
                self._metadata._connection.execute(
                    "CREATE TABLE IF NOT EXISTS description_files ("
                    "description_id TEXT PRIMARY KEY)"
                )
        except BaseException:
            self._metadata.close()
            raise

    def __enter__(self) -> "DocumentStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._metadata.close()

    def file_path(self, description_id: str) -> Path:
        if not isinstance(description_id, str):
            raise ValueError("description_id must be a canonical UUID")
        try:
            parsed = uuid.UUID(description_id)
        except ValueError:
            raise ValueError("description_id must be a canonical UUID") from None
        if str(parsed) != description_id:
            raise ValueError("description_id must be a canonical UUID")
        return self.documents_dir / (description_id + ".txt")

    def get(
        self, session_id: str, kind: str, target_id: str
    ) -> Optional[Document]:
        with self._metadata._transaction():
            description = self._metadata.get(session_id, kind, target_id)
            return self._document(description) if description is not None else None

    def get_mapping(
        self, session_id: str, kind: str, target_id: str
    ) -> Optional[Description]:
        return self._metadata.get(session_id, kind, target_id)

    def list_mappings(
        self,
        session_id: str,
        *,
        space_id: Optional[str] = None,
        tab_id: Optional[str] = None,
    ) -> List[Description]:
        return self._metadata.list_descriptions(
            session_id, space_id=space_id, tab_id=tab_id
        )

    def create(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        *,
        space_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        content: str = "",
    ) -> Document:
        description = self._metadata.create(
            session_id,
            kind,
            target_id,
            space_id=space_id,
            tab_id=tab_id,
            content=content,
        )
        with self._metadata._transaction():
            current = self._metadata.get(session_id, kind, target_id)
            self._metadata._check_current(current, description.description_id, None)
            return self._document(current)

    def list_descriptions(
        self,
        session_id: str,
        *,
        space_id: Optional[str] = None,
        tab_id: Optional[str] = None,
    ) -> List[Document]:
        with self._metadata._transaction():
            return [
                self._document(description)
                for description in self._metadata.list_descriptions(
                    session_id, space_id=space_id, tab_id=tab_id
                )
            ]

    def export_descriptions(self) -> List[Document]:
        with self._metadata._transaction():
            return [
                self._document(description)
                for description in self._metadata.export_descriptions()
            ]

    def save(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        content: str,
        *,
        description_id: str,
        expected_revision: int,
    ) -> Document:
        key = self._metadata._key(session_id, kind, target_id)
        self._metadata._content(content)
        self.file_path(description_id)
        self._metadata._revision(expected_revision)
        with self._metadata._transaction():
            current = self._metadata.get(*key)
            self._metadata._check_current(current, description_id, expected_revision)
            document = self._document(current)
            self._write_file(Path(document.file_path), content, replace=True)
            self._metadata._connection.execute(
                "UPDATE descriptions SET content = ?, revision = revision + 1, "
                "updated_at = ? "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                (content, time.time(), *key),
            )
            return self._document(self._metadata.get(*key))

    def delete(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        *,
        description_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        key = self._metadata._key(session_id, kind, target_id)
        if description_id is not None:
            self._metadata._identifier(description_id, "description_id")
        if expected_revision is not None:
            self._metadata._revision(expected_revision)
        with self._metadata._transaction():
            current = self._metadata.get(*key)
            if current is None:
                return False
            self._metadata._check_current(
                current, description_id, expected_revision
            )
            self._delete_metadata([current])
        self._delete_files([current])
        return True

    def save_file(
        self,
        session_id: str,
        kind: str,
        target_id: str,
        content: str,
        *,
        description_id: str,
        expected_digest: str,
        force: bool = False,
    ) -> Document:
        key = self._metadata._key(session_id, kind, target_id)
        self._metadata._content(content)
        self.file_path(description_id)
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in expected_digest
            )
        ):
            raise ValueError("expected_digest must be a SHA256 hexadecimal digest")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        with self._metadata._transaction():
            current = self._metadata.get(*key)
            self._metadata._check_current(current, description_id, None)
            document = self._document(current)
            actual_digest = hashlib.sha256(
                document.content.encode("utf-8")
            ).hexdigest()
            if document.content == content:
                return document
            if actual_digest != expected_digest.lower() and not force:
                raise DocumentConflictError(expected_digest, actual_digest)
            self._write_file(Path(document.file_path), content, replace=True)
            self._metadata._connection.execute(
                "UPDATE descriptions SET revision = revision + 1, updated_at = ? "
                "WHERE session_id = ? AND kind = ? AND target_id = ?",
                (time.time(), *key),
            )
            return self._document(self._metadata.get(*key))

    def delete_tab(self, session_id: str, tab_id: str) -> int:
        self._metadata._identifier(tab_id, "tab_id")
        with self._metadata._transaction():
            descriptions = [
                description
                for description in self._metadata.list_descriptions(session_id)
                if (
                    description.kind == "tab" and description.target_id == tab_id
                )
                or (description.kind == "pane" and description.tab_id == tab_id)
            ]
            self._delete_metadata(descriptions)
        self._delete_files(descriptions)
        return len(descriptions)

    def delete_space(self, session_id: str, space_id: str) -> int:
        self._metadata._identifier(space_id, "space_id")
        with self._metadata._transaction():
            descriptions = self._metadata.list_descriptions(
                session_id, space_id=space_id
            )
            self._delete_metadata(descriptions)
        self._delete_files(descriptions)
        return len(descriptions)

    def move_pane(
        self,
        session_id: str,
        old_target_id: str,
        new_target_id: str,
        *,
        space_id: str,
        tab_id: str,
    ) -> Optional[Document]:
        description = self._metadata.move_pane(
            session_id,
            old_target_id,
            new_target_id,
            space_id=space_id,
            tab_id=tab_id,
        )
        if description is None:
            return None
        with self._metadata._transaction():
            current = self._metadata.get(session_id, "pane", new_target_id)
            self._metadata._check_current(current, description.description_id, None)
            return self._document(current)

    def _document(self, description: Description) -> Document:
        path = self.file_path(description.description_id)
        registered = self._metadata._connection.execute(
            "SELECT 1 FROM description_files WHERE description_id = ?",
            (description.description_id,),
        ).fetchone()
        if registered is None:
            self._write_file(path, description.content, replace=False)
            self._metadata._connection.execute(
                "INSERT INTO description_files (description_id) VALUES (?)",
                (description.description_id,),
            )
        try:
            with path.open("r", encoding="utf-8", newline="") as source:
                content = source.read()
        except FileNotFoundError:
            raise FileNotFoundError(
                "Description file is missing: {}. Restore the file, or remove "
                "and recreate the description.".format(path)
            ) from None
        values = asdict(description)
        values.update(content=content, file_path=str(path))
        return Document(**values)

    def _delete_metadata(self, descriptions: List[Description]) -> None:
        for description in descriptions:
            self.file_path(description.description_id)
            self._metadata._connection.execute(
                "DELETE FROM description_files WHERE description_id = ?",
                (description.description_id,),
            )
            self._metadata._connection.execute(
                "DELETE FROM descriptions WHERE description_id = ?",
                (description.description_id,),
            )

    def _delete_files(self, descriptions: List[Description]) -> None:
        for description in descriptions:
            self.file_path(description.description_id).unlink(missing_ok=True)
        if descriptions:
            self._sync_directory(self.documents_dir)

    def _write_file(self, path: Path, content: str, *, replace: bool) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".description-", dir=str(self.documents_dir)
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            if replace:
                os.replace(str(temporary_path), str(path))
            else:
                try:
                    os.link(str(temporary_path), str(path))
                except FileExistsError:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise ValueError(
                            "Description path must be a regular file: {}".format(path)
                        ) from None
                    path.chmod(0o600)
            self._sync_directory(self.documents_dir)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
