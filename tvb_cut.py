"""In and out points, cuts and GIFs, and the info row for tern-video-block: i and o set the ends of a range, c cuts it out
of the file (ffmpeg, no re-encoding) and g makes a GIF of it (ffmpeg + gifski). The CutMixin is a mixin for Player."""
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import unicodedata

GIF_FPS = 10
GIF_WIDTH = 320
GIF_FILTER = (f"fps={GIF_FPS},scale=w='min({GIF_WIDTH}\\,trunc(iw*sar/2)*2)':h='trunc(ow/dar/2)*2':flags=lanczos,"
              "setsar=1")
INFO_MIN_ROWS = 8                                 # a pane this tall (rows) has the info row under the status line


def cells(ch):
    """How many terminal cells a character takes: 0 for a combining mark, 2 for a wide one, else 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def fit(text, width):
    """`text` cut to at most `width` terminal cells, so a line never wraps (a wrapped status line scrolls the pane)."""
    out, used = [], 0
    for ch in text:
        used += cells(ch)
        if used > width:
            break
        out.append(ch)
    return "".join(out)


def stamp(t):
    """A time to the hundredth: "01.50" under a minute, "2:05.25" after it."""
    m, cs = divmod(int(round(max(t, 0.0) * 100)), 6000)
    return f"{m}:{cs / 100:05.2f}" if m else f"{cs / 100:05.2f}"


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def _rate(text):
    num, _, den = (text or "").partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def describe(info):
    """A one-line summary of ffprobe's JSON (-show_streams -show_format): "mov · 00:04 · h264 640x360 25fps ·
    1.2 Mb/s · aac 44.1kHz stereo · 1.3 MB"; the parts the file lacks are left out."""
    fmt, streams = info.get("format") or {}, info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    parts = []
    names = (fmt.get("format_name") or "").split(",")
    ext = os.path.splitext(fmt.get("filename") or "")[1][1:].lower()
    if names[0]:
        parts.append(ext if ext in names else names[0])
    seconds = int(round(float(fmt.get("duration") or 0)))
    if seconds:
        h, rest = divmod(seconds, 3600)
        parts.append(f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}")
    if video:
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        rate = f" {fps:.2f}".rstrip("0").rstrip(".") + "fps" if fps else ""
        parts.append(f"{video.get('codec_name', '?')} {video.get('width', '?')}x{video.get('height', '?')}{rate}")
    if fmt.get("bit_rate", "").isdigit():
        parts.append(f"{int(fmt['bit_rate']) / 1e6:.1f} Mb/s")
    if audio:
        hz = int(audio.get("sample_rate") or 0)
        layout = audio.get("channel_layout") or (f"{audio['channels']}ch" if audio.get("channels") else "")
        parts.append(" ".join(p for p in (audio.get("codec_name", "?"), f"{hz / 1000:g}kHz" if hz else "", layout) if p))
    if str(fmt.get("size", "")).isdigit():
        parts.append(_size(int(fmt["size"])))
    return " \u00b7 ".join(parts)


def export_name(path, at_in, at_out, ext, taken=os.path.exists):
    """Where a cut or GIF of `path` between two times goes: beside it, as `stem_0m01.50s-0m04.00s.ext` (no colons, which
    some file systems refuse); a name already there is never taken, " (2)", " (3)" ... follows the range instead."""
    stem, old = os.path.splitext(path)
    base = f"{stem}_{_name_stamp(at_in)}-{_name_stamp(at_out)}"
    out, n = f"{base}{ext or old}", 1
    while taken(out) or taken(hidden(out)):
        n += 1
        out = f"{base} ({n}){ext or old}"
    return out


def _name_stamp(t):
    """A time for a file name: 0m01.50s, 12m05.25s."""
    m, cs = divmod(int(round(max(t, 0.0) * 100)), 6000)
    return f"{m}m{cs / 100:05.2f}s"


def hidden(path):
    """The hidden temporary file beside `path` an export is written to before it is moved into place."""
    return os.path.join(os.path.dirname(path), "." + os.path.basename(path))


