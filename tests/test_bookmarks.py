"""Bookmarks: reading never creates the database, bookmarks are keyed by the file's real path and come back in time
order, adding the same moment twice keeps one, and the row keeps the current bookmark in view."""
import os

import tvb_bookmarks as TB


def clock(at):
    return f"{int(at) // 60:02d}:{int(at) % 60:02d}"


def test_reading_a_missing_database_leaves_it_missing(tmp_path):
    db = tmp_path / "sub" / "bookmarks.db"
    assert TB.Bookmarks(str(db)).of(str(tmp_path / "song.mp3")) == []
    assert not db.exists() and not db.parent.exists()


def test_bookmarks_are_per_real_file_in_time_order_without_duplicates(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    link = tmp_path / "link.mp3"
    os.symlink(song, link)
    store = TB.Bookmarks(str(tmp_path / "data" / "bookmarks.db"))
    store.add(str(song), 90.0)
    store.add(str(link), 12.5, "drop")
    store.add(str(song), 90.0)
    assert store.of(str(link)) == [{"at": 12.5, "label": "drop"}, {"at": 90.0, "label": ""}]
    assert store.of(str(tmp_path / "other.mp3")) == []
    store.remove(str(song), 12.5)
    assert store.of(str(song)) == [{"at": 90.0, "label": ""}]


def test_the_row_marks_the_bookmark_reached_and_keeps_it_in_view():
    marks = [{"at": float(60 * i), "label": ""} for i in range(20)]
    before, mid, after = TB.row(marks, 600.5, 60, clock)
    assert mid == "10:00"
    assert len(before) + len(mid) + 2 + len(after) <= 60
    assert before.startswith(" \u2691 \u2026") and after.endswith("\u2026")
    before, mid, after = TB.row(marks, 30.0, 200, clock)
    assert mid == "00:00" and "\u2026" not in after and after.count("\u00b7") == 19
    assert TB.row(marks[1:], 30.0, 200, clock)[1] == ""
