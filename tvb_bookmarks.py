"""Bookmarks of media files: a SQLite database in Tern's plugin data, one row per bookmark, keyed by the file's real
path. The player reads and writes it; Tern's own database pane opens it (the palette's "Media: Open bookmarks"). The
BookmarksMixin is a mixin for Player: its methods for adding, removing and jumping marks, and the row of marks."""
import contextlib
import math
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
RANGES_SCHEMA = """CREATE TABLE IF NOT EXISTS ranges (
    path TEXT PRIMARY KEY,
    at_in REAL,
    at_out REAL
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
            con.execute(RANGES_SCHEMA)                                 # a database made before ranges gets the table
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

    def range_of(self, path):
        """(at_in, at_out) of the file at `path`: the in and out points of its range, None for one not set."""
        if not os.path.isfile(self.db):
            return None, None
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=2.0)) as con:
                row = con.execute("SELECT at_in, at_out FROM ranges WHERE path = ?",
                                  (os.path.realpath(path),)).fetchone()
        except sqlite3.OperationalError:                               # no ranges table yet
            return None, None
        return (None, None) if row is None else tuple(None if v is None else float(v) for v in row)

    def set_range(self, path, at_in, at_out):
        """Remember the range of the file at `path`; with no end set, forget it."""
        path = os.path.realpath(path)
        if at_in is None and at_out is None:
            if os.path.isfile(self.db):
                self._write("DELETE FROM ranges WHERE path = ?", (path,))
            return
        self._write("INSERT OR REPLACE INTO ranges (path, at_in, at_out) VALUES (?, ?, ?)", (path, at_in, at_out))


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


class BookmarksMixin:
    """Player mixin providing bookmark methods: load_marks, add_mark, remove_mark, jump_mark, draw_marks, marks_rows
    and mark_key. Uses only self.*; never imports tern_video_block."""

    def load_marks(self):
        """The bookmarks and the range of the file playing, from the store; none without one."""
        self.marks = self.store.of(self.items[0]["path"]) if self.store else []
        self.range = self.store.range_of(self.items[0]["path"]) if self.store else (None, None)

    def _change_marks(self, write):
        """Write to the store, then show the bookmarks again: the row comes and goes with the first and the last."""
        if not self.store:
            self.note("no bookmarks database (pass --bookmarks FILE)")
            return
        had = bool(self.marks)
        try:
            write()
        except Exception as e:                                         # sqlite3.Error, OSError: a note, play on
            self.note(f"bookmarks: {e}")
            return
        self.load_marks()
        if had != bool(self.marks):
            t = self.now()
            self.relayout(self.geom)
            self.seek(t)
        else:
            if self.wave is not None:
                self.wave.set_marks(m["at"] for m in self.marks)
                data = self.wave.frame(self.now())
                if data is not None:
                    self.show(data)
            self.status(force=True)

    def add_mark(self):
        """A bookmark at the current time, snapped to the frame."""
        at = int(math.floor(self.now() * self.fps + 1e-6)) / self.fps
        self._change_marks(lambda: self.store.add(self.items[0]["path"], at))

    def remove_mark(self):
        """Remove the nearest bookmark at or before the current time."""
        k = current(self.marks, self.now())
        if k >= 0:
            at = self.marks[k]["at"]
            self._change_marks(lambda: self.store.remove(self.items[0]["path"], at))

    def jump_mark(self, d):
        """To the next (d=1) or previous (d=-1) bookmark; back to this one first when it has played for a while."""
        if not self.marks:
            return
        t = self.now()
        k = current(self.marks, t)
        if not (d < 0 and k >= 0 and t - self.marks[k]["at"] > 0.5):
            k = min(max(k + d, 0), len(self.marks) - 1)
        self.seek(self.marks[k]["at"])

    def draw_marks(self, force=False):
        """The bookmarks row under the picture, the one the playhead has reached highlighted; no row without any."""
        if not self.marks or not self.geom:
            return
        length = round(self.duration)
        before, mid, after = row(self.marks, self.now(), self.geom["cols"] - 1,
                                 lambda at: self.clock(at, length))
        text = (f"\x1b[2m{before}\x1b[0m" + (f"\x1b[1;7m {mid} \x1b[0m" if mid else "")
                + f"\x1b[2m{after}\x1b[0m")
        if force or text != self.last_marks:
            self.out(f"\x1b[{self.marks_row};1H\x1b[2K{text}")
            self.last_marks = text

    def marks_rows(self, lay, geom):
        """The terminal row for bookmarks and the initial sentinel for draw_marks."""
        bottom = lay["row"] * geom["cell_h"] + lay["Y"] + lay["h"]
        rows = min(-(-bottom // geom["cell_h"]) + 1, self.status_row - 1)
        return rows, None

    def mark_key(self, s):
        """Bookmark keys: b adds, B removes, [ ] move between. True when handled."""
        if s == b"b":
            self.add_mark()
            return True
        if s == b"B":
            self.remove_mark()
            return True
        if s in (b"[", b"]"):
            self.jump_mark(1 if s == b"]" else -1)
            return True
        return False