def cut_script(src, at_in, at_out, out, tmp, log):
    """The shell script of a cut: ffmpeg copies every stream of the range into `tmp`, which is then moved to `out`, so a
    half-written file never has its final name. `log` gets ffmpeg's errors."""
    ffmpeg = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{at_in:.3f}", "-i", src, "-t", f"{at_out - at_in:.3f}",
              "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-avoid_negative_ts", "make_zero", tmp]
    return f"{shlex.join(ffmpeg)} 2>{shlex.quote(log)} && mv -n {shlex.quote(tmp)} {shlex.quote(out)}"


def gif_script(src, at_in, at_out, out, tmp, y4m, log):
    """The shell script of a GIF: ffmpeg writes the range as a y4m video (what gifski reads), gifski makes the GIF in
    `tmp`, which is moved to `out`."""
    ffmpeg = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{at_in:.3f}", "-t", f"{at_out - at_in:.3f}", "-i", src,
              "-an", "-vf", GIF_FILTER, "-pix_fmt", "yuv420p", y4m]
    gifski = ["gifski", "--fps", str(GIF_FPS), "-o", tmp, y4m]
    return (f"{shlex.join(ffmpeg)} 2>{shlex.quote(log)} && {shlex.join(gifski)} 2>>{shlex.quote(log)} && "
            f"mv -n {shlex.quote(tmp)} {shlex.quote(out)}")


def run_detached(script, log_dir, tmp, *scratch):
    """`script` in a shell of its own session (it goes on when the player quits, with nothing on the terminal), then
    the temporaries removed and `log_dir` too when it worked. The Popen, to be polled for how it went."""
    log = os.path.join(log_dir, "log")
    wrapped = (f"{script}; rc=$?; rm -f {shlex.join([tmp, *scratch])}; "
               f"if [ $rc -eq 0 ]; then rm -rf {shlex.quote(log_dir)}; fi; exit $rc")
    with open(log, "ab") as err:
        return subprocess.Popen(["sh", "-c", wrapped], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                start_new_session=True)


