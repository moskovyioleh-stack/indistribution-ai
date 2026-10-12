# Tilda Editor - staged implementation plan

## Scope
Four allowlisted domains: indistribution.ru, heinedasaw.indistribution.ru, powders.indistribution.ru, molot.website.

## Baseline
Current production MCP offers read-only Tilda API tools: list_projects, list_pages, audit_page. Keep `main` and Railway production unchanged until an isolated end-to-end validation passes.

## Stage 1: Browser session and read-only editor inspection
- Add Playwright + Chromium only to an isolated staging deployment, using a separate Railway service or branch-specific environment.
- Verify browser login to Tilda using a user-authorized interactive session. **Never** ask for or store Tilda passwords in GitHub or this chat.
- Store session state encrypted in a dedicated secret manager or encrypted volume with restricted access, never in commits or MCP results.
- Protect any internal browser management endpoints with authentication and an explicit project/page allowlist.
- Require exact project ID and page ID; validate page membership via the existing Tilda API.
- Confirm the editor's actual UI elements before implementing selectors; prefer stable role/label locators and explicit waits.
- Begin with molot.website project 14178377 and page 296498609. Do not write in this stage.

## Stage 2: One-block draft edit in a COPY of the molot.website page
- Snapshot current page HTML / metadata and save an external backup and operation record.
- User selects the exact text block and approves the old->new text change.
- Browser driver opens a *test copy* of the page and changes only that text, then saves the draft.
- Validate the draft via screenshot / readback and report the exact changes. Never publish.
- Handle session expiry, 2FA, unexpected UI, and failure with a safe stop. No silent retries of write operations.

## Stage 3: Extend to images, blocks and all four sites
- Integrate user-authorized Google Drive asset references. Avoid making private assets publicly readable without approval.
- Add preflight checks: project allowlist, page allowlist, unique block locator, proposed diff, rollback source, confirmation.
- Add explicit publish action only in a separate, separately approved phase, never bundled into save_draft.
- Build per-site access controls, audit logging and preview screenshots.
- Test Tilda Zero Block separately; browser editing may be fragile when the UI changes.

## Infrastructure
- Current OAuth owner_only gate must also guard any editor tools.
- Use a staging Railway service; Playwright adds significant image/memory requirements and may need a persistent encrypted session store.
- Never place credentials or session cookies in GitHub, issue trackers, logs or MCP tool output.
- Production release must be reviewed and explicitly authorized; no automatic deploy from feature branches.

## Acceptance criteria for first usable version
1. Existing three MCP tools still function without changes.
2. Editor can open the selected Tilda page in staging as the authorized user.
3. One text block on a **copy** of molot.website page is updated and saved as unpublished draft.
4. A screenshot and before/after diff are returned for review.
5. Live molot.website and other three published sites remain unchanged.
