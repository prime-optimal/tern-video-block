---
name: tern-video-block-development
description: Use when changing, testing or reviewing the tern-video-block Tern plugin (window.luau route.open + the Python kitty-graphics player, audio waveform, visualizers, bookmarks) or verifying it live in Tern.
---

# tern-video-block development

A window-only Tern plugin (`plugin.toml` has `window = "window.luau"`, no host, no lenses, no blocks).
`window.luau` routes media opens to a terminal split running `python3 tern_video_block.py --bookmarks <data>/bookmarks.db
FILE`; the player draws frames through the kitty graphics protocol and plays sound through mpv. Tern facts change
between releases: `tern plugin types .` writes `tern.d.luau` (git-ignored), the source of truth for the Luau API, and the
README's "Things about Tern that shaped it" lists the terminal quirks the player works around.

## Layout and the upstream rule

- `tern_video_block.py` is the upstream player (base `194ef80`, verticalrectangle/tern-video-block). Keep it close:
  features go in mixins, the core only gets small hooks. Check with
  `git diff --shortstat 194ef80 -- tern_video_block.py` (≈ +146/−39 after the split); a big jump means code moved
  the wrong way.
- `tvb_audio.py` (`AudioMixin`: playlist, waveform layout, zoom, `o` picker with `suspended()`, audio keys,
  `probe_audio`), `tvb_vis.py` (visualizer graphs, `_Wave` strip, `visualize_cmd`, `wave_cmd`, `_why`, `TERM_COLORS`),
  `tvb_bookmarks.py` (SQLite store + `BookmarksMixin`), `tvb_common.py` (the one `PlayError`).
- Mixins never import the core. The core reaches them through hooks: `audio_layout(geom, rows, layout)`,
  `audio_key(s)` / `mark_key(s)`, `probe_files`, `reset_frames`. Every module raises `tvb_common.PlayError`; a second
  `PlayError` class means errors from the mixins escape the player's handler.
- Code style here is upstream's: long prose docstrings, comments where upstream has them. Match it, and keep the README
  (keys table, `--help`, Files list) in step with any change.

## Tern behaviour the player depends on

- No `SIGWINCH`, and the kernel window size stays 80×24 with no pixel size: the player asks with `CSI 18 t` / `CSI 16 t`
  every second and relays out on change.
- Frames go by file path (`t=f`), one image id replaced in place, files deleted a second later: Tern answers `t=t`
  with "no such file" and ignores `t=s`.
- Theme colours come from `OSC 10` / `11` / `4;N` at start-up; tests and non-answering terminals get `TERM_COLORS`.
- Tern owns `cmd+=` / `cmd+-` (font zoom) unless `settings.json` has `"keybinds": {"cmd+=": "unbind", "cmd+-": "unbind"}`.
  Passed-through chords arrive as kitty CSI-u (`\x1b[45;9u`), matched before the bare `-` / `=` keys.
- `tern open video.mp4` exits 1 ("cannot open in a file block") even though the route opens it; test opens through the
  Files pane or the palette, not by that exit code.
- `route.open`: a Files-pane single click is `how = "preview"`. `window.luau` reuses the last preview pane by closing it
  with `pcall(cx.layout.close, …)`. Only close panes the plugin made itself, because `layout:close` skips the
  unsaved-changes prompt.
- Bookmarks live at `tern.plugin.data .. "/bookmarks.db"`; the palette command "Media: Open bookmarks" is only
  `available` once that file exists.
- Headless `tern serve` loads no plugins: route and live checks need a real window.

## Tests

```sh
uv run --with pytest pytest -q        # real ffmpeg/ffprobe/mpv; pty tests answer Tern's queries (TERN_ANSWERS)
```

- macOS caps `AF_UNIX` paths at 104 bytes and pytest's `tmp_path` is ~115, so a test that starts mpv must keep its IPC
  socket under a short `tempfile.mkdtemp(dir="/tmp")` (and `rmtree` it). Don't move that workaround into the core;
  production sockets live under `XDG_RUNTIME_DIR` / `TMPDIR` and fit.
- Tests for moved code import the module that owns it (`tvb_vis.wave_cmd`, `tvb_audio.zoom_chord`) and monkeypatch
  there. Patching the core name silently patches nothing.
- After a run, `ps -axo pid,command` must show no stray `mpv` or `tern_video_block` processes.
- The suite count must not drop in a refactor. Compare `pytest -q` totals against `main` before accepting one.

## Live verification (separate control window, never the user's panes)

```sh
mkdir -p /tmp/tvb && cd /tmp/tvb       # shots land in <window cwd>/target/shots/tern/live/
ffmpeg -v error -y -f lavfi -i testsrc=size=640x360:rate=25:duration=4 -f lavfi -i sine=440:duration=4 -shortest clip.mp4
ffmpeg -v error -y -f lavfi -i "sine=220:duration=60,volume='0.2+0.8*abs(sin(t))':eval=frame" song.mp3
cp song.mp3 other.mp3
tern --control /tmp/x.sock /tmp/tvb &  # wait for "control on /tmp/x.sock"
C="tern ctl --control /tmp/x.sock"
P=/path/to/checkout/tern_video_block.py
$C "run \"clear; python3 $P --bookmarks /tmp/tvb/bm.db /tmp/tvb/clip.mp4\""   # run takes ONE quoted command
$C shot video; $C key q
$C "run \"clear; python3 $P --bookmarks /tmp/tvb/bm.db /tmp/tvb/song.mp3 /tmp/tvb/other.mp3\""
$C key b; $C shot mark; $C key n; $C shot next; $C key o; $C shot picker; $C key Enter; $C shot resumed; $C key q
$C quit                                # closes the window
```

- The verb is `shot NAME`, not `screenshot`. `key` takes names such as `Enter` and `escape`. `tree` / `dump` give
  layout JSON.
- Read every PNG and check: the video picture with a status clock; the waveform in theme colours with the red playhead;
  the `⚑ MM:SS` row after `b`; `track 2/2` after `n`; the fzf list after `o`, then playback again after `Enter`.
- Test the plugin route itself (`tern plugin link .`, `tern plugin reload`, `tern plugin list` showing `video-block …
  ready`) by single- and double-clicking a media file in the window's Files pane. Use `tree` to find the row, and click at
  screenshot px / 2.
- Passing a video and an audio file together (`clip.mp4 song.mp3`) fails with an ffmpeg filtergraph error, and fails the
  same way on `main`. That's a known limit, not a regression.
- Cleanup: `$C quit`, `rm -rf /tmp/tvb/target /tmp/tvb/bm.db /tmp/x.sock`, and check no `mpv` is left.

## Reviewing a refactor (subagent output especially)

The split went wrong in ways the trimmed tests didn't catch. Diff against `main` and check each item:

- No feature, key, CLI flag, `--help` text or test removed (`git diff --stat main`, the pytest count, `--help` still
  documents zoom, `--audio-visualizer … cqt:viridis` and bookmarks).
- The alternate screen is still entered in `run()` and left in `suspended()` and on exit.
- Every method a mixin calls exists on the composed `Player`, and none is defined twice.
- Chapters still reach audio items; ffmpeg failures still surface as `PlayError("ffmpeg drew nothing for …")`.
- Then run the live check above. A passing suite has missed both a broken `o` picker and a missing alt screen.

## Repo workflow

- Remote `git@gh-prime:prime-optimal/tern-video-block`; `gh` needs `GH_TOKEN=$(gh auth token -u prime-optimal)`;
  commit with `-c commit.gpgsign=false`.
- Issue → branch from `main` → conventional commit with `Closes #N` → PR. Stacked PRs say so in the body and merge in
  order.