class CutMixin:
    """Player mixin providing the range and its exports: cut_init, set_range_at, clear_range, export, poll_exports,
    draw_info, info_rows, status_row and cut_key. Uses only self.*; never imports tern_video_block."""

    def cut_init(self):
        """The range (None for an end not set), the exports started and the info row as last drawn."""
        self.range, self.exports, self.last_info = (None, None), [], None

    def info_rows(self, geom):
        """The rows the info row takes under the status line: one in a pane of INFO_MIN_ROWS rows or more."""
        return 1 if geom["rows"] >= INFO_MIN_ROWS else 0

    @property
    def status_row(self):
        """The terminal row of the status line (the bottom one, or the one above the info row)."""
        return self.geom["rows"] - self.info_rows(self.geom)

    # ---- the range
    def set_range(self, at_in, at_out):
        """Make (at_in, at_out) the range, remembered in the bookmarks store; a note when it cannot be written."""
        self.range = (at_in, at_out)
        if self.store:
            try:
                self.store.set_range(self.items[0]["path"], at_in, at_out)
            except Exception as e:                                     # sqlite3.Error, OSError: a note, play on
                self.note(f"range: {e}")
                return
        self.status(force=True)

    def frame_start(self):
        """The start time of the frame on show."""
        return int(math.floor(self.now() * self.fps + 1e-6)) / self.fps

    def range_key(self, s):
        """i sets the in point at the start of this frame, o the out point at its end, x clears both. True when
        handled."""
        if s == b"i":
            self.set_range(self.frame_start(), self.range[1])
        elif s == b"o":
            self.set_range(self.range[0], min(self.frame_start() + 1.0 / self.fps, self.duration))
        elif s in (b"x", b"X"):
            self.set_range(None, None)
        else:
            return False
        return True

    # ---- the exports
    def bounds(self):
        """(in, out) of the range with the missing end filled in (the start, the end of the file), or None after a note
        saying what to do."""
        at_in, at_out = self.range
        if at_in is None and at_out is None:
            self.note("set an in point (i) and an out point (o) first")
            return None
        at_in, at_out = at_in or 0.0, self.duration if at_out is None else at_out
        if at_in >= at_out:
            self.note(f"the in point ({stamp(at_in)}) is not before the out point ({stamp(at_out)})")
            return None
        return at_in, at_out

    def export(self, kind):
        """Start cutting ("cut") or GIF-making ("gif") of the range, detached: the player may quit meanwhile."""
        item = self.items[0]
        if kind == "gif" and (item.get("waveform") or not item.get("w")):
            self.note("g makes a GIF of a video, and this is sound only")
            return
        if kind == "gif" and shutil.which("gifski") is None:
            self.note("gifski is not on the PATH (brew install gifski)")
            return
        span = self.bounds()
        if span is None:
            return
        src = item["path"]
        out = export_name(src, *span, ".gif" if kind == "gif" else "")
        tmp = hidden(out)
        if not os.access(os.path.dirname(out) or ".", os.W_OK):
            self.note(f"cannot write beside the file: {os.path.dirname(out)}")
            return
        try:
            log_dir = tempfile.mkdtemp(prefix="tern-video-block-export-")
            log = os.path.join(log_dir, "log")
            if kind == "gif":
                y4m = os.path.join(log_dir, "frames.y4m")
                script = gif_script(src, *span, out, tmp, y4m, log)
                proc = run_detached(script, log_dir, tmp, y4m)
            else:
                proc = run_detached(cut_script(src, *span, out, tmp, log), log_dir, tmp)
        except OSError as e:
            self.note(f"{kind}: {e}")
            return
        self.exports.append({"kind": kind, "out": out, "proc": proc, "dir": log_dir, "rc": None, "msg": ""})
        self.status(force=True)

    def poll_exports(self):
        """Look at the exports running (no waiting): one that has ended is done, or failed with the first line of what
        ffmpeg or gifski said. Of the ended ones only the newest is kept (it is the one the info row shows)."""
        for e in self.exports:
            if e["rc"] is None and (rc := e["proc"].poll()) is not None:
                e["rc"] = rc
                if rc:
                    try:
                        with open(os.path.join(e["dir"], "log"), encoding="utf-8", errors="replace") as fh:
                            e["msg"] = next((ln.strip() for ln in fh if ln.strip()), "")
                    except OSError:
                        pass
                    e["msg"] = e["msg"] or f"exit status {rc}"
                    shutil.rmtree(e["dir"], ignore_errors=True)
        newest = self.exports[-1:] if self.exports else []
        self.exports = [e for e in self.exports if e["rc"] is None or e is newest[0]]

    def export_text(self):
        """The newest export in a few words: what runs, what it made, or why it failed."""
        if not self.exports:
            return ""
        e = self.exports[-1]
        if e["rc"] is None:
            return f"making {os.path.basename(e['out'])} \u2026"
        if e["rc"] == 0:
            return f"\u2713 {os.path.basename(e['out'])}"
        return f"\u2717 {e['kind']} failed: {e['msg']}"

    # ---- the info row
    def info_text(self):
        """The info row: the range, the newest export, what the file is (probe's describe)."""
        at_in, at_out = self.range
        rng = "" if at_in is None and at_out is None else (
            f"[{'start' if at_in is None else stamp(at_in)} \u2192 {'end' if at_out is None else stamp(at_out)}]")
        return " " + "  ".join(p for p in (rng, self.export_text(), self.items[0].get("info", "")) if p)

    def draw_info(self, force=False):
        """The info row, redrawn when its text changed (or `force`)."""
        if not self.geom or not self.info_rows(self.geom):
            return
        self.poll_exports()
        text = fit(self.info_text(), self.geom["cols"] - 1)
        if force or text != self.last_info:
            self.out(f"\x1b[{self.geom['rows']};1H\x1b[2K\x1b[2;39m{text}\x1b[0m")
            self.last_info = text

    def cut_key(self, s):
        """Range and export keys: i o x, c cut, g GIF. True when handled."""
        if s == b"c":
            self.export("cut")
        elif s == b"g":
            self.export("gif")
        else:
            return self.range_key(s)
        return True
