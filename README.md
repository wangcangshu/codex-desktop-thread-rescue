# Codex Desktop Thread Rescue (Updated 2026-05-04 / 最近更新 2026-05-04)

Local GUI repair tool for one specific Codex Desktop problem:

`the conversation gets stuck on "Automatically compacting context" or "Compressing context" and does not recover cleanly`

This is not an official fix. It is a local recovery tool.

## Update Note / 更新说明

### English

After several recent Codex Desktop updates, the desktop `manual compact / resume / follower` chain became more fragile.

The main failure was not that same-model compaction disappeared. The real problem was that desktop compaction sessions could enter a `started but not finalized` state: the compact request was sent, but the session did not close cleanly and the frontend did not return to a stable state.

That is why:

- the official stuck auto-compaction bug was still not truly fixed
- the external recovery entry point this tool relied on also became unstable
- it looked like the repair tool had stopped working

What was repaired here was not the model capability itself. The repair was to restore the broken compaction session path, so a stuck compact request can be relaunched from a clean execution session.

Current status:

- same-model manual compaction works again
- the external recovery entry point works again
- the tool can bypass a broken desktop compaction session and relaunch compaction cleanly

### 中文

最近几次 Codex Desktop 更新后，桌面端的 `manual compact / resume / follower` 链路变得更脆弱了。

这次真正坏掉的，不是同模型压缩能力本身，而是桌面端的压缩会话更容易进入一种 `started but not finalized` 的坏状态：压缩请求已经发出，但会话没有正常收尾，前端也没有正确回到稳定状态。

这也是为什么：

- 官方“自动压缩上下文卡住”的老问题并没有真正修好
- 这个工具原本依赖的外部修复入口也一起变得不稳定
- 表面看起来像是修复工具失效了

这次修好的，不是模型能力本身，而是这条已经卡住的压缩会话链。现在工具可以把卡死的压缩请求从坏掉的桌面会话里摘出来，再从一个干净的执行会话里重新发起。

当前状态：

- 同模型手动压缩已恢复可用
- 外部修复入口已恢复可用
- 工具现在可以绕过坏掉的桌面压缩会话，重新拉起这次压缩

## What This Tool Does

- Inspect recent Codex Desktop threads
- Detect likely stuck compaction states
- Trigger same-model manual compaction first
- Use an external recovery path when the desktop compaction session is poisoned
- Avoid dangerous frontend reload actions that can break thread resume

## Main Buttons

- `Manual Compact (Same Model First)`
  Use this first when a thread is near or already stuck in compaction.

- `5.4 Fallback Compact`
  Use this only when the compact session itself is unhealthy and same-model recovery is not enough.

- `Repair Selected`
  Runs the broader repair flow.

## Launch

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

## Limits

- This is a workaround, not an official Codex patch.
- It does not modify the Codex application binary.
- It may help many stuck-thread cases, but not all of them.
