# Purchase Alert Domain Migration Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the occupied `jushuitan.skills.kktree.cn` host with `purchase-alert.kktree.cn` in the repository and production deployment without changing the `/purchase-alert` path or application data.

**Architecture:** Keep the existing FastAPI application routes and Traefik path prefix unchanged. Update the repository's Traefik host rule and documented public URLs, then update only `APP_BASE_URL` and the compose host rule on the server after creating a backup.

**Tech Stack:** Python, unittest, Docker Compose, Traefik, Git, Feishu OAuth

---

### Task 1: Add a domain configuration regression test

**Files:**
- Create: `tests/test_domain_config.py`

**Step 1: Write the failing test**

Add assertions that `docker-compose.yml` and `README.md` contain `purchase-alert.kktree.cn`, preserve `/purchase-alert`, and no longer contain `jushuitan.skills.kktree.cn`.

**Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_domain_config -v`

Expected: FAIL because the repository still contains the old host.

### Task 2: Update repository domain references

**Files:**
- Modify: `docker-compose.yml:13`
- Modify: `README.md:90`
- Modify: `README.md:99`

**Step 1: Implement the minimal configuration change**

Change the Traefik host to `purchase-alert.kktree.cn`. Change the documented entry URL to `https://purchase-alert.kktree.cn/purchase-alert/join` and the OAuth redirect URL to `https://purchase-alert.kktree.cn/purchase-alert/join/callback`.

**Step 2: Run the domain test**

Run: `python -m unittest tests.test_domain_config -v`

Expected: PASS.

**Step 3: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

**Step 4: Commit**

Commit the plan, test, README, and compose changes with message `Update purchase alert public domain`.

### Task 3: Synchronize GitHub safely

**Files:**
- No additional file changes.

**Step 1: Fetch both remotes and compare heads**

Confirm the organization and personal repository heads have not changed since inspection.

**Step 2: Push without force**

Push the tested commit to `kocotree/jst-purchase-alert` and `Srien11/jst-purchase-alert`. A concurrent remote update must cause the push to fail rather than overwrite it.

### Task 4: Update production configuration

**Files:**
- Modify on server: `/home/muer/apps/jst-purchase-alert/docker-compose.yml`
- Modify on server: `/home/muer/apps/jst-purchase-alert/.env`
- Modify on server: `/home/muer/apps/jst-purchase-alert/README.md`

**Step 1: Verify expected old values**

Require the current Traefik host and `APP_BASE_URL` to match the old domain before editing.

**Step 2: Create a timestamped backup**

Archive the current compose file, `.env`, README, and application source before replacement.

**Step 3: Upload the tested repository files**

Replace the repository files and change only `APP_BASE_URL` in `.env`; keep all secrets and data volumes unchanged.

**Step 4: Rebuild and verify**

Run the full test suite in the production image, rebuild with `docker compose up -d --build`, verify container health, and confirm the running Traefik rule contains only the new host.

### Task 5: Complete external configuration

**Files:**
- No repository file changes.

**Step 1: DNS**

The administrator must create an A record for `purchase-alert.kktree.cn` pointing to `121.40.167.37`.

**Step 2: Feishu OAuth**

Add `https://purchase-alert.kktree.cn/purchase-alert/join/callback` to the Feishu application's redirect URL allowlist.

**Step 3: Public verification**

After DNS propagation, verify `/purchase-alert/health`, `/purchase-alert/join`, OAuth login, settings, and report links.
