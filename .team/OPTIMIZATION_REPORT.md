# OPTIMIZATION REPORT (round 2 of 2, final)

**Verdict: APPROVED** (0 MUST-FIX, 4 SUGGESTION)

Scope: `src/euphoria/**`, `src/utils/**`, `tests/`. Stack deviations D1-D7 are approved and not flagged. The frozen API in DESIGN section 5 is unchanged: all fixes are internal. I did not modify production code. Scratch repros were placed in `tests/` only for the duration of a run and then removed.

## Verification of round-1 MUST-FIX items

| Id | Before (round 1) | After (round 2) | Status |
|---|---|---|---|
| OPT-1 control `stop()` | `verify_stop.py`: the `_stop_control` step **timed out after 3.01 s**, the state file was left on disk, and the `_handle_client` task was leaked | 3 runs: **stop finished in 2.03 s** each time. State file removed, 0 server tasks left. `bench_server.py`: `stop(grace=2)` with 1 stuck + 3 idle clients took 1.96 s with 0 tasks left. A non-reading pipelining client (idle_timeout=2) leaves 0 tasks alive after 3 s, so the drain bound works | RESOLVED |
| OPT-2 FileSink reopen after close | `stream reopened after close: True`, `unlink -> PermissionError` | `reopened: False`, file can be deleted | RESOLVED |
| OPT-3 teardown budget | scaled 1:10: `rc=1`, steps run `['unload']` (gateway and DB never closed) | Original repro: `rc=0`, steps `['unload','gateway','database']`. New worst case, scaled 1:10 (every step hangs: control, unload, gateway; DB bounded at close_timeout+1): `rc=0`, elapsed 3.01 s, all four steps ran, DB finished. At full scale: 3 + 10.5 + 10 + 6 (DB worst case) = 29.5 s, within the 30 s budget | RESOLVED |

Budget arithmetic: the `_close_database` step timeout is 10 s, but `Database.close()` is self-bounded at `close_timeout + 1` (terminate plus 1 s), and `_unload_budget()` reserves `close_timeout + 1.5`, so the sum holds. A large `DB_CLOSE_TIMEOUT` only shrinks unload to `MIN_UNLOAD_BUDGET` and does not overrun the budget.

## Measurements summary (round 2, CPython 3.14.6, Windows 11)

| What | Round 1 | Round 2 |
|---|---|---|
| Facade `Logger.info` -> FileSink | 16.4 us/record | **12.9 us/record** (OPT-7: no per-write `Path().stem`) |
| stdlib -> RotatingFileHandler / with rotation | 17.4 / 8.2 us | 17.5 / 9.0 us (noise) |
| Protocol encode/parse req/resp | 2.5 / 3.7 / 2.7 / 3.2 us | 2.9 / 3.7 / 3.0 / 3.4 us (noise) |
| Server: new-conn ping / keep-alive / 8 concurrent | 868 / 10,450 / 12,200 req/s | 801 / 10,334 / 11,728 req/s (noise, no regression) |
| Memory per idle connection pair | ~136 KiB | ~136 KiB |
| 9th client while 8 unauthenticated peers hold slots | busy in 2 ms; slots held 60 s | busy in 2.2 ms; slots now freed after FIRST_LINE_TIMEOUT = 5 s (OPT-4) |
| Test suite | 617 passed in ~39 s; slowest test 5.14 s | 617 passed, 2 skipped in 37.7 s, 98 % coverage; `test_request_shutdown...` no longer in the top 6 (OPT-9) |
| ruff check / format, mypy strict | clean | clean |

Other round-1 suggestions, verified by reading the code:
- OPT-5: `if not conn.closed` guard before rollback.
- OPT-8: `> 0` checks for MAX_CONNECTIONS, IDLE_TIMEOUT and COMMAND_TIMEOUT.
- OPT-10: `import os` moved to module level.
- OPT-11: `.coverage` and `htmlcov/` added to `.gitignore`.
- OPT-12: non-async scheduler jobs now log a warning.
- OPT-6 was skipped by the developer (no MySQL available) and remains a suggestion.

