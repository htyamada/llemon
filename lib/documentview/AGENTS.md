# Repository Guidelines

## Project Structure & Module Organization

`documentview` is a reusable Django app for browsing, lightly previewing,
downloading, and staging documents from a configured collection directory.
`paths.py` owns the secure-open path-resolution contract; `documents.py`
owns logical-document grouping, format-variant selection, and natural
sorting; `covers.py` extracts and caches cover images; `previews.py`
generates bounded format-appropriate previews and serves the archive
subresources they reference; `archives.py` is the bounded/streamed ZIP
reader shared by EPUB/CBZ code; `images.py` is the shared bounded-image
decode helper; `epub.py` is shared EPUB container/OPF parsing;
`pdfrender.py` wraps the `pdftoppm` subprocess; `subresources.py` signs
archive-internal preview identifiers; `active.py` owns the exports
directory's locked, directory-is-authority symlink staging;
`appconfig.py` loads the filesystem
paths from `etc/documentview.conf`; `config.py` owns settings, defaults,
and lazy filesystem validation; `views.py` / `urls.py` wire it all up.
Templates live under `templates/documentview/`, static assets under
`static/documentview/`, and tests under `tests/`. This file is the
permanent design and behavior record for the app; the original planning
document that shaped it (`upgrades/books.md`) has been superseded by this
file and the code, and was removed once the app was fully implemented.

The active consumer is `../../llime` (mounted at `/documents/`, nav label
"Document View"). `../qat/knip` has no document collection and is not
expected to mount this app; nothing here assumes it will.

## Configuration

`root` and `active_dir` are filesystem paths and live in the repo's own
`etc/documentview.conf` -- a visible, versioned TOML file following the
same convention as `etc/imhandler.conf`/`etc/llemon_djview.conf` (sections
keyed `[<variant>.documentview.core]`, parsed via the shared
`hty7.config.AppConfig`). Edit that file directly to change where the
collection root or the exports directory live; no code or Django settings
edit is needed for the common case:

```toml
[hty7.documentview.core]
root = "/srv/cloud/store/books-and-text/"      # required, no default
active_dir = "~/var/documentview/reader"        # required, no default
```

`apps.py`'s `ready()` loads this file into the `appconfig` module via
`appconfig.init_variant(getattr(settings, 'DOCUMENT_VIEWER_VARIANT', 'hty7'))`
-- the host selects a variant/section the same way `IMHANDLER_VARIANT` and
`MEDIAVIEW_LABEL` do for their own apps. `~` expands under the account
running `manage.py` / the WSGI process, which may differ from the
developer's own home directory -- both roots run through
`Path(...).expanduser().resolve()`.

A host's Django settings, when set, still take precedence over the conf
file -- `DOCUMENT_VIEWER_ROOT` and `DOCUMENT_VIEWER_ACTIVE_DIR` both follow
"host setting wins; otherwise the conf-file value" (`config.root()`/
`config.active_dir()`). This is what lets tests point each one at a fresh
temp directory via `override_settings` without touching the real conf
file; a host project isn't expected to set these in `settings.py` under
normal operation.

Optional, with defaults:

| Setting | Default |
|---|---|
| `DOCUMENT_VIEWER_CACHE_DIR` | `~/var/documentview/cache` |
| `DOCUMENT_VIEWER_AUTHORIZE` | `lambda request, action: request.user.is_authenticated` |
| `DOCUMENT_VIEWER_STYLESHEET_URL` | Django static URL for `documentview/documentview.css` |
| `DOCUMENT_VIEWER_COVER_SIZES` | `{"thumb": (150, 220), "detail": (300, 440)}` |

`DOCUMENT_VIEWER_AUTHORIZE(request, action)` is called with `action` one of
`"browse"` (index/browse/view/preview/cover), `"download"`, or `"mutate"`
(active_add/active_remove/cover_refresh). Override it per host for a
different policy without touching this app.

Numeric safety limits (`config.limit('DOCUMENT_VIEWER_...')`, all
overridable settings with the defaults below):

