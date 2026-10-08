"""Visualizers for audio files: presets, theme colors, the waveform strip (ffmpeg's showwavespic) drawn as a screen of
the whole track at a time, and non-waveform ffmpeg filtergraphs drawn frame by frame."""
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time

from tvb_common import PlayError

# A visualizer is drawn in the terminal's own colors: its foreground for the waveform, the palette's red for the
# playhead, and its background behind both (OSC 10, 11 and 4; N, asked for once at the start of the run). TERM_COLORS
# is what a terminal that does not answer them gets: a light grey waveform on a near-black pane.
TERM_COLORS = {"fg": "#e6e6e6", "bg": "#101010", "dim": "#303030", "red": "#e06c75", "blue": "#61afef",
               "magenta": "#c678dd", "cyan": "#56b6c2", "yellow": "#e5c07b", "green": "#98c379"}
PALETTE_NAMES = {0: "dim", 1: "red", 2: "green", 3: "yellow", 4: "blue", 5: "magenta", 6: "cyan"}
COLOR_QUERIES = b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\" + b"".join(f"\x1b]4;{n};?\x1b\\".encode() for n in PALETTE_NAMES)
COLOR_REPLY = re.compile(rb"\x1b\](10|11|4;\d+);rgb:([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})")

ERR_TAG = re.compile(r"\[[^\]]*\]\s*")                     # ffmpeg's own "[filter @ 0x…] " prefixes
AUDIO_SIZE = (16, 9)                                      # an audio track's picture: the visualizer's own shape
VIS_MODES = ("line", "p2p", "cline")                       # showwaves' own modes, by name
VIS_SIZE = (1280, 720)                                     # the picture a visualizer draws, scaled to the pane's layout
STRIP_SCREENS = 8                                          # screens of the track one rendered strip of waveform covers
SPANS = (1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0, 180.0, 600.0, 1800.0, 3600.0)   # seconds one screen of waveform covers
SPAN_DEFAULT = 90.0                                        # what a track starts at: a minute and a half to a screen
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


def _rgb01(color):
    """A "#rrggbb" color as showcqt's cscheme wants it: its three channels from 0 to 1, "|"-separated."""
    return "|".join(f"{int(color[i:i + 2], 16) / 255:.3f}" for i in (1, 3, 5))


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


def _why(stderr):
    """The one line of ffmpeg's stderr that says what went wrong with: the tags off, its generic "Error :" tail and
    the threading noise it prints after a filter has already failed left out."""
    lines = [ERR_TAG.sub("", l).strip() for l in (stderr or "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("Error :") and "Task finished with error" not in l
             and "Terminating thread" not in l]
    return lines[-1][:200] if lines else ""


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