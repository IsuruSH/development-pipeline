# Run summary — `20260522-041823-c88c3d`

- **Feature:** user_authentication
- **Status:** HALTED
- **Halt reason:** gate failed: ruff

## Totals

| Metric | Value |
| --- | --- |
| Total duration | 132321 ms |
| Tokens in / out | 4,853 / 1,861 |
| Estimated cost | $0.0000 |
| Files generated | 1 (57 lines) |
| Tests generated | 1 (57 lines) |
| Acceptance coverage | 0/6 (0.0%) |
| Quality gates passed | 1/4 (25.0%) |

## Per-stage timing

| Stage | Status | Duration (ms) | Tokens in/out |
| --- | --- | --- | --- |
| intake | passed | 22 | 0/0 |
| planner | passed | 24453 | 1063/409 |
| approval_plan | passed | 0 | 0/0 |
| implementer | passed | 51048 | 1603/829 |
| test_generator | passed | 52358 | 2187/623 |
| quality_gates | failed | 4440 | 0/0 |

## Quality gates

| Gate | Status | Duration (ms) | Log |
| --- | --- | --- | --- |
| ruff | FAIL | 339 | audit_logs/20260522-041823-c88c3d/gates/ruff.log |
| mypy | FAIL | 2430 | audit_logs/20260522-041823-c88c3d/gates/mypy.log |
| pytest | FAIL | 1228 | audit_logs/20260522-041823-c88c3d/gates/pytest.log |
| bandit | PASS | 324 | audit_logs/20260522-041823-c88c3d/gates/bandit.log |

## Approvals

- **pre-implementation** by `pipeline` at 2026-05-22T04:19:08.294499+00:00 → **APPROVED**

## Acceptance criteria coverage

- [ ] **AC-1** — Registering a new email with a strong password succeeds and returns a
user object with a generated id and the original email lower-cased.

- [ ] **AC-2** — Registering an email that is already registered (case-insensitively)
raises a duplicate-email error and does not create a second account.

- [ ] **AC-3** — Registering with a password that fails the strength policy raises a
weak-password error and does not create an account.

- [ ] **AC-4** — Logging in with the correct email + password returns a session token
whose signature can be verified and whose payload contains the user id
and a future expiry.

- [ ] **AC-5** — Logging in with the correct email but wrong password raises an
invalid-credentials error and does NOT leak whether the email exists.

- [ ] **AC-6** — After 5 failed login attempts for the same email within 5 minutes, a
6th attempt (even with the correct password) raises a rate-limited
error. The window is rolling and clears as old attempts age out.

