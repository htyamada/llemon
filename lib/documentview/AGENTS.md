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
archive-internal preview identifiers; `active.py` owns the active-reader
manifest and locked symlink staging; `config.py` owns settings, defaults,
and lazy filesystem validation; `views.py` / `urls.py` wire it all up.
Templates live under `templates/documentview/`, static assets under
`static/documentview/`, and tests under `tests/`. This file is the
permanent design and behavior record for the app; the original planning
document that shaped it (`upgrades/books.md`) has been superseded by this
file and the code, and was removed once the app was fully implemented.

The active consumer is `../../llime` (mounted at `/documents/`, nav label
"Library"). `../qat/knip` has no document collection and is not expected to
mount this app; nothing here assumes it will.

## Configuration

Required, no default (host must set both):

```python
DOCUMENT_VIEWER_ROOT = Path('/srv/cloud/store/books-and-text/')  # llime's value
DOCUMENT_VIEWER_ACTIVE_DIR = Path('~/var/documentview/reader')
```

`~` expands under the account running `manage.py` / the WSGI process,
which may differ from the developer's own home directory -- both roots run
through `Path(...).expanduser().resolve()`.

Optional, with defaults:

| Setting | Default |
|---|---|
| `DOCUMENT_VIEWER_CACHE_DIR` | `~/var/documentview/cache` |
| `DOCUMENT_VIEWER_ACTIVE_MANIFEST` | `~/var/documentview/state/active_manifest.json` |
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
`DOCUMENT_VIEWER_ROOT`) -- no filesystem access, so `manage.py check`,
migrations, and unrelated management commands never fail just because a
deployment mount happens to be absent. The equivalent live check (that the
configured root exists, and that the active dir / cache dir / manifest /
manifest lock file all resolve outside the root) runs once per process,
the first time a view or management command actually touches the
filesystem.

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

## Active-Reader Staging

`active.py` maintains an app-controlled JSON manifest (default
`~/var/documentview/state/active_manifest.json`, deliberately outside
`DOCUMENT_VIEWER_ACTIVE_DIR` so reader-sync software never sees it) mapping
active-link names to their canonical source. Each entry is
`{"source": "<canonical rel_path>"}`, plus `"requested"` when the document
was activated through an in-hierarchy symlink (as the real collection's
curated `humble-bundle/selected/` directory does): `source` stays the
single identity used for collision, idempotency, and source-validity
checks, while `requested` records the path the user actually acted on so
the browse/detail pages show the active badge under the path they list the
file at, not only under the symlink target's own directory.

A **missing** manifest legitimately means "no active links yet" and reads
as empty. A manifest that exists but is unreadable or malformed raises
`ManifestError`: mutating operations surface it (so a corrupt manifest can
never make existing managed links look foreign and block their removal),
while the display-only lookup degrades to "no badges" and logs, so
browsing still works. All add/remove/reconcile
operations acquire an `fcntl.flock` lock on a sibling `.lock` file before
touching either the manifest or the active directory; manifest writes are
temp-file-plus-`os.replace()` atomic. Crash durability *across* the
separate manifest and symlink operations is **not** a v1 guarantee -- an
interrupted operation may leave the two stores disagreeing, which is
reported clearly rather than silently repaired.

- `add_active(source)` uses the source's filename as the only candidate
  link name. If that name is already occupied by a different registered
  source, or by an unfamiliar filesystem entry, the operation is rejected
  with a collision error -- it never invents a second name, overwrites, or
  adopts. Repeating the same add is idempotent only when the on-disk
  symlink is present and points at the same source; a registered-but-
  missing-or-wrong symlink is a reported mismatch, not an implicit repair.
  An idempotent re-confirm still records a not-yet-seen `requested` alias
  (e.g. the document was first activated as `real/Book.epub` and is now
  also being activated as `selected/Book.epub`) -- otherwise the curated
  directory's badge would never appear, since the early "already active"
  return used to skip updating the manifest entry entirely.
- `remove_active(link_name)` only ever unlinks a symlink directly inside
  `DOCUMENT_VIEWER_ACTIVE_DIR` that is registered in the manifest as
  app-created (`dir_fd`-relative unlink, target never followed or
  touched). Removal always succeeds once that's confirmed, regardless of
  whether the registered source still validates -- missing, replaced by a
  directory, unreadable, or no longer a supported suffix are each reported
  with their own specific reason rather than a generic failure or silent
  no-op.
- Activation is per underlying format: activating an EPUB never implicitly
  activates or deactivates a sibling PDF variant.
- `./manage.py documentview_reconcile_active [--repair]` reports manifest
  entries whose symlink is missing, wrong, or whose source no longer
  validates, and (only with `--repair`, run explicitly by an operator)
  recreates, relinks, or drops those app-owned entries. Every manifest
  `source` and `link_name` is untrusted input -- it can come from a
  corrupted or hand-edited manifest -- so both are validated/resolved
  through the same safe, contained, symlink-resolving path
  (`_resolve_source_candidate()`, `_validate_link_name()`) before any
  repair action; a `source` containing `..` or resolving outside
  `DOCUMENT_VIEWER_ROOT`, or a `link_name` containing `/` or `..`, is
  never trusted enough to create, follow, or unlink a link from. A link
  that exists and whose source validates but whose target doesn't
  actually match the registered source (silently replaced with a symlink
  to something else) is reported/repaired as `wrong_target`, not silently
  treated as consistent. A symlink present in
  `DOCUMENT_VIEWER_ACTIVE_DIR` but absent from the manifest is always
  reported as foreign and **left in place** -- this command never adopts
  or removes an entry it didn't create, matching the app's only having
  delete authority over its own app-created symlinks in this one
  configured directory (spec 1.5). Deletion scope never extends to any
  other file or directory the web-server account happens to be able to
  reach.
- Back up `DOCUMENT_VIEWER_ACTIVE_MANIFEST` like any other small piece of
  application state you don't want to have to rebuild by hand; it's the
  sole source of truth for which active-directory symlinks this app owns.

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
behavior), `test_active.py` (add/remove/collision/reconcile, including
concurrency, symlinked sources, and corrupt manifests),
`test_headers.py` (the `nosniff`/CSP contract above), and
`test_config.py` (settings/limit resolution). Each
`DocumentViewTestCase` (in `tests/base.py`) points
`DOCUMENT_VIEWER_*` settings at a fresh temp collection per test via
`override_settings`, so tests never touch the real configured collection.
