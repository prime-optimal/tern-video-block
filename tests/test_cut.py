"""tern-video-block ranges and exports: i and o set the ends of a range, c cuts it and g makes a GIF of it (detached,
written to a hidden temporary file and moved into place, never over a file), the info row says what the file is, and the
up and down arrows move to the neighbouring media file of the folder."""

import json
import os
import re
import shutil
import struct
import subprocess

import pytest

import tern_video_block as TVB
import tvb_audio as AUDIO
import tvb_bookmarks as TB
import tvb_cut as CUT
from test_player import TERN_ANSWERS, _clip, _on_a_pty, needs_ff

needs_gifski = pytest.mark.skipif(shutil.which("gifski") is None, reason="gifski not available")


def _movie(path, seconds=4, size="640x360"):
    """A short clip with sound and a keyframe every 10 frames (so a copying cut lands within a third of a second)."""
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


class Stub(CUT.CutMixin):
    """What CutMixin needs of a Player, and nothing else."""

    def __init__(self, path, at_in=None, at_out=None, now=0.0, geom=None, **item):
        self.items = [{"path": str(path), "w": 640, "h": 360, "fps": 30.0, "duration": 4.0, "audio": True, **item}]
        self.fps, self.duration, self.store, self.t = 30.0, 4.0, None, now
        self.geom = geom or {"cols": 100, "rows": 30, "cell_w": 8, "cell_h": 16}
        self.notes, self.drawn = [], []
        self.out = self.drawn.append
        self.cut_init()
        self.range = (at_in, at_out)

    def now(self):
        return self.t

    def note(self, text):
        self.notes.append(text)

    def status(self, force=False):
        self.draw_info(force)

    def wait(self, timeout=60):
        self.exports[-1]["proc"].wait(timeout=timeout)
        self.poll_exports()
        return self.exports[-1]


def _duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def test_the_next_and_previous_media_file_follow_the_files_pane_order_and_stop_at_the_ends(tmp_path):
    for name in (
        "clip2.mp4",
        "clip10.mp4",
        "clip.mp4",
        "clip_cut.mp4",
        "Clip1.mkv",
        "song.mp3",
        "notes.txt",
        ".hidden.mp4",
        "._clip3.mp4",
        "x.png",
    ):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "clip5.mp4").mkdir()  # a folder is not a file to play
    order = ["clip.mp4", "Clip1.mkv", "clip2.mp4", "clip10.mp4", "clip_cut.mp4", "song.mp3"]  # as Tern lists them
    walked, here = [], str(tmp_path / order[0])
    while here:
        walked.append(os.path.basename(here))
        here = AUDIO.media_neighbour(here, 1)
    assert walked == order  # clip before clip2 and clip_cut, clip2 before clip10; non-media skipped
    assert AUDIO.media_neighbour(str(tmp_path / "song.mp3"), 1) is None
    assert AUDIO.media_neighbour(str(tmp_path / "clip.mp4"), -1) is None
    assert AUDIO.media_neighbour(str(tmp_path / "clip10.mp4"), -1) == str(tmp_path / "clip2.mp4")
    assert AUDIO.media_neighbour(str(tmp_path / "notes.txt"), 1) == str(tmp_path / "song.mp3")  # its own place


def test_i_sets_the_in_point_at_the_frames_start_o_the_out_point_at_its_end_and_x_clears(tmp_path):
    p = Stub(tmp_path / "c.mp4", now=1.01)
    assert p.cut_key(b"i") and p.range == (1.0, None)  # frame 30 starts at 1 s
    assert p.cut_key(b"o") and p.range == (1.0, pytest.approx(31 / 30))
    p.t = 3.99
    p.cut_key(b"o")
    assert p.range[1] == 4.0  # capped at the duration
    assert p.cut_key(b"x") and p.range == (None, None)
    assert not p.cut_key(b"z")


def test_the_range_is_written_to_the_store_when_there_is_one(tmp_path):
    p = Stub(tmp_path / "c.mp4", now=2.0)
    p.store = TB.Bookmarks(str(tmp_path / "bm.db"))
    p.cut_key(b"i")
    p.cut_key(b"o")
    assert p.store.range_of(p.items[0]["path"]) == (2.0, pytest.approx(2.0 + 1 / 30))
    p.cut_key(b"x")
    assert p.store.range_of(p.items[0]["path"]) == (None, None)


def test_an_export_needs_a_range_and_a_sensible_one(tmp_path):
    p = Stub(tmp_path / "c.mp4")
    p.export("cut")
    p.range = (2.0, 2.0)
    p.export("cut")
    assert "set an in point" in p.notes[0] and "not before" in p.notes[1] and p.exports == []


