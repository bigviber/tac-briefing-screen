# Approach 2 — device-export driven (pending)

Nothing here is built yet. This folder is waiting for data from the device owner; the design will be decided from it. See the root README for context and the candidate directions.

## What to drop here

- `device-export/` — the device folder export, raw, exactly as exported (no edits).
- `logs/` — Android native logs captured while reproducing the problem:
  - `logcat` (full, including the Bluetooth buffers)
  - `btsnoop_hci.log`
  - `dumpsys bluetooth_manager`
  - `dumpsys media.audio_policy`
  - `dumpsys media_session`
  - `dumpsys package com.android.bluetooth`
