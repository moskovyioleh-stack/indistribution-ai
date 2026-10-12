# Tilda Editor - pilot plan

**Status:** safety scaffolding only. No editing or browser automation is deployed.

## Current architecture

The production MCP service uses GitHub OAuth and the read-only Tilda API.
Do not merge an untested browser editor into `main` or deploy automatically.

## Pilot scope

- Project: `molot.website` (`14178377`)
- Initial page: `296498609`
- Existing published site and all other projects must remain untouched.
- No Tilda credentials, cookies, session tokens, or Google Drive secrets in GitHub or ChatGPT messages.

## Implementation milestones

1. Verify a supported, consented Tilda browser login mechanism. Do not bypass CAPTCHA or 2FA.
2. Run Playwright Chromium in a separate Railway *staging* service, not the existing production MCP service.
3. Secure persistent session state in an encrypted store with restricted access; redact cookies and credentials from logs. Railway containers are ephemeral; local browser profiles are not durable.
4. Implement authenticated, site-scoped **read-only** editor inspection. Confirm Tilda's current editor UI and identify blocks safely.
5. Implement one text edit on a **duplicate test page**, save in Tilda without publishing; compare before/after editor state, record audit log, allow revert.
6. Add image replacement using approved Google Drive file references, style and layout editing after tests pass.
7. Expose MCP edit tools only after owner verification, explicit draft selection, approval and rollback testing.
8. Add a separate publication flow requiring clear per-page confirmation. Never publish as an implicit side effect of saving.

## Browser security controls

- Pin/navigation allowlist for Tilda domains; inspect redirects and block arbitrary browser requests where applicable.
- Never accept arbitrary selectors or executable scripts from MCP tool arguments.
- Strong timeout, mutex per page, logs without sensitive content, replay/retry safety.
- Production OAuth identity checks stay enabled.
- Human reviews a screenshot/diff before publication.
- Test Tilda editor behaviours, including whether a save is auto-published, before any write action.

## Next gate

Provision an isolated Railway staging service from this feature branch, and test Chromium startup, editor login and read-only inspection. If browser automation is incompatible with Tilda's editor/terms, stop and use human-assisted editing rather than unsupported API calls.
