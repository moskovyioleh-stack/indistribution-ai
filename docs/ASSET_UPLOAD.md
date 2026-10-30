# Draft asset uploads

`editor_action` uses the same handler for `tilda_call/upload_image` and
`tilda_upload_assets`. Existing field contracts, project/page membership checks,
optimistic writes and publication/deletion prohibitions remain in place.
Uploads create project assets only; they do not update or publish a page.

## Calls

```json
{"action":"tilda_call","payload":{"tool":"upload_image","arguments":{"projectid":23291206,"images":[{"image_base64":"<actual file bytes encoded as Base64>","mimetype":"image/jpeg","filename":"hvof_process_visual.jpg"}]},"confirmation":"SAVE_DRAFT_ONLY"}}
```

Replace the placeholder with actual encoded bytes, not a filesystem path.
A data URI is accepted in `image_base64`. A client with attachment access must
read the attachment in its own environment and encode it; Railway cannot read
Codex paths or `sediment://` references. This MCP schema has no native attachment
transport. Do not put Base64 in logs or issue reports.

```json
{"action":"tilda_upload_assets","payload":{"project_id":23291206,"images":[{"image_base64":"<actual Base64>","mimetype":"image/svg+xml","filename":"hvof_hvaf_comparison.svg"}],"confirmation":"SAVE_DRAFT_ONLY"}}
```

Alternatively an image object can contain `image_url` instead of `image_base64`.
Only HTTPS `static.tildacdn.com` URLs without query strings, credentials or
redirects are accepted. Arbitrary public hosts are intentionally unsupported:
convert their files to Base64 on the client. This avoids server-side requests to
user-controlled/internal hosts and DNS rebinding. Returned CDN URLs are validated
but their availability must still be checked by the caller after live upload.

Supported: JPEG, PNG/APNG, GIF, WEBP, AVIF (Pillow content verification), a
conservative SVG subset, WOFF/WOFF2 (fontTools parsing). MIME must match content.
SVG scripting, CSS, links, external resources and animation are rejected.
SVG must use UTF-8 (an optional BOM/XML declaration is accepted). Processing
instructions, DTD/entities, NUL bytes and foreign-namespace elements are rejected
before upload, including stylesheet instructions otherwise discarded by XML parsers.
PDF and other documents are unsupported: no official document method has been
verified; no external file hosting is introduced.

1–8 files; default 7,500,000 decoded bytes per file, configurable via
`TILDA_UPLOAD_MAX_BYTES` (1–20,000,000). Fetch timeout 30 seconds, official MCP
operation timeout 120 seconds. Upstream restrictions may be lower. Timeout or
missing/mismatched CDN URL counts never returns success and must not be retried
automatically: an upload may already have happened.

Use `capabilities` to discover the route. Refresh the existing plugin's tool
metadata after deployment. The `editor_action(action, payload)` schema remains
compatible; no new plugin endpoint is needed. If external plugin instructions
contain a separate upload denylist, permit only `tilda_call/upload_image` (or the
alias), retaining owner authentication and SAVE_DRAFT_ONLY.

## Validation and rollout status

Run `python -m unittest discover -s tests -v` from the checkout. Tests cover shared
routing, success, input/content errors, project access, timeout, missing URLs,
no application log output and no page/publication calls. Existing regression
suite remains enabled. Tests use a mocked official upstream; this is not a real
Tilda acceptance test.

Before accepting the rollout, obtain authorized Railway access, TILDA_MCP_TOKEN
and the two requested real images. Read the current two record content schemas,
upload, check CDN availability, update only `img` with its current
`expected_old_value`, and read back the records and HTML. Preserve ALT and all
other fields. Do not publish. Deployment/log analysis and desktop/mobile draft
inspection remain unverified until actual access is available. The cause of past
"Official Tilda MCP request failed" errors cannot be inferred from unit tests;
Railway logs are required, and rate limiting is not assumed.
