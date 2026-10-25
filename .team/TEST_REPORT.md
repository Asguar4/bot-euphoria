# TEST REPORT (round 3)

**Verdict: PASS** (0 critical/major bugs; 2 minor notes)

## Summary
- Command: `.venv/Scripts/python -m pytest --cov=src --cov-report=term-missing`
- 619 tests: **617 passed, 0 failed, 2 skipped (POSIX-only)**. Run 3 times (2 with coverage), all green, no flakiness (~38 s).
- Coverage **98 %** (1406 stmts, 24 missed).
- Added by QA: `tests/test_round3.py` (34 cases): OPT-3 budget sum/bounds, OPT-4, OPT-8, OPT-12.
- Production code not modified (mutation checks done in a scratch copy).

## Quality gates
| Gate | Result |
|---|---|
| `ruff check .` | pass |
| `ruff format --check .` | pass (54 files) |
| `mypy src` (strict) | pass (24 files) |
| coverage >= 85 % | pass (98 %) |

## Acceptance criteria
All 39 PASS (unchanged from round 2; full suite green). Frozen API (section 5): tests/test_frozen_api.py green; `add_scheduler_task(self, func, day, hour, minute, id, args=())` signature verified unchanged.

## Developer regression tests: do they catch the old behaviour?
Each fix reverted in a scratch copy (`scratchpad/m`), only that test run:
| Test | Reverted fix | Result |
|---|---|---|
| test_file_sink_write_after_close_is_noop | remove `if self._closed: return` | FAILS (meaningful) |
| test_no_rollback_on_closed_connection | `if not conn.closed` -> `if True` | FAILS (meaningful) |
| test_slow_unload_does_not_starve_gateway_and_database | use constant 30 s unload timeout instead of `_unload_budget()` | FAILS (meaningful) |
| test_stop_within_step_budget_with_stuck_client | state file removal moved to end of stop() | FAILS (meaningful) |
| same | all four OPT-1 changes reverted together | FAILS (meaningful) |
| same | only `abort_clients()` -> `close_clients()` | passes (not detected) |
| same | only drain() timeout removed | passes (not detected) |
| same | only wait_closed() 1 s bound removed | passes (not detected) |
Conclusion: the OPT-1 test catches the original failure scenario (stop() exceeding budget / stale token file) but individual sub-fixes (abort vs close, drain bound, wait_closed bound) are not pinned independently (NOTE-1, minor; drain bound cannot be tested at idle_timeout=30 in this test).

## Extra checks
- Teardown budget with defaults (shutdown_timeout 30, close_timeout 5): stop 3 + unload 10.5 + gateway 10 + DB min(10, 5+1.5) = 30.0 <= 30 (test_default_teardown_budget_fits_shutdown_timeout). Note: with the DB close_timeout at 10 the unload budget is the 5.5 s quoted by the developer. `_unload_budget()` stays within [1, 30] for shutdown_timeout 0.5..120 (floor 1.0 s can exceed tiny shutdown_timeout; acceptable, overall deadline in run_app still applies).
- OPT-4: unauthenticated idle peer is closed after FIRST_LINE_TIMEOUT; a slow-but-legit first line (within limit) works; an authenticated client may idle beyond FIRST_LINE_TIMEOUT (up to idle_timeout) and is not cut; idle_timeout < 5 s is respected via min(). Note: a client that takes >5 s to send its first line is dropped (by design).
- OPT-8: 0, -1, -0.5, 0.0 rejected for MAX_CONNECTIONS / IDLE_TIMEOUT / COMMAND_TIMEOUT with ConfigError naming the key; 1, 0.001 accepted; bool/str/list/float-for-int rejected. NOTE-2 (minor): NaN/inf for the float timeouts are not rejected (`nan <= 0` is False); unreachable via JSON config in practice.
- OPT-12: sync job logs a warning (contains job id), async job does not, job still registered, frozen signature and `args` default unchanged.
- OPT-9 test tightened and passing; OPT-10/11 no regressions (hygiene tests green).

## Bugs
None open (BUG-1..5 remain fixed).

## Notes (minor, non-blocking)
- NOTE-1: OPT-1 regression test does not isolate abort_clients / drain bound / wait_closed bound (test gap, developer).
- NOTE-2: NaN/inf timeouts accepted by settings (coding, trivial: use `not value > 0` / math.isfinite).

## History
- Round 1: FAIL, 581 tests (572 passed / 2 failed / 5 xfailed / 2 skipped), cov 99 %, 1 major (BUG-5) + 4 minor (BUG-1..4).
- Round 2: PASS, 583 tests (581 passed / 2 skipped), cov 98 %, BUG-1..5 verified fixed.
- Round 3: PASS, 619 tests (617 passed / 2 skipped), cov 98 %, all gates pass, OPT-1..12 verified, 2 minor notes.
