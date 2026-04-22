# Codex 线程修复器 / Codex Thread Rescue

这是一个纯净版的本地修复工具，只包含运行所需文件和一份使用说明。  
This is a clean local repair tool package that includes only the runtime files and one usage guide.

## 包含内容 / Included Files

- `tool/run_rescue_gui.ps1`
- `tool/rescue_gui.py`
- `tool/auto_repair.py`
- `tool/unstick_thread.py`
- `tool/codex_ipc_control.js`

## 它能做什么 / What It Does

中文：

- 检查当前机器上最近的 Codex 线程
- 标记疑似卡在“正在自动压缩上下文”的线程
- 一键尝试修复
- 优先使用实时修复，不直接修改本地状态
- 只有你明确允许时，才使用保守兜底修复

English:

- inspects recent Codex threads on the current machine
- flags threads that are likely stuck on automatic context compaction
- attempts a one-click repair
- prefers live repair without directly patching local state
- uses conservative fallback repair only when you explicitly allow it

## 启动方式 / Launch

在 `tool` 目录里运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tool\run_rescue_gui.ps1
```

## 使用方法 / How To Use

1. 打开工具后先点“刷新列表 / Refresh”。
2. 选中状态为“可能卡死 / Likely Stuck”的线程。
3. 先直接点“修复选中线程 / Repair Selected”。
4. 如果实时修复不够，再勾选“允许保守补丁修复 / Allow fallback patch repair”后重试。

## 小贴士 / Tips

中文：

- 不要一看到“正在自动压缩上下文”就立刻重启，先刷新线程状态。
- 如果终端已经成功，但桌面版 UI 还没刷新，可以先在聊天里发送一句很简单的话，例如“嗨”或“继续”。
- 如果简单消息后还是没刷新，可以切到别的对话再切回来。
- 除非必要，不要把重启、切模型、派生、终端 compact 混在一起乱试。

English:

- do not restart immediately when you see automatic context compaction; refresh the thread state first
- if the terminal has already succeeded but the desktop UI has not refreshed, try sending a very simple message such as “hi” or “continue”
- if that still does not refresh the UI, switch to another conversation and then switch back
- unless necessary, avoid mixing restart, model switching, branching, and terminal compaction in random order

## 注意 / Notes

- 这个工具默认读取当前机器的 `~/.codex`
- 工具运行后可能在当前目录生成本地 `reports/`
- 如果你准备公开分享，请不要把运行后生成的 `reports/` 一起带出去