def test_export_names_are_beside_the_file_have_no_colons_and_never_take_an_existing_one():
    taken = set()
    names = [CUT.export_name("/m/clip.mp4", 1.5, 4.0, "", taken.__contains__)]
    taken.add(names[0])
    names.append(CUT.export_name("/m/clip.mp4", 1.5, 4.0, "", taken.__contains__))
    taken.add(CUT.hidden(names[1]))  # a stale temporary file counts too
    names.append(CUT.export_name("/m/clip.mp4", 1.5, 4.0, "", taken.__contains__))
    assert names == [
        "/m/clip_0m01.50s-0m04.00s.mp4",
        "/m/clip_0m01.50s-0m04.00s (2).mp4",
        "/m/clip_0m01.50s-0m04.00s (3).mp4",
    ]
    assert CUT.export_name("/m/a.mov", 61.0, 125.25, ".gif", lambda _: False) == "/m/a_1m01.00s-2m05.25s.gif"
    assert CUT.hidden("/m/a.gif") == "/m/.a.gif"
    assert ":" not in names[0]


@needs_ff
def test_a_cut_copies_the_range_to_a_new_file_beside_the_source_and_leaves_no_temporary_behind(tmp_path):
    clip = tmp_path / "clip.mp4"
    _movie(clip)
    p = Stub(clip, 1.0, 3.0)
    p.export("cut")
    assert "making" in p.export_text()  # running, until the detached ffmpeg ends
    done = p.wait()
    assert done["rc"] == 0 and p.export_text() == "\u2713 clip_0m01.00s-0m03.00s.mp4"
    assert sorted(os.listdir(tmp_path)) == ["clip.mp4", "clip_0m01.00s-0m03.00s.mp4"]  # no hidden .NAME left
    assert 1.9 <= _duration(tmp_path / "clip_0m01.00s-0m03.00s.mp4") <= 2.6  # -c copy: to the keyframe
    assert not os.path.exists(done["dir"])  # the log's folder went with the success
    p.export("cut")  # the same range again: a second file
    p.wait()
    assert sorted(os.listdir(tmp_path)) == ["clip.mp4", "clip_0m01.00s-0m03.00s (2).mp4", "clip_0m01.00s-0m03.00s.mp4"]


@needs_ff
def test_an_in_point_alone_cuts_to_the_end_and_an_out_point_alone_from_the_start(tmp_path):
    clip = tmp_path / "clip.mp4"
    _movie(clip)
    p = Stub(clip, 3.0, None)
    p.export("cut")
    p.wait()
    p.range = (None, 1.0)
    p.export("cut")
    p.wait()
    assert sorted(os.listdir(tmp_path)) == ["clip.mp4", "clip_0m00.00s-0m01.00s.mp4", "clip_0m03.00s-0m04.00s.mp4"]


@needs_ff
def test_a_failed_export_says_why_in_the_info_row_and_leaves_nothing_hidden_behind(tmp_path):
    p = Stub(tmp_path / "missing.mp4", 1.0, 2.0)
    p.export("cut")
    failed = p.wait()
    assert failed["rc"] != 0 and "No such file" in failed["msg"]
    assert p.export_text().startswith("\u2717 cut failed: ")
    assert os.listdir(tmp_path) == [] and not os.path.exists(failed["dir"])


@needs_ff
@needs_gifski
def test_a_gif_is_made_by_gifski_from_the_range_at_most_320_pixels_wide(tmp_path):
    clip = tmp_path / "clip.mp4"
    _movie(clip)
    p = Stub(clip, 1.0, 2.0)
    p.export("gif")
    done = p.wait()
    gif = tmp_path / "clip_0m01.00s-0m02.00s.gif"
    assert done["rc"] == 0, done["msg"]
    assert sorted(os.listdir(tmp_path)) == ["clip.mp4", gif.name]
    head = gif.read_bytes()[:10]
    width, height = struct.unpack("<HH", head[6:10])
    assert head[:3] == b"GIF" and width == 320 and height == 180


def test_a_gif_of_sound_alone_and_a_gif_without_gifski_are_notes_not_crashes(tmp_path, monkeypatch):
    p = Stub(tmp_path / "t.flac", 1.0, 2.0, waveform=True)
    p.export("gif")
    q = Stub(tmp_path / "c.mp4", 1.0, 2.0)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    q.export("gif")
    assert "sound only" in p.notes[0] and "gifski" in q.notes[0] and p.exports == q.exports == []


