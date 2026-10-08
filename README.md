# tern-video-block

Video files in [Tern](https://stencil.so/tern) open in a video block: the video plays right in the terminal, with its sound,
fitted and centred in the pane, frame by frame if you like. Give it several files and they play side by side, in sync
(a 16:9 and a 9:16 cut of the same video, two takes, before and after).

It is a Tern plugin plus a small player that draws through the kitty graphics protocol (frames sent by file path), so
other terminals that implement the protocol may run the player too; it is tested in Tern only.

Audio files (wav, wave, mp3, flac, ogg, oga, opus, m4a, aac, wma, aif, aiff) open in the same block and play as their
own sound, drawn in the pane's own colors: the waveform of the whole track, a screen of it at a time, which `-` and `=`
zoom out to the whole track and back in. One audio file is one track; give it several and they become a playlist.

## Install

```sh
tern plugin install github.com/verticalrectangle/tern-video-block
```

It needs `python3` (3.10 or newer), `ffmpeg` and `ffprobe`, and `mpv` for the sound (without it videos play silent).

Then a video opens in a video block however Tern opens files: the Files pane, the palette, a drop, a link, `tern open`.
The block goes where Tern was asked to put the file (beside, below, a tab; a preview replaces the last preview) and
takes the focus, so the keys work straight away.

For the command line, install the player as well:

```sh
uv tool install git+https://github.com/verticalrectangle/tern-video-block    # or: pipx install git+https://...
```

## Command line

```sh
tern-video-block clip.mp4                                   # plays in this terminal
tern-video-block --split right wide.mp4 tall.mp4            # a new Tern block beside this pane, focused
tern-video-block --tab clip.mp4                             # a new Tern tab
tern-video-block --chapters shots.ffmeta --first-frame 61 cut.mp4
tern-video-block --end 5 --once a.mp4 && tern-video-block --end 5 --once b.mp4   # the first 5 s of each, in turn
tern-video-block album/*.flac                               # a playlist: waveforms of their own, n and v move through it
tern-video-block --audio-visualizer cqt:viridis track.mp3   # another way of drawing the sound
```

| Option | |
|---|---|
| `--split right\|down`, `--tab` | open in a new Tern block (a split of the pane it runs in) or tab, and focus it |
| `--chapters FILE` | chapters from an FFMETADATA file (what `ffmpeg -f ffmetadata` writes and mpv's `--chapters-file` reads); default: the first file's own chapters |
| `--first-frame N` | the number the status line gives the first frame (default 0), e.g. to match the frame numbers of the project the video was rendered from |
| `--paused` | open on the first frame, paused |
| `--no-sound` | play without sound |
| `--start S`, `--end S` | play only from S seconds in / up to S seconds in (whole frames: the first starting at or after the start, the last starting before the end) |
| `--once` | play once and quit (the block closes) instead of looping |
| `--audio-visualizer NAME` | how an audio file is drawn: `waveform` (the default: the whole track as a strip), `line`, `p2p`, `cline`, `envelope`, `wavespic`, `spectrum`, `spectrogram`, `cqt`, `vectorscope`, `spectrumpic`, or an ffmpeg filtergraph; a colormap may follow a name, as in `cqt:viridis` |
| `--bookmarks FILE` | the SQLite database of bookmarks (default: the plugin's own, `bookmarks.db` in Tern's plugin data, which the block uses too) |

The first file sets the frame rate, the length and the sound; the others are brought to its rate and played beside it.

## Keys

| Key | |
|---|---|
| space | play / pause |
| ← → | 5 s back / on (shift: 1 s) |
| `,` `.` | one frame back / on (pauses) |
| PgUp PgDn | previous / next chapter (its first frame) |
| `b` `B` | bookmark this moment / remove the bookmark the playhead has reached |
| `[` `]` | previous / next bookmark (back to the start of this one first when it has played for a while) |
| Home, `0` | the start |
| `1` `2` `3` | speed 0.25x / 0.5x / 1x |
| ↑ ↓ | previous / next media file of the folder, opened in place, in the Files pane's order (`clip`, `clip2`, `clip10`, `clip_cut`; a note at either end). The Files pane's highlight stays where you clicked: Tern gives plugins no way to move it |
| `i` `o` | set the in point at the start of this frame / the out point at its end |
| `x` | clear the range |
| `c` | cut the range out of the file (no re-encoding) |
| `g` | make a GIF of the range (video only; needs `gifski`) |
| `n` `v` | next / previous track of a playlist |
| `O` | open another file (`fzf`, else a typed path) |
| `m` | mute |
| `-` `=` | zoom the waveform out (to the whole track) / in — also cmd+- and cmd+= where the terminal passes them on |
| `z` | the whole track at once |
| `q`, Esc | quit (the block closes) |

The status line (the bottom row, or the one above the info row) shows the play state, the time as a clock (`04:35`;
`01:02:10` once the file is an hour or longer), a video's frame number, chapter, speed, the span of the waveform and
the keys. Lines are cut to the pane's width, so a narrow pane never wraps (and scrolls) them.

In a pane of 8 rows or more the info row sits under it: the range (`[01.50 → 04.00]`), the export running or last done,
and what the file is: `mov · 00:04 · h264 640x360 25fps · 1.2 Mb/s · aac 44.1kHz stereo · 1.3 MB`, from the same
ffprobe call that opens the file.

## Ranges, cuts and GIFs

`i` sets the in point at the start of the frame on show, `o` the out point at its end, `x` clears both. They are kept per
file in the bookmarks database (table `ranges`, made by the first one), so the range is still there next time.

`c` cuts the range out of the file with ffmpeg stream copy (`-c copy`: no re-encoding, so it starts on the keyframe at
or before the in point) and `g` makes a GIF (10 fps, at most 320 px wide; ffmpeg feeds `gifski`, which is optional: without it `g` says so).
With only an in point the range runs to the end of the file, with only an out point from the start.

Exports run in the background, detached from the player, so they finish when the player quits. They land beside the
source file, as `NAME_0m01.50s-0m04.00s.EXT` (`.gif` for a GIF), written to a hidden `.NAME…` file first and moved into
place when whole; an existing file is never overwritten (`… (2).mp4`). The info row says running, done (the file's name)
or failed (the first line of what ffmpeg said).

## Audio

An audio file plays its own sound (`mpv`, no video) with its waveform in the pane, drawn in the terminal's own colors:
the player asks the terminal for its foreground, its background and the first seven palette colors (`OSC 10`, `11` and
`4;N`, which Tern answers) and draws the wave in the foreground over the background, with a red playhead.

The waveform is not drawn frame by frame. ffmpeg renders the track once as one long strip of waveform
(`showwavespic`) and the pane shows a screen of it, sliding along as the sound plays: one ffmpeg call covers eight
screens of sound, a seek is where in the strip the screen is taken from, and the whole of a track is already drawn
however long it is, so nothing has to be decoded to zoom out to all of it. `-` and `=` step the span out and in
(1 s … 1 h, and the track's own length), `z` shows the whole track; the status line says the span. The other
visualizers (`--audio-visualizer spectrum`, `cqt`, `vectorscope`, …) draw the sound as it plays, a frame at a time.

Several audio files are a playlist: `n` and `v` move through it, the status line gives the track number, and each
track's waveform is rendered from its own sound.

What was tried and dropped, what was in the way, and what was measured:
[docs/audio-visualizations.md](docs/audio-visualizations.md).

## Bookmarks

`b` bookmarks the moment playing, in a video or a track. Bookmarks are not written into the file: they are rows of a
SQLite database in the plugin's data (`~/Library/Application Support/Tern/plugin-data/video-block/bookmarks.db` on
macOS), keyed by the file's real path, so a file keeps its bookmarks wherever it is opened from. The database is created
by the first bookmark; until then nothing is written.

A file with bookmarks gets a row under the picture: each bookmark's time (and label, when it has one), the one the
playhead has reached highlighted, as many as fit around it. A waveform also marks them with yellow lines. A file
without bookmarks has no row. `B` removes the highlighted bookmark, `[` and `]` jump between them.

The palette's **Media: Open bookmarks** (shown once the database exists) opens it in Tern's database pane, to browse
every file's bookmarks, or to label or delete them; the player reads them again when it opens the file.

## How it works

ffmpeg decodes the files from the frame asked for, scales each to the picture's height and its own width and stacks
them into raw RGB frames at the pane's pixel size. Each frame is written to a file in `$XDG_RUNTIME_DIR` (memory) and
handed to the terminal by path, replacing one image in place, so the pane never flickers or stacks pictures; the files
are deleted a second after they are shown. mpv plays the first file's sound with no video, driven over its JSON IPC
(pause, exact seeks, speed), and its position is the clock the frames follow: late frames are skipped, never queued.
Files without sound play on the wall clock. The clock follows mpv's `audio-pts` (smooth), not `time-pos`, which with
`--no-video` only moves in half-second steps. A pane resize keeps the old picture until the first frame at the new
size replaces it and redraws at the clock without seeking the sound. What a resize still costs is ffmpeg starting again
at the new size: one hold of about 150–180 ms for a 720p file (the player asks for the size once a second, so at most
one a second while a pane is dragged); the kitty protocol itself adds nothing.

Things about Tern that shaped it:

- Tern keeps the kernel's window size at 80 x 24 with no pixels and sends no SIGWINCH when a pane resizes. The player
  asks the terminal itself (`CSI 18 t` for rows and columns, `CSI 16 t` for the cell in pixels) at the start and every
  second after, and redraws when the pane changes size.
- Tern answers temporary-file transmissions (`t=t`) with OK and then "no such file", and acknowledges shared-memory
  ones (`t=s`) without drawing them, so frames go by plain file path (`t=f`) and the player deletes them itself.
- `tern split` leaves the focus in the pane it was run from, so `--split` focuses the new block itself.
- A Files-pane click keeps the keyboard on the Files list even after the plugin focuses the new pane (Space there opens
  the file again, in an HTML preview), so for an open from the Files pane `window.luau` waits 150 ms, runs the
  `focus_tabs` command and focuses the pane: Space and the other keys then reach the player while the Files pane stays
  open. Plugins cannot move the Files pane's highlight, so ↑ / ↓ in the player walk the folder themselves.
- `tern open` on a video prints "cannot open in a file block" and exits 1 even though the plugin opens it: Tern's
  command line checks the file type before routing the open.
- Tern binds `cmd+=` and `cmd+-` to its own font zoom, so those chords zoom the terminal rather than the pane; the
  player's own keys are `-` and `=`. To have the chords reach the player instead, put
  `"keybinds": {"cmd+=": "unbind", "cmd+-": "unbind"}` in `settings.json` (`"unbind"` passes the chord on to the
  program, where an empty list would swallow it).

Closing the block cleans up (the player handles SIGHUP), and ffmpeg and mpv die with the player however it dies.

## Development

```sh
tern plugin link .              # use this checkout as the plugin
python3 -m pytest               # needs ffmpeg; pytest
```

`tern plugin types .` writes `tern.d.luau`, the plugin API's types for luau-lsp.

### Files

- `tern_video_block.py` — the core player, playable from the terminal: probes files, decodes frames, draws them, and
  handles the sound. Stays close to its upstream base (194ef80). Relies on mixins from `tvb_audio.py`,
  `tvb_bookmarks.py` and `tvb_cut.py` for audio, bookmark and range features.
- `tvb_audio.py` — `AudioMixin` (the visualizer theme, the zoom, the waveform, the playlist, the file picker and the
  up / down move to the folder's neighbouring file) plus `probe_audio`, `span_name`, `zoom_chord`, `media_files`,
  `media_neighbour`, `AUDIO_EXT` and helpers; never imports the core module.
- `tvb_vis.py` — the visualizer presets, `_visualizer`, `visualize`, `visualize_cmd`, `wave_cmd`, `_Wave`,
  `term_colors`, `_why` and related constants; no circular imports.
- `tvb_bookmarks.py` — the `Bookmarks` SQLite store (bookmarks and ranges), `BookmarksMixin` (load, add, remove, jump
  marks), and the `current` / `row` helpers.
- `tvb_cut.py` — `CutMixin` (the in / out range, cuts, GIFs, the info row), `describe` (the info row's one-line
  summary of a probe), `fit` and the export helpers.
- `tvb_common.py` — `PlayError`, the one exception shared by all modules.
