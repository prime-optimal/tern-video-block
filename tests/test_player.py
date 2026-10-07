"""tern-video-block: probing reads a file's displayed size, rate, sound and chapters; the side-by-side picture fits the
pane and is centred to the pixel; ffmpeg starts on exactly the frame asked for, a file of another rate kept in step with
the first; chapter jumps visit every chapter when chapters start between frames; the terminal's size reports never act
as keys; the terminal's size comes from its answers to the escape queries (Tern leaves the pixels out of the kernel's
window size); a waveform is a strip of the whole track drawn once, a screen of it at a time, zoomed by the keys."""
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import types

import pytest

import tern_video_block as TVB

needs_ff = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                              reason="ffmpeg / ffprobe not available")
needs_mpv = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("mpv") is None,
                               reason="ffmpeg / mpv not available")


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


ITEM = {"path": "x.mp4", "w": 64, "h": 36, "fps": 30.0, "duration": 10.0, "audio": False, "chapters": []}


def _player(chapters, geom=None, **options):
    p = TVB.Player([ITEM], chapters, first_frame=61, paused=True, **options)
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


def test_playback_keeps_to_the_range_on_whole_frames():
    p = _player([], start=1.01, end=2.0)
    started = []
    p.frames = types.SimpleNamespace(start=lambda lay, t: started.append(t), read=lambda: None)
    for t in (0.0, 9.0, 1.5):                                                # before, after and inside the range
        TVB.Player.seek(p, t)                                                # the real seek (_player records its own)
    assert started == [31 / 30, 59 / 30, 45 / 30]       # the first frame from 1.01 s; the last starting before 2 s
    with pytest.raises(TVB.PlayError, match="no frame"):
        TVB.Player([ITEM], start=1.01, end=1.02)                             # between frames 30 and 31: none


@needs_mpv
def test_the_sound_plays_on_when_the_loop_goes_back_after_its_end(tmp_path, monkeypatch):
    clip = tmp_path / "tone.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x36:r=30:d=1", "-f",
                    "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    str(clip)], check=True)
    popen = subprocess.Popen                                                 # muted: nothing to hear
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: popen([argv[0], "--mute=yes", *argv[1:]], **kw))
    sound = TVB._Audio(str(clip), str(tmp_path / "mpv.sock"))
    try:
        sound.send("set_property", "pause", False)                           # the 1 s tone from its start
        deadline = time.monotonic() + 5
        while sound.get("eof-reached") is not True and time.monotonic() < deadline:
            time.sleep(0.05)
        if sound.get("current-ao") in (None, "null"):
            pytest.skip("no audio output: mpv pauses at the end of a file only when it plays to one")
        time.sleep(0.6)                                                      # past the last of the sound
        sound.send("seek", 0.0, "absolute+exact")                            # the loop goes back to the start
        time.sleep(0.4)
        assert sound.get("pause") is False and 0.15 < sound.get("time-pos") < 0.9   # and it plays on from there
    finally:
        sound.close()


def test_the_clock_follows_the_sound_until_the_sound_has_ended():
    p = _player([])
    p.paused = False
    sound = {"time-pos": 1.0, "eof-reached": False}
    p.audio = types.SimpleNamespace(get=lambda prop, timeout=0.2: sound[prop])
    p.anchor(2.0)
    p.sync_to_sound()
    assert p.now() == pytest.approx(1.0, abs=0.02)                           # drifted apart: back to the sound
    sound["eof-reached"] = True                                              # the sound stopped at 1 s
    p.anchor(2.0)
    p.next_sync = 0.0
    p.sync_to_sound()
    assert p.now() == pytest.approx(2.0, abs=0.02)                           # the picture goes on by itself


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
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:                             # still running: it hangs (or loops for ever)
            proc.kill()
            code = proc.wait()
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


