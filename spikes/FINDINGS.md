# Feasibility spike — 2026-11-05

Machine: Windows 11 (26300), Hermes v0.22.7, Hermes venv = CPython 3.11.15 (has tkinter, pywin32, Pillow; **no PySide6, no comtypes**).
Monitors: DISPLAY1 3440×1440 @100%, DISPLAY2 1920×1080 @100% (x=3440). Virtual screen 5360×1440.
Run: `python spikes/run.py` with a Python that has PySide6-Essentials, comtypes, pillow, numpy, psutil. `SPIKE_MONITORS=1` limits to monitor index 1.

Ground truth = a magenta button in a test window, located by pixel scan of a physical-pixel screenshot.
The overlay draws a 3 px lime ring whose outer edge should sit exactly 7 px outside the target.

## Results (monitor 2)

| Check | Tk | PySide6 | PySide6, QT_SCALE_FACTOR=1.5 |
|---|---|---|---|
| Ring edge error (px, L/T/R/B) | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 |
| Click-through (WindowFromPoint hits app under the overlay, including on painted pixels) | yes | yes | yes* |
| Emergency hotkey Ctrl+Alt+Shift+F12 → process exit | 0.39 s | 0.45 s | 0.26 s |
| Resident memory | 62 MB | 105 MB | 104 MB |
| Startup (machine under game load) | 3.0 s | 2.0 s | 2.0 s |
| Real translucent dim / anti-aliasing / rounded shapes | no (stipple dither, jaggies) | yes | yes |

\* One harness run reported a miss. A standalone rerun showed it was another window taking z-order, not a mapping bug.

Element-position routes, compared with the magenta ground truth:

| Route | Error |
|---|---|
| UI Automation `BoundingRectangle` (comtypes, per-monitor-v2-aware client) | 0 px |
| UIA `ElementFromPoint` → name | correct |
| Vision: screenshot downscaled to 1280 wide, located, mapped back | ≤1 px (scale 0.667) |
| Win32 `GetWindowRect` | **7 px too wide on left, right and bottom** (invisible resize border). Use `DwmGetWindowAttribute(EXTENDED_FRAME_BOUNDS)`. |

Quality comparison: `out/quality_compare.png` (top = Tk, bottom = PySide6).

## Gotchas found

- `QScreen.name()` returns the monitor model (e.g. "27GN7"), not `\\.\DISPLAYn`. Use `screen.nativeInterface().handle()` (an HMONITOR) with `GetMonitorInfoW`.
- One Qt window per monitor; map with `(phys - monitor_origin) / devicePixelRatio`. A single window spanning monitors with mixed DPI renders wrong.
- The hotkey thread must not call Tk or Qt directly. Set a `threading.Event` and let a UI timer poll it.
- ctypes `SetWindowPos` needs `c_void_p` for HWND and insert-after arguments, or it silently no-ops.
- `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` hides the overlay from screenshots (verified). Hermes' own screenshots then don't see its annotations. Side effect: screen recordings miss them too, so make it a setting.
- CPython on Windows has no `AF_UNIX`. Plan to use `multiprocessing.connection` over a named pipe with an authkey.
- A fullscreen game (Marvel Rivals) on monitor 1 covered a TOPMOST window. UIA `ElementFromPoint` over it returned *Access denied* (UIPI / protected process). The overlay cannot be promised over exclusive-fullscreen or elevated apps.

## Not yet verified

- **Mixed DPI on real hardware.** Both monitors are at 100%. The Qt mapping was verified with QT_SCALE_FACTOR=1.5, which is a simulation. A real check needs one monitor set to 125% or 150% in Windows Settings.
- Monitor 1 was skipped because a game was running fullscreen there.
- UIA on browser content (Chrome/Edge expose web elements through UIA) is untested.

## Focus (added 2026-11-07)

`spikes/focus_test.py` hooks foreground and focus events. The overlay windows produced none across show, redraw, the topmost timer, clear and stop.
The one grab came from starting the daemon: `DETACHED_PROCESS` on a console launcher made its child allocate a console, which opened Windows Terminal in front.
The daemon now starts with `pythonw.exe` and `CREATE_NO_WINDOW`. Layers are also non-activating before their first show, and appear only on monitors with marks.

Re-run after the fix: no Windows Terminal, no events from the overlay, and the game kept the foreground. That run began during a focus transition (no foreground window), so the guard now refuses that state too.

## Pointer, step guide, glass notes (added 2026-11-07)

`spikes/guide_test.py` on monitor 2, on the real screen:

- The step mark matched the UI Automation element exactly, and followed its window 2.6 s after it moved.
- A step whose target was missing showed "Looking for this on screen..." and drew no mark. A step with no target was fine.
- Next, Back, jump and finish all reported the right step. A real mouse click on Next moved to step 2 with no foreground or focus change. The panel answers `WM_MOUSEACTIVATE` with `MA_NOACTIVATE`.
- No focus or foreground events came from the overlay. Only the monitor with marks had a layer.
- 18% of one core while animating, 157 MB resident (199 MB in a run that used both monitors).

The first screenshots showed app text bleeding through the glass plate at alpha 196, so it is now 232.
A note with no target first went to the monitor under the mouse, even with a game on it. It now avoids a fullscreen app when another monitor is free.

To test while a game is running, start the test on a private desktop (`CreateDesktopW` plus `STARTUPINFO.lpDesktop`). Logic and UI Automation work there, and nothing can reach the user's screen or focus. Pixels and real input do not.
