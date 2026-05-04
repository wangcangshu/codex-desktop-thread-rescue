# Changelog

## 2026-05-04

### Updated

- Marked the release date clearly in the README.
- Rewrote the update note to explain the real issue more precisely:
  - recent Codex Desktop updates made the desktop `manual compact / resume / follower` chain more fragile
  - the main failure was a `started but not finalized` compaction session
  - the repair tool looked broken because its external recovery path was tied to the same fragile chain

### Fixed

- Restored same-model manual compaction as the primary recovery path.
- Restored the external recovery entry point for poisoned desktop compaction sessions.
- Normalized external resume path handling to reduce `mismatched path` resume failures.
- Kept frontend reload actions disabled in the GUI to avoid breaking thread resume on newer Codex Desktop builds.

## 2026-04-24

### Changed

- Reframed the repair strategy around the actual Codex Desktop issue: chats stuck on `Automatically compacting context`.
- Stopped treating `interrupt` as the default first action.
- Prioritized `Manual Compact (Same Model First)` to better match terminal/manual compaction behavior.
- Kept `5.4 Fallback Compact` as a separate recovery path for compact-specific failures, especially around `gpt-5.5`.
- Added clearer frontend sync guidance:
  - `Soft Reload UI`
  - `Restart Renderer Only`

### Fixed

- Corrected the tool logic so "manual compact" and "5.4 fallback compact" are treated as different paths instead of one ambiguous action.
- Improved detection for cases where backend compaction succeeded but the chat page stayed stale.
- Clarified that a full progress circle in the background-info panel can also be a stale-frontend symptom after compaction has already finished.
- Updated the GUI wording to better reflect the real workflow and newer `gpt-5.5` behavior.