@needs_ff
def test_once_plays_the_range_and_then_quits_by_itself(tmp_path):
    clip, run_dir = tmp_path / "c.mp4", tmp_path / "run"
    _clip(clip, 64, 36)
    run_dir.mkdir()
    code = ("import sys\nimport tern_video_block as TVB\n"
            f"sys.exit(TVB.main(['--end', '0.5', '--once', '--no-sound', {str(clip)!r}]))\n")
    out, status = _on_a_pty(code, TERN_ANSWERS, env={"XDG_RUNTIME_DIR": str(run_dir)})
    assert status == 0                                                       # it ended by itself, not killed looping
    assert out.count(b"\x1b_Ga=T") >= 2                                      # after playing, not at the first frame
    assert os.listdir(run_dir) == []


# ---- the waveform: a strip of the whole track, a screen of it at a time, and the zoom

def test_a_waveform_visualizer_is_a_strip_and_the_other_names_are_graphs():
    wave = TVB._visualizer(None)
    assert wave["wave"]["mode"] == TVB.WAVE_LOOK["mode"] == "cline"
    assert wave["size"] == list(TVB.VIS_SIZE) and wave["wave"]["bg"] == TVB.TERM_COLORS["bg"]
    assert wave["wave"]["fg"] == TVB.TERM_COLORS["fg"]                       # the theme's own colors, not a palette
    assert wave["wave"]["red"] == TVB.TERM_COLORS["red"]
    assert TVB._visualizer("wavespic")["wave"]["draw"] == "full"             # the wave filled in
    assert TVB._visualizer("p2p")["wave"]["mode"] == "p2p"                   # showwaves' own modes, as strips
    for name in ("spectrum", "spectrogram", "cqt", "vectorscope", "spectrumpic", "viridis"):
        assert "wave" not in TVB._visualizer(name), name                     # graphs: drawn as the sound plays
    cscheme = TVB._visualizer("cqt", {"cyan": "#ff0000", "magenta": "#00ff80"})["filter"]
    assert "cscheme=1.000|0.000|0.000|0.000|1.000|0.502" in cscheme          # the theme's two colors, as showcqt wants
    assert "{" not in TVB._visualizer("showwaves=line:s=320x200")["filter"]  # a graph of the caller's own, filled in
    with pytest.raises(TVB.PlayError, match="no quotes"):
        TVB._visualizer("showwaves=line'")                                   # ffmpeg's graph syntax has no place for it


def test_a_span_reads_as_seconds_minutes_or_the_whole_track():
    assert TVB.span_name(45, 600) == "45 s"
    assert TVB.span_name(90, 600) == "1m30s"
    assert TVB.span_name(1800, 6000) == "30m"
    assert TVB.span_name(180, 600) == "3m"
    assert TVB.span_name(600, 600) == "the whole track"                      # the whole span is the whole track
    assert TVB.span_name(None, 600) == "?"


def _fake_strip(monkeypatch, calls, fill=10):
    """ffmpeg as far as _Wave is concerned: draws its sw x ph of raw RGB, every pixel `fill`."""
    code = ("import sys\n"
            "sw, ph, fill = (int(a) for a in sys.argv[1:4])\n"
            "sys.stdout.buffer.write(bytes([fill]) * (sw * ph * 3))\n")

    def cmd(item, wave, t0, t1, sw, ph):
        calls.append((t0, t1, sw, ph))
        return [sys.executable, "-c", code, str(sw), str(ph), str(fill)]
    monkeypatch.setattr(TVB, "wave_cmd", cmd)


