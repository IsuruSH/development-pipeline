# Run summary — `20260521-185303-71a20f`

- **Feature:** user_authentication
- **Status:** HALTED
- **Halt reason:** planner: ollama HTTP error: [Errno -2] Name or service not known

## Totals

| Metric | Value |
| --- | --- |
| Total duration | 8098 ms |
| Tokens in / out | 0 / 0 |
| Estimated cost | $0.0000 |
| Files generated | 0 (0 lines) |
| Tests generated | 0 (0 lines) |
| Acceptance coverage | 0/6 (0.0%) |
| Quality gates passed | 0/0 (0.0%) |

## Per-stage timing

| Stage | Status | Duration (ms) | Tokens in/out |
| --- | --- | --- | --- |
| intake | passed | 31 | 0/0 |
| planner | failed | 8067 | 0/0 |

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

