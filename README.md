# Codex Desktop Thread Rescue

Local GUI workaround for one specific Codex Desktop problem:

`the conversation gets stuck on "Automatically compacting context" and stops responding`

This project is not an official fix. It is a local recovery tool that tries to rescue the original thread without forcing a full app restart.

## What Problem This Targets

This tool is focused on a very specific Codex Desktop failure mode:

- the desktop chat stays on `Automatically compacting context`
- the thread looks frozen for minutes or hours
- the terminal may still work while the desktop chat page is stale
- restarting, branching, or switching models may sometimes help, but not reliably

## What We Found

On this machine, the failure pattern was not just "large context" and not just "bad network".

The most useful working theory became:

1. A long thread triggers compaction.
2. Sometimes the backend compaction does finish, but the desktop page does not refresh.
3. Sometimes the compaction path itself fails, especially around `gpt-5.5` compact handling.
4. In those cases, `interrupt` alone is often not enough anymore.

One important detail:

- terminal/manual compaction can sometimes succeed on the original model with no model switch
- but the desktop page may still fail to update
- when that happens, the missing step is often UI sync, not more interruption

## Current Repair Order

The tool now follows this order:

1. `Manual Compact (Same Model First)`
   This tries the closest path to terminal/manual compaction first.
2. `5.4 Fallback Compact`
   If the compact path looks like a `gpt-5.5`-specific compact failure, the tool can retry the compact step with `gpt-5.4`.
3. `Soft Reload UI`
   Use this when the backend looks healed but the chat page is still stale.
4. `Restart Renderer Only`
   Use this when the terminal work is already done, but the current chat page still does not sync.
5. `Interrupt / fallback patch repair`
   This is now a later fallback, not the first thing to try.

## Why Interrupt Alone Is No Longer Enough

For the newer `gpt-5.5` cases we observed:

- the thread may not be "purely stuck" in the old sense
- the real issue may be a failed compact path or a compact that succeeded without page sync
- interrupt can clear some situations, but it can also be the wrong first move

So the tool no longer treats interruption as the primary action.

## Buttons In The GUI

- `Manual Compact (Same Model First)`
  Use this first when a thread is stuck on automatic compaction.

- `5.4 Fallback Compact`
  Use this when same-model manual compact does not help, or when the compact path looks like a `gpt-5.5` compact failure.

- `Soft Reload UI`
  Use this when the backend looks healed but the chat page still shows the old compaction state.

- `Restart Renderer Only`
  Use this when the chat page still does not update after a soft reload, and terminal work is already finished.

- `Repair Selected`
  Runs the broader repair flow with fallbacks.

## Practical Workflow

Use this order in practice:

1. Refresh the thread list.
2. If the thread has been stuck for around 3 minutes, try `Manual Compact (Same Model First)`.
3. If that does not help, try `5.4 Fallback Compact`.
4. If the backend is healed but the page is still stale, try `Soft Reload UI`.
5. If that still does not sync the page, try `Restart Renderer Only`.
6. Only after that, use the heavier repair path.

## Important Note About Frontend Sync

If a compact really succeeded in the backend but the chat page still looks frozen:

- do not assume the compact failed
- do not immediately restart the whole app
- use the frontend sync actions first

That distinction matters. A stale page and a failed compact are not the same problem.

## Launch

Run from the project root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tool\run_rescue_gui.ps1
```

## Included Files

- `tool/run_rescue_gui.ps1`
- `tool/rescue_gui.py`
- `tool/auto_repair.py`
- `tool/external_compact_fallback.py`
- `tool/unstick_thread.py`
- `tool/codex_ipc_control.js`

## Limitations

- this is a local workaround, not an official Codex fix
- it does not patch the Codex application itself
- it may help many stuck-thread cases, but not all of them
- I cannot promise it will work on every machine or network setup

If it does not solve your setup, I am sorry. The goal is to provide a practical recovery path and a clearer troubleshooting direction.
