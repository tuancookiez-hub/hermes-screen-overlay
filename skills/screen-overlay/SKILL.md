---
name: screen-overlay
description: Point at things on the user's real Windows screen with the screen_overlay tool — a gliding pointer, spotlights, boxes, arrows, numbered steps, a floating step guide, and handwritten glass notes.
---

# Pointing at the user's screen

Use `screen_overlay` when the user should *see* where something is: "where is X", "how do I…",
walking them through settings, or confirming which button you mean. The user keeps control:
marks never take clicks, nothing takes keyboard focus, and they can clear it with **Ctrl+Alt+Shift+F12**.
Do not use it to act on the user's behalf; it only draws. The pointer is a drawing and never moves the real mouse.

Pick the lightest thing that answers the question:

- One place: `pointer` (add `text` for a short caption).
- Several things to do in order: `guide`.
- An explanation that belongs next to the work: `note`, `ink` or `flow`.

## Pick a target, best first

1. **element**: found live through Windows UI Automation. Exact, survives DPI scaling and window moves between calls.
   `{"element": {"window": "Notepad", "name": "File", "control_type": "MenuItem"}}`
   `window` defaults to the foreground window. `name` is a case-insensitive substring. Use `index` to pick the 2nd/3rd match.
   Always give a `name` or `automation_id`. `control_type` alone matches the first control of that type, which is often the window's Minimize button. Check `resolved` in the result.
2. **window**: the whole visible window. `{"window": "Settings"}` or `{"window": "notepad.exe"}`.
3. **rect / point** in physical screen pixels (as returned by `inspect`).
4. **rect / point from a screenshot**: add `space` so the plugin rescales:
   `"space": {"frame_width": 1280, "frame_height": 720, "region": "primary"}`.
   `region` is what the screenshot covered: `primary`, `virtual`, `{"monitor": 1}` or `[x,y,w,h]`.

When unsure of names, look first:

```json
{"action": "inspect", "what": "find", "window": "Settings", "name": "bluetooth"}
```

Other inspect modes: `screens` (monitors, scaling), `windows` (visible apps), `element_at` with `x`, `y`.

## Shapes

| type | use |
|---|---|
| `step` | numbered badge + ring; the main teaching mark. Numbers auto-increment. |
| `spotlight` | dims the rest of that monitor. Use one at a time. |
| `box` | ring around a target without dimming. |
| `arrow` | arrow to the target; the tail is placed automatically, or set `from`. Add `text` for a caption at the tail. |
| `label` | caption pill next to a target or at a point. |
| `pointer` | a drawn pointer glides to the target and pulses. `text` appears when it lands. One at a time. |
| `note` | handwritten note on dark glass that writes itself on. `text` up to 800 chars, optional `title`, lines starting `- ` become bullets. |
| `ink` | hand-drawn `mark` on a target: `circle`, `underline`, `box`, `arrow`, `check`, `cross`. |
| `flow` | small hand-drawn diagram: 2-6 `nodes` joined by arrows, `direction` right or down. |

Common fields: `text`, `color` (orange default; blue, green, red, yellow, purple, pink, teal, white or `#RRGGBB`),
`placement` (auto/below/above/right/left), `pad`, `id`.

`note` and `flow` need no target: they find a clear corner of the monitor the user is on. Give a `point` to set their
top-left, or an element or window to sit beside it. They are drawn one after another, in the order you list them.
Add to the screen without wiping it by passing `"replace": false`. Keep notes short: this is a margin, not a document.
Do not draw freehand by coordinates; use these shapes so it stays tidy.

## Examples

Show one step:

```json
{"action": "show", "shapes": [
  {"type": "spotlight", "target": {"element": {"window": "Settings", "name": "Bluetooth & devices"}},
   "text": "Open Bluetooth & devices"}
]}
```

Several steps at once (keep it to 2–4):

```json
{"action": "show", "shapes": [
  {"type": "step", "target": {"element": {"name": "File", "control_type": "MenuItem"}}, "text": "Open File"},
  {"type": "step", "target": {"element": {"name": "Save as", "control_type": "MenuItem"}}, "text": "Then Save as"}
], "ttl_seconds": 120}
```

Then `{"action": "clear"}` when the user is done or the topic changes.

Explain something next to the work:

```json
{"action": "show", "ttl_seconds": 0, "shapes": [
  {"type": "ink", "mark": "circle", "target": {"element": {"name": "Compare"}}, "color": "yellow"},
  {"type": "note", "title": "Why this one", "text": "Compare keeps both versions.\n- Nothing is overwritten\n- You choose line by line"},
  {"type": "flow", "nodes": ["Compare", "Pick lines", "Save"]}
]}
```

## Walk the user through a task

A guide shows one step at a time on a small floating panel. The user clicks Next and Back themselves, the pointer glides
to each step, and the panel moves out of the way. Use it for three or more steps.

```json
{"action": "guide", "title": "Turn on Bluetooth", "save_as": "turn on bluetooth", "steps": [
  {"title": "Open Bluetooth & devices", "target": {"element": {"window": "Settings", "name": "Bluetooth & devices"}}},
  {"title": "Switch Bluetooth on", "text": "The switch is at the top.", "target": {"element": {"window": "Settings", "name": "Bluetooth", "control_type": "Button"}}},
  {"title": "That's it", "text": "Your device should appear in the list."}
]}
```

- Each step needs a `title` or `text`. `target` is optional. `mark` is `step` (default), `spotlight`, `box` or `none`.
- Name the `window` in every element target. Otherwise it means whichever app is in front when the guide starts.
- A step's target may not exist until the step before is done, such as an item in a menu that is still closed. That is fine:
  `checks` in the result lists those, and the guide keeps looking when it reaches them. If a name in `checks` is simply wrong, fix it.
- The current step follows its window if it moves.
- `{"action": "guide_move", "to": "next"}`, `"back"` or a step number moves the guide when the user asks in chat instead.
  `status` tells you which step they reached and how the last guide ended (`finished`, `closed`, `expired`).
- `save_as` keeps the guide. `{"action": "guide", "guide_name": "turn on bluetooth"}` replays it, `{"action": "guides"}` lists saved ones,
  and `{"action": "guides", "delete": "name"}` removes one.
- Starting a guide replaces any running guide. `clear` ends it too.
- It ends after 30 minutes with no Next or Back, or after `ttl_seconds`.

## Read the result

- `shapes[].ring_rect` / `label_rect`: where things landed (physical px).
- `errors`: targets that could not be resolved, with a reason. Fix and call `show` again; good shapes were still drawn.
- `resolved`: which window/element each target matched. Check it is the one you meant.
- `capture_note`: by default the overlay is hidden from screenshots, so do not try to verify it by screenshotting.
- *Access denied* means Windows blocks UI Automation for that app (admin app, game anti-cheat). Use a `window` or `rect` target.

## Limits

Windows only. Cannot draw over exclusive-fullscreen games. Elements in minimized or off-screen windows cannot be targeted.
You are not told when the user clicks Next: read `status` on your next turn.
