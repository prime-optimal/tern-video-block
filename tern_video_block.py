"""tern-video-block: video files played in a terminal through the kitty graphics protocol (made for Tern; tested there
only), with their sound.

Every file given plays side by side in one picture, at one height, fitted into the pane and centred to the pixel, in
sync and looping, with the first file's sound; the bottom row is a status line with the time, the frame, the chapter
and the keys. ffmpeg decodes the files from the frame asked for, scales them and stacks them into raw RGB frames at the
pane's pixel size; each frame is written to a file in the runtime directory (memory: $XDG_RUNTIME_DIR) and handed to the
terminal by path (t=f), replacing one image in place (one image id, one placement), so the pane never flickers or
stacks pictures. The files are deleted a second after they are shown: Tern answers temporary-file transmissions with OK
and then "no such file", and acknowledges shared-memory ones without drawing them. mpv plays the sound with no video,
driven over its JSON IPC (pause, exact seeks, speed), and its position is the clock the frames follow: late frames are
skipped, never queued.

Terminals that leave the pixel size out of the kernel's window size (Tern does, and sends no SIGWINCH) answer the
queries CSI 18 t (rows and columns) and CSI 16 t (the cell in pixels): the player asks at the start and every second
after, taking the answers out of the keys, and redraws for a new size.

Standard library only; needs ffmpeg and ffprobe, and mpv for the sound."""
import argparse
import base64
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque

__version__ = "0.1.0"

KITTY_ID = 7311                                   # the image the player replaces frame after frame
KEEP_FRAMES = 30                                  # frame files kept after showing them (the terminal may read late)
SPEEDS = {"1": 0.25, "2": 0.5, "3": 1.0}
KEYS = "space play/pause  \u2190\u2192 5 s  , . frame  PgUp/PgDn chapter  1 2 3 speed  m mute  q quit"
SIZE_REPORT = re.compile(rb"\x1b\[(8|6);(\d+);(\d+)t")    # the answers to CSI 18 t (rows, cols) and CSI 16 t (cell px)


class PlayError(Exception):
    """A file or a terminal the player cannot play in; the message says which and why."""


def _rate(text):
    """A frame rate from ffprobe's "num/den", or 0.0."""
    m = re.match(r"^(\d+)/(\d+)$", text or "")
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else 0.0


def _chapters(entries):
    """[{name, from, to}] in time order from ffprobe's chapter entries; unnamed chapters are numbered."""
    out = [{"name": (c.get("tags") or {}).get("title") or f"chapter {i + 1}", "from": float(c["start_time"]),
            "to": float(c["end_time"])} for i, c in enumerate(entries)]
    return sorted(out, key=lambda c: c["from"])


def _ffprobe(args):
    if shutil.which("ffprobe") is None:
        raise PlayError("ffprobe (part of ffmpeg) is not on the PATH")
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise PlayError(r.stderr.strip() or f"ffprobe failed on {args[-1]}")
    return json.loads(r.stdout or "{}")


def probe(path):
    """What the player needs of a video file: {path, w, h (as displayed: rotation and pixel aspect applied), fps,
    duration, audio (it has a sound stream), chapters}."""
    info = _ffprobe(["-show_streams", "-show_format", "-show_chapters", str(path)])
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise PlayError(f"{path}: no video stream")
    w, h = int(video["width"]), int(video["height"])
    sar = re.match(r"^(\d+):(\d+)$", video.get("sample_aspect_ratio") or "")
    if sar and int(sar.group(1)) and int(sar.group(2)):
        w = max(2, round(w * int(sar.group(1)) / int(sar.group(2))))
    rotation = (video.get("tags") or {}).get("rotate")
    for side in video.get("side_data_list") or []:
        rotation = side.get("rotation", rotation)
    if rotation is not None and abs(int(float(rotation))) % 180 == 90:
        w, h = h, w
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")) or 30.0
    duration = float((info.get("format") or {}).get("duration") or video.get("duration") or 0.0)
    if duration <= 0:
        raise PlayError(f"{path}: no duration")
    return {"path": str(path), "w": w, "h": h, "fps": fps, "duration": duration,
            "audio": any(s.get("codec_type") == "audio" for s in streams),
            "chapters": _chapters(info.get("chapters", []))}


