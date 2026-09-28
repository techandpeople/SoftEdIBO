# SoftEdIBO - rules for AI coding assistants

You are working on SoftEdIBO: a PySide6 desktop app (shipped with PyInstaller)
that drives soft inflatable robot skins over ESP-NOW for a study with children.
Contributors use Linux/WSL and native Windows - see "Platform notes".

## Architecture - where code goes
- Activities are DATA. A new activity (e.g. CPR) is a JSON spec in `data/`
  built from catalog verbs/conditions in `src/activities/catalog.py`. The
  Blockly editor blocks are generated from the catalog automatically.
- If an activity needs something new, add ONE generic verb or condition to the
  catalog (plus its evaluator in `scripted_activity.py`, plus tests). Name it
  by what it does (`group_touch_sync`), never by the activity (`cpr_...`).
- NEVER put activity names or activity-specific state in the engine
  (`scripted_activity.py`), the hardware layer (`src/hardware/`, `Skin`) or
  the GUI. Never detect an activity by its name (`"cpr" in activity.name`).
- Never write activity state onto hardware objects (no `skin.some_status = ...`).
  The GUI pulls from the activity (`activity.progress(robot_id)`) or listens
  to a callback/signal the activity exposes.
- Activity parameters live in the block/condition params. Skin/touch settings
  are only for SENSOR tuning and must never override an activity parameter.
- Before adding logic, search for an existing class that already does it and
  reuse it: `PressRateMeter` (press frequency), `GroupTouchSyncTracker`,
  `TouchSensorProfile`, skin geometry (sensor count), `Settings.touch_tuning`.
- Do not hardcode sensor counts (4 quadrants). Use the geometry or the length
  of the stream.

## OOP
- Encapsulation: never read another object's private attributes (`_x`) or
  `self.parent()._something`. Add a public method or property instead.
- Single responsibility, small functions (no 200-line methods), inject
  collaborators, and use signals/callbacks instead of poking at state.
- Callbacks from the gateway thread must reach the GUI through a Qt
  `QueuedConnection` signal.

## GUI
- Every layout is a Qt Designer `.ui` file in `src/gui/ui/`. Do not build
  layouts in Python code. Custom painted widgets become promoted widgets.
- Compile the .ui files after editing them (commands in "Platform notes").
  The generated `src/gui/ui_*.py` files are gitignored - never commit them.
- Every new control gets a `whatsThis` help string (and a `toolTip`).
- Every user-facing tool is a GUI dialog; CLI scripts are dev-only.

## Settings / data
- Use a fresh `Settings()` for each read-modify-write; do not keep an
  instance around (it would overwrite other saves).
- Database: never add columns to existing tables (no migrations); add a new table.

## Code style
- English only (code, comments, strings, docs). ASCII only in source: no
  arrows, em-dashes, ellipses, emojis, micro or ohm signs (write `uT`, `ohm`).
- No personal names in the repo. Never call the PCBs "Themio"/"Bigthemio";
  say `node_direct` / `node_multiplexed`.
- Match the surrounding code's naming, comment density and idioms.

## Before you finish
- `python -m pytest -q tests` must pass, and new logic needs tests.
- `python -m pyright` must report 0 errors.
- Do not touch `firmware/`, `hardware/` or the build/CI setup without asking
  the maintainer first.
- For anything bigger than a small change, describe the plan and which layer
  each part goes in BEFORE writing code.

## Git
- Short, descriptive commit messages ("group sync round log", not "update"
  or a name). One logical change per commit. No AI co-author trailers.
- `git pull --rebase` before pushing.

## Platform notes
- Linux / WSL: `bash scripts/compile_ui.sh`; the `scripts/*.sh` helpers work.
- Native Windows (PowerShell): the `.sh` scripts do not run. Compile the UI with
  `Get-ChildItem src/gui/ui/*.ui | % { pyside6-uic $_.FullName -o "src/gui/ui_$($_.BaseName).py" }`.
  Run `git config core.autocrlf true` once, so commits keep LF line endings;
  never commit whole-file CRLF churn.
- Code must stay cross-platform: use `pathlib`, not hardcoded `/` or `\`
  paths or shell-specific calls.