## Findings

### MUST-FIX
None.

### SUGGESTION

**OPT-6 (carried over): reads with `DB_AUTOCOMMIT=false` defeat the pool**
- File: `src/utils/database.py`, `fetchall` / `get`
- Problem: unchanged from round 1. It affects only the non-default `autocommit=false`.
- Fix: after reading, `if not conn.get_autocommit(): await conn.rollback()`.

**OPT-13: `NaN` timeouts in config crash the event loop; `Infinity` disables the bounds**
- Files:
  - `src/euphoria/settings.py:182` (`value <= 0`)
  - `:145` (SHUTDOWN_TIMEOUT)
  - `src/utils/database.py` (`DB_CLOSE_TIMEOUT`)
- Problem: `json.load` accepts the `NaN` and `Infinity` literals, and `nan <= 0` is False, so these values pass validation. Measured: `asyncio.timeout(float('nan'))` raises `ValueError: cannot convert float NaN to integer` from inside `windows_events._poll`. That kills the whole loop, not just the request. With `Infinity`, the idle, drain, shutdown or close bounds become unbounded.
- Severity: this is a suggestion because only the owner can trigger it, with a non-RFC JSON literal.
- Fix: `if not (math.isfinite(value) and value > 0): raise ConfigError(...)`, applied in the same places. About 3 lines.

**OPT-14: Flaky test `test_oversize_line_bad_request_then_server_alive`**
- File: `tests/test_control_server.py:48-66` (the `rpc` helper), called at `:222`
- Problem: it failed in 4 of about 12 isolated runs, and once in a full-suite run. The error is `ConnectionResetError: [WinError 64]` at `rpc` line 57.
- Cause: the server closes the socket while about 128 KiB of the client's oversized, newline-less payload is still unread. Windows then sends RST instead of FIN.
- Evidence (`flake_oversize.py`, 200 iterations):
  - The `bad_request` response line arrived **200/200** times.
  - Only the subsequent EOF read sees the reset: new code 13/200, round-1 close path 19/200.
  - So the product behaves correctly, and this is not a regression from OPT-1.
- Fix: in `rpc`, `except (ValueError, ConnectionError): break` (treat a reset as EOF). One line, test-only.
- Why it matters: the green gate is otherwise unreliable on Windows.

**OPT-15: The OPT-1 sub-changes are not individually pinned by tests (tester note 1)**
- File: `tests/test_control_server.py`
- Problem: `test_stop_within_step_budget_with_stuck_client` covers the combined behaviour. Nothing would catch a revert of only `abort_clients()`, only the `drain()` timeout, or only the order of `_remove_state_file()`.
- Fix (optional, about 30 lines):
  1. Test that a non-reading pipelining client is dropped after `idle_timeout` (mirrors `bench_server.py`).
  2. Test that the state file is gone even if `stop()` is cancelled right after `server.close()`.

### Reviewed, no action
- `_remove_state_file()` now runs before the client drain. This is correct: the listener is already closed, and the file only advertises it.
- `_send` drain bound = `idle_timeout` (60 s). This is longer than `grace`, but `stop()` aborts the transports anyway (measured 1.96-2.03 s), so the bound only matters for live connections, as intended.
- `_handle_client` `finally`: the 1 s `wait_closed` bound with `transport.abort()` fallback is correct. After `abort_clients()` it returns immediately.
- `ConsoleSink` after close still writes to stderr: there is no handle to leak, so this is fine.

## History
- Round 1: NEEDS_REWORK. 3 MUST-FIX (OPT-1 control stop/drain, OPT-2 FileSink reopen leak, OPT-3 teardown budget) and 9 suggestions.
- Round 2: APPROVED. OPT-1..3 resolved and verified. OPT-4, 5, 7-12 done. Remaining suggestions: OPT-6, OPT-13 (NaN/inf config), OPT-14 (flaky test helper), OPT-15 (targeted stop tests).
