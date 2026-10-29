# Yandex tools in the existing INDISTRIBUTION AI plugin

The existing OAuth-protected FastMCP app registers nine read-only tools from
`yandex_tools.py`. They call the existing Yandex MCP endpoint. No new server,
plugin, OAuth provider, or Python dependency is required. Tilda and Avito tools
and their existing owner authorization remain unchanged.

## Activation after deployment approval

1. In Railway project `precious-tenderness`, production service `indistribution-ai`,
   use `YANDEX_MCP_ACCESS_KEY` or the existing `MCP_ACCESS_KEY`. Copy the value privately from `MCP_ACCESS_KEY` in
   the existing `indistribution-yandex-webmaster` / `yandex-webmaster-mcp` service.
   Do not send it to chat, commit it, or print it. Leave existing variables intact.
   Adding a Railway variable can trigger a deployment: perform this step only
   after deployment approval, together with the reviewed code rollout.
2. Merge the reviewed branch into `main`, which is the existing Railway source.
   Confirm the deployment succeeds. Do not change the editor service or Yandex Function.
3. Refresh tools for the existing `2_INDISTRIBUTION AI` connection in ChatGPT.
   Reauthorize if requested; existing OAuth storage may be lost on container replacement.
4. Verify authenticated `tools/list` exposes all nine new tools alongside existing tools.
5. Call `metrika_counters`, then traffic/sources/pages/goals for IDs 109258712,
   111834069, 108746685. For the audit use explicit dates 2026-09-29 to 2026-10-28.
6. Call `webmaster_sites` and `webmaster_site_summary` for an approved domain.
   Verify an unauthorized caller cannot execute the new tools.

`metrika_goals` returns goal definitions only; goal achievements and conversion
rates require a separate upstream implementation. Large upstream reports can be
truncated by the existing Yandex Function; the bridge rejects invalid JSON rather
than silently treating it as a valid report. No automatic retries or startup
diagnostic requests run. The upstream key is sent only to a fixed HTTPS URL;
redirect following is disabled.

## Local validation

`python -m unittest test_yandex_tools -v`

These isolated contract tests replace the HTTP client and do not contact Yandex,
Railway, or ChatGPT. They check forwarding, fixed destination, owner auth wiring,
read-only annotations, fail-closed configuration and sanitized errors. Real MCP
discovery, OAuth and live report delivery still require post-deployment acceptance.

## Rollback

Revert the bridge commit(s) on `main` using the existing deployment process.
Existing Yandex Function and counters remain unchanged. A new deployment may
require reconnecting the existing ChatGPT plugin.
