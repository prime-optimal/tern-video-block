"""tern-video-block: video and audio files played in a terminal through the kitty graphics protocol (made for Tern;
tested there only), with their sound.

Audio files (wav, mp3, flac, ogg, and what ffmpeg reads as sound alone: opus, m4a, aac, aiff, wma) play as their own
sound, drawn in the pane in the terminal's own colors (asked for over OSC 10, 11 and 4; N). A waveform is not drawn a
frame at a time: the track is rendered once as one long strip of waveform (ffmpeg's showwavespic) and the pane shows a
screen of it, sliding along as the sound plays, so one ffmpeg call covers eight screens of sound, a seek is where in the
strip the screen is taken from, and the whole of a track of any length it has already drawn — the - and = keys (and
cmd+- / cmd+=, where the terminal passes them through) zoom the span out to the whole track and back in, and z shows it.
The other visualizers (showwaves' own modes, showspectrum, showcqt, avectorscope, …) draw the sound as it plays, a
frame at a time, as a video's own picture is drawn. One audio file is one track; several are a playlist (n previous, v
next) and o opens another file, playing it in place.

Video files still play side by side in one picture, at one height, fitted into the pane and centred to the pixel, in
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
import contextlib
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

import tvb_bookmarks

__version__ = "0.1.0"

KITTY_ID = 7311                                   # the image the player replaces frame after frame
KEEP_FRAMES = 30                                  # frame files kept after showing them (the terminal may read late)
SPEEDS = {"1": 0.25, "2": 0.5, "3": 1.0}
KEYS = ("space play/pause  \u2190\u2192 5 s  , . frame  PgUp/PgDn chapter  b B [ ] mark  1 2 3 speed  m mute  "
        "q quit")
AUDIO_KEYS = "space play/pause  \u2190\u2192 5 s  {zoom}b B [ ] mark  n v track  o open  m mute  q quit"
ZOOM_KEYS = "- = zoom  z whole  "
SIZE_REPORT = re.compile(rb"\x1b\[(8|6);(\d+);(\d+)t")    # the answers to CSI 18 t (rows, cols) and CSI 16 t (cell px)
AUDIO_EXT = {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".aif", ".aiff"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
             ".ogv", ".3gp"}                              # the extensions the Tern plugin routes here
AUDIO_SIZE = (16, 9)                                      # an audio track's picture: the visualizer's own shape
VIS_MODES = ("line", "p2p", "cline")                       # showwaves' own modes, by name
VIS_SIZE = (1280, 720)                                     # the picture a visualizer draws, scaled to the pane's layout
STRIP_SCREENS = 8                                          # screens of the track one rendered strip of waveform covers
SPANS = (1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0, 180.0, 600.0, 1800.0, 3600.0)   # seconds one screen of waveform covers
SPAN_DEFAULT = 90.0                                        # what a track starts at: a minute and a half to a screen
# A visualizer is drawn in the terminal's own colors: its foreground for the waveform, the palette's red for the
# playhead, and its background behind both (OSC 10, 11 and 4; N, asked for once at the start of the run). TERM_COLORS
# is what a terminal that does not answer them gets: a light grey waveform on a near-black pane.
TERM_COLORS = {"fg": "#e6e6e6", "bg": "#101010", "dim": "#303030", "red": "#e06c75", "blue": "#61afef",
               "magenta": "#c678dd", "cyan": "#56b6c2", "yellow": "#e5c07b", "green": "#98c379"}
PALETTE_NAMES = {0: "dim", 1: "red", 2: "green", 3: "yellow", 4: "blue", 5: "magenta", 6: "cyan"}
COLOR_QUERIES = b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\" + b"".join(f"\x1b]4;{n};?\x1b\\".encode() for n in PALETTE_NAMES)
COLOR_REPLY = re.compile(rb"\x1b\](10|11|4;\d+);rgb:([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})")
# A waveform is not drawn frame by frame as the sound plays: the track is rendered once as a strip of waveform
# (showwavespic, STRIP_SCREENS screens wide) and the pane shows a screen of it, moved along as the time goes. One
# ffmpeg call per STRIP_SCREENS screens of sound instead of one per frame, the whole track a strip drawn once, and a
# seek is where in the strip the screen is taken from (no decoding at all). A visualizer that is not a waveform draws
# its own picture as the sound plays (showspectrum, showcqt, avectorscope), a frame at a time, as before.
WAVE_LOOK = {"mode": "cline", "draw": "scale", "filter": "peak", "scale": "lin"}
# The named visualizers, in the theme's colors. "wave" ones are waveform strips ({mode} {draw} {filter} {scale} tell
# showwavespic how to draw, {fg} is the wave and {red} the playhead); the rest are graphs drawing the sound as it plays,
# with {w} {h} {fps} and the theme's colors ({fg} {bg} {dim} {red} {green} {yellow} {blue} {magenta} {cyan}) filled in
# — the pseudocolor ones take the theme's own colors as 0xRRGGBB. A name in neither is an ffmpeg graph of the caller's.
VIS_PRESETS = {
    "wavespic": {"wave": {**WAVE_LOOK, "draw": "full"}},             # the wave filled in, a solid envelope
    "envelope": {"wave": {**WAVE_LOOK, "draw": "full", "filter": "average"}},
    "spectrum": {"filter": "showspectrum=s={w}x{h}:scale=cbrt:slide=scroll:color=intensity:legend=0,"
                           "pseudocolor=c0={bg0x}:c1={blue0x}:c2={cyan0x}:c3={magenta0x}"},
    "spectrogram": {"filter": "showspectrum=s={w}x{h}:scale=cbrt:slide=scroll:color=intensity:legend=0,"
                              "pseudocolor=c0={bg0x}:c1={blue0x}:c2={cyan0x}:c3={magenta0x}"},
    "cqt": {"filter": "showcqt=s={w}x{h}:count=6:gamma=3:bar_g=3:sono_g=4:axis=0:cscheme={cscheme}"},
    "vectorscope": {"filter": "avectorscope=s={w}x{h}:mode=lissajous_xy:draw=line:scale=lin:rate={fps}", "bg": "{bg}"},
    "spectrumpic": {"filter": "showspectrumpic=s={w}x{h}:scale=cbrt:color=intensity:legend=0,"
                              "pseudocolor=c0={bg0x}:c1={blue0x}:c2={cyan0x}:c3={magenta0x}"},
}
for _mode in VIS_MODES:                                              # showwaves' own modes, as strips of their own
    VIS_PRESETS.setdefault(_mode, {"wave": {**WAVE_LOOK, "mode": _mode}})
for _map in ("magma", "inferno", "viridis", "plasma", "turbo", "rainbow", "fire", "cool", "green", "nebulae", "fiesta"):
    VIS_PRESETS.setdefault(_map, {"filter": f"showspectrum=s={{w}}x{{h}}:scale=cbrt:slide=scroll:color={_map}:legend=0"})


class PlayError(Exception):
    """A file or a terminal the player cannot play in; the message says which and why."""


def audio_file(path):
    """Whether a path's extension is one of the audio formats the block opens as sound (rather than by its streams)."""
    return os.path.splitext(str(path))[1].lower() in AUDIO_EXT


