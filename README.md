# CUA_Collector

`CUA_Collector` is a lightweight desktop data collection tool for Computer Use Agent workflows. It records UI interaction traces as paired screenshots around each user action:

- `State_A`: screenshot before the action
- `Action`: click or scroll event with coordinates and metadata
- `State_B`: screenshot after the action

The output is useful for building datasets for UI automation, behavior cloning, and action prediction experiments.

## What It Does

The collector runs as a local desktop app with hotkeys:

- `Ctrl+F8`: start a new task and enter a task description
- `Ctrl+F9`: capture the "before" screenshot and begin listening for an action
- mouse click or scroll: record the action
- automatic debounce: wait briefly, then capture the "after" screenshot
- `Ctrl+F12`: end the current task
- `Esc`: cancel the current pending action

Each task is stored with:

- task metadata
- timestamps
- operating system and session information
- screen resolution
- ordered action records
- before/after screenshots for every captured action

## Repository Layout

```text
collector.py            Main entrypoint
platform_backends.py    Platform-specific screenshot and input backends
screenshot_wayland.py   Wayland screencast capture support
requirements.txt        Python dependencies
data/                   Collected output (ignored by git)
```

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python collector.py
```

Optional flags:

```bash
python collector.py --data-dir ./data --debounce 0.5
```

## Output Format

Collected tasks are written under `data/<task_id>/`:

```text
data/
  index.json
  <task_id>/
    task.json
    screenshots/
      action_0001_before.png
      action_0001_after.png
      ...
```

`task.json` contains the task description plus the ordered action list. `index.json` provides a compact summary across all collected tasks.

## Platform Notes

- Linux is the primary target.
- On Wayland, the screenshot backend uses the XDG desktop portal and PipeWire screencast flow.
- Depending on desktop environment and permissions, the first capture may trigger a screen-share approval prompt.
- Global hotkey behavior can vary across environments.

## Typical Use Case

Use this when you want to manually demonstrate a desktop workflow and save it as structured training data for:

- UI behavior cloning
- desktop agent evaluation
- action-conditioned screenshot datasets
- task replay and annotation pipelines

## License

No license file is included by default. Add one before broader reuse if you want to define redistribution terms.