def test_a_strip_is_drawn_once_a_screen_of_the_whole_track_and_the_playhead_moves_over_it(monkeypatch):
    calls = []
    _fake_strip(monkeypatch, calls)
    wave = TVB._Wave({"path": "t.flac", "duration": 600.0}, TVB._visualizer(None)["wave"], 6.0, {"w": 40, "h": 6})
    assert (wave.seconds, wave.count) == (48.0, 13)          # 8 screens of 6 s: a strip per 48 s of the track
    first = wave.frame(0.0)
    assert len(first) == 40 * 6 * 3 and calls == [(0.0, 48.0, 320, 6)]       # one call, whole screens of sound wide
    red = bytes.fromhex(TVB.TERM_COLORS["red"].lstrip("#"))
    for r in range(6):
        assert first[r * 120:(r + 1) * 120].count(red) == 2                  # the playhead, at the left of the pane
        assert set(first[r * 120:(r + 1) * 120]) == {10, *red}               # over the strip, nothing else
    assert wave.frame(0.0) is None                                          # the pane already shows that picture
    assert wave.frame(0.2) is not None and calls == [(0.0, 48.0, 320, 6)]    # 6.7 px a second: one pixel on, same strip
    assert wave.frame(50.0) is not None and len(calls) == 2                  # into the next strip: drawn then, once
    assert calls[-1] == (48.0, 96.0, 320, 6)
    assert wave.frame(60.0) is not None and len(calls) == 2                  # and not again for the rest of it


def test_a_span_of_the_whole_track_is_one_screen_one_strip_over_the_whole_of_it(monkeypatch):
    calls = []
    _fake_strip(monkeypatch, calls)
    wave = TVB._Wave({"path": "t.flac", "duration": 600.0}, TVB._visualizer(None)["wave"], 600.0, {"w": 40, "h": 6})
    assert (wave.seconds, wave.count) == (600.0, 1)
    wave.frame(0.0)
    assert calls == [(0.0, 600.0, 40, 6)]                                    # the track once, a pane wide, in one call
    tail = wave.frame(599.0)
    assert tail is not None and len(calls) == 1                              # nothing drawn again as it plays


WAVE_ITEM = {"path": "t.flac", "w": 16, "h": 9, "fps": 30.0, "duration": 600.0, "audio": True, "waveform": True,
             "chapters": []}


STATUS_LINE = re.compile(r"\x1b\[\d+;1H\x1b\[2K\x1b\[2m(.*?)\x1b\[0m")


def _status(out):
    """The last status line the player wrote (the row it writes to, cleared, then the text)."""
    lines = [m for s in out for m in STATUS_LINE.finditer(s)]
    return lines[-1].group(1) if lines else ""


def _wave_player(monkeypatch, tmp_path, duration=600.0, visualizer=None):
    calls = []
    _fake_strip(monkeypatch, calls)
    p = TVB.Player([dict(WAVE_ITEM, duration=duration)], [], paused=True, visualizer=visualizer)
    p.geom = {"cols": 80, "rows": 24, "cell_w": 8, "cell_h": 16}
    out: list[str] = []
    p.out = out.append
    p.frame_dir = str(tmp_path / "frames")
    os.makedirs(p.frame_dir)
    p.seek = lambda t: p.anchor(t)                                           # no decoder under a strip
    p.relayout(p.geom)
    return p, calls, out


def test_a_track_opens_on_a_span_of_its_own_shown_whole_in_the_pane(monkeypatch, tmp_path):
    p, calls, out = _wave_player(monkeypatch, tmp_path)
    assert p.span == TVB.SPAN_DEFAULT == 90.0 and isinstance(p.wave, TVB._Wave)
    assert (p.wave.pw, p.wave.ph) == (p.lay["w"], p.lay["h"]) == (640, 360)  # the pane in pixels, above the status row
    assert p.next_frame_in() == pytest.approx(90.0 / 640)                    # a pixel of the strip: 0.14 s
    p.status(force=True)
    assert "span 1m30s" in _status(out)                                      # the status line says the span
    assert p.wave.seconds == 600.0                                           # 8 screens of 90 s: the whole track


def test_zooming_steps_out_to_the_whole_track_and_back_in_again(monkeypatch, tmp_path):
    p, calls, out = _wave_player(monkeypatch, tmp_path)
    p.zoom(-1)
    assert p.span == 180.0 and "span 3m" in _status(out)
    p.zoom(-1)
    assert p.span == 600.0 and p.wave.count == 1 and "the whole track" in _status(out)
    p.zoom(-1)
    assert p.span == 600.0                                                   # nothing wider than the track itself
    p.zoom(1)
    assert p.span == 180.0 and p.wave.seconds == 600.0                       # back in: the strip follows the span
    p.zoom(1)
    assert p.span == 90.0
    p.zoom(0)
    assert p.span == 600.0                                                   # z: the whole track, whatever the span was
    assert calls[-1] == (0.0, 600.0, 640, 360)                               # drawn at once: one pane-wide screen
    assert p.wave.frame(0.0) is None                                         # and the pane is already showing it


