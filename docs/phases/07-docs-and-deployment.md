# Phase 7: Documentation and deployment

Builds on Phases 1-6, all merged to `main`. This phase rewrites `README.md`
from scratch (it still described the pre-Phase-1 fully-local, ChromaDB,
JSONL-audit-log system), updates `render.yaml` for a Phase 4 addition it
was missing, and live-verifies the remaining un-checked items from the
plan's Definition of Done that could be verified without an actual public
deployment.

## What changed and why

### `README.md`: full rewrite, not an edit

The previous README was accurate for roughly the first quarter of this
project's life and stale for the rest: it claimed "fully local... no
external API calls" (Phase 1 replaced this with a 4-provider cloud
fallback chain), described ChromaDB-based schema retrieval (removed in
Phase 2b), a JSONL audit log (moved to Postgres/SQLite in Phase 2c), a
60-case eval set (68 as of Phase 5), and had no mention at all of the LLM
gateway, per-session sandboxing, the error taxonomy, observability,
Langfuse, or the Next.js frontend — i.e., five of the seven phases behind
this one. Patching it incrementally would have left seams; rewritten
section by section instead, keeping only what was still true (the
validator-as-safety-boundary framing, the guardrails list, the "using
your own database" walkthrough) and updating each to reflect what
actually changed.

New sections: **Live demo** (URLs left as placeholders — see "What this
phase could not do" below), **Provider fallback** (table sourced directly
from `config/model_groups.yaml`, not transcribed from memory),
**Observability** (the full Phase 4 SSE event vocabulary and error
taxonomy), **Frontend** (Phase 6's three-pane layout), an updated
**Evals** section with the current 68-case scorecard and bootstrap CIs,
**Memory and latency** (this phase's own live measurement, see below), a
step-by-step **Deployment** section for Render/Vercel/Neon, and a
**Local development** section covering both the cloud provider chain and
the offline `QP_LLM_PROFILE=local` path.

### `render.yaml`: added the Langfuse env vars

Phase 4 added `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`
to `.env.example` but never updated the Render blueprint to declare them
(as `sync: false` secrets, same pattern as every other credential) — a
real gap this phase closed. Without this, deploying via the blueprint
would have had no way to enable tracing short of manually adding the env
vars in Render's dashboard after the fact.

## Live verification

### Memory: fresh `docker build`, real manipulation flow, real cloud providers

Phase 2d's own measurement (142.4MiB peak RSS) predates Phase 4's Langfuse
+ OpenTelemetry dependency addition, so this phase re-measured rather than
assumed the old number still held.

```
docker build -t querypilot:phase7 .
# 607MB image (up from 581MB in Phase 2d)
docker run -d --name qp-mem-test -p 8010:8000 --env-file <.env minus QP_DATABASE_URL>
```

`QP_DATABASE_URL` was deliberately excluded from the test container's env
(local SQLite fallback instead) so this memory-only test never touched
the real production Neon database. Container came up cleanly (`alembic
upgrade head` succeeded against the local fallback, `uvicorn` started,
`/health` returned `{"healthy": true, ...}`).

Sampled `docker stats --no-stream` across a real, live `POST /chat`
("Cancel order 42") through to a real `POST /confirm`, against real
OpenRouter/Gemini/Groq/Ollama Cloud calls (no mocking):

| Point | RSS |
|---|---|
| Idle, before any request | 133.2MiB |
| 20s into `/chat` (guardrail → intent_classifier) | 148.9MiB |
| Immediately before confirming (preview shown) | 152.3MiB |
| During `/confirm` (commit + audit log) | 152.6MiB |
| After completion | 152.3MiB |

**Peak: 152.6MiB — about 30% of Render free tier's 512MB limit**, with
real writes actually committed inside the container (verified via the
`done` event's `final_response`: `"Done - 1 row(s) affected in orders."`,
and a working Langfuse trace URL in the same payload, confirming tracing
adds negligible memory even when active end to end). Container removed
after the test (`docker rm -f qp-mem-test`).

### Provider fallback with a revoked key

The plan's Definition of Done asks: "With the Ollama Cloud key revoked,
requests still succeed through OpenRouter, then Gemini, then Groq." Tested
directly against `app.core.llm.chat()` (not the full HTTP stack, to
isolate the gateway's own fallback logic) by overwriting
`provider_credentials.ollama_api_key` with an invalid value in-process and
registering an `llm_attempt` listener to capture every attempt:

```
provider=ollama_cloud    success=False error=Unauthorized (status code: 401)
provider=openrouter      success=False error=Error code: 429 - Rate limit exceeded: free-models-per-day
provider=gemini          success=True  error=
RESULT: pong
```

Ollama Cloud correctly failed with 401 (the simulated revoked key) and the
chain advanced; OpenRouter also happened to be genuinely rate-limited at
the moment of this test (real, unrelated free-tier exhaustion, not
something this test induced) and the chain advanced past that too,
succeeding on Gemini. This demonstrates the actual behavior the
Definition of Done asks for — advancing past an unavailable deployment to
a working one — under real, unstaged conditions, including a second
real-world failure the test didn't even plan for.

### Everything else in the Definition of Done

Already live-verified in earlier phases, re-confirmed rather than
re-tested from scratch here:

- **Session isolation** ("two browsers cannot see each other's writes"):
  `tests/test_node_session_isolation.py` plus Phase 6's live browser
  verification (a confirmed write changed one session's row count,
  invisible until that session's own reset).
- **"Try to break it" presets, UI shows which layer blocked each one**:
  Phase 6's live run showed exactly this — guardrail passing destructive
  SQL through to the validator, the validator blocking `DROP`, a
  self-correction attempt then blocked by the row-impact ceiling, with
  `not_reached` styling on everything downstream.
- **No raw exception/traceback reachable from any endpoint**: Phase 3's
  error taxonomy plus `tests/test_main_errors.py`'s explicit negative
  assertions (`"postgres://user:pass@host/db" not in body`, etc.).
- **`/health`, `/health/providers`, `/health/budget` report truthfully**:
  live-verified in Phase 6's browser session (provider status dots,
  budget meter) and again this phase (the revoked-key test above).
- **pytest passes**: 279 passed (3 pre-existing, documented, live-Gemini-
  quota-dependent tests in `test_schema_embeddings.py` excluded, per their
  own module docstring). **Playwright**: 7/7 passed locally. Neither
  workflow (`eval.yml`, `frontend-tests.yml`) has run in GitHub Actions
  yet — see below.

## What this phase could not do, and why

**Actual deployment to Render and Vercel, and the "Live demo" URLs in the
README, are left undone.** Both require an account on a third-party
hosting platform connected to this repository — creating accounts on the
user's behalf is outside what this assistant does autonomously, and
completing an OAuth/login flow for a real account isn't something this
session can finish without the account holder present. `render.yaml` and
`web/`'s Vercel-ready configuration (root directory `web/`, one env var)
are both complete and ready to go the moment those accounts exist and are
connected — the README's Deployment section is a literal step-by-step for
that hand-off. Checked for existing CLI credentials first rather than
assuming: neither `vercel` nor a Render CLI had any prior login state in
this environment.

**The two GitHub Actions workflows (`eval.yml`, `frontend-tests.yml`)
haven't run in CI yet**, for the same class of reason: this session
worked entirely in a local working tree with no verified push access to a
remote confirmed by the user. Both workflows are complete, and both were
effectively dry-run locally already (the eval harness's full 68-case run
in Phase 5, the Playwright suite in Phase 6) — running them in Actions
for the first time is a push/PR away, not a code change.

## What to watch going forward

- The README's Live demo table has placeholder rows — fill them in
  immediately after following its own Deployment section.
- Once Render is live, re-run this phase's memory measurement against the
  *actual* Render container (this phase's number is from a local `docker
  run`, which should match closely but wasn't measured on Render's own
  infrastructure).
- `config/model_groups.yaml`'s free-tier models (`cohere/north-mini-
  code:free`, `nvidia/nemotron-3-super-120b-a12b:free`, and others) are
  exactly that — free tiers that providers can delist or rate-limit
  without notice, as already observed live multiple times across Phases
  3-7. The fallback chain absorbs this by design, but a demo audience
  hitting `ALL_PROVIDERS_FAILED` on a bad-luck day is a real, accepted
  risk of building on free tiers, not a bug.
