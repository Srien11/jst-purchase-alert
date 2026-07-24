# Personal Schedules and Manual Fetch Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use coding-agent to implement this plan task-by-task.

**Goal:** Let each bound purchaser configure an independent notification frequency/time and manually request an immediate personal in-transit report.

**Architecture:** Persist schedule preferences on each buyer row with safe migration defaults matching the current daily 09:00 behavior. Replace the once-daily scheduler with a minute-level dispatcher that fetches upstream data only when at least one buyer is due, sends due-node alerts per buyer, and records the last schedule slot. Add authenticated management-page forms for schedule updates and an immediate fetch action that sends all current pending rows without changing automatic reminder deduplication.

**Tech Stack:** Python 3.12, FastAPI, APScheduler, SQLite, httpx, unittest, Feishu interactive cards.

---

### Task 1: Persist per-buyer schedules

**Files:**
- Modify: `app/storage.py`
- Test: `tests/test_storage.py`

**Steps:**
1. Add failing tests for migration defaults, preference updates, and due-slot recording.
2. Add buyer columns for frequency, hour, minute, weekday, and last schedule slot.
3. Add storage helpers to update and read schedule state.
4. Run storage tests and confirm they pass.

### Task 2: Add schedule selection and dispatch logic

**Files:**
- Modify: `app/service.py`
- Test: `tests/test_logic.py`

**Steps:**
1. Add failing tests for daily, weekday, weekly, and already-run slot behavior.
2. Implement pure due-schedule predicates.
3. Implement a dispatcher that only fetches upstream data when buyers are due.
4. Keep automatic reminder node deduplication unchanged.
5. Run logic tests and confirm they pass.

### Task 3: Add management-page controls and immediate fetch

**Files:**
- Modify: `app/main.py`
- Modify: `app/service.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_logic.py`

**Steps:**
1. Add schedule controls to the authenticated buyer management page.
2. Add a schedule update POST endpoint with strict validation.
3. Add an immediate-fetch POST endpoint scoped to the buyer token.
4. Make immediate fetch send all current pending rows and leave automatic sent records untouched.
5. Run focused tests and full suite.

### Task 4: Deploy and verify

**Files:**
- Modify production source under `/home/muer/apps/jst-purchase-alert`

**Steps:**
1. Upload changed files to the server.
2. Build the production image.
3. Run the full test suite inside the image.
4. Recreate the service without triggering a report.
5. Verify `/health`, scheduler startup, database migration, and retained buyer data.
