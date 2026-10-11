# Live: subtitle status indicator and recent-subtitle history (2026-10-11)

Author: Claude Code. Status: implemented; Codex PASS (round 2).
Owner request: "狀態小燈跟字幕回看" — know at once when something is wrong, and re-read missed lines.

## 1. Why

On 2026-10-10/11 every problem (profile stuck on general, ElevenLabs stalls, Groq fallback,
dropped sentences) was found afterwards in the logs. Display drops are not the issue
(`subtitle.pending_overwritten` = 1 in the latest runs), so history is a convenience, not a fix.

## 2. Design

- `modules/live_status.py`: one thread-safe `LiveStatus` singleton, in-memory only.
  - `record_stt_engine(engine)` — STT worker, for every produced transcription event.
  - `record_translation(ok)` — translation emitter: True when a subtitle is produced, False only
    for real failures (`status == "failed"` or final-script rejection). Policy skips (laughter,
    filtered noise) record nothing; stale/duplicate suppression counts as success.
  - `record_subtitle(source, target)` — emitter, for every published subtitle (ring of 20).
  - `flags(profile_unresolved)` → labels: `general` (auto mode, no effective profile),
    `Groq 辨識` (latest transcription came from Groq within 30 s), `翻譯失敗` (last 3 finished
    sentences all failed, first within 90 s). Known blind spot: `filtered` outcomes (including
    `meta_garbage_output`) never count, so repeated garbage output does not light it.
- `SubtitleWindow`:
  - Status line (small, amber) drawn above the subtitle only while labels exist; it stays visible
    when the subtitle hides; re-evaluated once a second in `_poll`; off while paused; disabled by
    `cfg.subtitle.status_indicator_enabled`.
  - Right-click on the subtitle text toggles a history view: the last `history_lines` (6)
    **final** subtitles (the provisional path is not recorded), Korean source (grey, small) above
    each translation, left-aligned. It closes on a second right-click or after
    `history_display_ms` (15 s). While it is open the display pipeline waits: the subtitle queue is
    not drained, so nothing is shown, timed or reported as `displayed` until the view closes
    (≤ 15 s; the queue holds 10). Pause/resume closes it first and it cannot open while paused.
    A tall view lifts the window to stay above the screen bottom (taskbar margin) and restores the
    position on close.
  - Why right-click: the window is click-through except text pixels and rarely gets keyboard
    focus (existing key bindings only work when focused); double-click already quits.
  - Rendering: `_draw_outlined_text(text)` keeps its signature and builds lines for a new
    stacked, outlined `_draw_lines(lines, justify)`. `_flash` (pause/resume symbols) now records
    the symbol as the visible text so a status redraw does not replace it.

## 3. Validation

- `tests/test_live_status.py` (5): quiet → no flags; general; Groq flag expiry; failing streak
  (needs 3, cleared by a success, expires after 90 s); history ring and empty-target skip.
- `tests/test_subtitle_display.py` (+5): status line added/removed above a subtitle; stays when the
  subtitle hides; history shows source + translation, is not overdrawn by new subtitles, closes on
  right-click and by timer; the pause symbol survives a status change.
- Real Tk smoke (withdrawn root): subtitle+status, history and hide render without errors.
- Full suite 1717 passed, 1 skipped (also with sounddevice unavailable, as on CI).

## 4. Review log

### Codex review round 1 (2026-10-11): FIX

Blockers: (1) with history open, `_show` skipped drawing but still started the minimum-display
clock and the pipeline emitted `displayed`/`final_revision_displayed` for text not on screen; after
closing, a final could be replaced after 100 ms; (2) pausing during history left the status line on
screen; (3) the resume flash stayed past its hide timer during history. Non-blocking: tall history
can run off the screen bottom; history only records finals; `filtered` (incl. meta garbage) never
lights the failure flag.

### Claude Code revision

- `_poll` does not drain or show anything while history is open (only the status refresh runs);
  an expired view is closed there, then the queued subtitles are processed normally — timing and
  telemetry start when they are actually drawn (test with a provisional payload and captured
  events).
- `_toggle_translation` closes history before pausing/resuming; history cannot open while paused
  (tests for pause-during-history, the status line clearing and the resume flash hide).
- History lifts the window when `winfo_screenheight` would be exceeded and restores it on close
  (real Tk check: y 1000 → 756 → 1000 for a 348 px view on a 1152 px screen).
- Plan wording: finals only; failure-flag blind spot documented.
- Full suite 1719 passed, 1 skipped (also with sounddevice unavailable).

### Codex re-review round 2 (2026-10-11): PASS

All three blockers resolved; revision/final ordering, redraw on close and position restore
(manual, timeout, pause) verified. Limitation noted: if more than 10 subtitles arrived during one
15 s history view, the existing `put_latest` policy would drop the older queued ones (typical rate
is 3–5 per 15 s).
