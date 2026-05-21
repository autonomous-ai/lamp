# Vision loop — see-think-act on the user's Mac

This reference unlocks the **synchronous vision loop**: take a screenshot of the user's Mac, look at it, decide where to click, fire the click, screenshot again. Use this **only** when the marker-based actions in the parent `SKILL.md` cannot do the job — for example, the user asks you to click a UI element that has no stable label, drag a slider, locate a window by appearance, or read text off the screen.

The marker pattern (`[HW:/buddy/exec/<action>:{...}]`) is fire-and-forget and cannot return data. Vision needs return values, so it uses a different transport: **bash + curl against the lamp's local API** with the full Command schema.

## When to load this reference

Load this file (i.e. follow these instructions instead of the parent SKILL.md's "fire-and-forget" pattern) when the task requires:

- **Visual reasoning** — "find the blue button in the toolbar", "click the X on that dialog", "what's on my screen right now?".
- **No stable accessibility label** — `click_button` only works when macOS Accessibility exposes the label. Web content in Chrome/Safari often does not.
- **Multi-step UI flows that depend on prior state** — fill a form whose fields change order between visits, navigate a settings panel.
- **Reading text off the screen** — capture and OCR / describe an error dialog, summarise a webpage section.

Do NOT load this for tasks the parent skill already handles (open app, open URL, type into focused field, keyboard shortcuts, named-button clicks). Those are faster and far more reliable than vision.

## Transport

Call the lamp's local HTTP endpoint via bash:

```bash
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"<action>","params":{...},"timeout_ms":15000}'
```

The endpoint is localhost-only (lamp-internal). The lamp dispatches over the buddy's persistent WebSocket and returns the buddy's response **synchronously** wrapped in the standard envelope:

```json
{"status": 1, "data": {"id": "...", "ok": true, "result": {...}, "error": null, "duration_ms": 213}, "message": null}
```

- `status: 1` + `data.ok: true` → use `data.result`.
- `data.ok: false` → read `data.error`. Common errors: `"no buddy connected"`, `"buddy paused by user"`, `"unknown action"`, `"timeout after Nms"`, `"Screen Recording access required"`, `"Accessibility access required"`.
- `status: 0` → lamp-level failure (buddy not paired, bad params).

## Coordinate system — read this once before clicking

All x/y in vision actions are in **CGEvent global display space**: top-left origin, units = **points** (not pixels). On a Retina Mac the screenshot pixels are 2× the click coordinates.

```
result.path           absolute path to PNG on the user's Mac (saved by buddy)
result.width/height   PNG dimensions in PIXELS
result.display_scale  pixels-per-point for the captured display (e.g. 2.0 for Retina)
```

To convert a pixel coordinate you measured in the screenshot to a click target:

```
click_x_points = pixel_x / display_scale
click_y_points = pixel_y / display_scale
```

If you forget to divide on a Retina display, your click lands at roughly half the intended position — usually outside the target.

For multi-display setups, call `list_displays` first and pick the display the user is asking about (typically `is_main: true`). Pass that display's `id` to `screenshot` and use the same display's `x`/`y` origin offset when computing click coords.

## Available vision actions

| Action | Returns / Effect | Key params |
|---|---|---|
| `screenshot` | `{path, width, height, display_id, display_scale, bytes, image_b64?}` | `display_id` (default main), `scale` (default 1.0 — shrink for token budget), `return_format` `"path"` / `"base64"` / `"both"` |
| `list_displays` | `{displays:[{id,is_main,x,y,width,height,pixel_width,pixel_height,scale}], count}` | — |
| `cursor_pos` | `{x, y, screen_height, backing_scale}` (CGEvent space) | — |
| `click_at` | `{clicked, x, y, button, clicks}` | `x`, `y` (points), `button` `"left"`/`"right"`/`"middle"`, `clicks` |
| `mouse_move` | `{moved, x, y, smooth}` | `x`, `y`, `smooth` |
| `drag` | `{dragged, from, to}` | `from:{x,y}`, `to:{x,y}`, `duration_ms` |
| `scroll` | `{scrolled, delta_y, delta_x}` | `delta_y`, `delta_x`, optional `x`/`y` to position cursor first |
| `read_clipboard` | `{text}` | — |
| `write_clipboard` | `{wrote}` | `text` |

The non-vision actions from the parent SKILL.md (`open_app`, `open_url`, `type_text`, `key_combo`, `notification`, `click_button`) also work over `/api/buddy/command` if you want a synchronous confirmation. Prefer them when the action does not require seeing the screen.

## Loop template

```
1. screenshot                          → know what's on screen
2. (look at the image / reason)        → decide next single action
3. fire that action (click_at / scroll / type_text / key_combo / drag)
4. screenshot again                    → verify the action landed
5. repeat from step 2, or stop when done
```

Some practical rules for the loop:

- **One action per iteration.** Don't batch multiple clicks before re-screenshotting — the screen state may have shifted (modal appeared, focus changed, animation in progress).
- **Wait briefly after navigations.** After `open_url` or a click that opens a new view, sleep ~500-1000 ms before the next screenshot so the page can render. In bash: `sleep 1`.
- **Stop the loop on success or after ~6-8 iterations.** Vision is unreliable; if you can't make progress in that many steps, tell the user what you saw and ask for a more specific instruction.
- **Stop on error.** If `data.ok: false`, surface the error to the user — do not retry blindly. `"Screen Recording access required"` and `"Accessibility access required"` need the user to grant permission in System Settings; no amount of retrying will fix that.
- **Don't narrate every step.** Speak one short caring sentence at the start ("Let me have a look") and one at the end ("Done — clicked the Submit button."). All the screenshot/click reasoning stays in `thinking`.

## Reading the screenshot

The screenshot result includes `path` (filesystem path on the user's Mac, visible only to the buddy app) and optionally `image_b64`. The lamp-side LLM cannot read the user's filesystem, so to actually **see** the image you must request base64:

```bash
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":0.5,"return_format":"base64"},"timeout_ms":15000}'
```

`scale: 0.5` shrinks the PNG by half before encoding (still readable for most UI tasks, ~4× fewer tokens than full Retina). Use `scale: 0.25` for very wide displays. Use `scale: 1.0` only when fine pixel detail matters (small text, dense UI).

The `image_b64` field is a plain base64-encoded PNG. To consume it in your reasoning, treat it as image input on the next turn.

## Example flows

### A — Click a button with no accessibility label (e.g. a custom web button)

```bash
# 1. take screenshot
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":0.5,"return_format":"base64"},"timeout_ms":15000}'
# → returns image_b64, width 1920, height 1080, display_scale 2.0 (so original was 3840x2160)
# → I see the "Subscribe" button at pixel (1600, 800) IN THE SCALED IMAGE
# → original pixel coords: (3200, 1600) ; point coords: (1600, 800) after dividing by display_scale 2.0
#   BUT WAIT — I already requested scale 0.5, so the image is half size. The pixel I read (1600, 800) in the
#   scaled image corresponds to (3200, 1600) in the ORIGINAL screenshot, which is (1600, 800) in points.
#   Click target in points = pixel_in_scaled_image / scale / display_scale * display_scale = pixel_in_scaled_image / scale
#   With scale=0.5, divide pixel_in_scaled by 0.5 → original pixel → divide by display_scale → points.
#   Simpler: with scale=0.5 and display_scale=2.0, points = pixel_in_scaled / (0.5 * 2.0) = pixel_in_scaled / 1.0.

# 2. click
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"click_at","params":{"x":1600,"y":800,"button":"left"},"timeout_ms":3000}'

# 3. verify
curl -s -X POST http://127.0.0.1:5000/api/buddy/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"screenshot","params":{"scale":0.5,"return_format":"base64"},"timeout_ms":15000}'
```

### B — Read text off a dialog

```bash
# 1. screenshot
# 2. look at image → transcribe the dialog text
# 3. reply to user: "Your Mac is showing a dialog that says: '...'."
# No further actions — pure read flow.
```

### C — Multi-step: open Settings, find Bluetooth, toggle off

Prefer **deep links over visual navigation** whenever possible:
```
open_url x-apple.systempreferences:com.apple.Bluetooth-Settings.extension
```
If no deep link exists, fall back to the vision loop:
```
1. open_app "System Settings"
2. (wait 1s, screenshot)
3. type_text "Bluetooth" + key_combo ["return"]   ← search bar is focused on launch
4. (wait 1s, screenshot)
5. click_at on the toggle (read coords from screenshot)
6. screenshot to verify toggle changed state
```

### D — Locate a window by appearance, then drag it

```
1. screenshot
2. identify the window's title bar coords
3. drag from (title_x, title_y) to (target_x, target_y) with duration_ms 400
4. screenshot to verify
```

## Reliability — set expectations

Vision-driven computer use is **inherently unreliable**: Anthropic's Computer Use benchmark and OSWorld both report ~22-40% success per multi-step task. Errors compound: a 4-step flow at 80% per step is 41% end-to-end.

To stay useful:

- **Prefer Tier 1 (API integrations) over vision** when the user's goal has an API. Joining a Google Meet via `https://meet.google.com/<id>` is 100% reliable; navigating Google Calendar visually to find and click "Join" is maybe 30%.
- **Prefer Tier 2 (markers / deep links / shortcuts) over vision** when the task fits. `key_combo ["cmd","space"]` to open Spotlight always works; visually finding the Spotlight icon and clicking it is fragile.
- **Tier 3 (vision) is the last resort.** It's powerful but brittle. If you find yourself looping more than ~6 times, stop and tell the user honestly what you tried and what you saw.
- **Tell the user when the task is on the edge of feasibility.** "I can try clicking through Calendar, but it's not very reliable — want me to just open the meeting URL directly if you have it?"

## Error responses to handle

| `data.error` | Meaning | What to do |
|---|---|---|
| `no buddy connected` | The buddy app is offline | Tell the user the Mac isn't reachable; ask them to wake their Mac or check the Buddy menu bar icon |
| `buddy paused by user` | User paused the buddy from the menu bar | Tell the user the buddy is paused; ask them to resume from the menu bar |
| `Screen Recording access required …` | macOS TCC not granted | Tell the user to grant Screen Recording in System Settings → Privacy & Security, then ask again |
| `Accessibility access required …` | macOS TCC not granted for clicks/typing | Same — grant Accessibility in System Settings → Privacy & Security |
| `timeout after Nms` | Action exceeded its own timeout | Surface to the user; do not retry the same action automatically |
| `unknown action: X` | Typo or unsupported action | Pick a supported action from the table above |

## Notes

- **One short spoken sentence per turn.** Keep the user in the loop ("Let me take a look", "Found it — clicking now", "Done"). All the screenshot/coordinate math stays in `thinking`.
- **Don't expose paths or coordinates to the user.** They want to know whether the task succeeded, not pixel (1234, 567).
- **Match the user's language** in the spoken confirmation (English in, English out; Vietnamese in, Vietnamese out).
- **Stop and ask** if the screen is locked, the user is mid-typing in a sensitive app (password manager, banking), or the task drifts beyond what they originally asked. Don't keep clicking through unfamiliar UI.
