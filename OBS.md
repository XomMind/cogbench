# Cogbench sources for OBS

Two ports. `31720` is the overlay server (pages, state, control, MJPEG); `31722`
is the lossless A/V feed. Replace `slacker` with the node if it moves.

The old MP3 feed on `31721` is gone — audio rides inside the A/V stream now.

## Just watching — open this in Safari

    http://slacker:31724/

A plain web page. Fragmented MP4 over a single HTTP response, appended to a
MediaSource — the lowest-latency route into Safari that involves no WebRTC, no
signalling, no UDP and no second server. A few hundred milliseconds on a LAN.

Unlike every feed below it, **this one serves any number of viewers at once**:
the Python process owns the encoder's pipe and each browser is just another
consumer, so opening it on a phone does not steal it from the laptop.

It starts muted, because Safari refuses to autoplay with sound — one click on
*Sound on*. Keys: `m` mute, `f` fullscreen, `h` HUD, `r` resync.

The page keeps itself at the live edge rather than trusting the element to:
after any stall a MediaSource will happily stay a second behind forever, so it
seeks when far behind and plays 5% fast when merely drifting. The reported
latency in the bar is that gap.

## The A/V feed — Media Source

    http://slacker:31722/stream.mkv

Matroska, H.264 at 60fps + **FLAC** 48kHz stereo, ~10 Mbit/s.

**Audio is lossless.** FLAC stores the null sink's s16 samples exactly.

**Video is not, by default, and deliberately.** Lossless and/or 4:4:4 both
encode as High 4:4:4 Predictive, and OBS's Media Source plays the audio from
such a stream while showing no picture — a confusing failure, because it
sounds like the stream is working. The default is therefore Constrained
Baseline / yuv420p at CRF 12, which every decoder handles and which is visually
very close on a screen that is mostly static text.

To get the lossless 4:4:4 version back on the `raw` container (fine for ffmpeg
or VLC, not for OBS):

    COGBENCH_RAW_PIXFMT=yuv444p
    COGBENCH_RAW_PROFILE=high444
    COGBENCH_RAW_CRF=0

In OBS: **Media Source**, uncheck *Local File*, paste the URL, uncheck *Restart
playback when source becomes active*, set *Network Buffering* to 0–1 MB.

One client at a time. ffmpeg's HTTP muxer serves a single consumer and then
re-listens — so if you open it in VLC to check, OBS drops until you close VLC,
and reconnects about a second later.

## Widgets — Browser Source, one per card

    http://slacker:31720/overlay.html?only=NAME&video=0

Each renders one card alone on a transparent background, 480px wide and as tall
as the card needs, scaled to whatever you size the source. Add as many as you
like and place them yourself.

| `only=` | Card | What it shows |
|---|---|---|
| `perf` | Inference | Model name, current latency, decisions/min, a pp/gen/wait phase bar, latency sparkline, and P50/P95/P99, context used, prompt and generation tok/s, cache hits, draft acceptance |
| `decisions` | Decisions | The literal string the model emitted, then the last several decisions as index / action / result |
| `view` | Agent view | The agent's *fogged* map — what it has actually seen, not the game's view — with walls, items, hostiles, exits coloured, plus tiles-mapped count |
| `guards` | Guards | Which safety guards are engaged, as chips, plus the active script and its dossier line |
| `run` | Run | Location, core integrity, matter, energy and heat as bars |
| `build` | Build | Parts attached per slot (power/propulsion/utility/weapon), inventory, and nearby contacts with bearings |
| `log` | Log | The game's own last six messages |
| `chat` | Chat → model | The Twitch lines actually being fed into the prompt |
| `quip` | The model says | The one sentence from the **Say one line** button. Stays blank until pressed, so the source is invisible until it has something |

## Vertical / mobile scene

    http://slacker:31720/overlay.html?layout=vertical

1080x1920. The game sits on top at 1080x810 — the X display is 1440x1080 and
both are 4:3, so it fills the width exactly, no letterbox, no distortion — and
Inference, Run, Decisions, Log and the model's line stack underneath at twice
the size the desktop column uses.

The agent view, guards and build cards are left out on purpose: they reward
leaning in, which is the one thing a phone viewer cannot do. Pull any of them
in as their own `?only=` source if you want them.

`?cam=1` reserves a strip for a webcam here too.

## Whole scene in one source

    http://slacker:31720/overlay.html

1920x1080: game video at native 1440x1080 plus the full right-hand column. Add
at 1920x1080 and untick *Shutdown source when not visible*.

Flags: `?video=0` leaves the game area transparent (use with the Media Source
under it), `?panel=hud` swaps the agent-only column for the resources/parts/log
one, `?chat=1` adds the chat card, `?cam=1` reserves a 480x480 hole for a
webcam. Leave `cam` off the URL to let the control page switch layout live
without the source reloading.

## Game video on its own

    http://slacker:31720/video.mjpg

MJPEG at 15fps, for a Browser Source. Fine as a fallback; prefer the Media
Source above, which is lossless, 60fps and carries the audio.

## Control page

    http://slacker:31720/control.html

Run lifecycle (Start / Restart / Stop, decisions, twitch channel, policy),
Auto-revive and Loop episodes, live model switch, chat window and cap, scene
and webcam toggles, and the Say one line / Clear buttons.

## Fonts

Both pages load Cogmind's own faces from
`https://cogmind-cdn.plasticheart.info/cogfont.css` — `cogmind-cog` for text,
`cogmind-smallcaps` for the all-caps labels. The old mono stack stays behind
them, so a machine that cannot reach the CDN still renders correctly rather
than breaking. The agent's fogged map deliberately stays on a metric monospace
font: it is a character grid and needs uniform advance widths.

The game itself is set to `18/Cog` via `COGBENCH_FONTSET`, applied by the
entrypoint before Cogmind starts (the game rewrites `system.cfg` on exit, so
editing it live does not stick). Keep any override in the `18/` size class —
`18/Cog`, `18/CogNarrow`, `18/CogWide`, `18/Smallcaps`, `18/Terminus`,
`18/X11`… — because the harness reads the screen on a 9x18 glyph grid and a
different size moves every cell.

## Raw endpoints

| | |
|---|---|
| `GET :31724/live.mp4` | the fMP4 stream itself, multi-client |
| `GET :31724/stat` | viewer count, encoder uptime, restarts |
| `GET /state.json` | everything the overlay draws, rewritten every decision |
| `GET /quip.json` | the current spoken line |
| `GET,POST /params` | live knobs: `cam`, `chat_on`, `chat_window_min`, `chat_limit`, `model` |
| `GET /models` | models the endpoint is serving, `?refresh=1` to re-ask |
| `GET /run/status` | running, pid, uptime, decisions, spec, how the last run ended |
| `POST /run/start\|stop\|restart` | lifecycle; start takes `model`, `decisions`, `twitch`, `policy`, `temperature` |
| `POST /run/spec` | change the spec without restarting, e.g. `?supervise=1`, `?loop=1` |
| `GET /run/log?n=` | tail of the agent log |
| `POST /say`, `POST /say?clear=1` | one sentence from the model, and clear it |
