"""Bookmarks of media files: a SQLite database in Tern's plugin data, one row per bookmark, keyed by the file's real
path. The player reads and writes it; Tern's own database pane opens it (the palette's "Media: Open bookmarks")."""
import contextlib
import os
import shutil
import sqlite3
import subprocess

SCHEMA = """CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    at REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (path, at)
)"""
SEP = " \u00b7 "


def default_store():
    """Where Tern keeps this plugin's data (beside its plugins directory), or None without Tern."""
    tern = shutil.which("tern")
    if tern is None:
        return None
    r = subprocess.run([tern, "plugin", "dir"], capture_output=True, text=True)
    plugins = r.stdout.strip()
    if r.returncode != 0 or not plugins:
        return None
    return os.path.join(os.path.dirname(plugins), "plugin-data", "video-block", "bookmarks.db")


class Bookmarks:
    """The bookmarks in the database at `db`. Reading never creates it; the first bookmark does."""

    def __init__(self, db):
        self.db = db

    def _write(self, sql, args):
        os.makedirs(os.path.dirname(self.db) or ".", exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.db, timeout=2.0)) as con, con:
            con.execute(SCHEMA)
            con.execute(sql, args)

    def of(self, path):
        """[{at, label}] of the file at `path`, in time order."""
        if not os.path.isfile(self.db):
            return []
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=2.0)) as con:
                rows = con.execute("SELECT at, label FROM bookmarks WHERE path = ? ORDER BY at",
                                   (os.path.realpath(path),)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"at": float(at), "label": label or ""} for at, label in rows]

    def add(self, path, at, label=""):
        self._write("INSERT OR IGNORE INTO bookmarks (path, at, label) VALUES (?, ?, ?)",
                    (os.path.realpath(path), float(at), label))

    def remove(self, path, at):
        self._write("DELETE FROM bookmarks WHERE path = ? AND abs(at - ?) < 0.0005",
                    (os.path.realpath(path), float(at)))


def current(marks, t):
    """Index of the bookmark the playhead has reached (the last at or before t), or -1 before the first."""
    k = -1
    for i, m in enumerate(marks):
        if m["at"] <= t + 1e-6:
            k = i
    return k


def row(marks, t, width, clock):
    """The bookmarks row in `width` columns as (before, current, after): each bookmark its time (`clock(at)`) and
    label, as many as fit around the current one, an ellipsis where some are left out."""
    k = current(marks, t)
    names = [f"{clock(m['at'])} {m['label']}".strip() for m in marks]
    lo = hi = max(k, 0)
    room = width - 8 - len(names[lo])
    grew = True
    while grew:
        grew = False
        for j in (hi + 1, lo - 1):
            if 0 <= j < len(names) and not lo <= j <= hi and len(names[j]) + len(SEP) <= room:
                room -= len(names[j]) + len(SEP)
                lo, hi, grew = min(lo, j), max(hi, j), True
    shown = names[lo:hi + 1]
    at = k - lo
    before = SEP.join(shown[:at]) + (SEP if at > 0 else "") if k >= 0 else ""
    after = SEP.join(shown[at + 1:]) if k >= 0 else SEP.join(shown)
    if k >= 0 and after:
        after = SEP + after
    return (f" \u2691 {'… ' if lo > 0 else ''}{before}", names[k] if k >= 0 else "",
            f"{after}{' …' if hi < len(names) - 1 else ''}")
