# Audio in a block: how the waveform is drawn, what was tried, and what was in the way

Audio files (wav, wave, mp3, flac, ogg, oga, opus, m4a, aac, wma, aif, aiff) open in the media block and play their
own sound with a picture in the pane. The user-facing story is in [the README](../README.md#audio); this is the
engineering record: what worked, what did not, what was tricky, and how each of it was checked.

## The shape of it

One render, a screen at a time. ffmpeg draws the track **once** as one long strip of waveform (`showwavespic`) and the
pane shows a screen of that strip, sliding along as the sound plays. `STRIP_SCREENS = 8` screens of `span` seconds per
ffmpeg call, so a seek inside a strip is a different column of bytes already in memory and no decoding happens at all
while the sound plays. The whole of a track is drawn from the start, however long it is, so zooming out is free.
Everything else (`--audio-visualizer spectrum`, `cqt`, `vectorscope`, a caller's filtergraph) still draws the sound as
it plays, a frame at a time, through the same `_Frames` producer the video side has always used.

Measured on a real 1 h mastered flac, pane 720 x 864 (the whole track is 720 px wide there, one screen of 90 s is
5760 px):

| Render | Time |
| --- | --- |
| 8 screens of 90 s (5760 px strip) | 0.35 s |
| The whole track, one screen (720 px) | 0.97 s |

Both are synchronous and block the loop, which is what makes the keys feel slow if you press them quickly (see
*Tricky*).

## What worked

- **The strip.** One `showwavespic` call per eight screens, cached by (track, span, pane width, screen), the playhead
  drawn into the screen the pane shows. Zooming from 90 s to the whole track is 600 s / 720 px: one render of 0.97 s
  and then nothing until the playhead leaves that screen.
- **The terminal's own colors.** `OSC 10` / `OSC 11` / `OSC 4;N` at the start of the run (Tern answers all three),
  the wave drawn in the foreground over the background with a red playhead; a terminal that answers nothing keeps the
  fallback palette.
- **Every audio format.** wav, mp3, flac, ogg and oga each: a frame drawn at the pane's size, the status line knowing
  the track's length from `ffprobe`, and mpv's own IPC reporting the codec it decoded (`FLAC (Free Lossless Audio
  Codec)`, `MP3 (MPEG audio layer 3)`, `PCM signed 16-bit little-endian`, `Vorbis`, `Opus (Opus Interactive Audio
  Codec)`) with the duration, all within a few tenths of the 3 s fixture's length.
- **The graph visualizers.** `spectrum`, `spectrogram` and `vectorscope` each drew about a hundred frames in four
  seconds through the per-frame path, so the alternative visualizers stayed real while the waveform became a strip.
- **A resize.** A 90 x 56 pane drew 782 x 440; a size report to 130 x 40 redrew 554 x 312 — the new pane's height
  times the visualizer's own 16:9 — with the span (`span 1m30s`) kept.
- **The keys, live in Tern**, on four mastered 1 h tracks: `span 1m30s` on opening, `z` -> `span the whole track`,
  `=` -> `span 60m` -> `span 30m`, `-` -> `span the whole track`, `,` pausing, the playhead moving over the strip.

## What did not work

- **mpv for the picture.** `mpv --audio-visual` is gone upstream (0.41 has no such option), and mpv's own lavfi
  graphs only accumulate from the seek point, so they can never show a whole track. ffmpeg draws the picture and mpv
  keeps the sound, which is what this plugin did for video from its first commit anyway.
- **A frame a second of waveform for the zoom.** Drawing the sound as it plays means a zoom-out to the whole track is
  either a lie (the graph only holds what has played) or a decode of the whole track. The strip removes the question.
- **A graph ffmpeg refuses drew nothing, in silence.** A typo'd name (`--audio-visualizer nosuchviz`) or a graph with
  a bad option left the pane blank with the clock running and no message: the producer's stderr went to
  `DEVNULL` and `read()` returning nothing looked exactly like the end of a file. Fixed — `_Frames.read()` now reports
  ffmpeg's own words when it stopped before its first frame, for a video as much as for a graph, so a run says
  `tern-video-block: the visualizer drew nothing for tone.flac: No such filter: 'nosuchviz'` and exits 2.
- **Rejecting an unknown name up front was considered and dropped.** A name that is not a preset is a filtergraph of
  the caller's, and single-filter graphs with no `=` are legitimate (`avectorscope`), so the check moved to "did it
  draw?" instead of "is this a name I know?".
- **Fixtures, not the player.** ffmpeg in this environment has no `libvorbis` (only the experimental `vorbis` encoder,
  which refuses mono input), so the ogg fixture is `-ac 2 -strict -2 -c:a vorbis` and oga uses `libopus`. And the
  first version of the harness passed `--open-file`, which does not exist — files are positional — and reported five
  formats broken because of it.

## What was tricky

- **The strip geometry.** A screen is `span` seconds wide at the pane's pixel width; a strip is eight screens, clipped
  to the track; the span ladder is `SPANS` (1 s … 1 h) plus the track's own length, clamped, with "the whole track"
  meaning one screen of the entire track rather than another step of the ladder.
- **Tern resizes without telling the kernel.** No SIGWINCH and no pixel size in the kernel's window size, so the
  player asks `CSI 18 t` and `CSI 16 t` at the start and every second, and the answers have to be taken **out of the
  key stream** — and never act as keys. SIGWINCH, where a terminal does send it, only brings the next ask forward.
- **Theming a `showwavespic` picture.** It paints black around the wave. Screening it over the terminal's background
  color (`blend=all_mode=screen`) makes the strip the pane's own background with the foreground over it; both sides
  have to be `format=rgb24` first, because blend's own planes turn every color after the first violet.
- **The chords.** `-` / `=` / `z` are the bare keys, but a terminal that passes `cmd+-` / `cmd+=` through sends them as
  kitty CSI-u sequences (`\x1b[45;9u`) — the modifier is in the code and the event type may be too (`:3`), and
  `modifyOtherKeys` spells the same chord differently. The key regex had to allow `[0-9;:?<=>]` inside a sequence, and
  the chords are matched before the bare keys so a chord never also acts as its own key.
- **Reporting a failure without making it worse.** A stderr pipe fills and blocks a long run; re-running the same
  command for its words would run as long as the graph does, since a graph drawn over the terminal's own background
  has an infinite colour input. Hence `-frames:v 1` on the re-run: the words, one frame, milliseconds.
- **Driving the live pane.** Renders block the loop, so a key pressed mid-render waits; keys sent as fast as the
  harness can write them race with each other and with the redraw. One key every 4–6 s, then read the status line.

## How it was checked

```sh
uv run --with pytest python -m pytest -q     # 25 passed, 1 failed (pre-existing, see below)
```

The automated tests cover the parts that are not a terminal: the visualizer specs and their colors, `span_name`, a
strip drawn once with the playhead moving over it (ffmpeg faked), the whole track as one screen, the span ladder and
the status text, the zoom keys including CSI-u chords and event types, a video pane leaving them alone, the theme
colors falling back with no terminal, the AppleDouble filter, and — new — ffmpeg refusing a graph and the player
saying so.

The rest was checked by running the real player on a pty: answering `CSI 18 t` / `CSI 16 t` by hand, keeping every
frame it transmitted, and reading its status line. That is what the format table, the resize numbers and the graph
frame counts above come from. In Tern itself the same keys were driven with `tern send <block> keys "z"` and read back
with `tern capture <block>`.

`test_the_sound_plays_on_when_the_loop_goes_back_after_its_end` fails **on `main` too**, in this environment: pytest's
temporary path is 115 bytes and macOS caps a unix socket path (`sun_path`) at 104, so mpv cannot bind its IPC socket.
It is reported here, not touched by this work.

## Known limits

- A render blocks: changing the span on a long track takes up to about a second before the new picture is there.
- The span ladder tops out at an hour, and at the track's own length.
- `fpcalc`-style analysis, peaks and beat grids are not part of this: the strip is a picture of the waveform, drawn
  from peak or average amplitude by `showwavespic`.