def _visualizer(text, colors=None):
    """{wave, filter, size, bg} for an audio visualizer, drawn in `colors` (the terminal's own, TERM_COLORS by
    default). An empty name, a showwaves mode (line, p2p, cline) or a preset (wavespic, envelope, spectrum, cqt,
    vectorscope, spectrumpic, or one of showspectrum's own colormaps: magma, viridis, …) is that visualizer; any other
    text is an ffmpeg filtergraph, with {fg} {bg} {dim} {red} {green} {yellow} {blue} {magenta} {cyan} {w} {h} {fps}
    filled in. A waveform visualizer is {wave: {mode, draw, filter, scale, fg, red, bg}} (see _Wave: the whole track is
    rendered as one strip and the pane shows a screen of it); any other is {filter: graph, bg: color}, drawn a frame at
    a time. Raises PlayError for a graph with quotes or control characters (ffmpeg's filtergraph syntax has no place
    for either)."""
    c = dict(TERM_COLORS, **(colors or {}))
    name = (text or "").strip()
    spec: dict = {"wave": dict(WAVE_LOOK)} if not name else dict(VIS_PRESETS[name]) if name in VIS_PRESETS else {}
    if not spec and (any(ch in name for ch in "'\"\\") or any(ord(ch) < 32 for ch in name)):
        raise PlayError(f"visualizer {text!r}: an ffmpeg filtergraph, with no quotes or control characters")
    if not spec:
        spec = {"filter": name}                                  # a filtergraph of the caller's own
    if "wave" in spec:
        spec["wave"] = {**spec["wave"], "fg": c["fg"], "red": c["red"], "bg": c["bg"], "mark": c["yellow"]}
    else:
        graph = spec["filter"]
        if "showwaves" in graph and re.search(r"showwaves[^,;]*[:=]s=", graph) is None:
            graph = (graph.replace("showwaves=", f"showwaves=s={VIS_SIZE[0]}x{VIS_SIZE[1]}:", 1) if "showwaves=" in graph
                     else graph.replace("showwaves", f"showwaves=s={VIS_SIZE[0]}x{VIS_SIZE[1]}", 1))
        cscheme = "|".join(_rgb01(c[n]) for n in ("cyan", "magenta"))     # showcqt's own two-color scheme
        for key, value in {"{fg}": c["fg"], "{bg}": c["bg"], "{dim}": c["dim"], "{red}": c["red"], "{green}": c["green"],
                           "{yellow}": c["yellow"], "{blue}": c["blue"], "{magenta}": c["magenta"], "{cyan}": c["cyan"],
                           "{bg0x}": "0x" + c["bg"][1:], "{blue0x}": "0x" + c["blue"][1:],
                           "{cyan0x}": "0x" + c["cyan"][1:], "{magenta0x}": "0x" + c["magenta"][1:],
                           "{w}": str(VIS_SIZE[0]), "{h}": str(VIS_SIZE[1]), "{cscheme}": cscheme}.items():
            graph = graph.replace(key, value)
        spec["filter"], spec["bg"] = graph, (c["bg"] if spec.get("bg") in ("{bg}", None) else spec["bg"])
    spec.setdefault("size", list(VIS_SIZE))
    return spec


def _rgb01(color):
    """A "#rrggbb" color as showcqt's cscheme wants it: its three channels from 0 to 1, "|"-separated."""
    return "|".join(f"{int(color[i:i + 2], 16) / 255:.3f}" for i in (1, 3, 5))


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