| Setting | Default | Purpose |
|---|---|---|
| `DOCUMENT_VIEWER_MAX_ARCHIVE_ENTRIES` | 2000 | Max ZIP central-directory entries |
| `DOCUMENT_VIEWER_MAX_ENTRY_BYTES` | 64 MiB | Max decompressed bytes per archive member (streamed) |
| `DOCUMENT_VIEWER_MAX_TOTAL_BYTES` | 256 MiB | Max cumulative decompressed bytes per cover/preview operation |
| `DOCUMENT_VIEWER_MAX_COMPRESSION_RATIO` | 100 | Declared-size pre-check before opening a ZIP entry |
| `DOCUMENT_VIEWER_MAX_XML_BYTES` | 4 MiB | EPUB container/OPF/NCX/nav-doc parsing |
| `DOCUMENT_VIEWER_MAX_IMAGE_PIXELS` | 40,000,000 | Decoded-pixel cap, checked locally per image (never assigned to `Image.MAX_IMAGE_PIXELS`) |
| `DOCUMENT_VIEWER_COVER_SIZES` | see above | The only named boxes a cover request may ask for |
| `DOCUMENT_VIEWER_MAX_PREVIEW_SECTIONS` | 3 | EPUB preview sections (TOC + preface + opening chapters) |
| `DOCUMENT_VIEWER_MAX_PREVIEW_BYTES` | 200 KiB | Markdown/text/EPUB-section preview excerpt |
| `DOCUMENT_VIEWER_MAX_CBZ_PREVIEW_IMAGES` | 10 | CBZ preview pages |
| `DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES` | 10 | PDF preview pages |
| `DOCUMENT_VIEWER_PDF_RENDER_DPI` | 96 | `pdftoppm` render DPI |
| `DOCUMENT_VIEWER_MAX_PDF_RENDER_DIMENSION` | 2048 | Max width/height `pdftoppm` may produce (via `-scale-to`) |
| `DOCUMENT_VIEWER_SUBPROCESS_TIMEOUT` | 10s | `pdftoppm` hard timeout |
| `DOCUMENT_VIEWER_MAX_SYMLINK_HOPS` | 2 | `resolve_document()`'s final-component symlink resolution only |

There is **no in-process CPU wall-clock deadline** on ZIP reading, XML
parsing, sanitization, or image decoding beyond the byte/entry/pixel caps
above -- only the external `pdftoppm` call has a hard timeout. The size
caps are the accepted bound on in-process work; the deployment's own
request timeout is the outer backstop. Optimizing for very large or
adversarial documents beyond these limits is out of scope for v1.

**Config validation is lazy, not at startup.** `AppConfig.ready()` only
checks that each setting is present and of the right type (a pure
string/`PurePath` comparison that `DOCUMENT_VIEWER_ACTIVE_DIR` isn't inside
`DOCUMENT_VIEWER_ROOT`, and that `DOCUMENT_VIEWER_CACHE_DIR` isn't inside
`DOCUMENT_VIEWER_ACTIVE_DIR` -- the exports-directory lock file lives under
the cache dir, so this containment is what keeps it from ever landing
inside the exports directory itself) -- no filesystem access, so
`manage.py check`, migrations, and unrelated management commands never
fail just because a deployment mount happens to be absent. The equivalent
live check (that the configured root exists, and that the active dir /
cache dir both resolve outside the root, and the cache dir outside the
active dir) runs once per process, the first time a view or management
command actually touches the filesystem -- including the Exports page and
`exports/prune/`, which validate live config just like `browse()`/`view()`
do via `paths.resolve_*()`.

## Dependencies

Not in a repo-wide manifest today (none exists at the top level of this
checkout) -- a fresh checkout, or `../qat/knip` opting in later, must
install these itself:

- **Pillow** -- required (not optional): generic covers, fit-and-pad, and
  the bounded-image-decode helper all use it.
- **bleach** -- EPUB/Markdown preview HTML sanitization.
- **markdown** -- Markdown-to-HTML rendering (core extensions only).
- **defusedxml** -- all EPUB `container.xml`/OPF/NCX/nav-doc parsing, since
  EPUB content is untrusted ZIP input.
- **`pdftoppm`** (poppler-utils, external binary on `$PATH`) -- optional at
  runtime: if absent, PDF cover extraction and preview both degrade to the
  generic-cover / "no preview available" fallback, and download still
  works. `mutool`/PyMuPDF/`pypdf`/`pdf2image`/`ebooklib` are deliberately
  not used; `pdftoppm` is the one PDF rendering code path.
- **`hty7.config`** -- `appconfig.py`'s `etc/documentview.conf` loader, the
  same shared TOML/variant-selection helper `imhandler.appconfig` and
  `llemon_djview` use for `etc/imhandler.conf`/`etc/llemon_djview.conf`.

