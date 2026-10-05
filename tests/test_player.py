"""tern-video-block: probing reads a file's displayed size, rate, sound and chapters; the side-by-side picture fits the
pane and is centred to the pixel; ffmpeg starts on exactly the frame asked for, a file of another rate kept in step with
the first; chapter jumps visit every chapter when chapters start between frames; the terminal's size reports never act
as keys; the terminal's size comes from its answers to the escape queries (Tern leaves the pixels out of the kernel's
window size)."""
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time

import pytest

import tern_video_block as TVB

needs_ff = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                              reason="ffmpeg / ffprobe not available")


def _clip(path, w, h, lum="16+N*7", rate=30, args=()):
    """A 1 s clip whose frame N is flat grey with luma `lum` (inside video range, 16 to 235), losslessly encoded."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={rate}:d=1",
                    *args, "-vf", f"format=yuv420p,geq=lum='{lum}':cb=128:cr=128", "-c:v", "libx264", "-qp", "0",
                    str(path)], check=True)


@needs_ff
def test_probe_reads_the_displayed_size_the_rate_the_sound_and_the_chapters(tmp_path):
    _clip(tmp_path / "plain.mp4", 64, 36)
    meta = tmp_path / "ch.ffmeta"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=400\ntitle=first\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=400\nEND=1000\ntitle=second\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(tmp_path / "plain.mp4"),
                    "-i", str(meta), "-map", "0", "-map_chapters", "1", "-c", "copy", str(tmp_path / "turned.mp4")],
                   check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "plain.mp4"), "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-shortest", "-c:v", "copy", "-c:a", "aac",
                    str(tmp_path / "sound.mp4")], check=True)
    turned, sound = TVB.probe(tmp_path / "turned.mp4"), TVB.probe(tmp_path / "sound.mp4")
    assert (turned["w"], turned["h"]) == (36, 64)                          # shown upright, as ffmpeg decodes it
    assert (sound["w"], sound["h"]) == (64, 36)
    assert turned["fps"] == pytest.approx(30) and turned["duration"] == pytest.approx(1.0, abs=0.05)
    assert (turned["audio"], sound["audio"]) == (False, True)              # no sound: the wall clock, not mpv's
    assert [(c["name"], c["from"], c["to"]) for c in turned["chapters"]] == [("first", 0.0, 0.4), ("second", 0.4, 1.0)]
    assert sound["chapters"] == []


@needs_ff
def test_a_chapters_file_reads_in_time_order_and_unnamed_chapters_are_numbered(tmp_path):
    f = tmp_path / "shots.ffmeta"
    f.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1920\nEND=2900\ntitle=a\\=b\\;c\n"
                 "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1920\n", encoding="utf-8")
    assert [(c["name"], c["from"], c["to"]) for c in TVB.read_chapters(f)] == [("chapter 2", 0.0, 1.92),
                                                                               ("a=b;c", 1.92, 2.9)]


@pytest.mark.parametrize("geom", [
    {"cols": 106, "rows": 46, "cell_w": 8, "cell_h": 16},                   # a tall pane: the width limits
    {"cols": 220, "rows": 20, "cell_w": 9, "cell_h": 19},                   # a wide pane: the height limits
    {"cols": 51, "rows": 33, "cell_w": 7, "cell_h": 15},                    # odd sizes
])
def test_the_picture_fits_the_pane_above_the_status_row_and_is_centred_to_the_pixel(geom):
    items = [{"w": 960, "h": 540}, {"w": 540, "h": 960}]
    lay = TVB.layout(items, geom)
    avail_w, avail_h = geom["cols"] * geom["cell_w"], (geom["rows"] - 1) * geom["cell_h"]
    x = lay["col"] * geom["cell_w"] + lay["X"]
    y = lay["row"] * geom["cell_h"] + lay["Y"]
    assert 0 <= lay["X"] < geom["cell_w"] and 0 <= lay["Y"] < geom["cell_h"]
    assert lay["w"] == sum(lay["widths"]) <= avail_w and lay["h"] <= avail_h
    assert abs(x - (avail_w - lay["w"] - x)) <= 1 and abs(y - (avail_h - lay["h"] - y)) <= 1
    assert avail_w - lay["w"] <= 4 or avail_h - lay["h"] <= 4                # it fills one direction
    for it, w in zip(items, lay["widths"]):
        assert w % 2 == 0 and abs(w - it["w"] * lay["h"] / it["h"]) <= 2     # one height, each its own aspect


def _frames(cmd, w, h, n):
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    size = w * h * 3
    return [out[i * size:(i + 1) * size] for i in range(min(n, len(out) // size))]


def _mean(frame, w, h, x0, x1):
    """The mean of every channel of the columns x0..x1 of an RGB frame."""
    total = sum(sum(frame[(y * w + x0) * 3:(y * w + x1) * 3]) for y in range(h))
    return total / (h * (x1 - x0) * 3)


@needs_ff
def test_decoding_starts_on_exactly_the_frame_asked_for_and_keeps_a_file_of_another_rate_in_step(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _clip(a, 64, 36)
    _clip(b, 36, 64, lum="235-N*3", rate=60)                                # twice the rate, the grey the other way
    items = [TVB.probe(a), TVB.probe(b)]
    lay = TVB.layout(items, {"cols": 20, "rows": 5, "cell_w": 8, "cell_h": 16})
    w, h, w0 = lay["w"], lay["h"], lay["widths"][0]
    assert len(_frames(TVB.ffmpeg_cmd(items, lay, 0.0, 30), w, h, 99)) == 30   # 30 frames from both, at the first's rate
    for k in (1, 7, 15, 29):
        first = _frames(TVB.ffmpeg_cmd(items, lay, k / 30, 30), w, h, 1)[0]
        luma_a = 16 + k * 7                                                  # frame k of a
        luma_b = 235 - 2 * k * 3                                             # frame 2k of b: the same instant
        assert abs(_mean(first, w, h, 2, w0 - 2) - (luma_a - 16) * 255 / 219) < 3, k
        assert abs(_mean(first, w, h, w0 + 2, w - 2) - (luma_b - 16) * 255 / 219) < 3, k


def _player(chapters, geom=None):
    item = {"path": "x.mp4", "w": 64, "h": 36, "fps": 30.0, "duration": 10.0, "audio": False, "chapters": []}
    p = TVB.Player([item], chapters, first_frame=61, paused=True)
    p.geom = geom or {"cols": 80, "rows": 24, "cell_w": 8, "cell_h": 16}
    p.out = lambda s: None                                                   # no terminal
    p.landed = []

    def seek(t):
        p.landed.append(t)
        p.anchor(t)
    p.seek = seek
    return p


def test_chapter_jumps_visit_every_chapter_in_order_when_chapters_start_between_frames():
    chapters = [{"name": n, "from": f, "to": t} for n, f, t in
                [("a", 0.0, 1.92), ("b", 1.92, 2.9), ("c", 2.9, 3.86), ("d", 3.86, 5.81), ("e", 5.81, 10.0)]]
    p = _player(chapters)
    for _ in range(6):
        p.jump_chapter(1)
    assert [TVB.chapter_at(chapters, t) for t in p.landed] == [1, 2, 3, 4, 4, 4]   # never stuck before b (frame 57.6)
    assert all(abs(t * 30 - round(t * 30)) < 1e-9 for t in p.landed)        # on frames
    p.anchor(4.5)                                                            # well into d: back to its start first
    p.jump_chapter(-1)
    p.jump_chapter(-1)
    assert [TVB.chapter_at(chapters, t) for t in p.landed[-2:]] == [3, 2]


def test_the_terminals_size_reports_set_the_new_size_and_never_act_as_keys():
    p = _player([])
    assert p.key(b"\x1b[8;31;123t\x1b[6;18;9t") is True
    assert p.new_geom == {"cols": 123, "rows": 31, "cell_w": 9, "cell_h": 18}
    assert p.speed == 1.0                                                    # the 1s and 3s in them are speed keys
    p.new_geom = None
    p.key(b"\x1b[8;24;80t")                                                  # the size it already has: nothing to do
    assert p.new_geom is None
    p.key(b"1")
    assert p.speed == 0.25
    assert p.key(b"q") is False


def _on_a_pty(code, answers, env=None, until=None):
    """Runs `code` in a fresh Python on a pseudo-terminal whose other end answers the terminal's queries with `answers`
    ({query: reply}, each once), until the output matches `until` or the program ends; (output, exit code). A new
    process, not a fork: forking the threaded test process can deadlock the child."""
    import pty
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
                            cwd=os.path.dirname(os.path.abspath(TVB.__file__)), start_new_session=True,
                            env={**os.environ, **(env or {})})
    os.close(slave)
    buf, done, deadline = b"", set(), time.monotonic() + 15
    try:
        while time.monotonic() < deadline and not (until and re.search(until, buf)):
            ready, _, _ = select.select([master], [], [], 0.05)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:                                               # the child exited: the pty is closed
                break
            if not chunk:
                break
            buf += chunk
            for q, a in answers.items():
                if q in buf and q not in done:
                    os.write(master, a)
                    done.add(q)
    finally:
        if proc.poll() is None and until:
            proc.kill()
        code = proc.wait(timeout=15)
        os.close(master)
    return buf, code


TERN_ANSWERS = {b"\x1b[18t": b"\x1b[8;41;99t", b"\x1b[16t": b"\x1b[6;16;8t"}   # what Tern answers for a 99x41 pane


def _geometry(answers):
    code = ("import json, os\nimport tern_video_block as TVB\n"
            "os.write(1, ('GEOM' + json.dumps(TVB.term_geometry(timeout=1.0)) + '\\n').encode())\n")
    out, _ = _on_a_pty(code, answers, until=rb"GEOM.*\n")
    m = re.search(rb"GEOM(.*?)\r?\n", out)
    assert m, out
    return json.loads(m.group(1))


def test_the_terminal_size_comes_from_its_answers_and_a_silent_terminal_gives_none():
    assert _geometry(TERN_ANSWERS) == {"rows": 41, "cols": 99, "cell_h": 16, "cell_w": 8}
    assert _geometry({}) is None                                         # no answer: no size


@needs_ff
def test_a_terminal_that_hangs_up_twice_does_not_leave_the_frames_behind(tmp_path):
    clip, run_dir = tmp_path / "c.mp4", tmp_path / "run"
    _clip(clip, 64, 36)
    run_dir.mkdir()
    # Every stop of the decoder hangs up: the first, as playback starts, ends the player; the next comes during its
    # cleanup, as a closing terminal's second SIGHUP does (kitty sends one, the kernel another when the pty closes).
    code = ("import os, signal, sys, time\nimport tern_video_block as TVB\nstop = TVB._Frames.stop\n"
            "def hang_up(self):\n    stop(self)\n    os.kill(os.getpid(), signal.SIGHUP)\n    time.sleep(0.2)\n"
            f"TVB._Frames.stop = hang_up\nsys.exit(TVB.main(['--no-sound', {str(clip)!r}]))\n")
    _, status = _on_a_pty(code, TERN_ANSWERS, env={"XDG_RUNTIME_DIR": str(run_dir)})
    assert status == 128 + signal.SIGHUP                                  # the first hangup ended it
    assert os.listdir(run_dir) == []                                      # the second did not stop the cleanup
