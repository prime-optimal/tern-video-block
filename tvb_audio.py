"""Audio support for tern-video-block: the playlist, zoom, the file picker, and the AudioMixin for Player."""
import contextlib
import math
import os
import re
import shutil
import signal
import subprocess

import tvb_cut
import tvb_vis

from tvb_common import PlayError

AUDIO_EXT = {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".aif", ".aiff"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
             ".ogv", ".3gp"}                              # the extensions the Tern plugin routes here
AUDIO_KEYS = ("space play/pause  \u2190\u2192 5 s  \u2191\u2193 file  {zoom}i o x c range cut  b B [ ] mark  "
              "n v track  O open  m mute  q quit")
ZOOM_KEYS = "- = zoom  z whole  "
CSI_U = re.compile(rb"^\x1b\[(\d+)(?:;\d+)?(?::\d+)?[u~]$")


def audio_file(path):
    """Whether a path's extension is one of the audio formats the block opens as sound (rather than by its streams)."""
    return os.path.splitext(str(path))[1].lower() in AUDIO_EXT


def probe_audio(path, info, streams, chapters):
    """Probe response for an audio-only file: {path, w, h as the visualizer's shape, fps 30, duration, audio, waveform,
    chapters (the file's own, read by the caller)}. Raises PlayError when there is no duration or no sound."""
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_audio:
        raise PlayError(f"{path}: no video and no sound stream")
    duration = float((info.get("format") or {}).get("duration") or 0.0)
    duration = duration or float(next(s for s in streams if s.get("codec_type") == "audio").get("duration") or 0.0)
    if duration <= 0:
        raise PlayError(f"{path}: no duration")
    return {"path": str(path), "w": tvb_vis.AUDIO_SIZE[0], "h": tvb_vis.AUDIO_SIZE[1], "fps": 30.0, "duration": duration,
            "audio": True, "waveform": True, "chapters": chapters, "info": tvb_cut.describe(info)}


def span_name(span, duration):
    """A span of sound in the shortest form that reads: "45 s", "1m30s", "20m", and "the whole track" for the track's
    own (the last zoom step, when one screen covers all of it)."""
    if not span:
        return "?"
    if duration and span >= duration - 1e-6:
        return "the whole track"
    m, s = divmod(int(round(span)), 60)
    if not m:
        return f"{s} s"
    return f"{m}m{s:02d}s" if s else f"{m}m"


def zoom_chord(seq):
    """1 for a key that means zoom in, -1 for zoom out, 0 for any other key. A terminal in its extended keyboard mode
    writes the keys as key codes rather than as themselves, and the command key's chords come the same way: 43 is "+",
    61 is "=" (zoom in), 45 is "-", 95 is "_" (zoom out), with whatever modifiers a terminal puts beside them. Any
    other key code, and every key written as itself, is not a chord."""
    m = CSI_U.match(seq)
    if not m:
        return 0
    return 1 if m.group(1) in (b"43", b"61") else -1 if m.group(1) in (b"45", b"95") else 0


def natural_key(name):
    """A sort key in the order of Tern's Files pane: the name before its extension first, so "clip.mp4" comes before
    "clip2.mp4" and "clip_cut.mp4"; numbers by value ("clip2" before "clip10"); case ignored."""
    stem, ext = os.path.splitext(name.lower())
    return [[int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)] for s in (stem, ext)] + [name]


def media_neighbour(path, step):
    """The media file `step` places after (1) or before (-1) `path` among the media files of its folder (the extensions
    the Tern plugin routes, hidden files left out, natural order), or None at either end."""
    folder, name = os.path.split(os.path.abspath(path))
    try:
        names = {n for n in os.listdir(folder) if not n.startswith(".")
                 and os.path.splitext(n)[1].lower() in AUDIO_EXT | VIDEO_EXT and os.path.isfile(os.path.join(folder, n))}
    except OSError:
        return None
    names = sorted(names | {name}, key=natural_key)
    k = names.index(name) + step
    return os.path.join(folder, names[k]) if 0 <= k < len(names) else None


def media_files(start, depth=2):
    """The media files under `start` (audio or video), at most `depth` folders down, hidden ones and hidden folders left
    out: what the player's file picker offers. AppleDouble sidecars are left out too: "._track.flac" carries an
    extension and a duration, so it would otherwise be offered as a track of its own."""
    exts, out = AUDIO_EXT | VIDEO_EXT, []
    base = os.path.abspath(start).rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(start):
        if root.rstrip(os.sep).count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        out += [os.path.join(root, f) for f in sorted(files)
                if os.path.splitext(f)[1].lower() in exts and not f.startswith("._")]
    return out


