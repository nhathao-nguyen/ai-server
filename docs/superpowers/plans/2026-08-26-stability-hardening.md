# TTS Server Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make the existing FastAPI/Ollama/Chatterbox/VieNeu gateway safe to operate under real requests, cancellation, restarts, invalid model artifacts, and browser clients.

**Architecture:** Keep the current provider/scheduler boundaries. Add shared manifest verification, cancellation-aware job ownership, canonical request normalization, and cached non-invasive readiness reporting; deep worker probes remain explicit. Preserve existing API paths and WAV contracts.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, asyncio, SQLite, pytest, Ollama, local TTS workers.

**Spec:** Prior approved audit findings in the conversation.

## Global Constraints

- Do not download models during server startup or health checks.
- Keep Ollama loopback-only and preserve API-key scope enforcement.
- Preserve `/v1/models` legacy fields and WAV response format.
- Refund each credit reservation exactly once on failure or cancellation.
- Verify real HTTP and provider inference before claiming completion.

### Task 1: Add regression tests for request validation and manifests

**Files:**
- Create: `tests/test_request_contracts.py`
- Create: `tests/test_model_manifest.py`

- [x] **Step 1: Write failing tests** for whitespace-only speech input, canonical language normalization, invalid manifest file/hash, and valid manifest acceptance.
- [x] **Step 2: Run `\.venv\Scripts\python.exe -m pytest tests/test_request_contracts.py tests/test_model_manifest.py -q` and confirm the new tests fail for the current behavior.
- [x] **Step 3: Implement only the minimum production changes required by the tests.
- [x] **Step 4: Re-run the focused tests and confirm they pass.

### Task 2: Harden manifest creation and verification

**Files:**
- Modify: `app/core/model_manifest.py`
- Modify: `app/engines/chatterbox.py`
- Modify: `app/engines/vieneu.py`
- Modify: `scripts/bootstrap_models.py`

- [x] **Step 1:** Add a shared validator that checks schema, expected model/provider, file existence, path containment under cache root, declared byte size, and SHA-256.
- [x] **Step 2:** Make manifest writes atomic and pass the Hugging Face cache root from bootstrap.
- [x] **Step 3:** Use the shared validator for both providers, including VieNeu.
- [x] **Step 4:** Make bootstrap return non-zero if any model is blocked or failed.
- [x] **Step 5:** Run manifest tests, compileall, and a cache-only bootstrap/status check.

### Task 3: Fix canonical TTS input and browser response headers

**Files:**
- Modify: `app/api/schemas.py`
- Modify: `app/api/routes_audio.py`
- Modify: `app/main.py`
- Create: `tests/test_audio_contract.py`

- [x] **Step 1:** Add failing tests for whitespace input rejection, `en-US`/`vi-VN` canonicalization, and exposed `X-TTS-*` headers.
- [x] **Step 2:** Run the focused tests and confirm RED.
- [x] **Step 3:** Strip and validate text, canonicalize language before constructing `TtsRequest`, and configure `expose_headers` for TTS metadata.
- [x] **Step 4:** Run the focused tests and confirm GREEN.

### Task 4: Make scheduler and providers cancellation-safe

**Files:**
- Modify: `app/scheduler/manager.py`
- Modify: `app/api/routes_chat.py`
- Modify: `app/api/routes_audio.py`
- Modify: `app/engines/chatterbox.py`
- Modify: `app/engines/vieneu.py`
- Create: `tests/test_cancellation_accounting.py`

- [x] **Step 1:** Add failing tests showing cancelled submit cancels the execution task and refunds the reservation once.
- [x] **Step 2:** Run the focused tests and confirm RED.
- [x] **Step 3:** Cancel execution from `JobManager.submit`, catch `CancelledError` explicitly in routes, and stop/reset worker processes on cancellation with shielded cleanup.
- [x] **Step 4:** Ensure repeated finalization is idempotent and does not double-refund.
- [x] **Step 5:** Run focused tests plus the full test suite.

### Task 5: Separate readiness from deep model probes

**Files:**
- Modify: `app/core/model_registry.py`
- Modify: `app/main.py`
- Modify: `app/api/routes_admin.py`
- Create: `tests/test_registry_readiness.py`

- [x] **Step 1:** Add failing tests proving health/model-list calls do not start a TTS worker and provider status is cached for a bounded TTL.
- [x] **Step 2:** Run the focused tests and confirm RED.
- [x] **Step 3:** Add non-invasive cached refresh for health and an explicit deep refresh for admin diagnostics.
- [x] **Step 4:** Add per-provider timeout/error isolation.
- [x] **Step 5:** Run the full suite and a real `/health`, `/v1/models`, and admin smoke.

### Task 6: Reproducible environment and real-system verification

**Files:**
- Modify: `pyproject.toml`
- Create: `uv.lock`
- Modify: `.env.example`
- Modify: `scripts/test_server_e2e.py`

- [x] **Step 1:** Pin compatible model/runtime versions and generate the lockfile in a clean environment.
- [x] **Step 2:** Run `uv pip check --python .venv\\Scripts\\python.exe`, imports, compileall, and pytest.
- [x] **Step 3:** Start the real server with the project virtualenv, create temporary admin/product keys, and run chat, translation, speech, clone, cancellation, restart, and CORS checks.
- [x] **Step 4:** Record HTTP statuses, model/provider headers, WAV validity, audio duration, processing time, reservation/usage state, and artifact paths.
- [x] **Step 5:** Report any unavailable provider explicitly; do not claim all functions pass unless real Chatterbox and VieNeu inference both pass.