@needs_ff
def test_the_probe_describes_the_file_in_one_line_for_the_info_row(tmp_path):
    clip = tmp_path / "clip.mp4"
    _movie(clip)
    item = TVB.probe(clip)
    assert item["info"].startswith("mp4 \u00b7 00:04 \u00b7 h264 640x360 30fps")
    assert "aac 44.1kHz" in item["info"] and item["info"].split(" \u00b7 ")[-1].endswith(("KB", "MB"))
    track = tmp_path / "tone.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(track)], check=True
    )
    assert TVB.probe(track)["info"].startswith("flac \u00b7 00:01 \u00b7 ") and "h264" not in TVB.probe(track)["info"]
    assert CUT.describe({}) == ""


def test_the_info_row_shows_the_range_the_export_and_the_file_cut_to_the_width(tmp_path):
    p = Stub(tmp_path / "c.mp4", 1.5, 4.0, info="mp4 \u00b7 00:04 \u00b7 h264 640x360 30fps")
    p.draw_info(force=True)
    assert "[01.50 \u2192 04.00]  mp4 \u00b7 00:04" in p.drawn[-1] and p.drawn[-1].startswith("\x1b[30;1H")
    p.range = (None, 4.0)
    assert p.info_text().startswith(" [start \u2192 04.00]")
    p.geom = dict(p.geom, cols=20)
    p.draw_info(force=True)
    assert len(re.search(r"\x1b\[2;39m(.*?)\x1b\[0m", p.drawn[-1]).group(1)) <= 19
    p.geom = dict(p.geom, rows=7)  # too short a pane: no info row
    drawn = len(p.drawn)
    p.draw_info(force=True)
    assert len(p.drawn) == drawn and p.status_row == 7


def test_text_is_cut_to_the_terminals_cells_wide_characters_counting_twice():
    assert CUT.fit("abcdef", 4) == "abcd" and CUT.fit("ab", 9) == "ab"
    assert CUT.fit("\u3042\u3044\u3046", 5) == "\u3042\u3044"


@needs_ff
def test_keys_on_a_terminal_set_the_range_in_the_info_row_and_the_arrows_change_file(tmp_path):
    a, b, db = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "bm.db"
    _clip(a, 64, 36)
    _clip(b, 64, 36)
    code = (
        "import sys\nimport tern_video_block as TVB\n"
        f"sys.exit(TVB.main(['--paused', '--no-sound', '--bookmarks', {str(db)!r}, {str(a)!r}]))\n"
    )
    arrow = "\u2192".encode()
    keys = [
        (rb"00:00 / 00:01", b".i"),
        (rb"\[00\.03 " + arrow + rb" end\]", b".o"),
        (rb"\[00\.03 " + arrow + rb" 00\.10\]", b"\x1b[B"),
        (rb"\x1b\]2;b\.mp4\x07", b"\x1b[A"),
        (rb"\x1b\]2;a\.mp4\x07", b"\x1bOB"),
        (rb"\x1b\]2;b\.mp4\x07", b"q"),
    ]
    out, status = _on_a_pty(code, TERN_ANSWERS, keys=keys)
    assert status == 0, out[-300:]
    assert TB.Bookmarks(str(db)).range_of(str(a)) == (pytest.approx(1 / 30), pytest.approx(0.1))
    assert TB.Bookmarks(str(db)).range_of(str(b)) == (None, None)  # b opened with none of a's state
    assert "mp4 \u00b7".encode() in out  # the info row has the file's description


@needs_ff
def test_at_nineteen_columns_no_line_the_player_draws_is_wider_than_the_pane(tmp_path):
    clip = tmp_path / "c.mp4"
    _clip(clip, 64, 36)
    code = (
        f"import sys\nimport tern_video_block as TVB\nsys.exit(TVB.main(['--paused', '--no-sound', {str(clip)!r}]))\n"
    )
    narrow = {b"\x1b[18t": b"\x1b[8;24;19t", b"\x1b[16t": b"\x1b[6;16;8t"}
    out, status = _on_a_pty(code, narrow, keys=[(rb"00:00", b"i"), (rb"\[00\.00", b"q")])
    assert status == 0
    rows = re.findall(rb"\x1b\[(\d+);1H\x1b\[2K(?:\x1b\[[0-9;]*m)*([^\x1b]*)", out)
    assert rows and {int(r) for r, _ in rows} <= {23, 24}  # the status row above the info row
    assert all(len(text.decode()) <= 18 for _, text in rows), rows
    assert b"\x1b[?7l" in out  # and a line that runs on would not wrap
