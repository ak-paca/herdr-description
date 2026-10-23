import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


class Runtime:
    def __init__(self, state_dir, socket_path):
        self.session_id = hashlib.sha256(os.fsencode(socket_path)).hexdigest()
        self.directory = Path(state_dir) / "runtime" / self.session_id
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / "runtime.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS views "
                "(tab_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS identities "
                "(workspace_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS name_indicators "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)"
            )
        self.database.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self):
        with (self.directory / "controller.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def view(self, tab_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM views WHERE tab_id = ?", (tab_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def views(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM views")]

    def put_view(self, view):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO views VALUES (?, ?)",
                (view["tab_id"], json.dumps(view)),
            )

    def remove_view(self, tab_id):
        with self.connect() as db:
            db.execute("DELETE FROM views WHERE tab_id = ?", (tab_id,))

    def request(self, tab_id, action, **values):
        view = self.view(tab_id)
        if view is None:
            raise RuntimeError("The description pane is not running.")
        view.update(values)
        view["request"] = {"id": time.time_ns(), "action": action}
        self.put_view(view)
        return view

    def identity(self, workspace_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM identities WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def identities(self):
        with self.connect() as db:
            return {
                row[0]: json.loads(row[1])
                for row in db.execute("SELECT workspace_id, data FROM identities")
            }

    def put_identity(self, workspace_id, identity):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO identities VALUES (?, ?)",
                (workspace_id, json.dumps(identity)),
            )

    def forget_identity(self, workspace_id):
        with self.connect() as db:
            db.execute(
                "DELETE FROM identities WHERE workspace_id = ?", (workspace_id,)
            )

    def indicator_state(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM name_indicators WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {"enabled": False, "entries": {}}

    def put_indicator_state(self, state):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO name_indicators VALUES (1, ?)",
                (json.dumps(state),),
            )