def clock(t, length):
    """t as a clock: "04:35", or "01:02:10" when `length` (the file's) reaches an hour, so a time and its file's length
    always read in the same form."""
    h, rest = divmod(max(int(t), 0), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if length >= 3600 else f"{m:02d}:{s:02d}"


CSI_U = re.compile(rb"^\x1b\[(\d+)(?:;\d+)?(?::\d+)?[u~]$")


def zoom_chord(seq):
    """1 for a key that means zoom in, -1 for zoom out, 0 for any other key. A terminal in its extended keyboard mode
    writes the keys as key codes rather than as themselves, and the command key's chords come the same way: 43 is "+",
    61 is "=" (zoom in), 45 is "-", 95 is "_" (zoom out), with whatever modifiers a terminal puts beside them. Any
    other key code, and every key written as itself, is not a chord."""
    m = CSI_U.match(seq)
    if not m:
        return 0
    return 1 if m.group(1) in (b"43", b"61") else -1 if m.group(1) in (b"45", b"95") else 0


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
    """What the player needs of a media file: {path, w, h (as displayed: rotation and pixel aspect applied; an audio
    file takes the visualizer's), fps, duration, audio (it has a sound stream), waveform (sound alone: the picture is
    the visualizer's, drawn from the sound), chapters}. An audio-only file is probed for its sound; a video file for
    its picture."""
    info = _ffprobe(["-show_streams", "-show_format", "-show_chapters", str(path)])
    streams = info.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    duration = float((info.get("format") or {}).get("duration") or 0.0)
    if video is None:
        if not has_audio:
            raise PlayError(f"{path}: no video and no sound stream")
        duration = duration or float(next(s for s in streams if s.get("codec_type") == "audio").get("duration") or 0.0)
        if duration <= 0:
            raise PlayError(f"{path}: no duration")
        return {"path": str(path), "w": AUDIO_SIZE[0], "h": AUDIO_SIZE[1], "fps": 30.0, "duration": duration,
                "audio": True, "waveform": True, "chapters": _chapters(info.get("chapters", []))}
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
    duration = duration or float(video.get("duration") or 0.0)
    if duration <= 0:
        raise PlayError(f"{path}: no duration")
    return {"path": str(path), "w": w, "h": h, "fps": fps, "duration": duration, "audio": has_audio,
            "waveform": False, "chapters": _chapters(info.get("chapters", []))}


def visualize(item, lay, vis, fps):
    """The item with `vis`'s waveform drawn as its picture, for a track playing sound alone: {path, w, h (as displayed:
    the visualizer's picture scaled to the layout's height, keeping its aspect), fps, duration, audio, waveform,
    visualization ({filter, size}), chapters}. The picture is the visualizer's, so the item keeps its duration and its
    sound."""
    bw, bh = float(vis["size"][0]), float(vis["size"][1])
    height = max(2, int(lay["h"]))
    width = max(2, int(round(bw * height / bh / 2)) * 2)
    return {**item, "w": width, "h": height, "fps": float(fps),
            "visualization": {"filter": vis["filter"], "size": [bw, bh], "bg": vis.get("bg")}}


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


def visualize_cmd(item, lay, t, fps):
    """ffmpeg drawing an audio file from time t as raw RGB frames of lay[w] x lay[h] on stdout, at `fps` frames a
    second: the file's sound is read up to t+0.0005 s with atrim (dead accurate where -ss on a lavfi source is not),
    the tail is what the visualizer's graph draws, and its picture is scaled to the layout. A graph that draws its
    waveform on black (visualization["bg"] set) is screened over that color, the pane's own, so the picture is the
    terminal's background with its foreground over it rather than a black rectangle in the middle of the theme; both
    sides are made RGB first, blend's own planes turning every color after the first frame violet. `{fps}` in the graph
    is the frame rate, which the graph's own filters (showwaves, showspectrum, …) otherwise make 25 a second of; -r
    holds the output to the rate the clock counts frames in (the fps filter does that too, and rather more slowly)."""
    vis = item["visualization"]
    graph = vis["filter"].replace("{fps}", f"{fps:.6g}")
    w, h = vis["size"]
    parts = [f"[0:a]atrim=start={max(t - 0.0005, 0.0):.6f},asetpts=N/SR/TB,{graph}[wave]"]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", item["path"]]
    if vis.get("bg"):
        cmd += ["-f", "lavfi", "-i", f"color=c={vis['bg']}:s={int(w)}x{int(h)}:r={fps:.6g}"]
        parts.append("[wave]format=rgb24[w];[1:v]format=rgb24[c];[c][w]blend=all_mode=screen[lit]")
        last = "lit"
    else:
        last = "wave"
    parts.append(f"[{last}]scale={lay['w']}:{lay['h']}:flags=bicubic,setsar=1,format=rgb24[out]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-an", "-r", f"{fps:.6g}", "-f", "rawvideo",
                  "-pix_fmt", "rgb24", "-"]


def wave_cmd(item, wave, t0, t1, sw, sh):
    """ffmpeg drawing one frame of the whole of the sound between t0 and t1, as a waveform picture sw x sh on stdout:
    showwavespic draws everything it is given as a single frame, which is what a strip is. A visualizer asking for a
    background (showwavespic paints black around the wave) has the picture screened over that color, so the strip is
    the pane's own background with its foreground over it rather than a black band across the theme; both sides are made
    RGB first, blend's own planes turning every color after the first frame violet."""
    graph = (f"showwavespic=s={sw}x{sh}:colors={wave['fg']}:scale={wave['scale']}:draw={wave['draw']}"
             f":filter={wave['filter']}")
    parts = [f"[0:a]atrim=start={t0:.6f}:end={t1:.6f},asetpts=N/SR/TB,{graph}[wave]"]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", item["path"]]
    if wave.get("bg"):
        cmd += ["-f", "lavfi", "-i", f"color=c={wave['bg']}:s={sw}x{sh}:r=1"]
        parts.append("[wave]format=rgb24[w];[1:v]format=rgb24[c];[c][w]blend=all_mode=screen[v]")
    else:
        parts.append("[wave]format=rgb24[v]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", "-frames:v", "1", "-f", "rawvideo",
                  "-pix_fmt", "rgb24", "-"]


class _Wave:
    """The waveform of one track: the whole of it drawn as a strip and the pane shown a screen of it, as a player with
    a real waveform does it. `span` is the seconds of sound one screen covers; one strip is STRIP_SCREENS screens wide
    (the whole track, when the span reaches it: then a screen is the track), so ffmpeg is called once per that much
    sound — a seek or a loop is where in the strip the screen is taken from, and nothing is decoded at all while the
    sound plays. `frame(t)` is the pane's picture: the strip under t with the playhead drawn where t is, or None when
    it is the picture the terminal is already showing (the wave moves a pixel at a time and the pane at 30 frames a
    second, so most frames are the same one)."""

    def __init__(self, item, wave, span, lay):
        self.item, self.wave = item, dict(wave)
        self.duration = float(item["duration"]) or 1.0
        self.lay, self.span = lay, max(float(span), 0.05)
        self.pw, self.ph = int(lay["w"]), int(lay["h"])
        self.seconds = min(self.span * STRIP_SCREENS, self.duration)   # the sound one strip covers
        self.count = max(1, math.ceil(self.duration / self.seconds - 1e-9))
        self.n, self.strip, self.sw, self.t0, self.t1, self.px = None, b"", 0, 0.0, 0.0, 0.0
        self.shown, self.renders, self.marks = (-1, -1), 0, ()
        self.line = bytes.fromhex(self.wave["red"].lstrip("#"))        # the playhead, two pixels of it
        self.mark = bytes.fromhex(self.wave.get("mark", TERM_COLORS["yellow"]).lstrip("#"))

    def set_marks(self, marks):
        """Draw these bookmarks (seconds) over the strip from the next frame on."""
        self.marks, self.shown = tuple(marks), (-1, -1)

    def render(self, n):
        """Draw strip n (the sound of strip_seconds from its start) and keep it: sw x ph of raw RGB, the playhead
        column and the pixel a second of it works out to."""
        t0 = n * self.seconds
        t1 = min(t0 + self.seconds, self.duration)
        sw = max(self.pw, min(self.pw * STRIP_SCREENS, int(round((t1 - t0) / self.span * self.pw))))
        r = subprocess.run(wave_cmd(self.item, self.wave, t0, t1, sw, self.ph),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if len(r.stdout) < sw * self.ph * 3:
            raise PlayError(f"ffmpeg drew no waveform for {os.path.basename(str(self.item['path']))}")
        self.n, self.strip, self.sw, self.t0, self.t1 = n, r.stdout, sw, t0, t1
        self.px, self.shown, self.renders = sw / (t1 - t0), (-1, -1), self.renders + 1

    def frame(self, t):
        """The pane's picture at time t, or None when it is the one already shown: the strip's columns a screen wide
        under t (the playhead centred until the strip's own ends come into it), the rest of the pane the strip already
        has, and the playhead over it."""
        t = min(max(t, 0.0), self.duration)
        n = min(int(t / self.seconds), self.count - 1)
        if self.strip is None or n != self.n:
            self.render(n)
        col = min(max(int((t - self.t0) * self.px), 0), self.sw - 1)   # the playhead's column in the strip
        off = min(max(col - self.pw // 2, 0), max(self.sw - self.pw, 0))
        x = col - off
        if (x, off) == self.shown:
            return None
        self.shown = (x, off)
        row, out = self.sw * 3, bytearray(self.pw * self.ph * 3)
        for r in range(self.ph):
            b = r * row + off * 3
            out[r * self.pw * 3:(r + 1) * self.pw * 3] = self.strip[b:b + self.pw * 3]
        for at in self.marks:                                          # bookmarks: a dashed column, a tab on top
            mx = int((at - self.t0) * self.px) - off
            if not (self.t0 <= at <= self.t1 and 0 <= mx < self.pw):
                continue
            for r in range(self.ph):
                if r < 6 or (r // 4) % 2 == 0:
                    i = r * self.pw * 3 + mx * 3
                    out[i:i + 3] = self.mark
                    if r < 6 and mx + 2 < self.pw:
                        out[i + 3:i + 9] = self.mark * 2
        for r in range(self.ph):                                       # the playhead, and not one pixel past the edge
            i = r * self.pw * 3 + x * 3
            out[i:i + 3], edge = self.line, self.pw * 3 - x * 3
            if edge > 3:
                out[i + 3:i + 6] = self.line
        return bytes(out)


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


def term_colors(timeout=0.3):
    """The terminal's own colors as {fg, bg, dim, red, green, yellow, blue, magenta, cyan} of "#rrggbb", from its
    answers to OSC 10, 11 and 4; N on file descriptions 0 and 1, with TERM_COLORS for whatever it leaves unanswered (a
    terminal that draws no answers at all gives TERM_COLORS back whole). Terminal palette colors, so a visualizer is
    drawn in the theme the pane is already in rather than in one of its own."""
    import termios
    import tty
    fd_in, fd_out = 0, 1
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return dict(TERM_COLORS)
    try:
        old = termios.tcgetattr(fd_in)
    except termios.error:
        return dict(TERM_COLORS)
    colors, buf, t0 = {}, b"", time.monotonic()
    try:
        tty.setraw(fd_in)
        os.write(fd_out, COLOR_QUERIES)
        while time.monotonic() - t0 < timeout:
            ready, _, _ = select.select([fd_in], [], [], 0.02)
            if not ready:
                continue
            buf += os.read(fd_in, 64)
            for m in COLOR_REPLY.finditer(buf):
                which = m.group(1)
                rgb = "#" + "".join(g[:2].decode().lower() for g in m.groups()[1:])
                colors["fg" if which == b"10" else "bg" if which == b"11" else
                       PALETTE_NAMES.get(int(which.split(b";")[1]))] = rgb
            if len(colors) >= 2 + len(PALETTE_NAMES):
                break
    finally:
        termios.tcsetattr(fd_in, termios.TCSADRAIN, old)
    return {**TERM_COLORS, **{k: v for k, v in colors.items() if k and v}}


def _die_with_parent():
    """In a child before exec: a SIGTERM when the player dies, however it dies (Linux; elsewhere nothing), so no mpv is
    left playing and no ffmpeg decoding after a killed player."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class _Audio:
    """mpv playing the first file's sound, driven over its JSON IPC socket; its position is the clock. It stays open at
    the end of the file without pausing (a pause there would outlast the loop back to the start), and it reads no mpv
    configuration or scripts (resume files, media-key scripts), so only the player moves it."""

    def __init__(self, path, sock_path):
        self.path, self.rid, self.buf = sock_path, 0, b""
        self.proc = subprocess.Popen(["mpv", "--no-config", "--load-scripts=no", "--no-video", "--no-terminal",
                                      "--really-quiet", "--pause", "--keep-open=yes", "--keep-open-pause=no",
                                      "--audio-display=no", f"--input-ipc-server={sock_path}", str(path)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=_die_with_parent)
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


ERR_TAG = re.compile(r"\[[^\]]*\]\s*")                     # ffmpeg's own "[filter @ 0x…] " prefixes


def _why(stderr):
    """The one line of ffmpeg's stderr that says what went wrong with: the tags off, its generic "Error :" tail and
    the threading noise it prints after a filter has already failed left out."""
    lines = [ERR_TAG.sub("", l).strip() for l in (stderr or "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("Error :") and "Task finished with error" not in l
             and "Terminating thread" not in l]
    return lines[-1][:200] if lines else ""


class _Frames:
    """ffmpeg's frames from a start time: frame k of the run shows at start + k / fps."""

    def __init__(self, items, fps):
        self.items, self.fps, self.proc, self.lay = items, float(fps), None, None
        self.start_t, self.k, self.size = 0.0, 0, 0

    def start(self, lay, t):
        self.stop()
        self.lay, self.start_t, self.k = lay, t, 0
        self.size = lay["w"] * lay["h"] * 3
        item = self.items[0]
        cmd = visualize_cmd(item, lay, t, self.fps) if item.get("visualization") else ffmpeg_cmd(self.items, lay, t, self.fps)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.size * 4,
                                     preexec_fn=_die_with_parent)

    def read(self):
        """The next frame's bytes, or None at the end. ffmpeg that stopped before drawing a frame at all (a filter it
        will not run, a file it will not draw) says so here rather than leaving the pane blank without a word."""
        data = self.proc.stdout.read(self.size)
        if len(data) < self.size:
            if self.k == 0 and self.proc is not None and self.proc.poll() is not None:
                name = os.path.basename(str(self.items[0]["path"]))
                what = "the visualizer drew nothing for" if self.items[0].get("visualization") else "ffmpeg drew nothing for"
                raise PlayError(f"{what} {name}: {self.why() or 'ffmpeg gave no reason'}")
            return None
        self.k += 1
        return data

    def why(self):
        """ffmpeg's own words: the same command run again for its first frame alone, which is as long as a graph
        drawing the sound on a colour of its own would otherwise run for."""
        item = self.items[0]
        cmd = (visualize_cmd(item, self.lay, self.start_t, self.fps) if item.get("visualization")
               else ffmpeg_cmd(self.items, self.lay, self.start_t, self.fps))
        try:
            r = subprocess.run([*cmd[:-1], "-frames:v", "1", "-"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return _why(r.stderr.decode("utf-8", "replace"))

    def next_time(self):
        return self.start_t + self.k / self.fps

    def stop(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


class Player:
    """The terminal player: run() until q. `items` are probe() results; the first sets the frame rate, the length and
    the sound; `chapters` are [{name, from, to}]; frames are numbered from `first_frame`. Playback keeps to the range
    from `start` to `end` (seconds; default the whole file), looping, or played `once` and then done. Files that are
    sound alone (item["waveform"]) are drawn by `visualizer` (the name of one, the terminal's colors asked for at the
    start of the run) instead of a video's picture: a waveform is a strip of the track shown a screen at a time
    (_Wave, the - and + keys zoom, z shows the whole track); any other visualizer draws its own picture as the sound
    plays. One of them is one track, several are a playlist (`play_track`, the n and v keys), the o key opens another
    file in place of them, and the range and `once` shape the first track alone."""

    def __init__(self, items, chapters=(), first_frame=0, sound=True, paused=False, start=0.0, end=None, once=False,
                 visualizer=None, bookmarks=None):
        self.vis_text, self.visualizer = visualizer or "", _visualizer(visualizer)
        items = [dict(it) for it in items]
        self.span = SPAN_DEFAULT if self.visualizer.get("wave") and items[0].get("waveform") else None
        self.items, self.chapters = items, list(chapters)
        self.tracks = self.items if all(it.get("waveform") for it in items) else None
        self.track = 0
        self.fps, self.duration = float(items[0]["fps"]), float(items[0]["duration"])
        self.start = min(max(float(start), 0.0), self.duration)
        self.end = self.duration if end is None else min(max(float(end), 0.0), self.duration)
        self.once = once
        first, last = self.frame_range()
        if first > last:
            raise PlayError(f"no frame between {self.start:g} s and {self.end:g} s")
        self.first_frame = int(first_frame)
        self.sound = sound and items[0]["audio"]
        self.silent_note = ""
        self.paused, self.speed = paused, 1.0
        self.fd_in, self.fd_out = 0, 1
        self.serial, self.frame_dir, self.recent = 0, None, deque()
        self.audio, self.frames, self.wave = None, _Frames(items, self.fps), None
        self.geom, self.lay = None, None
        self.old_termios = None                              # the terminal's settings before the player took it
        self.new_geom, self.ask_size_at = None, 0.0          # a size the terminal reported; when to ask again
        self.t_media, self.t_wall = 0.0, time.monotonic()
        self.last_status, self.next_sync = "", 0.0
        self.store, self.marks, self.marks_row, self.last_marks = bookmarks, [], 0, None

    # ---- the clock: media time anchored to the wall clock, re-anchored to the sound now and then
    def now(self):
        if self.paused:
            return self.t_media
        return self.t_media + (time.monotonic() - self.t_wall) * self.speed

    def anchor(self, t):
        self.t_media, self.t_wall = t, time.monotonic()

    def sync_to_sound(self):
        """Every half second: the clock follows the sound's position when they drift apart, until mpv has read the
        sound to its end (eof-reached, a moment before the last of it plays): from there the clock goes on by itself,
        so a file whose sound is shorter than its picture does not freeze where the sound stops."""
        if self.audio is None or time.monotonic() < self.next_sync:
            return
        self.next_sync = time.monotonic() + 0.5
        pos = self.audio.get("time-pos")
        if isinstance(pos, (int, float)) and abs(pos - self.now()) > 0.04 and self.audio.get("eof-reached") is False:
            self.anchor(float(pos))

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
        self.draw_marks(force)
        t = self.now()
        k = chapter_at(self.chapters, t)
        icon = "\u275a\u275a" if self.paused else "\u25b6"
        speed = "" if self.speed == 1.0 else f"  {self.speed:g}x"
        track = f"  track {self.track + 1}/{len(self.tracks)}" if self.tracks and len(self.tracks) > 1 else ""
        span = f"  span {span_name(self.span, self.duration)}" if self.wave is not None else ""
        frame = "" if self.tracks else f"  frame {self.first_frame + int(math.floor(t * self.fps + 1e-6))}"
        length = round(self.duration)
        keys = AUDIO_KEYS.format(zoom=ZOOM_KEYS if self.wave is not None else "") if self.tracks else KEYS
        chapter = f"  {self.chapters[k]['name']}" if k >= 0 else ""
        text = (f" {icon} {clock(t, length)} / {clock(length, length)}{frame}{chapter}{speed}{track}{span}"
                f"{self.silent_note}    {keys}")
        text = text[: self.geom["cols"] - 1]
        if force or text != self.last_status:
            self.out(f"\x1b[{self.geom['rows']};1H\x1b[2K\x1b[2m{text}\x1b[0m")
            self.last_status = text

    def note(self, text):
        """A line at the bottom row instead of the status line (an error the next status redraw takes back)."""
        self.out(f"\x1b[{self.geom['rows']};1H\x1b[2K\x1b[2m{text}\x1b[0m")
        self.last_status = ""

    def draw_marks(self, force=False):
        """The bookmarks row under the picture, the one the playhead has reached highlighted; no row without any."""
        if not self.marks:
            return
        length = round(self.duration)
        before, mid, after = tvb_bookmarks.row(self.marks, self.now(), self.geom["cols"],
                                               lambda at: clock(at, length))
        text = (f"\x1b[2m{before}\x1b[0m" + (f"\x1b[1;7m {mid} \x1b[0m" if mid else "")
                + f"\x1b[2m{after}\x1b[0m")
        if force or text != self.last_marks:
            self.out(f"\x1b[{self.marks_row};1H\x1b[2K{text}")
            self.last_marks = text

    def title(self):
        """The terminal's title: the files playing, and which track of the playlist they are."""
        names = " | ".join(os.path.basename(it["path"]) for it in self.items)
        if self.tracks and len(self.tracks) > 1:
            names = f"{names}  ({self.track + 1}/{len(self.tracks)})"
        self.out(f"\x1b]2;{names}\x07")

    def relayout(self, geom=None):
        """Lay the picture out for `geom`, else for the size the terminal answers now; clear the pane. A track playing
        sound alone is drawn by its visualizer, at the layout's height instead of a video's own size: a waveform is a
        strip of the track shown a screen at a time (self.wave), anything else a graph drawing the sound as it plays."""
        geom = geom or term_geometry()
        if geom is None:
            raise PlayError("the terminal does not report its size in pixels (CSI 16 t / 18 t)")
        self.geom = geom
        item, rows = self.items[0], 2 if self.marks else 1
        if item.get("waveform"):
            shape = dict(item, w=float(VIS_SIZE[0]), h=float(VIS_SIZE[1]))
            self.lay = layout([shape], geom, rows)
            self.wave = (_Wave(item, self.visualizer["wave"], self.span, self.lay)
                         if self.visualizer.get("wave") else None)
            if self.wave is None:
                self.items[0] = visualize(item, self.lay, self.visualizer, self.fps)
        else:
            self.wave = None
            self.lay = layout(self.items, geom, rows)
        if self.wave is not None:
            self.wave.set_marks(m["at"] for m in self.marks)
        bottom = self.lay["row"] * geom["cell_h"] + self.lay["Y"] + self.lay["h"]
        self.marks_row, self.last_marks = min(-(-bottom // geom["cell_h"]) + 1, geom["rows"] - 1), None
        self.out(f"\x1b_Ga=d,d=I,i={KITTY_ID},q=2\x1b\\\x1b[2J")

    # ---- the visualizer: the terminal's colors, the span of the waveform, the keys for it
    def theme_colors(self):
        """Dress the visualizer in the terminal's own colors, asked for once at the start of the run (the answers are
        the pane's, not this process's) and before the first layout draws anything. A terminal that answers nothing
        leaves TERM_COLORS; a visualizer the caller named in full (an ffmpeg graph) is theirs as it stands."""
        self.visualizer = _visualizer(self.vis_text, term_colors())

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
        steps = sorted({round(s, 3) for s in (*SPANS, self.duration) if s <= self.duration + 1e-6}) or [self.duration]
        if d > 0:
            span = max([s for s in steps if s < now_span - 1e-6] or steps[:1])
        elif d < 0:
            span = min([s for s in steps if s > now_span + 1e-6] or steps[-1:])
        else:
            span = self.duration
        if abs(span - now_span) > 1e-6:
            self.span = span
            self.wave = _Wave(self.items[0], self.visualizer["wave"], span, self.lay)
            self.wave.set_marks(m["at"] for m in self.marks)
        self.status(force=True)
        data = self.wave.frame(self.now())                          # redraw now: the next frame may be a while off
        if data is not None:
            self.show(data)

    # ---- moving about
    def frame_range(self):
        """The range's first and last frames: the first starting at or after `start`, the last starting before `end`."""
        return math.ceil(self.start * self.fps - 1e-6), math.ceil(self.end * self.fps - 1e-6) - 1

    def seek(self, t):
        first, last = self.frame_range()
        k = min(max(int(math.floor(t * self.fps + 1e-6)), first), last)
        t = k / self.fps
        if self.wave is not None:                                          # a screen of the strip: nothing to decode
            data = self.wave.frame(t)
        else:
            self.frames.start(self.lay, t)
            data = self.frames.read()
        if data is not None:
            self.show(data)
        self.anchor(t)
        if self.audio:
            self.audio.send("seek", t, "absolute+exact")
            self.audio.send("set_property", "pause", self.paused)          # the sound plays exactly when we do
        self.status(force=True)

    def set_paused(self, p):
        t = self.now()
        self.paused = p
        self.anchor(t)
        if self.audio:
            self.audio.send("set_property", "pause", p)
        if not p and t >= self.end - 1.0 / self.fps:
            self.seek(self.start)
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

    # ---- bookmarks
    def load_marks(self):
        """The bookmarks of the file playing, from the store; none without one."""
        self.marks = self.store.of(self.items[0]["path"]) if self.store else []

    def change_marks(self, write):
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
        at = int(math.floor(self.now() * self.fps + 1e-6)) / self.fps
        self.change_marks(lambda: self.store.add(self.items[0]["path"], at))

    def remove_mark(self):
        k = tvb_bookmarks.current(self.marks, self.now())
        if k >= 0:
            at = self.marks[k]["at"]
            self.change_marks(lambda: self.store.remove(self.items[0]["path"], at))

    def jump_mark(self, d):
        """To the next (d=1) or previous (d=-1) bookmark; back to this one first when it has played for a while."""
        if not self.marks:
            return
        t = self.now()
        k = tvb_bookmarks.current(self.marks, t)
        if not (d < 0 and k >= 0 and t - self.marks[k]["at"] > 0.5):
            k = min(max(k + d, 0), len(self.marks) - 1)
        self.seek(self.marks[k]["at"])

    # ---- the tracks, the sound and other files
    def open_audio(self):
        """Start mpv on the file playing now (closing the last), the sound on and at the player's speed and pause; a
        file without sound, or a machine without mpv, plays silent."""
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        self.silent_note = ""
        if not (self.sound and self.items[0]["audio"]):
            return
        if shutil.which("mpv") is None:
            self.silent_note = "  silent (no mpv)"
            return
        self.audio = _Audio(self.items[0]["path"], os.path.join(self.frame_dir, "mpv.sock"))
        self.audio.send("set_property", "speed", self.speed)
        self.audio.send("set_property", "pause", self.paused)

    def restart(self, items, tracks=None, track=0):
        """Play `items` now (`tracks` the playlist they are from, `track` the one current, or None for files playing
        side by side): the frames, the clock, the chapters, the sound and the title all start over on them. The whole of
        each file plays from here: --start, --end and --once shape the first file alone."""
        self.tracks, self.track = tracks, track
        self.items = [dict(it) for it in items]
        self.fps, self.duration = float(self.items[0]["fps"]), float(self.items[0]["duration"])
        self.start, self.end = 0.0, self.duration
        self.chapters = list(self.items[0].get("chapters", []))
        self.frames.stop()
        self.frames = _Frames(self.items, self.fps)
        self.sound = bool(self.items[0]["audio"])
        self.speed = 1.0
        was_paused = self.paused
        self.paused = True
        self.open_audio()
        self.load_marks()
        self.title()
        self.relayout(self.geom)
        self.seek(self.start)
        self.set_paused(was_paused)

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
            items = [probe(p) for p in paths]
        except PlayError as e:
            self.note(str(e))
            return
        if all(it.get("waveform") for it in items):
            self.restart([items[0]], items, 0)
        else:
            self.restart(items, None, 0)

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
        self.out(f"\x1b[{self.geom['rows']};1H\x1b[2K{prompt}")
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
        self.out(f"\x1b_Ga=d,d=I,i={KITTY_ID},q=2\x1b\\\x1b[2J\x1b[?25h\x1b[?1049l")
        if self.old_termios is not None:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.old_termios)
        try:
            yield
        finally:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, raw)
            signal.signal(signal.SIGINT, quiet)
            self.out("\x1b[?1049h\x1b[?25l\x1b[2J")
            t = self.now()
            self.relayout()
            self.seek(t)

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
        seqs = re.findall(rb"\x1b\[[0-9;:?<=>]*[A-Za-z~]|\x1b.|.", SIZE_REPORT.sub(b"", data), re.S)
        for s in seqs:
            if s in (b"q", b"Q", b"\x03", b"\x1b"):
                return False
            chord = zoom_chord(s)
            if chord:
                self.zoom(chord)
            elif s == b" ":
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
                self.seek(self.start)
            elif s in (b"n", b"v") and self.tracks:
                self.next_track(1 if s == b"n" else -1)
            elif s in (b"o", b"O"):
                paths = self.pick_file()
                if paths:
                    self.open_file(paths)
            elif s.decode("latin-1") in SPEEDS:
                t = self.now()
                self.speed = SPEEDS[s.decode()]
                self.anchor(t)
                if self.audio:
                    self.audio.send("set_property", "speed", self.speed)
                self.status(force=True)
            elif s in (b"m", b"M") and self.audio:
                self.audio.send("cycle", "mute")
            elif self.wave is not None and s in (b"-", b"_"):
                self.zoom(-1)                                       # a wider span: further out, whole track last
            elif self.wave is not None and s in (b"+", b"="):
                self.zoom(1)                                        # a narrower span: a second a screen at the end
            elif self.wave is not None and s in (b"z", b"Z"):
                self.zoom(0)                                        # the whole track: one screen, end to end
            elif s == b"b":
                self.add_mark()
            elif s == b"B":
                self.remove_mark()
            elif s in (b"[", b"]"):
                self.jump_mark(1 if s == b"]" else -1)
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
            self.old_termios = old
            tty.setraw(self.fd_in)
            self.theme_colors()
            self.out("\x1b[?1049h\x1b[?25l\x1b[2J")
            self.title()
            self.load_marks()
            self.relayout()
            self.open_audio()
            want_paused = self.paused
            self.paused = True
            self.seek(self.start)
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
                timeout = 0.25 if self.paused else max(0.0, self.next_frame_in() / self.speed)
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
                if t >= self.end - 0.5 / self.fps:                 # the end of the range: done, the next track, or round
                    if self.once:
                        break
                    if self.tracks and len(self.tracks) > 1:
                        self.next_track(1)
                        continue
                    self.seek(self.start)
                    continue
                if self.wave is not None:
                    data = self.wave.frame(t)                      # None: the terminal is showing this picture already
                else:
                    data = None
                    while self.frames.next_time() <= t:            # late frames are skipped, the newest shown
                        data = self.frames.read()
                        if data is None:
                            break
                if data is not None:
                    self.show(data)
                self.sync_to_sound()
                self.status()
        finally:
            for s in (signal.SIGHUP, signal.SIGTERM):              # a closing terminal hangs up more than once:
                signal.signal(s, signal.SIG_IGN)                    # a second signal must not cut the cleanup short
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


HELP = """Play video and audio files in this terminal through the kitty graphics protocol (made for Tern): video files side
by side, in sync, looping, with the first file's sound; an audio file (wav, mp3, flac, ogg, ...) as its own waveform,
drawn in the pane's own colors; a status line with the time, the frame, the chapter and the span.

A track's waveform is drawn from the whole track at once (ffmpeg's showwavespic, a strip a screen wide, the screen
moving with the sound), so the pane can be zoomed out to the whole track -- no waiting, no decoding, however long it is.
- and = (or cmd+- / cmd+=, where the terminal passes them through) zoom the span out and in; z shows the whole track.

Keys: space play / pause, left / right 5 s (shift: 1 s), . and , one frame on / back (pausing), PgUp / PgDn the previous /
next chapter, Home or 0 the start, 1 2 3 speed 0.25x / 0.5x / 1x, n and v the next / previous track, o another file
(fzf, else a typed path), m mute, - and = zoom the waveform out and in, z the whole track, q quit.

Examples:
  tern-video-block clip.mp4
  tern-video-block album/*.flac                                # a playlist: n and v move through it
  tern-video-block --audio-visualizer spectrum track.mp3       # another way of drawing the sound: line, p2p, cline,
                                                               # envelope, wavespic, spectrum, spectrogram, cqt,
                                                               # vectorscope, spectrumpic, or an ffmpeg filtergraph,
                                                               # each with a colormap: --audio-visualizer cqt:viridis
  tern-video-block --split right wide.mp4 tall.mp4             # a new Tern block beside this pane, focused
  tern-video-block --chapters shots.ffmeta --first-frame 61 cut.mp4
  tern-video-block --end 5 --once a.mp4 && tern-video-block --end 5 --once b.mp4    # the first 5 s of each, in turn
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="tern-video-block", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", metavar="FILE", help="video files, played side by side; audio files, as tracks")
    p.add_argument("--chapters", metavar="FILE", help="chapters from an FFMETADATA file (default: the first file's own)")
    p.add_argument("--first-frame", type=int, default=0, metavar="N", help="the number of the first frame (default 0)")
    p.add_argument("--paused", action="store_true", help="open on the first frame, paused (space plays)")
    p.add_argument("--no-sound", action="store_true", help="play without sound")
    p.add_argument("--start", type=float, default=0.0, metavar="S", help="play from S seconds in (default 0)")
    p.add_argument("--end", type=float, metavar="S", help="play up to S seconds in (default the end)")
    p.add_argument("--once", action="store_true", help="play once and quit, instead of looping")
    p.add_argument("--audio-visualizer", metavar="NAME", help="how an audio file is drawn: waveform (the default: the "
                                                              "whole track as a strip, zoomable with - and =), line, p2p, "
                                                              "cline, envelope, wavespic, spectrum, spectrogram, cqt, "
                                                              "vectorscope, spectrumpic, or an ffmpeg filtergraph; a "
                                                              "colormap may follow a name, as in cqt:viridis")
    p.add_argument("--bookmarks", metavar="FILE", help="the SQLite database of bookmarks (default: the plugin's own, in "
                                                       "Tern's plugin data); b adds one, B removes it, [ ] move between")
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
        _visualizer(args.audio_visualizer)                        # refuse a name or graph that is not one, before the pane
        chapters_file = os.path.abspath(args.chapters) if args.chapters else None
        store = os.path.abspath(args.bookmarks) if args.bookmarks else tvb_bookmarks.default_store()
        if args.split or args.tab:
            rest = [*files, "--first-frame", str(args.first_frame)]
            rest += ["--chapters", chapters_file] if chapters_file else []
            rest += ["--audio-visualizer", args.audio_visualizer] if args.audio_visualizer else []
            rest += ["--bookmarks", store] if store else []
            rest += ["--paused"] if args.paused else []
            rest += ["--no-sound"] if args.no_sound else []
            rest += ["--start", repr(args.start)] + (["--end", repr(args.end)] if args.end is not None else [])
            rest += ["--once"] if args.once else []
            print(json.dumps({"block": open_in_tern(rest, "tab" if args.tab else args.split)}))
            return 0
        if shutil.which("ffmpeg") is None:
            raise PlayError("ffmpeg is not on the PATH")
        items = [probe(f) for f in files]
        chapters = read_chapters(chapters_file) if chapters_file else items[0]["chapters"]
        Player(items, chapters, args.first_frame, sound=not args.no_sound, paused=args.paused, start=args.start,
               end=args.end, once=args.once, visualizer=args.audio_visualizer,
               bookmarks=tvb_bookmarks.Bookmarks(store) if store else None).run()
    except PlayError as e:
        print(f"tern-video-block: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