## Supported Formats & Logical-Document Grouping

PDF, EPUB, CBZ, Markdown (`.md`), and plain text (`.txt`), matched
case-insensitively. Files in one directory sharing a basename after
stripping exactly one supported suffix are format variants of one logical
document (`documents.scan_directory()`, `documents.strip_supported_suffix()`):

- Only the single rightmost supported suffix is ever stripped, so
  `Book.tar.pdf` groups under basename `Book.tar` (`.tar` is never itself
  stripped).
- The grouping key is compared byte-for-byte, case-sensitively --
  `Book.epub` and `book.epub` do **not** group.
- More than one file normalizing to the same format (e.g. `Book.pdf` and
  `Book.PDF`) is a collection error naming both files, shown to the user
  and logged. Directory entries are sorted by name before grouping, so
  which of the two is kept as that format's variant is deterministic
  rather than dependent on `os.scandir`'s arbitrary order -- the point
  being that the conflict is always surfaced, never quietly resolved
  differently from one request to the next.
- `documents.FORMAT_PREFERENCE = ('epub', 'pdf', 'cbz', 'md', 'txt')` is
  one hardcoded module constant (not a Django setting) used by both
  `representative_variant()` (which logical-document `view`/`cover` URL
  resolves to) and `covers.cover_for()` (cover-source preference) --
  deliberately the same ordering, not two independently-tunable ones.
  Explicit downloads, activation, and directly-requested previews always
  identify the exact selected variant, never the representative.
- A document reached through an in-hierarchy symlink (e.g. the real
  collection's curated `humble-bundle/selected/` directory) regroups with
  the *other entries actually listed in that same directory* -- the
  symlink's own directory -- never the resolved target's real directory.
  `views._resolve_logical()` rescans against the requested path's own
  directory listing and matches variants by that requested path, not the
  canonicalized one, so `selected/Alias.epub -> real/Canonical.epub` sitting
  next to `selected/Alias.pdf` is one "Alias" document with both variants
  on both the browse and detail pages, not "Canonical" with only the EPUB.

## Browsing, Covers, Previews, Download

Cover and title views present the same logical documents; switching views
never changes document identity, grouping, or active state.

Covers are extracted per format (EPUB manifest `cover-image` /
EPUB2 `<meta name="cover">` / `<guide>` reference; CBZ first image in
natural order; PDF first page via `pdftoppm`; Markdown/text get a generic
generated cover), cached under `DOCUMENT_VIEWER_CACHE_DIR/covers/` keyed on
the selected source's path/mtime/size *and* every candidate variant
considered (so adding, removing, or editing an alternate format busts the
cache too), and **fit-and-pad**ded: scaled preserving aspect ratio to the
largest size fitting the requested named box from `DOCUMENT_VIEWER_COVER_SIZES`,
then padded to fill it exactly -- never cropped, never stretched, and
never rendered at a client-requested arbitrary dimension. Any extraction,
decoding, or rendering failure falls back to the generic cover rather than
breaking browsing.

PDF preview pages are rendered **once** per (document, mtime/size, render
settings) and cached under `DOCUMENT_VIEWER_CACHE_DIR/pdfpages/`; the page
count and every page image share that one render, so a detail page costs a
single `pdftoppm` run rather than one per page plus another just to count
them, and a reload costs none. A failed or unavailable render caches
nothing, so a later request retries.

Previews are intentionally bounded and format-appropriate (spec 1.3): EPUB
gets its TOC plus a preface/introduction and the first chapter-ish spine
section(s) (semantic nav landmarks preferred, falling back to a
conservative spine heuristic on malformed/missing navigation); PDF and CBZ
get a first few rendered/decoded pages; Markdown and text get a bounded,
sanitized/escaped excerpt. None of this is a full-document reader --
there's no pagination, bookmarks, or continuous scrolling by design.

Archive-internal preview resources (an EPUB's embedded image, a specific
PDF/CBZ page) are served through `documents/preview/<path>/`, addressed by
a `django.core.signing`-signed id (`subresources.py`) carrying an index
plus an `mtime_ns:size` fingerprint of the parent document -- tamper-evident
(bad signature -> 400) and freshness-checked (fingerprint mismatch -> 409)
rather than merely obscure; the index is also independently bounds-checked
against a freshly re-parsed manifest/page list before use.

EPUB and Markdown preview HTML share one `bleach.clean()` tag/attribute
allowlist (script/on\*/iframe/object/embed/form/svg/style/`data:` always
stripped) but differ on remote-resource handling: EPUB `<img src>` is
rewritten to an internal signed subresource URL only if it names a real
manifest item (dropped otherwise) and remote image/audio/video/font/CSS
loads are always stripped, since packaged archive content has no
legitimate need to phone home during a bounded preview. A manifest item's
own `href` is always resolved against the OPF's directory (that's where
the EPUB spec puts it), but a reference found *inside* a document --
`<img src>` in a chapter, `<a href>` in a nav doc, `<content src>` in an
NCX -- is resolved against *that document's own directory* instead, since
that's what it's relative to; a layout like `Text/chapter.xhtml`
referencing `../Images/cover.jpg` only resolves correctly against
`Text/`, not the OPF's own directory. `<a href>` is kept
as inert `rel="noopener noreferrer nofollow"` text-with-link for
`http(s)` targets. Markdown, being a locally-authored standalone document
rather than archive-packaged content, keeps `http(s)` `img src`/`a href`
as-is (spec 1.3, 4.4) with the same `rel` hardening and tag/attribute
stripping.

