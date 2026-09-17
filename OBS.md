# Watch Cogbench and set up OBS

**Just watching?** [Open the live game](http://slacker.local:31724/) and click
**Sound on**. You do not need OBS. On your Mac, `just watch` opens the same page.

The addresses below use `slacker.local`. If it is unreachable, follow
[the connection guide](SETUP.md#connection-problems).

## OBS: game picture and sound

1. In OBS, select your scene. Under **Sources**, click **+ → Media Source**.
   Name it **Cogmind game**.
2. Turn off **Local File** and paste this into **Input**:

   ```text
   http://slacker.local:31723/stream.ts
   ```

   Or run `just obs-url` on your Mac to copy it.
3. Turn off **Restart playback when source becomes active**. If your OBS version
   shows **Network Buffering**, start with 1 MB; try 0 MB for less delay.
4. Click **OK**. Check that the game is visible and its meter moves in the
   **Audio Mixer** when the game makes a sound.

The feed carries both picture and sound: 1440×1080, 60 fps by default, H.264
video and AAC stereo audio. Neither is lossless.

**Only one client can use this feed at a time.** Close VLC or another OBS source
using the same URL before connecting. Other people can use the browser watch
page at the same time; it supports multiple viewers.

The old `:31722/stream.mkv` and `:31721` audio feed are no longer exposed by the
current deployment. Replace saved sources that use them.

## Add the stats beside the game

For the standard landscape scene:

1. Set the OBS base canvas to **1920×1080**.
2. Position **Cogmind game** at the top-left, at **1440×1080**.
3. Add a **Browser Source** named **Cogbench stats** with this URL:

   ```text
   http://slacker.local:31720/overlay.html?video=0
   ```

4. Set its width to **1920** and height to **1080**. Turn off **Shutdown source
   when not visible**.
5. Put **Cogbench stats above Cogmind game** in the Sources list, aligned to the
   top-left. Its transparent game area lets the Media Source show through.

You should now have the game on the left and agent information on the right.
Use [the control page](http://slacker.local:31720/control.html) to start or stop
the agent, choose a model, and change scene settings. `just control` opens it.

## Other layouts

| Layout | Browser Source URL | Source size |
|---|---|---|
| Whole scene, including fallback video | `http://slacker.local:31720/overlay.html` | 1920×1080 |
| Vertical / mobile | `http://slacker.local:31720/overlay.html?layout=vertical` | 1080×1920 |
| Vertical stats over a Media Source | `http://slacker.local:31720/overlay.html?layout=vertical&video=0` | 1080×1920 |

The built-in overlay video is MJPEG at 15 fps and has **no audio**. Use the
Media Source for sound and smoother video. Avoid leaving both pictures visible:
use `video=0` when placing the overlay over the Media Source.

For the vertical layout, set the canvas to 1080×1920 and place the game at the
top-left, sized to 1080×810. The cards stack below it.

Add URL options with `?` for the first option and `&` for later ones:

| Option | Effect |
|---|---|
| `video=0` | Make the game area transparent |
| `panel=hud` | Show resources, parts, and log instead of the default agent panel |
| `chat=1` | Include the chat card |
| `cam=1` | Reserve space for a webcam source underneath the overlay |

Leave `cam` out to let the control page toggle webcam space live.

### Individual cards

Add a Browser Source per card using this pattern:

```text
http://slacker.local:31720/overlay.html?only=perf&video=0
```

Start with a width of **480**, choose enough height to show the card, then
position or crop it in your scene. Replace `perf` with a name below.

| Name | Shows |
|---|---|
| `perf` | Model, response speed, context usage, and inference statistics |
| `decisions` | Model output and recent actions/results |
| `view` | The explored map available to the agent |
| `guards` | Active safety guards and script |
| `run` | Location, core integrity, matter, energy, and heat |
| `build` | Attached parts, inventory, and nearby contacts |
| `log` | The game's recent messages |
| `chat` | Twitch messages being passed to the model |
| `quip` | The line requested with **Say one line**; blank until used |

## If something looks wrong

| Problem | What to try |
|---|---|
| URL downloads a file in a browser | Use port **31724** to watch; **31723/stream.ts** belongs in OBS Media Source |
| No picture or sound | Check the URL, close other clients of the OBS feed, then reconnect the source |
| Sound but no picture | Replace any old Matroska URL with `:31723/stream.ts`, then reconnect |
| Picture but no sound | Check the OBS mixer and source mute; Browser Source overlay video has no audio |
| Double or echoing audio | Mute the browser watch page and remove duplicate audio sources |
| Stats hidden behind the game | Move the stats Browser Source above the Media Source |
| Old or blank stats | Check the control page or `just status`; observations update as the agent runs |
| Playback falls behind | Reconnect the OBS source; on the watch page, press `r` to resync |
| Nothing loads | Run `just doctor`, then use [the tunnel fallback](SETUP.md#connection-problems) |

Browser watch shortcuts: **m** mute, **f** fullscreen, **h** HUD, **r** resync.
The page starts muted and reconnects after stream interruptions.

## Technical reference

These are deployment defaults, not a live health report:

| Address/path | Purpose |
|---|---|
| `:31724/` | Browser player |
| `:31724/live.mp4` | Shared browser A/V stream |
| `:31724/stat` | Viewer count and encoder health |
| `:31723/stream.ts` | Single-client OBS A/V stream |
| `:31720/video.mjpg` | Silent MJPEG fallback |
| `:31720/state.json` | Latest overlay state |
| `:31720/quip.json` | Current model line |
| `:31720/params` | GET/POST live model, chat, and camera settings |
| `:31720/models` | Available models; `?refresh=1` refreshes the list |
| `:31720/run/status` | Agent status and last result |
| `:31720/run/start`, `/run/stop`, `/run/restart` | POST agent lifecycle actions |
| `:31720/run/spec` | POST saved run settings |
| `:31720/run/log?n=40` | Recent agent log |
| `:31720/say`, `/say?clear=1` | POST request or clear a model line |

Overlays load Cogmind fonts from `https://cogmind-cdn.plasticheart.info/cogfont.css`
and fall back to local fonts if unavailable. The agent map uses a monospace grid.
The game's `COGBENCH_FONTSET` defaults to `18/Cog`; keep overrides in the `18/`
size class because screen reading assumes a 9×18 glyph grid. The entrypoint
applies this setting before launch; live edits may be overwritten on exit.
