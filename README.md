# development-pipeline

An AI-native, spec-driven development pipeline. Feed it a structured
feature spec and it plans, implements, tests, and validates the feature
through a 7-stage LangGraph flow — with deterministic quality gates, two
human approval checkpoints, sandboxed AI file writes, and a fully
auditable JSONL trail per run.

Built for the Newton Russell **AI-native, Spec-driven Development
Pipeline** technical assessment.

---

## The 7 stages

| # | Stage | Module | What happens |
| --- | --- | --- | --- |
| 1 | Intake | [`intake.py`](pipeline/intake.py) | Parse YAML/JSON/MD spec into a typed `FeatureSpec`. |
| 2 | Planner | [`planner.py`](pipeline/planner.py) | LLM returns a structured `Plan` (tasks, design, risks). |
| – | **Approval #1** | [`approval.py`](pipeline/approval.py) | Human reviews the plan before any code is written. |
| 3 | Implementer | [`implementer.py`](pipeline/implementer.py) | LLM generates source files under `src/` (sandboxed). |
| 4 | Test Generator | [`test_generator.py`](pipeline/test_generator.py) | LLM generates `pytest` tests under `tests/`, tagged `Covers: AC-<n>`. |
| 5 | Quality Gates | [`quality_gates.py`](pipeline/quality_gates.py) | Runs **ruff → mypy → pytest → bandit**; halts on failure. |
| – | **Approval #2** | [`approval.py`](pipeline/approval.py) | Human approves "deployment" after gates are green. |
| 7 | Finalize | [`auditor.py`](pipeline/auditor.py), [`metrics.py`](pipeline/metrics.py) | Write `summary.md`, print the Rich report. |

Orchestration is a LangGraph `StateGraph` — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the diagram and the
design decisions behind it.

---

## Quick start (no API key needed)

The default setup spins up Ollama in Docker and runs the whole pipeline
locally against `qwen2.5-coder:1.5b`. No paid API key required.

```bash
git clone <repo> && cd development-pipeline

# 1. Start Ollama (long-running, leave it; only needed first time / after reboot).
docker compose up -d ollama

# 2. Run the pipeline. First run pulls the model (~986 MB, ~1-2 min);
#    subsequent runs reuse the cached model and start in seconds.
docker compose run --rm --build pipeline specs/user_authentication.yaml --verbose
```

Why two commands instead of one: Compose v5.x has a known bug where
`condition: service_completed_successfully` / `condition: service_healthy`
dependencies hang forever. The pipeline container does its own model-pull
check on startup ([`pipeline/ensure_model.py`](pipeline/ensure_model.py))
so we don't need that dependency at all. The trade-off is that you start
Ollama with one explicit command, then the pipeline as many times as
you like.

Stop everything (Ollama stays running across runs by design):

```bash
docker compose down       # stop Ollama, keep the cached model
docker compose down -v    # also wipe the model cache
```

### Other common commands

```bash
# Non-interactive (skip approvals — what CI uses):
docker compose run --rm pipeline specs/user_authentication.yaml --auto-approve

# Same, but pick a stronger model (better odds of passing the gates):
$env:LLM_MODEL = "qwen2.5-coder:7b"; docker compose run --rm pipeline `
    specs/user_authentication.yaml --auto-approve --verbose

# Native (no Docker) against an already-running Ollama:
docker compose up -d ollama
python run_pipeline.py specs/user_authentication.yaml --verbose
```

### Switching providers (one env var each)

```bash
# OpenAI
LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini LLM_API_KEY=sk-... \
    docker compose run --rm pipeline specs/user_authentication.yaml --verbose

# Anthropic
LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-4-5 LLM_API_KEY=sk-ant-... \
    docker compose run --rm pipeline specs/user_authentication.yaml --verbose
```

Full configuration reference: [`.env.example`](.env.example).

---

## What gets produced

Every successful run leaves these on disk (bind-mounted from the
container):

```
src/                            # AI-generated source code (sandbox-enforced)
tests/                          # AI-generated pytest tests
audit_logs/<run_id>/
├── audit.jsonl                 # append-only event stream (source of truth)
├── spec.snapshot.yaml          # frozen input spec
├── plan.json                   # parsed planner output
├── changes_summary.md          # implementer description of files written
├── summary.md                  # human-readable run report + metrics
├── graph.mmd                   # mermaid of the LangGraph that actually ran
├── prompts/<stage>.<ver>.txt   # exact prompts sent to the model
├── responses/<stage>.raw.json  # exact responses received
└── gates/{ruff,mypy,pytest,bandit}.log
```

`audit.jsonl` alone is enough to reconstruct any run.

---

## Writing your own spec

Specs are YAML, JSON, or Markdown — see
[`specs/user_authentication.yaml`](specs/user_authentication.yaml) for a
full example. The schema is enforced by
[`FeatureSpec`](pipeline/state.py):

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | Used in paths and logs. |
| `objective` | string | What the feature exists to do. |
| `user_story` | string | "As a … I want … so that …" |
| `business_rules` | string list | Invariants the implementer must respect. |
| `acceptance_criteria` | list of `{id, description}` | `id` must match `^AC-\d+$`. Tests embed `Covers: AC-<n>` for the coverage metric. |
| `non_functional_requirements` | string list | e.g. "no third-party deps". |
| `out_of_scope` | string list | Anything the implementer must *not* build. |

---

## Governance highlights

- **Sandboxed writes** — [`sandbox.guard`](pipeline/sandbox.py) rejects
  absolute paths (incl. Windows drive letters), `..` traversal,
  home-relative paths, and anything outside the allowed root. Unit-tested
  in [`tests/pipeline_sandbox_test.py`](tests/pipeline_sandbox_test.py).
- **Hard-coded write roots** — Implementer can only write under `src/`,
  Test Generator only under `tests/`.
- **Halt-on-failure gates** — `ruff`, `mypy`, `pytest`, `bandit`; non-zero
  exit halts before any "deploy".
- **Two approval checkpoints** — decisions (actor, timestamp, decision)
  recorded to the audit log.
- **Prompt version stamping** — every template hash is captured in every
  audit event, so a run is exactly reproducible.
- **CI security scan** — `bandit` (SAST) + `pip-audit` (dependency CVEs)
  run on every push / PR; see [CI](#ci) below.

---

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push
to `main` and every pull request. It has two parallel jobs:

| Job | Steps | Fails the build on |
| --- | --- | --- |
| `lint-type-test` | `ruff` → `mypy` → `pytest tests/pipeline_*.py` | any lint, type, or test failure in the pipeline source |
| `security` | `bandit -r pipeline run_pipeline.py -ll` → `pip-audit --skip-editable` | any Medium+ SAST finding, or any known CVE in the resolved dependency tree |

The `security` job is the CI-level mirror of the in-pipeline `bandit` quality
gate (which scans the *generated* code) — together they cover both the
pipeline's own source and every run's output.

End-to-end pipeline runs aren't wired into CI yet because the LLM call
would either need an API key secret (OpenAI / Anthropic) or a ~1 GB
Ollama model download on every run. The artefacts an end-to-end run
produces locally (`audit_logs/<run_id>/`) are committed as evidence.

---

## More

- Design decisions, trade-offs, limitations, future work →
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Full env-var reference → [`.env.example`](.env.example)
- CLI flag reference → `python run_pipeline.py --help`

---

## License

MIT (assessment exercise).