Download preserves exact original bytes, filename, and attachment
disposition; each format variant has its own download URL and always
serves the explicitly selected file, never a substituted preferred format.

## Exports Directory

`active.py` is directory-is-authority: whatever symlink physically exists
in `DOCUMENT_VIEWER_ACTIVE_DIR` (still the technical/setting name; the
user-facing term is "exports directory") *is* an export link, full stop.
There is no separate manifest recording ownership or intent, and nothing
is ever "foreign" the way an older manifest-backed design would treat an
unregistered symlink -- presence as a symlink in that one configured
directory is the only authorization `remove_active()` needs.

All add/remove/reconcile/prune operations acquire an `fcntl.flock` lock on
`config.active_lock_path()` (`DOCUMENT_VIEWER_CACHE_DIR/active.lock`) --
deliberately *not* inside the exports directory itself, since that
directory is exported as-is to external sync software/devices and the app
must never create metadata there.

- **`add_active(source)`** uses the source's filename as the only
  candidate link name, checked with an `lstat`-style, non-following
  existence check (a dangling symlink still occupies that directory-entry
  name). Nothing there -> create the symlink. A symlink there (dangling or
  not) -> resolves and matches the new source exactly -> idempotent no-op;
  otherwise (points elsewhere, or is dangling) -> unlink and recreate,
  conflict resolved as latest-write-wins. A non-symlink entry there (a
  real file or directory) -> refused and raised, rather than silently
  deleted -- the one safety net kept, matching the "prevent accidents, not
  attacks" trust model below.