def read_chapters(path):
    """The chapters of an FFMETADATA file (what `ffmpeg -f ffmetadata` writes and mpv's --chapters-file reads)."""
    return _chapters(_ffprobe(["-f", "ffmetadata", "-show_chapters", "-i", str(path)]).get("chapters", []))


def chapter_at(chapters, t):
    """Index of the chapter playing at time t (the last that starts at or before it), or -1 before the first."""
    k = -1
    for i, c in enumerate(chapters):
        if c["from"] <= t + 1e-6:
            k = i
    return k


def layout(items, geometry, status_rows=1):
    """Where the side-by-side picture goes in a terminal of `geometry` ({cols, rows, cell_w, cell_h}): every video
    scaled to one height, the row of them fitted into the pane above `status_rows` and centred. Returns {widths (each
    video's, even), w, h, row, col, X, Y}: the picture's size in pixels, its top-left cell and the pixel offsets inside
    that cell."""
    cw, ch = int(geometry["cell_w"]), int(geometry["cell_h"])
    avail_w, avail_h = int(geometry["cols"]) * cw, (int(geometry["rows"]) - status_rows) * ch
    ratio = sum(it["w"] / it["h"] for it in items)              # the row's width at height 1
    h = max(2, int(min(avail_h, avail_w / ratio)) // 2 * 2)
    widths = [max(2, int(round(it["w"] * h / it["h"] / 2)) * 2) for it in items]
    while sum(widths) > avail_w and h > 2:                     # rounding up may overflow by a pixel or two
        h -= 2
        widths = [max(2, int(round(it["w"] * h / it["h"] / 2)) * 2) for it in items]
    w = sum(widths)
    x, y = (avail_w - w) // 2, (avail_h - h) // 2
    return {"widths": widths, "w": w, "h": h, "row": y // ch, "col": x // cw, "X": x % cw, "Y": y % ch}


def ffmpeg_cmd(items, lay, t, fps):
    """ffmpeg decoding every video from the frame at time t (a frame's time, k / fps: decoding starts half a
    millisecond earlier so rounding never skips it), each at `fps` frames a second, scaled to the layout's height and
    its own width and put side by side, as raw RGB frames of lay[w] x lay[h] on stdout."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    for it in items:
        cmd += ["-ss", f"{max(t - 0.0005, 0.0):.4f}", "-i", it["path"]]
    parts = []
    for k, it in enumerate(items):
        rate = f"fps={fps:.6g}," if abs(it["fps"] - fps) > 1e-3 else ""
        parts.append(f"[{k}:v]{rate}scale={lay['widths'][k]}:{lay['h']}:flags=bicubic,setsar=1[v{k}]")
    if len(items) > 1:
        parts.append("".join(f"[v{k}]" for k in range(len(items))) + f"hstack=inputs={len(items)},format=rgb24[out]")
    else:
        parts.append("[v0]format=rgb24[out]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def _ask(fd_in, fd_out, query, pattern, timeout):
    os.write(fd_out, query)
    buf, t0 = b"", time.monotonic()
    while time.monotonic() - t0 < timeout:
        ready, _, _ = select.select([fd_in], [], [], 0.02)
        if ready:
            buf += os.read(fd_in, 64)
            m = re.search(pattern, buf)
            if m:
                return tuple(int(g) for g in m.groups())
    return None


def term_geometry(timeout=0.4):
    """{cols, rows, cell_w, cell_h} of the terminal on file descriptors 0 and 1 from its answers to CSI 18 t and
    CSI 16 t, or None when they are not a terminal or it does not answer."""
    import termios
    import tty
    fd_in, fd_out = 0, 1
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return None
    try:
        old = termios.tcgetattr(fd_in)
    except termios.error:
        return None
    try:
        tty.setraw(fd_in)
        grid = _ask(fd_in, fd_out, b"\x1b[18t", rb"\x1b\[8;(\d+);(\d+)t", timeout)
        cell = _ask(fd_in, fd_out, b"\x1b[16t", rb"\x1b\[6;(\d+);(\d+)t", timeout)
    finally:
        termios.tcsetattr(fd_in, termios.TCSADRAIN, old)
    if not grid or not cell or min(grid + cell) <= 0:
        return None
    return {"rows": grid[0], "cols": grid[1], "cell_h": cell[0], "cell_w": cell[1]}


def _die_with_parent():
    """In a child before exec: a SIGTERM when the player dies, however it dies (Linux; elsewhere nothing), so no mpv is
    left playing and no ffmpeg decoding after a killed player."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class _Audio:
    """mpv playing the first file's sound, driven over its JSON IPC socket; its position is the clock."""

    def __init__(self, path, sock_path):
        self.path, self.rid, self.buf = sock_path, 0, b""
        self.proc = subprocess.Popen(["mpv", "--no-video", "--no-terminal", "--really-quiet", "--pause",
                                      "--keep-open=yes", "--audio-display=no", f"--input-ipc-server={sock_path}",
                                      str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent)
        self.sock = None
        for _ in range(150):                                      # up to 3 s for mpv to open its socket
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(sock_path)
                self.sock = s
                break
            except OSError:
                time.sleep(0.02)
        if self.sock is None:
            self.close()
            raise PlayError("mpv did not open its IPC socket for the sound")

    def send(self, *command):
        self.rid += 1
        self.sock.sendall(json.dumps({"command": list(command), "request_id": self.rid}).encode() + b"\n")
        return self.rid

    def get(self, prop, timeout=0.2):
        rid = self.send("get_property", prop)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            ready, _, _ = select.select([self.sock], [], [], 0.01)
            if ready:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("request_id") == rid:
                    return msg.get("data")
        return None

    def close(self):
        try:
            if self.sock is not None:
                self.send("quit")
                self.sock.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if os.path.exists(self.path):
            os.unlink(self.path)


class _Frames:
    """ffmpeg's frames from a start time: frame k of the run shows at start + k / fps."""

    def __init__(self, items, fps):
        self.items, self.fps, self.proc, self.lay = items, float(fps), None, None
        self.start_t, self.k, self.size = 0.0, 0, 0

    def start(self, lay, t):
        self.stop()
        self.lay, self.start_t, self.k = lay, t, 0
        self.size = lay["w"] * lay["h"] * 3
        self.proc = subprocess.Popen(ffmpeg_cmd(self.items, lay, t, self.fps), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.size * 4,
                                     preexec_fn=_die_with_parent)

    def read(self):
        """The next frame's bytes, or None at the end."""
        data = self.proc.stdout.read(self.size)
        if len(data) < self.size:
            return None
        self.k += 1
        return data

    def next_time(self):
        return self.start_t + self.k / self.fps

    def stop(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


class Player:
    """The terminal player: run() until q. `items` are probe() results; the first sets the frame rate, the length and
    the sound; `chapters` are [{name, from, to}]; frames are numbered from `first_frame`."""

    def __init__(self, items, chapters=(), first_frame=0, sound=True, paused=False):
        self.items, self.chapters = items, list(chapters)
        self.fps, self.duration = float(items[0]["fps"]), float(items[0]["duration"])
        self.first_frame = int(first_frame)
        self.sound = sound and items[0]["audio"]
        self.silent_note = ""
        self.paused, self.speed = paused, 1.0
        self.fd_in, self.fd_out = 0, 1
        self.serial, self.frame_dir, self.recent = 0, None, deque()
        self.audio, self.frames = None, _Frames(items, self.fps)
        self.geom, self.lay = None, None
        self.new_geom, self.ask_size_at = None, 0.0          # a size the terminal reported; when to ask again
        self.t_media, self.t_wall = 0.0, time.monotonic()
        self.last_status, self.next_sync = "", 0.0

    # ---- the clock: media time anchored to the wall clock, re-anchored to the sound now and then
    def now(self):
        if self.paused:
            return self.t_media
        return self.t_media + (time.monotonic() - self.t_wall) * self.speed

    def anchor(self, t):
        self.t_media, self.t_wall = t, time.monotonic()

    # ---- the terminal
    def out(self, s):
        os.write(self.fd_out, s.encode() if isinstance(s, str) else s)

    def show(self, data):
        self.serial += 1
        path = os.path.join(self.frame_dir, f"{self.serial}.rgb")
        with open(path, "wb") as fh:
            fh.write(data)
        self.recent.append(path)
        while len(self.recent) > KEEP_FRAMES:
            try:
                os.unlink(self.recent.popleft())
            except FileNotFoundError:
                pass
        lay = self.lay
        self.out(f"\x1b[{lay['row'] + 1};{lay['col'] + 1}H\x1b_Ga=T,t=f,f=24,s={lay['w']},v={lay['h']},i={KITTY_ID},"
                 f"p=1,X={lay['X']},Y={lay['Y']},C=1,q=2;{base64.b64encode(path.encode()).decode()}\x1b\\")

    def status(self, force=False):
        t = self.now()
        k = chapter_at(self.chapters, t)
        icon = "\u275a\u275a" if self.paused else "\u25b6"
        speed = "" if self.speed == 1.0 else f"  {self.speed:g}x"
        text = (f" {icon} {t:5.2f} / {self.duration:.2f}  frame {self.first_frame + int(math.floor(t * self.fps + 1e-6))}"
                f"  {self.chapters[k]['name'] if k >= 0 else ''}{speed}{self.silent_note}    {KEYS}")
        text = text[: self.geom["cols"] - 1]
        if force or text != self.last_status:
            self.out(f"\x1b[{self.geom['rows']};1H\x1b[2K\x1b[2m{text}\x1b[0m")
            self.last_status = text

    def relayout(self, geom=None):
        """Lay the picture out for `geom`, else for the size the terminal answers now; clear the pane."""
        geom = geom or term_geometry()
        if geom is None:
            raise PlayError("the terminal does not report its size in pixels (CSI 16 t / 18 t)")
        self.geom, self.lay = geom, layout(self.items, geom)
        self.out(f"\x1b_Ga=d,d=I,i={KITTY_ID},q=2\x1b\\\x1b[2J")

    # ---- moving about
    def seek(self, t):
        last = max(self.duration - 1.0 / self.fps, 0.0)
        t = min(max(t, 0.0), last)
        k = int(math.floor(t * self.fps + 1e-6))
        t = k / self.fps
        self.frames.start(self.lay, t)
        data = self.frames.read()
        if data is not None:
            self.show(data)
        self.anchor(t)
        if self.audio:
            self.audio.send("seek", t, "absolute+exact")
        self.status(force=True)

    def set_paused(self, p):
        t = self.now()
        self.paused = p
        self.anchor(t)
        if self.audio:
            self.audio.send("set_property", "pause", p)
        if not p and t >= self.duration - 1.0 / self.fps:
            self.seek(0.0)
        self.status(force=True)

    def jump_chapter(self, d):
        """To the first frame of the next (d=1) or previous (d=-1) chapter; back to the start of this one first when it
        has played for a while. Chapters start between frames: a chapter's first frame is the first at or after it."""
        if not self.chapters:
            return
        t = self.now()
        k = chapter_at(self.chapters, t)
        if not (d < 0 and k >= 0 and t - self.chapters[k]["from"] > 0.3):
            k = min(max(k + d, 0), len(self.chapters) - 1)
        self.seek(math.ceil(self.chapters[k]["from"] * self.fps - 1e-6) / self.fps)

    def step(self, n):
        if not self.paused:
            self.set_paused(True)
        k = int(math.floor(self.now() * self.fps + 1e-6)) + n
        self.seek(k / self.fps)

    def key(self, data):
        """Act on the keys in `data`, taking out the terminal's size reports (answers to the queries the loop sends
        every second: Tern resizes a pane without telling the kernel, so no SIGWINCH comes); False to quit."""
        for m in SIZE_REPORT.finditer(data):
            g = dict(self.new_geom or self.geom)
            a, b = int(m.group(2)), int(m.group(3))
            if m.group(1) == b"8":
                g["rows"], g["cols"] = a, b
            else:
                g["cell_h"], g["cell_w"] = a, b
            if min(g.values()) > 0 and g != self.geom:
                self.new_geom = g
        seqs = re.findall(rb"\x1b\[[0-9;]*[A-Za-z~]|\x1b.|.", SIZE_REPORT.sub(b"", data), re.S)
        for s in seqs:
            if s in (b"q", b"Q", b"\x03", b"\x1b"):
                return False
            if s == b" ":
                self.set_paused(not self.paused)
            elif s in (b"\x1b[C", b"\x1b[D"):
                self.seek(self.now() + (5.0 if s == b"\x1b[C" else -5.0))
            elif s in (b"\x1b[1;2C", b"\x1b[1;2D"):
                self.seek(self.now() + (1.0 if s == b"\x1b[1;2C" else -1.0))
            elif s in (b".", b","):
                self.step(1 if s == b"." else -1)
            elif s in (b"\x1b[5~", b"\x1b[6~"):
                self.jump_chapter(-1 if s == b"\x1b[5~" else 1)
            elif s in (b"\x1b[H", b"\x1b[1~", b"0"):
                self.seek(0.0)
            elif s.decode("latin-1") in SPEEDS:
                t = self.now()
                self.speed = SPEEDS[s.decode()]
                self.anchor(t)
                if self.audio:
                    self.audio.send("set_property", "speed", self.speed)
                self.status(force=True)
            elif s in (b"m", b"M") and self.audio:
                self.audio.send("cycle", "mute")
        return True

    # ---- the loop
    def run(self):
        import termios
        import tty
        if not (os.isatty(self.fd_in) and os.isatty(self.fd_out)):
            raise PlayError("the player draws in a terminal: run it in one, or pass --split right to open a Tern block")
        old = termios.tcgetattr(self.fd_in)

        def leave(signum, _frame):                                # the pane closed or a kill: clean up, then leave
            raise SystemExit(128 + signum)

        prev = {signal.SIGWINCH: signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "ask_size_at", 0.0))}
        for s in (signal.SIGHUP, signal.SIGTERM):
            prev[s] = signal.signal(s, leave)
        base = os.environ.get("XDG_RUNTIME_DIR")
        self.frame_dir = tempfile.mkdtemp(prefix="tern-video-block-", dir=base if base and os.path.isdir(base) else None)
        try:
            tty.setraw(self.fd_in)
            title = " | ".join(os.path.basename(it["path"]) for it in self.items)
            self.out(f"\x1b]2;{title}\x07\x1b[?1049h\x1b[?25l\x1b[2J")
            self.relayout()
            if self.sound:
                if shutil.which("mpv") is None:
                    self.silent_note = "  silent (no mpv)"
                else:
                    self.audio = _Audio(self.items[0]["path"], os.path.join(self.frame_dir, "mpv.sock"))
            want_paused = self.paused
            self.paused = True
            self.seek(0.0)
            if not want_paused:
                self.set_paused(False)
            while True:
                if time.monotonic() >= self.ask_size_at:
                    self.ask_size_at = time.monotonic() + 1.0
                    self.out("\x1b[18t\x1b[16t")
                if self.new_geom:
                    t, geom, self.new_geom = self.now(), self.new_geom, None
                    self.relayout(geom)
                    self.seek(t)
                timeout = 0.25 if self.paused else max(0.0, (self.frames.next_time() - self.now()) / self.speed)
                ready, _, _ = select.select([self.fd_in], [], [], min(timeout, 0.25))
                if ready:
                    data = os.read(self.fd_in, 256)
                    while re.search(rb"\x1b(\[[0-9;]*)?$", data) and select.select([self.fd_in], [], [], 0.05)[0]:
                        data += os.read(self.fd_in, 256)          # the rest of a sequence the read cut off
                    if not self.key(data):
                        break
                    continue
                if self.paused:
                    self.status()
                    continue
                t = self.now()
                if t >= self.duration - 0.5 / self.fps:            # the end: round again
                    self.seek(0.0)
                    continue
                data = None
                while self.frames.next_time() <= t:                # late frames are skipped, the newest shown
                    data = self.frames.read()
                    if data is None:
                        break
                if data is not None:
                    self.show(data)
                if self.audio and time.monotonic() >= self.next_sync:
                    self.next_sync = time.monotonic() + 0.5
                    pos = self.audio.get("time-pos")
                    if isinstance(pos, (int, float)) and abs(pos - self.now()) > 0.04:
                        self.anchor(float(pos))
                self.status()
        finally:
            self.frames.stop()
            if self.audio:
                self.audio.close()
            if self.frame_dir:
                shutil.rmtree(self.frame_dir, ignore_errors=True)
            try:                                                   # the pane may be gone already
                self.out(f"\x1b_Ga=d,d=I,i={KITTY_ID},q=2\x1b\\\x1b[2J\x1b[?25h\x1b[?1049l")
                termios.tcsetattr(self.fd_in, termios.TCSADRAIN, old)
            except (OSError, termios.error):
                pass
            for s, handler in prev.items():
                signal.signal(s, handler)


def open_in_tern(argv, where):
    """Runs this player with `argv` in a new Tern block, `where` "right" / "down" (a split of the pane this runs in,
    else of the focused one) or "tab", and focuses it (tern split leaves the focus where it was). Returns the block id."""
    tern = shutil.which("tern")
    if tern is None:
        raise PlayError("tern is not on the PATH: --split and --tab open a Tern block")
    launch = ["--", sys.executable, os.path.abspath(__file__), *argv]
    if where == "tab":
        cmd = [tern, "new", "tab", "--json", *launch]
    else:
        cmd = [tern, "split", os.environ.get("TERN_PANE") or "@focused", where, "--json", *launch]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PlayError(f"tern could not open a block: {(r.stderr or r.stdout).strip()}")
    block = json.loads(r.stdout)["block"]
    subprocess.run([tern, "focus", str(block)], capture_output=True)
    return block


HELP = """Play video files in this terminal through the kitty graphics protocol (made for Tern): every file side by side, in
sync, looping, with the first file's sound; a status line with the time, the frame and the chapter.

Keys: space play / pause, left / right 5 s (shift: 1 s), . and , one frame on / back (pausing), PgUp / PgDn the previous /
next chapter, Home or 0 the start, 1 2 3 speed 0.25x / 0.5x / 1x, m mute, q quit.

Examples:
  tern-video-block clip.mp4
  tern-video-block --split right wide.mp4 tall.mp4          # a new Tern block beside this pane, focused
  tern-video-block --chapters shots.ffmeta --first-frame 61 cut.mp4
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="tern-video-block", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", metavar="FILE", help="video files, played side by side")
    p.add_argument("--chapters", metavar="FILE", help="chapters from an FFMETADATA file (default: the first file's own)")
    p.add_argument("--first-frame", type=int, default=0, metavar="N", help="the number of the first frame (default 0)")
    p.add_argument("--paused", action="store_true", help="open on the first frame, paused (space plays)")
    p.add_argument("--no-sound", action="store_true", help="play without sound")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--split", choices=("right", "down"), help="open in a new Tern block beside this pane, focused")
    where.add_argument("--tab", action="store_true", help="open in a new Tern tab")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    try:
        files = [os.path.abspath(f) for f in args.files]
        for f in files:
            if not os.path.isfile(f):
                raise PlayError(f"{f}: no such file")
        chapters_file = os.path.abspath(args.chapters) if args.chapters else None
        if args.split or args.tab:
            rest = [*files, "--first-frame", str(args.first_frame)]
            rest += ["--chapters", chapters_file] if chapters_file else []
            rest += ["--paused"] if args.paused else []
            rest += ["--no-sound"] if args.no_sound else []
            print(json.dumps({"block": open_in_tern(rest, "tab" if args.tab else args.split)}))
            return 0
        if shutil.which("ffmpeg") is None:
            raise PlayError("ffmpeg is not on the PATH")
        items = [probe(f) for f in files]
        chapters = read_chapters(chapters_file) if chapters_file else items[0]["chapters"]
        Player(items, chapters, args.first_frame, sound=not args.no_sound, paused=args.paused).run()
    except PlayError as e:
        print(f"tern-video-block: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
