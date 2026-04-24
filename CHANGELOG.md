# Changelog

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
- Updated the GUI wording to better reflect the real workflow and newer `gpt-5.5` behavior.

### Notes

- For some newer `gpt-5.5` cases, `interrupt` alone is often not enough.
- The effective recovery path may require:
  1. manual compact
  2. optional 5.4 fallback compact
  3. frontend sync