- **`remove_active(link_name)`** only ever unlinks a symlink directly
  inside `DOCUMENT_VIEWER_ACTIVE_DIR` (`dir_fd`-relative unlink, target
  never followed or touched). Removal always succeeds once "is a symlink
  at this name" is confirmed, regardless of whether its target still
  validates -- missing (including a symlink loop), outside the collection
  root, replaced by a directory, unreadable, or no longer a supported
  suffix are each reported with their own specific reason
  (`_classify_link()`'s `REASON_*` constants) rather than a generic
  failure or silent no-op.
- **`_classify_link(link_path)`** classifies a symlink from its own
  resolved target (not a trusted rel_path -- a hand-created symlink can
  point anywhere): a dangling target or a symlink loop both fold into one
  `missing` reason (identical handling everywhere, and a hand-created
  loop is too obscure an edge case to earn a second reason code); a
  target that resolves outside `DOCUMENT_VIEWER_ROOT` is `outside_root`;
  only then do not-a-file / unreadable / unsupported-suffix apply.
- **Badge lookup is a deliberately-lossy set, not a link registry.**
  `active_badge_paths()` scans `active_dir`, keeps only links
  `_classify_link()` reports no reason for, and returns the **set** of
  their real paths (a plain, display-only `Path.resolve()`, not the
  hardened O_NOFOLLOW resolver used to actually open files) -- used only
  to answer "is this document currently exported at all" for
  collection-page badges. Multiple links (including hand-created
  duplicates, or a document reached both directly and through an
  in-hierarchy curated symlink directory like `humble-bundle/selected/`)
  resolving to the same real file collapse to one set entry, which is
  correct for an existence check. The Exports page and bulk-invalid
  cleanup never go through this set -- they iterate `active_dir` directly,
  one row per directory entry, since two hand-created links pointing at
  the same target must still appear (and be individually removable) as
  two separate rows.
- **Enumeration rules for `active_dir`**, applied consistently everywhere
  it's scanned (badge lookup, the Exports page, `reconcile()`,
  `remove_invalid()`): hidden entries (name starting with `.`) are always
  skipped entirely -- macOS metadata like `.DS_Store`/`._*` transiently
  dropped by Samba sync is invisible to the app, not even flagged. Only
  symlinks are ever passed to `_classify_link()`; a visible, non-hidden,
  non-symlink entry is never something the app itself creates (the lock
  file lives under `DOCUMENT_VIEWER_CACHE_DIR`, never here), so one
  showing up is flagged as informational only (the Exports page's
  "Unexpected files" notice, or `reconcile()`'s `unexpected_entry` issue)
  and never touched by bulk removal or `--repair`.
- Activation is per underlying format: activating an EPUB never implicitly
  activates or deactivates a sibling PDF variant.
- `remove_invalid()` (wired to the Exports page's "Delete all invalid
  links" button and `documentview:exports_prune`) deletes every symlink
  `_classify_link()` flags with any reason, under the same lock; it never
  calls `_classify_link()` on a non-symlink entry, so it can't touch an
  unexpected file.
- `./manage.py documentview_reconcile_active [--repair]` reports every
  invalid export symlink (any `REASON_*`, via `_classify_link()`) and, with
  `--repair`, deletes them -- a pure "remove broken/invalid links" tool; it
  can no longer recreate a missing symlink, since there's no manifest
  recording that intent. A visible non-symlink entry is reported as its
  own `unexpected_entry` issue, informational only, never touched by
  `--repair`.

### Exports Page

`documentview:exports_index` (`/documents/exports/`) renders through the
*same* `browse.html` template and cover/title-toggle UI as any other
directory (an `exports_mode` flag swaps the breadcrumb/heading and adds
the "Invalid links"/"Unexpected files" sections below the grid), but is
populated by scanning `active_dir` directly rather than a collection
directory: each valid symlink becomes a one-off, single-variant
`LogicalDocument` (reusing `documents.LogicalDocument`/`Variant`, so tiles,
covers, and downloads need no new rendering code), with `view`/`cover`/
`download` URLs built from the link's canonical real rel_path so clicking
through lands on the normal detail page for that document. An invalid
link (any `_classify_link()` reason) instead renders in a separate
"Invalid links" list (name + reason + an individual remove button), with
a "Delete all invalid links" button wired to `remove_invalid()`
(`documentview:exports_prune`, POST-only). A stray non-symlink entry gets
its own read-only "Unexpected files" notice, no delete action at all.

Add/remove is asymmetric by page: the Exports page only ever offers
Remove (every tile there is by definition already exported; there's no
"add" affordance since the exports-directory browser doesn't create
links). Collection browse/detail pages only ever offer Add -- once a
variant is exported, its per-variant action cell (`_export_controls.html`,
`view.html`'s variant table) shows nothing further, just the existing
"exported" badge. Removal is therefore reachable from exactly one place
(the Exports page), regardless of which page the user navigated from.

## Security Model

This is an operational-reliability model, not a multi-user security boundary:
Grove has one person who configures, develops, operates, and creates the
archive accessed by this app. The measures below prevent accidents and bad
personal data from causing surprising behavior; they do not defend against
separate or untrusted users.

- **Path resolution** (`paths.py`): `resolve_directory()`/`resolve_document()`
  reject absolute input, `..`, and NUL bytes, and walk every directory
  component `O_NOFOLLOW` relative to the previous component's pinned
  `dir_fd` -- this is Linux/POSIX `openat()`-style and relies on
  `/proc/self/fd`, matching this deployment's platform. Only a document's
  *final* path component may ever be a symlink (the real collection links
  only to files, never directories); resolving it follows up to
  `DOCUMENT_VIEWER_MAX_SYMLINK_HOPS` hops, containment-checks the target
  against `DOCUMENT_VIEWER_ROOT`, and re-opens the *real* file `O_NOFOLLOW`
  relative to its own freshly-walked parent directory so nothing can
  substitute a different file at the last moment. A symlink resolving
  outside the root, or an unexpected directory-position symlink, is hidden
  from listings and rejected by the resolvers -- not a hard error, since
  none are expected to exist in the real collection. Concurrent hostile
  mutation of the collection is explicitly outside the threat model: the
  person browsing is the same person who manually maintains the archive.
- **Archive input** (`archives.py`): EPUB/CBZ are treated as potentially
  hostile ZIP input -- traversal member names and encrypted entries are
  rejected outright, and the streamed decompressed-byte total (not the
  central directory's claimed sizes) is what actually enforces the
  per-entry/per-operation byte caps, with a cheap declared-ratio pre-check
  as a fast rejection for an obviously hostile compression ratio.
- **Image decoding** (`images.py`): every untrusted image byte stream in
  this app goes through one bounded-decode helper that checks pixel count
  against `DOCUMENT_VIEWER_MAX_IMAGE_PIXELS` before `.load()`, and never
  mutates the process-global `PIL.Image.MAX_IMAGE_PIXELS` (this app shares
  its process with other Pillow consumers).
- **PDF rendering** (`pdfrender.py`): `pdftoppm` runs with a fixed argv
  list (`shell=False`) in a private temp directory under
  `DOCUMENT_VIEWER_CACHE_DIR`, addressed via the already-open source
  descriptor through `/proc/self/fd/<fd>` (never reopening the original
  path), with `start_new_session=True` so a timeout can kill the whole
  process group, and the temp directory is always removed in a `finally`
  path. PDF bytes are treated as potentially hostile despite low expected
  risk and are never interpreted by the Django process itself.
- **Response headers** (`views.py`, covered by `tests/test_headers.py`):
  every response this app produces sets `X-Content-Type-Options: nosniff`
  and a `Content-Security-Policy`. There are two policies:
  - *Served bytes* (covers, preview subresources, downloads):
    `default-src 'none'; sandbox`. These aren't documents we render, but a
    viewer can navigate straight to one, and `sandbox` puts any such
    navigation in an opaque origin with scripting off.
  - *HTML pages*: `default-src 'none'` with `script-src`, `object-src` and
    `frame-src` all `'none'`, `base-uri 'none'`, `style-src`/`font-src`
    `'self'`, `form-action 'self'`, `frame-ancestors 'self'`. `img-src` is
    `'self'` for every page *except* a detail page embedding a **Markdown**
    preview, which additionally allows `https:`/`http:` images, matching
    the sanitizer's protocol allowlist and spec 4.4's trust distinction
    (a locally-authored Markdown file may embed a remote diagram; packaged
    EPUB/CBZ content may not phone home). The app's own templates carry no
    inline `<style>`, `style=`, or `<script>`, so nothing needs
    `'unsafe-inline'`.

  The CSP is defense in depth *on top of* the sanitization above, not a
  replacement for it -- EPUB/CBZ/Markdown/text content must not execute
  document-supplied script in the host origin even if one layer fails.
- **Authorization**: browsing follows the host's authenticated-user
  policy; download and active-link/cover-refresh mutations require
  explicit authorization and are POST-only, CSRF-protected.

## Build, Test, and Development Commands

- `python3 -m py_compile *.py tests/*.py management/commands/*.py
  templatetags/*.py` checks local Python syntax without starting Django.
- From `../../llime`, `./manage.py check` validates settings, URL
  configuration, and app loading.
- From `../../llime`, `./manage.py test documentview` runs this app's test
  suite.
- From the repository root, `python3 -m unittest discover -s tests -t .`
  runs the repo-level tests (`-t .` is required -- see the root
  `CLAUDE.md`).

## Testing Guidelines

Tests are organized by module: `test_paths.py` (traversal, symlink
policy, TOCTOU), `test_documents.py` (grouping, natural sort),
`test_covers.py` (extraction, fallbacks, bomb fixtures, fit-and-pad),
`test_previews.py` (bounded previews, sanitization, signed subresource
ids), `test_download.py` (exact-byte download, preview subresource HTTP
behavior), `test_active.py` (add/remove/reconcile/prune, including
concurrency, symlinked sources, hidden entries, unexpected non-symlink
entries, and the Exports page), `test_headers.py` (the `nosniff`/CSP
contract above), and
`test_config.py` (settings/limit resolution). Each
`DocumentViewTestCase` (in `tests/base.py`) points
`DOCUMENT_VIEWER_*` settings at a fresh temp collection per test via
`override_settings`, so tests never touch the real configured collection.