class AudioMixin:
    """Player mixin providing audio-only methods: the visualizer theme, the zoom, the track playlist, and the file
    picker. Methods use only self.* — the mixin never imports tern_video_block."""

    def audio_init(self, items, visualizer, bookmarks):
        """Set up the audio-only state: the visualizer (from a name, the default waveform), the span, the playlist
        (audio-only files), the bookmarks store and the waveform (None until relayout)."""
        self.vis_text = visualizer or ""
        self.visualizer = tvb_vis._visualizer(self.vis_text)
        items = [dict(it) for it in items]
        self.span = tvb_vis.SPAN_DEFAULT if self.visualizer.get("wave") and items[0].get("waveform") else None
        self.tracks = items if all(it.get("waveform") for it in items) else None
        self.track = 0
        self.wave = None
        self.store = bookmarks
        self.marks, self.marks_row, self.last_marks = [], 0, None

    def theme_colors(self):
        """Dress the visualizer in the terminal's own colors, asked for once at the start of the run (the answers are
        the pane's, not this process's) and before the first layout draws anything. A terminal that answers nothing
        leaves TERM_COLORS; a visualizer the caller named in full (an ffmpeg graph) is theirs as it stands."""
        self.visualizer = tvb_vis._visualizer(self.vis_text, tvb_vis.term_colors())

    def title(self):
        """The terminal's title: the files playing, and which track of the playlist they are."""
        names = " | ".join(os.path.basename(it["path"]) for it in self.items)
        if self.tracks and len(self.tracks) > 1:
            names = f"{names}  ({self.track + 1}/{len(self.tracks)})"
        self.out(f"\x1b]2;{names}\x07")

    def note(self, text):
        """A line at the bottom row instead of the status line (an error the next status redraw takes back)."""
        self.out(f"\x1b[{self.status_row};1H\x1b[2K\x1b[2m{tvb_cut.fit(text, self.geom['cols'] - 1)}\x1b[0m")
        self.last_status = ""

    def next_frame_in(self):
        """How long until the pane has something new to show: for a waveform, the time one pixel of the strip covers
        (the screen is moved a pixel at a time), else the time of the next frame the graph draws."""
        if self.wave is not None:
            return self.wave.span / max(1, int(self.lay["w"]))
        return max(0.0, self.frames.next_time() - self.now())

    def zoom(self, d):
        """Zoom the waveform: d = 1 a narrower span, d = -1 a wider one, d = 0 the whole track. The steps are the SPANS
        ladder and the track's own length, so zooming out reaches the whole track (the strip is then the whole track and
        the playhead runs the screen from end to end); zooming in reaches a second a screen. Nothing happens to a
        visualizer that is not a waveform."""
        if self.wave is None:
            return
        now_span = self.span or self.duration
        steps = sorted({round(s, 3) for s in (*tvb_vis.SPANS, self.duration) if s <= self.duration + 1e-6}) or [self.duration]
        if d > 0:
            span = max([s for s in steps if s < now_span - 1e-6] or steps[:1])
        elif d < 0:
            span = min([s for s in steps if s > now_span + 1e-6] or steps[-1:])
        else:
            span = self.duration
        if abs(span - now_span) > 1e-6:
            self.span = span
            self.wave = tvb_vis._Wave(self.items[0], self.visualizer["wave"], span, self.lay)
            self.wave.set_marks(m["at"] for m in self.marks)
        self.status(force=True)
        data = self.wave.frame(self.now())                          # redraw now: the next frame may be a while off
        if data is not None:
            self.show(data)

    def picture(self, t):
        """The pane's picture at time t: a waveform's screen (self.wave.frame, None when it is what is already shown)
        or, for a video or a visualizer drawing as it plays, the first decoded frame of the run at t."""
        if self.wave is not None:
            return self.wave.frame(t)
        self.frames.start(self.lay, t)
        return self.frames.read()

    def at_end(self):
        """The end of the range: move on to the next track when there is a playlist of several audio files; return
        True when a new track has started, so the loop skips back to the top. One track plays as before."""
        if self.tracks and len(self.tracks) > 1:
            self.next_track(1)
            return True
        return False

    def audio_status_extra(self, t):
        """The audio-only status extras ({track}, {span}) and the keys line. "" for a track-less video."""
        track = f"  track {self.track + 1}/{len(self.tracks)}" if self.tracks and len(self.tracks) > 1 else ""
        span = f"  span {span_name(self.span, self.duration)}" if self.wave is not None else ""
        keys = AUDIO_KEYS.format(zoom=ZOOM_KEYS if self.wave is not None else "") if self.tracks else ""
        return track, span, keys

    def audio_layout(self, geom, rows, fit):
        """Lay an audio track's picture out for `geom` with `fit` (the player's layout: the visualizer's own shape at
        the layout's height) and set up the picture: a waveform visualizer makes a _Wave strip (self.wave, a screen at
        a time); any other visualizer draws the sound as it plays (self.wave stays None and the item is given the
        visualizer's picture). Returns the layout."""
        item = self.items[0]
        shape = dict(item, w=float(tvb_vis.VIS_SIZE[0]), h=float(tvb_vis.VIS_SIZE[1]))
        self.lay = fit([shape], geom, rows)
        if self.visualizer.get("wave"):
            self.wave = tvb_vis._Wave(item, self.visualizer["wave"], self.span or self.duration, self.lay)
            self.wave.set_marks(m["at"] for m in self.marks)
        else:
            self.wave = None
            self.items[0] = tvb_vis.visualize(item, self.lay, self.visualizer, self.fps)
        return self.lay

    def play_track(self, k):
        """Play track k of the playlist, by its index; nothing when there is no such track."""
        if not self.tracks or not 0 <= k < len(self.tracks):
            return
        self.restart([self.tracks[k]], self.tracks, k)

    def next_track(self, d):
        """The next (d=1) or previous (d=-1) track of the playlist, wrapping; one file alone or a video plays on."""
        if not self.tracks or len(self.tracks) < 2:
            return
        self.play_track((self.track + d) % len(self.tracks))

    def open_file(self, paths):
        """Play `paths` in place of what is playing: audio files become the playlist (the first playing now), video
        files (or a mix) play side by side as they do at the start."""
        paths = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
        if not paths:
            return
        try:
            items = self.probe_files(paths)
        except PlayError as e:
            self.note(str(e))
            return
        if all(it.get("waveform") for it in items):
            self.restart([items[0]], items, 0)
        else:
            self.restart(items, None, 0)

    def open_neighbour(self, step):
        """Play the next (1) or previous (-1) media file of the folder of the file playing, in place of it; a note at
        either end of the folder."""
        path = media_neighbour(self.items[0]["path"], step)
        if path is None:
            self.note("the last file of the folder" if step > 0 else "the first file of the folder")
            return
        self.open_file([path])

    def pick_file(self, start=None):
        """A path to play, picked from the filesystem: fzf over the media files under `start` (the directory of the
        file playing, two folders deep), else a path typed on the bottom row. None when nothing was picked."""
        start = start or os.path.dirname(os.path.abspath(self.items[0]["path"])) or os.getcwd()
        found = media_files(start)
        fzf = shutil.which("fzf")
        with self.suspended():
            if fzf:
                chosen = subprocess.run([fzf, "--no-sort", "--select-1", "--exit-0", "--prompt", "open > "],
                                        input="\n".join(found), capture_output=True, text=True).stdout.strip()
            else:
                chosen = self.read_line(f"open (of {len(found)} under {start}) > ")
        return [chosen] if chosen else None

    def read_line(self, prompt):
        """A line typed at the bottom row, in the terminal's own line mode (no fzf to pick a file with)."""
        self.out(f"\x1b[{self.status_row};1H\x1b[2K{tvb_cut.fit(prompt, self.geom['cols'] - 1)}")
        raw = b""
        while True:
            c = os.read(self.fd_in, 1)
            if not c or c in (b"\r", b"\n", b"\x03", b"\x1b"):
                break
            raw += c
        return raw.decode("utf-8", "replace").strip()

    @contextlib.contextmanager
    def suspended(self):
        """The terminal back as the player found it while something else (fzf, a typed path) uses it: no image, line
        mode, the cursor shown, the alternate screen left; all of it put back after, the picture redrawn where the
        playing has got to."""
        import termios
        raw = termios.tcgetattr(self.fd_in)
        quiet = signal.signal(signal.SIGINT, signal.SIG_IGN)          # ^C cancels the picker, not the player
        self.relayout(self.geom)
        self.clear_picture()                                          # the picture taken down, the pane cleared
        self.out("\x1b[?7h\x1b[?25h\x1b[?1049l")
        if self.old_termios is not None:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.old_termios)
        try:
            yield
        finally:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, raw)
            signal.signal(signal.SIGINT, quiet)
            self.out("\x1b[?1049h\x1b[?7l\x1b[?25l\x1b[2J")
            t = self.now()
            self.relayout()
            self.seek(t)

    def restart(self, items, tracks=None, track=0):
        """Play `items` now (`tracks` the playlist they are from, `track` the one current, or None for files playing
        side by side): the frames, the clock, the chapters, the sound and the title all start over on them."""
        self.tracks, self.track = tracks, track
        self.items = [dict(it) for it in items]
        self.fps, self.duration = float(self.items[0]["fps"]), float(self.items[0]["duration"])
        self.start, self.end = 0.0, self.duration
        self.chapters = list(self.items[0].get("chapters", []))
        self.sound = bool(self.items[0]["audio"])
        self.speed = 1.0
        was_paused = self.paused
        self.paused = True
        self.reset_frames()
        self.open_audio()
        self.load_marks()
        self.title()
        self.relayout(self.geom)
        self.seek(self.start)
        self.set_paused(was_paused)

    def audio_key(self, s):
        """Audio keys: zoom (the - + / = keys and extended keyboard zoom chords, z the whole track), the playlist (n, v),
        another file (O), the folder's files (up and down arrows). True when handled."""
        d = zoom_chord(s)
        if d:
            self.zoom(d)
            return True
        if s == b"n":
            self.next_track(1)
            return True
        if s == b"v":
            self.next_track(-1)
            return True
        if s in (b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB"):
            self.open_neighbour(-1 if s.endswith(b"A") else 1)
            return True
        if s == b"O":
            paths = self.pick_file()
            if paths:
                self.open_file(paths)
            return True
        if self.wave is not None:
            if s in (b"-", b"_"):
                self.zoom(-1)
                return True
            if s in (b"+", b"="):
                self.zoom(1)
                return True
            if s in (b"z", b"Z"):
                self.zoom(0)
                return True
        return False