def test_the_zoom_keys_are_the_bare_ones_and_the_command_key_chords(monkeypatch, tmp_path):
    p, _, _ = _wave_player(monkeypatch, tmp_path)
    assert p.key(b"-") is True and p.span == 180.0
    assert p.key(b"\x1b[45;9u") is True and p.span == 600.0                  # cmd+-
    assert p.key(b"\x1b[61;9u") is True and p.span == 180.0                  # cmd+=
    assert p.key(b"\x1b[61;9:1u") is True and p.span == 90.0                 # the same chord, with an event type
    assert p.key(b"\x1b[45u") is True and p.span == 180.0                    # a terminal writing - as a key code
    p.key(b"z")
    assert p.span == 600.0
    assert p.key(b"=") is True and p.span == 180.0
    assert p.key(b"+") is True and p.span == 90.0
    assert p.key(b"\x1b[1;2C") is True and p.span == 90.0                    # an arrow: not a chord, not a zoom
    p.zoom(1)
    assert p.span == 45.0 and p.wave.seconds == 45.0 * TVB.STRIP_SCREENS


def test_a_video_pane_has_no_span_and_leaves_the_zoom_keys_alone():
    p = _player([])                                                          # a video: no strip, nothing to zoom
    for key in (b"-", b"_", b"+", b"=", b"z", b"\x1b[45;9u"):
        assert p.key(key) is True
    assert p.span is None and p.wave is None


def test_the_command_key_chords_are_read_as_zoom_and_other_keys_are_not():
    assert TVB.zoom_chord(b"\x1b[61;9u") == 1 and TVB.zoom_chord(b"\x1b[43;9u") == 1
    assert TVB.zoom_chord(b"\x1b[45;9u") == -1 and TVB.zoom_chord(b"\x1b[95;9u") == -1
    assert TVB.zoom_chord(b"\x1b[45;5u") == -1                               # control, where a terminal swaps them
    assert TVB.zoom_chord(b"\x1b[45u") == -1                                 # written as a key code, with no modifier
    assert TVB.zoom_chord(b"\x1b[5~") == 0 and TVB.zoom_chord(b"\x1b[1;2C") == 0   # PgUp, an arrow
    assert TVB.zoom_chord(b"a") == 0 and TVB.zoom_chord(b"\x1b[62u") == 0    # a plain key, another key code


def test_an_unanswering_terminal_leaves_the_visualizer_in_the_fallback_colors(monkeypatch):
    monkeypatch.setattr(TVB.os, "isatty", lambda fd: False)                  # no terminal under the tests
    monkeypatch.setattr(TVB, "TERM_COLORS", {**TVB.TERM_COLORS, "fg": "#111111", "bg": "#222222", "red": "#333333"})
    p = TVB.Player([dict(WAVE_ITEM)], [], paused=True, visualizer="spectrum")
    assert p.visualizer["filter"].count("#101010") == 0                      # nothing of the fallback left in it
    p.theme_colors()
    assert p.visualizer["bg"] == "#222222" and "0x222222" in p.visualizer["filter"]
    q = TVB.Player([dict(WAVE_ITEM)], [], paused=True)
    q.theme_colors()
    assert q.visualizer["wave"]["fg"] == "#111111" and q.visualizer["wave"]["red"] == "#333333"


def test_apple_double_sidecars_are_not_offered_as_tracks(tmp_path):
    (tmp_path / "mastered").mkdir()
    for name in ("track.flac", "._track.flac", "notes.txt", "./sub/._other.mp3", "./sub/other.mp3"):
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert [os.path.relpath(f, tmp_path) for f in TVB.media_files(str(tmp_path))] == ["track.flac", "sub/other.mp3"]
