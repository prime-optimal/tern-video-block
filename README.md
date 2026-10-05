# tern-video-block

Video files in [Tern](https://stencil.so/tern) open in a video block: the video plays right in the terminal, with its sound,
fitted and centred in the pane, frame by frame if you like. Give it several files and they play side by side, in sync
(a 16:9 and a 9:16 cut of the same video, two takes, before and after).

It is a Tern plugin plus a small player. The player draws through the kitty graphics protocol, so on its own it also
runs in kitty, WezTerm and Ghostty.

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
```

| Option | |
|---|---|
| `--split right\|down`, `--tab` | open in a new Tern block (a split of the pane it runs in) or tab, and focus it |
| `--chapters FILE` | chapters from an FFMETADATA file (what `ffmpeg -f ffmetadata` writes and mpv's `--chapters-file` reads); default: the first file's own chapters |
| `--first-frame N` | the number the status line gives the first frame (default 0), e.g. to match the frame numbers of the project the video was rendered from |
| `--paused` | open on the first frame, paused |
| `--no-sound` | play without sound |

The first file sets the frame rate, the length and the sound; the others are brought to its rate and played beside it.

## Keys

| Key | |
|---|---|
| space | play / pause |
| ← → | 5 s back / on (shift: 1 s) |
| `,` `.` | one frame back / on (pauses) |
| PgUp PgDn | previous / next chapter (its first frame) |
| Home, `0` | the start |
| `1` `2` `3` | speed 0.25x / 0.5x / 1x |
| `m` | mute |
| `q`, Esc | quit (the block closes) |

The bottom row is a status line: play state, time, frame number, chapter, speed and the keys.

## How it works

ffmpeg decodes the files from the frame asked for, scales each to the picture's height and its own width and stacks
them into raw RGB frames at the pane's pixel size. Each frame is written to a file in `$XDG_RUNTIME_DIR` (memory) and
handed to the terminal by path, replacing one image in place, so the pane never flickers or stacks pictures; the files
are deleted a second after they are shown. mpv plays the first file's sound with no video, driven over its JSON IPC
(pause, exact seeks, speed), and its position is the clock the frames follow: late frames are skipped, never queued.
Files without sound play on the wall clock.

Things about Tern that shaped it:

- Tern keeps the kernel's window size at 80 x 24 with no pixels and sends no SIGWINCH when a pane resizes. The player
  asks the terminal itself (`CSI 18 t` for rows and columns, `CSI 16 t` for the cell in pixels) at the start and every
  second after, and redraws when the pane changes size.
- Tern answers temporary-file transmissions (`t=t`) with OK and then "no such file", and acknowledges shared-memory
  ones (`t=s`) without drawing them, so frames go by plain file path (`t=f`) and the player deletes them itself.
- `tern split` leaves the focus in the pane it was run from, so `--split` focuses the new block itself.
- `tern open` on a video prints "cannot open in a file block" and exits 1 even though the plugin opens it: Tern's
  command line checks the file type before routing the open.

Closing the block cleans up (the player handles SIGHUP), and ffmpeg and mpv die with the player however it dies.

## Development

```sh
tern plugin link .              # use this checkout as the plugin
python3 -m pytest               # needs ffmpeg; pytest
```

`tern plugin types .` writes `tern.d.luau`, the plugin API's types for luau-lsp.
