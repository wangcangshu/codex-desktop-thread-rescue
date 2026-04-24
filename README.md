# Codex Desktop Automatic Context Compaction Freeze Rescue

Focused on one specific issue: Codex Desktop threads getting stuck on automatic context compaction.

> Important UI refresh tip:
> If the tool reports success and you already see a completion notice, but the chat window still says automatic context compaction, do not restart immediately.
> Go back to the same chat, send a short message such as `continue`, and wait a moment.
> In some cases this is enough to refresh the frontend and reveal that compaction has already completed.

> 重要前端刷新提示：
> 如果工具已经提示修复成功，右下角也弹出了完成提醒，但聊天窗口还停在“正在自动压缩上下文”，先不要急着重启。
> 请回到原聊天里随便发一句简单的话，比如 `继续`，然后稍微等一会。
> 有些情况下，这一步就足以刷新前端，让已经完成的压缩结果显示出来。

聚焦一个具体问题：Codex 桌面版对话卡在“自动压缩上下文”并长时间无响应。

## What Problem This Targets

This tool is specifically aimed at a Codex Desktop bug where a conversation gets stuck on automatic context compaction and stops responding.

Typical symptoms:

- the desktop UI stays on "automatic context compaction"
- the conversation appears frozen for minutes or hours
- switching models does not reliably recover it
- the terminal may still work while the desktop UI remains stuck
- restarting or branching sometimes helps, but not consistently

This project is not a general Codex enhancer. It is a focused workaround for this specific stuck-thread behavior.

## 这个工具针对什么问题

这个工具专门针对一个 Codex Desktop 的 bug:

- 某个对话卡在“正在自动压缩上下文”
- 长时间没有响应
- 看起来像彻底卡死

常见表现包括：

- 桌面版 UI 一直停在“正在自动压缩上下文”
- 对话数分钟甚至数小时无法继续
- 切换模型不一定有效
- 终端有时还能继续，但桌面版 UI 仍然停在旧状态
- 重启、派生、切线程有时能缓解，但不稳定

这个项目不是通用型 Codex 增强工具，而是针对这种“线程卡死”现象的定向修复尝试。

## Working Theory

Based on local debugging, the actual situation is usually not just "the context is too large" and not just "the network is bad."

A more realistic chain looks like this:

1. Long conversations and large tool outputs make auto-compaction more likely.
2. During compact or resume, an unstable transport path, proxy or VPN path, or incomplete client recovery can leave the thread in a half-finished state.
3. The backend may already be interrupted or recoverable, but the desktop UI can still keep showing it as streaming or compacting.
4. That is why the thread may look dead, but sometimes comes back after a restart, a branch, a terminal action, or even a simple follow-up message.

So the likely trigger can involve network or proxy conditions, but the visible pain point is usually a stale thread state in the desktop client.

## 原理说明

根据本地排查，真实情况通常不是“单纯上下文太大”，也不是“单纯网络不好”。

更接近下面这条组合链路：

1. 长对话和大量工具输出更容易触发自动压缩。
2. 在 compact 或 resume 阶段，如果传输链路不稳定，或者代理、VPN 路径有异常，又或者客户端恢复不完整，就可能留下半收尾状态。
3. 后端线程有时其实已经中断，或者已经可以恢复，但桌面版 UI 仍然把它显示成还在 streaming 或 compacting。
4. 所以你会看到一种现象：线程像死了，但重启、派生、终端操作，甚至发一句简单消息后，又可能活过来。

也就是说，诱因可能和网络代理有关，但真正让人痛苦的，往往是桌面客户端把线程状态卡在了错误状态上。

## What This Tool Does

This package provides a local GUI that:

- inspects recent Codex Desktop threads
- flags threads that look likely stuck
- if a thread is using `gpt-5.5` and recent compact logs show remote `404` or model access failure, it first tries a compact-only fallback by running compaction through a separate local app-server with `gpt-5.4`
- tries a live repair first by sending a real interrupt through the local Codex IPC path
- optionally falls back to a conservative local repair if you explicitly allow it

The tool tries to avoid restart-first recovery and focuses on rescuing the original thread whenever possible.

## 这个工具怎么做

这个工具提供了一个本地 GUI，可以：

- 检查最近的 Codex Desktop 线程
- 标记疑似卡死线程
- 如果线程使用的是 `gpt-5.5`，并且最近的 compact 日志显示远端 `404` 或模型无权限，它会先尝试一种“只修 compact”的回退办法：通过独立的本地 app-server 用 `gpt-5.4` 临时执行压缩
- 优先尝试实时修复，也就是通过本地 Codex IPC 发送真实 interrupt
- 只有你明确允许时，才使用更保守的本地兜底修复

它的重点不是“先重启再说”，而是尽量把原线程本身救回来。

## Launch

Run from the project root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tool\run_rescue_gui.ps1
```

## 启动方式

在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tool\run_rescue_gui.ps1
```

## Practical Tips

- Do not restart immediately just because you see automatic context compaction.
- Refresh the thread list first and see whether the thread really looks stuck.
- If the tool reports success but the chat UI still says automatic context compaction, send a very short follow-up such as "continue" in that same chat and wait a moment before trying anything more drastic.
- If the terminal already succeeded but the desktop UI still shows the old state, try sending a very simple message such as "hi" or "continue" in that chat.
- If that still does not refresh the UI, switch to another conversation and then switch back.
- If a `gpt-5.5` thread is failing only during compaction, the compact-only fallback may take a little longer than a normal interrupt repair. Give it a short moment before assuming it failed.
- Avoid mixing restart, model switching, branching, and terminal compaction randomly, because it makes the failure harder to understand.

## 使用小贴士

- 不要一看到“正在自动压缩上下文”就立刻重启。
- 先刷新线程列表，判断它是不是真的卡住了。
- 如果终端已经成功，但桌面版 UI 还没刷新，可以先在那个聊天里发一句很简单的话，比如“嗨”或者“继续”。
- 如果这样还是没刷新，可以切到别的对话，再切回来。
- 如果是 `gpt-5.5` 线程只在压缩阶段失败，compact-only fallback 可能会比普通 interrupt 修复稍慢一点，先给它一点时间，不要立刻判断失败。
- 不建议把重启、切模型、派生、终端 compact 混在一起乱试，否则更难判断是哪一步真正起作用。

## Scope And Limitations

- this is a local workaround, not an official fix
- it does not modify the Codex application itself
- it may help many stuck-thread cases, but not all of them
- if it does not solve your setup, I am sorry, but I hope it at least gives you a useful direction for investigating other solutions

## 适用边界与说明

- 这是一个本地 workaround，不是官方修复
- 它不会修改 Codex 程序本体
- 它可能对很多“自动压缩上下文卡死”的情况有帮助，但不能保证解决所有环境里的问题
- 如果它没有解决你的问题，我很抱歉；但我希望它至少能给你一些排查思路，帮助你继续寻找别的解决办法

## Included Files

- `tool/run_rescue_gui.ps1`
- `tool/rescue_gui.py`
- `tool/auto_repair.py`
- `tool/external_compact_fallback.py`
- `tool/unstick_thread.py`
- `tool/codex_ipc_control.js`

## Notes

- this tool reads the current machine's `~/.codex` by default
- it may generate a local `reports/` directory after running
- do not publish the generated `reports/` if you plan to share the project

## 注意

- 这个工具默认读取当前机器的 `~/.codex`
- 运行后可能在当前目录生成 `reports/`
- 如果你准备分享这个项目，不要把运行后生成的 `reports/` 一起公开
