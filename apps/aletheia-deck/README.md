# Aletheia Deck

Native C++/Qt6 Omarchy touchscreen deck built for the ASUS ZenBook Pro Duo UX582HS ScreenPad Plus (`DP-3`, `3840x1100 @ scale 2` -> `1920x550` logical).

## Three-Pane Layout

1. **Swarm Board & Worker Roster (Left ~34%)** — Live worker status pills (`working`/`waiting`), filter pills (`All`, `Done`, `GPU`, `Audits`), and auto-scrolling board feed with tap-to-expand detail.
2. **Interactive Track Queue (Center ~31%)** — Live `EXPERIMENTATION_QUEUE.md` cards with touch Pin/Prioritize, Flag/Note, and `+ New Track / Idea` (`SOP-12`) quick capture.
3. **Bi-Directional Shared Workspace (Right ~35%)** — Conflict-safe tabbed `SHARED_WORKSPACE.md` (`Pinboard`, `Shared Doc`, `PI Notes`, `Priorities`) with `600ms` atomic auto-save plus direct **"Write to Coordinator"** bar with live `Queued -> Read by Coordinator` delivery tracking.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `?` | Toggle keyboard shortcut overlay |
| `Ctrl+S` | Save Shared Workspace immediately |
| `Ctrl+Enter` | Send message to `@coordinator` |
| `F5` | Refresh swarm board & track queue |
| `Q` / `Ctrl+Q` | Save & quit Aletheia Deck |

## Build, Test, Install

```sh
./bin/build
./bin/test
./bin/install
```
