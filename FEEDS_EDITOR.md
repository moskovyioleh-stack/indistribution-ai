# Tilda Feeds editor

Use existing editor_action(action="tilda_call", payload=...). No new server or credentials.
Approved projects: 12112685 (corporate), 23291206 (powders), 14178377 (MOLOT), 30763609 (HEINEDA).

## Reading

- feeds_get_feeds: arguments={projectid}.
- feeds_get_feed / feeds_get_posts / feeds_get_parts: arguments={projectid,feeduid}; post listing accepts pagination.
- feeds_get_post: arguments={projectid,postuid}. Returns data and post_version.

## Creating a draft

feeds_add_post: arguments={projectid,feeduid,title}, confirmation="SAVE_DRAFT_ONLY".
Creation is followed by a readback. Success is draft_created_verified, published=false.
Do not repeat an uncertain creation; inspect the feed first.

## Editing a draft

feeds_update_post: arguments={projectid,postuid}, confirmation="SAVE_DRAFT_ONLY",
expected_post_version=<from latest read>, patch={...}, custom_fields_patch={...}.
The patch containers are siblings of arguments in payload.

Use patch for standard fields: title, descr, text, image, imagealt, thumb, thumbalt,
seo_title, seo_descr, fb_title, fb_descr, postalias and editorial SEO flags.
text is a JSON-encoded array of editor blocks, not rendered HTML.
Use custom_fields_patch with exact numeric fields[].lid keys from the current post.
HEINEDA uses custom ta (announcement), image and formated (HTML body) fields.
All current custom values are preserved. Supported custom types: title, in, ta,
formated, image and blockText. Unsupported types stop writes. For blockText, supply
the same editor JSON in patch.text and its custom field; never resend rendered HTML.

Published posts cannot be edited through the draft route. Existing publication
content is not modified by installation or tests. Reads and writes verify project
access through the connected Tilda account. No full-page publication is exposed.

## Publishing one agreed post

feeds_publish_post: arguments={projectid,postuid}, confirmation="PUBLISH_FEED_POST",
expected_post_version=<from latest read>. This local command checks draft state,
performs one visibility toggle and verifies publication. Already-published posts
are a verified no-op. Raw toggle, unpublish, deletion, and feed-setting mutations
are not exposed. The agent still needs the user's authorization for that post.
Publication was tested with mocks; no live post was published during setup.

## Operational notes

When Feeds is not enabled for a project, the server returns error_category=
module_not_enabled. Connect it in Tilda Site Settings / built-in services / Feeds
before using the new commands on that project. This update does not create or
connect a module automatically.

The provider treats nonempty disableamp/disablerss strings as true in some flows.
All false editorial flags are sent as empty strings and verified semantically;
never send literal "n" through an unguarded upstream write.

After an uncertain write or verification mismatch, read current state before
any further mutation. Mutations are not automatically retried.
