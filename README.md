# INDISTRIBUTION AI - GitHub OAuth server

Protected read-only MCP server for four Tilda websites. Uses GitHubProvider OAuthProxy.

## Timeweb variables
- `GITHUB_CLIENT_ID` - from GitHub OAuth application
- `GITHUB_CLIENT_SECRET` - from GitHub OAuth application
- `OAUTH_BASE_URL` - `https://moskovyioleh-stack-indistribution-ai-b43c.twc1.net`
- `GITHUB_ALLOWED_LOGIN` - `moskovyioleh-stack`
- Existing `TILDA_PUBLIC_KEY` and `TILDA_SECRET_KEY` remain unchanged

The previous MCP_ACCESS_TOKEN is not used by this OAuth implementation. Do not delete it until after a successful connection.

## OAuth callback
GitHub OAuth application callback must equal `https://moskovyioleh-stack-indistribution-ai-b43c.twc1.net/auth/callback`.

## Safety
All three MCP tools enforce GitHub owner-only access using token claims; if claims are missing, access fails closed. The server is read-only. Protect the GitHub OAuth App credentials in Timeweb secret environment settings. GitHub OAuth scopes should be minimized. Do not commit any `.env` file. The `/healthz` endpoint is public only for health monitoring.

## Deploy
Upload changed files to the *root* of the existing private GitHub repository; auto-deploy in Timeweb. Check logs and `/healthz`; then in ChatGPT add a custom MCP server with `https://moskovyioleh-stack-indistribution-ai-b43c.twc1.net/mcp` and OAuth auth.

**Not yet tested against the user's live Timeweb deployment or ChatGPT handshake.** OAuth proxy may require persistent token state for resilient multi-replica deployments; initial deployment should use a single instance. If tool calls return Access Denied, inspect the verified GitHub token claims and configure an owner check matching documented data. Do not remove the owner gate.
