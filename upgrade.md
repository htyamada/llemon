# documentview: drop the active-link manifest, rename to Exports, add a browsable Exports page

Status: plan for review, not yet implemented.

## Motivation

`active.py` currently tracks active-reader links in a separate JSON
manifest (`DOCUMENT_VIEWER_ACTIVE_MANIFEST`) that duplicates what the
active directory's symlinks already encode, plus a locked, atomic-write
scheme to keep the two in sync. This plan removes the manifest: the active
directory's actual symlinks become the sole source of truth. It also
renames the user-facing "Reader" concept to "Exports", makes the export
directory browsable through the same UI as the collection itself, and adds
invalid-link flagging with a bulk-cleanup action.

## 1. Directory-is-authority active-link model

- **No more manifest.** Whatever symlink physically exists in
  `DOCUMENT_VIEWER_ACTIVE_DIR` *is* an active link — full stop. There is no
  separate registration step, and nothing is ever "foreign" (present but
  untouchable) the way the old manifest scheme treated unregistered
  symlinks.
- **`add_active(source_rel_path)`**: resolve the source as today, compute
  `link_name = source_abs.name`. Under a lock, check `active_dir /
  link_name` with an **lstat-style check that does not follow the link**
  (`os.path.lexists()`/`Path.is_symlink()`, not `Path.exists()`) — a
  dangling symlink still occupies that directory-entry name, and a
  follow-symlinks existence check would report it as absent, misrouting
  into "create fresh" and then failing with `FileExistsError` since the
  entry is still physically there:
  - nothing at that name (`lexists()` is false) → create the symlink.
  - a symlink is there (dangling or not) → try to resolve it and compare to
    the new source. Resolves *and* matches exactly → no-op (idempotent).
    Otherwise — points elsewhere, **or is dangling** — unlink and recreate
    pointing at the new source (`ln -sfn` semantics): **conflict, latest
    write wins,** and a dangling link is treated as just another "wrong
    target" to replace, not a special case. (Test to add: activating a
    source whose destination name is currently a dangling symlink
    succeeds and replaces it.)
  - a non-symlink entry (a real file/dir) is there → refuse and raise,
    rather than silently deleting what might be the user's own data. This
    is the one safety net kept, matching the "prevent accidents, not
    attacks" trust model in `AGENTS.md`. (Also see the note below: the app
    itself should never be the source of a stray non-symlink file here in
    the first place.)
- **`remove_active(link_name)`**: validate the name, confirm the entry is a
  symlink (no manifest membership check needed — presence is enough),
  classify the target's current validity (via `_classify_link()`, below)
  for the removal message, unlink.
- **`_classify_link(link_path)`** (new primitive, replaces the old
  `_classify_source(source_rel_path)` for every directory-authority use):
  a symlink under `active_dir` gives us only its own target path, which
  may or may not be a collection-relative source string, and may not
  resolve inside `DOCUMENT_VIEWER_ROOT` at all (a hand-created symlink can
  point anywhere). So classification has to start from the link's actual
  resolved target, not a rel_path:
  1. `link_path.resolve(strict=True)`, catching **both** `OSError` (a
     dangling target) **and** `RuntimeError` (`resolve()`'s documented
     exception for a symlink loop — a hand-created circular symlink is
     possible and must not be allowed to propagate as an unhandled
     exception and break the Exports page, badge scan, `reconcile()`, and
     bulk cleanup, which is what would happen today if only `OSError` were
     caught). Both cases are folded into the single existing reason
     `missing`, deliberately not split into a separate "broken/loop"
     reason: either way there is no resolvable real path, both are handled
     identically everywhere (reported, and removed by "Delete all invalid
     links"/`reconcile --repair`), and a symlink loop is an obscure enough
     hand-created edge case that a second reason code would add a label
     and a test matrix without changing any actual behavior. Broaden the
     `missing` label text to cover both: "the source file is missing or
     the link could not be resolved (e.g. a symlink loop)." (Test to add:
     a hand-created circular symlink in `active_dir` is classified as
     `missing`, not an unhandled exception, everywhere `_classify_link()`
     is used.)
  2. Check containment: `real.relative_to(config.root())` — if this fails,
     the target is outside the collection entirely. This is a new, explicit
     reason, **`REASON_OUTSIDE_ROOT`** ("the link points outside the
     collection"), not a variant of "missing." A symlink someone
     hand-created to point somewhere else is possible and is treated as an
     error to flag, exactly like the other invalid reasons — it appears in
     the Exports page's "Invalid links" list and is removed by "Delete all
     invalid links" (§4), since by definition nothing in the exports
     directory is supposed to point outside the collection.
  3. Only once containment holds do the existing checks continue
     unchanged: regular file, readable, supported suffix.

  Returns `(reason, real_path)`; `reason is None` means the link is a
  fully valid, in-collection, supported document, and `real_path` is then
  its real absolute path.
- **Badge lookup is a separate, deliberately-lossy set, not a link
  registry.** Collection-page "exported" badges (§2/§3) only ever need to
  answer "is this document currently exported at all" — they no longer
  drive any remove control (§3 moves removal exclusively to the Exports
  page), so they don't need to name a specific link. `active_badge_paths()`
  scans `active_dir`, keeps only links where `_classify_link()` returns no
  reason, and returns the **set** of their real paths (a plain,
  display-only `Path.resolve()`, not the hardened O_NOFOLLOW resolver used
  to actually open files). Multiple links — including hand-created
  duplicates — resolving to the same real file collapse to one set entry,
  which is correct here: it's an existence check, not an enumeration. This
  is also what lets a document reached two ways — its own directory and an
  in-hierarchy curated symlink directory like `humble-bundle/selected/` —
  both still show the badge, with no separate "requested"-alias
  bookkeeping needed; they simply resolve to the same real file.
- **The Exports page and bulk-invalid cleanup never go through
  `active_badge_paths()`.** They iterate `active_dir` directly, one row per
  directory entry (§3, §4) — a plain scan already gives one entry per
  `link_name`, and `link_name` is unique per directory by construction, so
  two hand-created links pointing at the same target simply appear (and
  are individually removable) as two separate rows. No multimap is needed:
  the filesystem itself is already keyed the right way for this listing:
  by link name, not by target.
- **`reconcile(repair=False)`**: shrinks to "prune broken links," using
  `_classify_link()` per entry. For each **symlink** entry in `active_dir`
  (see the enumeration rules below — hidden entries are never classified
  or reported at all): a target that no longer validates — including
  `REASON_OUTSIDE_ROOT` — is reported (and, with `--repair`, unlinked). The
  old `missing_symlink`/`wrong_target`/`foreign`/`invalid_entry` categories
  all go away — nothing is ever "foreign," and there's no recorded intent
  to recreate a missing link from. A visible **non-symlink** entry is
  never passed to `_classify_link()` (per the enumeration rules), but *is*
  still reported — as its own `unexpected_entry` issue kind, informational
  only, never touched by `--repair` — since flagging stray content in the
  exports directory is exactly what this maintenance command is for; it
  would be inconsistent for the CLI to stay silent about something the
  Exports page's UI (§4) surfaces.
- **Enumeration rules for `active_dir`, applied consistently everywhere it
  is scanned** (the Exports listing, `active_badge_paths()`,
  `reconcile()`, `remove_invalid()`) — this is what keeps classification
  and bulk removal from ever touching something that was never one of the
  app's own links:
  - **Hidden entries (name starts with `.`) are always skipped**, exactly
    like `documents.scan_directory()` already does for the collection.
    `DOCUMENT_VIEWER_ACTIVE_DIR` is expected to be exported over Samba to
    a reader device, which transiently drops macOS metadata like
    `.DS_Store`/`._*` AppleDouble files there; these are invisible to the
    app entirely, not even shown as "flagged."
  - **Only symlinks are ever passed to `_classify_link()`.** A visible,
    non-hidden, non-symlink entry (a real file or directory) is never
    something the app itself creates (see the lock-file note below), so
    one showing up is flagged — on the Exports page as its own separate
    "unexpected file" notice (§4), and by `reconcile()` as an
    `unexpected_entry` issue — but in both places purely informational: it
    is never classified as valid/invalid, never included in
    `remove_invalid()`'s bulk deletion, and never touched by
    `reconcile --repair`. This is what keeps a stray real file — or,
    before this fix, the lock file itself — from ever being misclassified
    as an out-of-root link and silently deleted, while still surfacing it
    rather than staying silent.
- **The lock file must not live inside (or next to, in a way that could be
  mistaken for an entry of) `active_dir` at all.** The app must never
  create lock files or other metadata inside the exports directory —
  it's exported as-is to external sync software/devices, and (per the
  point above) anything placed there risks being surfaced as a stray
  file. The lock moves to `DOCUMENT_VIEWER_CACHE_DIR` instead, e.g.
  `cache_dir() / 'active.lock'` — no new setting needed, it reuses the
  existing cache directory that's already validated/created — and
  continues to guard add/remove/prune the same way.
- Drop entirely: `ManifestError`, `_read_manifest`/`_write_manifest`, the
  temp-file+`os.replace()` atomic-write dance, `CollisionError`,
  `MismatchError`.

### Config/settings cleanup

- Remove `active_manifest_path()`, `active_manifest_lock_path()`,
  `DEFAULT_ACTIVE_MANIFEST`, the `DOCUMENT_VIEWER_ACTIVE_MANIFEST` setting,
  and its `validate_shape()`/`validate_live()` checks.
- Drop `active_manifest` from `appconfig.py` and from
  `etc/documentview.conf` (including its comment block).
- `active_dir()` stays; add a small `active_lock_path()` returning
  `cache_dir() / 'active.lock'` — deliberately under
  `DOCUMENT_VIEWER_CACHE_DIR`, not `DOCUMENT_VIEWER_ACTIVE_DIR` (see §1).

### Behavior changes this implies (flagging explicitly)

1. **Conflict resolution changes from error to silent overwrite.**
   Activating a second document with the same filename as an already-active
   one now replaces the old link instead of requiring deactivation first —
   this is exactly what was asked for, just noting it's user-visible.
2. **`reconcile --repair` can no longer recreate a missing symlink** —
   there's nothing left recording that intent once the manifest is gone.
   It becomes a pure "remove broken/invalid links" tool.
3. **A link pointing outside `DOCUMENT_VIEWER_ROOT` is now a first-class,
   explicit error (`REASON_OUTSIDE_ROOT`)**, not something the old
   manifest scheme had a category for at all (a manifest-recorded `source`
   was always a collection-relative string by construction, so this case
   couldn't previously arise in the same form). It's flagged in the
   Exports page's invalid list and removed by both `reconcile --repair`
   and "Delete all invalid links."

## 2. Rename "Reader" → "Exports"

Renaming every **user-facing and prose** occurrence. Internal Python/Django
technical identifiers stay as-is (`active.py`, `add_active()`/
`remove_active()`, `ActiveError`, `DOCUMENT_VIEWER_ACTIVE_DIR`, the
`active_add`/`active_remove` URL names) — same reasoning as the earlier
"Document View" rename: the technical name and the displayed name don't
have to match, and renaming these identifiers would be a large, purely
cosmetic diff. Flag if the Python-level identifiers should be renamed too.

| Surface | Old | New |
|---|---|---|
| New browse page breadcrumb/title | "Reader" | "Exports" |
| New browse page URL | `/documents/reader/` | `/documents/exports/` |
| New URL name | `documentview:reader_index` | `documentview:exports_index` |
| Add button | "Add to reader" / "Add EPUB to reader" | "Add to exports" / "Add EPUB to exports" |
| Remove button | "Remove from reader" / "Remove EPUB from reader" | "Remove from exports" / "Remove EPUB from exports" |
| Collection-page badge | `<span class="dv-active-badge">reader</span>` | `<span class="dv-export-badge">exported</span>` |
| Partial template | `_reader_controls.html`, `.dv-reader-controls` | `_export_controls.html`, `.dv-export-controls` |
| Flash notices | `Added "X" to the reader.` / `Removed "X" from the reader (...).` | `Added "X" to Exports.` / `Removed "X" from Exports (...).` |
| `AGENTS.md` section header | `## Active-Reader Staging` | `## Exports Directory` |
| `AGENTS.md`/`active.py` prose | "active-reader directory/link", "reader-sync software" | "exports directory", "export link", "sync software" |
| `README.md` | "a separate active-reader directory" | "a separate exports directory" (the other phrase, "not an online reader," stays — unrelated to this naming, it's about the app not being a reading application) |

## 3. Browsable Exports page

A new view/URL, `documentview:exports_index` at `/documents/exports/`,
distinct from the collection browse tree (still rooted at
`DOCUMENT_VIEWER_ROOT`). It renders through the *same* `browse.html`
template and cover/title-toggle UI as any other directory — reusing
`documents.LogicalDocument`/`Variant` so tiles, covers, and downloads need
no new rendering code — but is populated differently and flagged
distinctly:

- **Listing**: iterate `active_dir` directly, one row per **symlink** entry
  (not through `active_badge_paths()` — see §1's note on why that set is
  badge-only), applying the same enumeration rules as §1: hidden entries
  are skipped entirely, and a non-symlink entry never reaches
  `_classify_link()` — it's surfaced separately (§4) instead. For each
  symlink, classify it with `_classify_link()`; a valid one resolves to a
  canonical real path under `DOCUMENT_VIEWER_ROOT`, from which we build one
  single-variant `LogicalDocument`. Since `link_name` is always exactly the
  canonical filename for app-created links (how `add_active` names it),
  this is a direct, cheap per-entry lookup. Two links — including
  hand-created ones — that happen to resolve to the same target each still
  get their own row, since the iteration is keyed by `link_name` (always
  unique per directory), never by target. The listing is flat (no
  subdirectories; `active_dir` never has any), sorted the same natural way.
- **Identity for downstream links stays canonical**: each tile's
  `view`/`cover`/`download`/`preview` URLs use the canonical rel_path
  (inside root), so clicking through lands on the real, normal detail page
  for that document.
- **Clear separation**: reuse `browse.html` with an `exports_mode` flag —
  swap the breadcrumb root label to "Exports," and add a short explanatory
  line (e.g. "Files currently exported for your reading device"). No new
  visual system, just a distinct heading/breadcrumb so it can't be mistaken
  for the collection root.
- A small persistent link from the main "Document View" page's header to
  the Exports page, so it's discoverable rather than a URL you have to
  know.

### Add/remove asymmetry

- **On the Exports page**: every tile is by definition already active, so
  it only ever gets a "Remove from exports" control. There is no "add"
  affordance — offering one from here would be non-functional (the browser
  for the export directory doesn't create links).
- **On collection (source-directory) browse/detail pages**: drop "Remove
  from exports" entirely. `_export_controls.html` and `view.html`'s
  per-variant action cell currently branch on `active_link` to show either
  Add or Remove; both become "show Add when not active, show nothing (or
  just the existing export badge, informational only) when active." This
  is a template-only change — `active_add`/`active_remove` views need no
  new authorization logic, since the constraint is "don't offer the
  control" (matching the accidents-not-attacks posture already documented
  in `AGENTS.md`), not "block the request."

Removal is therefore reachable from exactly one place (the Exports page),
regardless of which page you navigated from.

## 4. Invalid-link flagging + bulk cleanup

On the Exports page, three distinct buckets — **only the first two ever
come from `_classify_link()`, and only symlinks are ever classified at
all** (§1's enumeration rules: hidden entries are invisible; a non-symlink
entry is bucket 3, below, never bucket 2):

1. **Valid** symlinks render as normal document tiles.
2. **Invalid** symlinks — reasons: missing, **outside the collection
   root**, not-a-file, unreadable, unsupported-suffix — can't be resolved
   into a real, in-collection document (no cover, no canonical path to
   link to), so they render in a separate "Invalid links" list: link name
   + reason (e.g. "Book.epub — the source file is missing",
   "external.epub — the link points outside the collection") + an
   individual "Remove" button (the existing single-link removal,
   unchanged). A **"Delete all invalid links"** button/form above that
   list is wired to a new `active.remove_invalid()` function: under the
   same lock used by add/remove, iterate `active_dir`'s **symlinks only**,
   unlink every one `_classify_link()` flags with any reason (missing,
   outside-root, not-a-file, unreadable, unsupported-suffix alike — an
   out-of-collection link is exactly as much an error as a broken one
   here), and report how many were removed (or that there were none). New
   POST-only view + URL (`documentview:exports_prune`,
   `/documents/exports/prune/`) parallel to `active_add`/`active_remove`.
   `remove_invalid()` never even calls `os.scandir()`'s non-symlink
   entries through `_classify_link()`, so it can't touch bucket 3.
3. **Unexpected files** — a visible, non-hidden, non-symlink entry (a real
   file or directory). The app never creates these (the lock file lives
   under `DOCUMENT_VIEWER_CACHE_DIR` now, not here — §1), so one showing up
   means something else put it there. Shown as its own read-only notice
   (name only, no reason/classification, since `_classify_link()` is never
   called on it), with no bulk or individual delete action from this page
   at all — `remove_active()` already refuses to unlink anything that
   isn't a symlink, so there is no code path that would remove it even if
   a button were offered.

This is a strict subset of what `reconcile(repair=True)` already computes,
just reachable from the web UI instead of only the management command —
the CLI tool stays as the fuller/authoritative one for offline maintenance.

## 5. File-level change list

- `lib/documentview/active.py` — manifest removal (§1), `_classify_link()`
  (with `REASON_OUTSIDE_ROOT`), `active_badge_paths()`, `remove_invalid()`,
  export-listing iteration for §3, terminology in docstrings (§2).
- `lib/documentview/config.py` — drop manifest path/lock helpers and
  validation; add `active_lock_path()`.
- `lib/documentview/appconfig.py` — drop `active_manifest`.
- `etc/documentview.conf` — drop `active_manifest` key/comment.
- `lib/documentview/views.py` — `browse()`/`_variant_rows()` switch to
  `active_badge_paths()`; new `exports_index`/`exports_prune` views; rename
  notice strings (§2).
- `lib/documentview/urls.py` — add `exports/` and `exports/prune/` routes.
- `lib/documentview/templates/documentview/browse.html` — `exports_mode`
  flag, badge rename, invalid-links section.
- `lib/documentview/templates/documentview/view.html` — drop Remove
  control, rename Add control.
- `lib/documentview/templates/documentview/_reader_controls.html` →
  `_export_controls.html` — Add-only.
- `lib/documentview/templates/documentview/active_removed.html` — title
  rename.
- `lib/documentview/management/commands/documentview_reconcile_active.py`
  — simplified output categories, plus the new informational
  `unexpected_entry` issue kind for stray non-symlink entries.
- `llime/base/lib/tools.py` — no change expected (nav still points at
  `documentview:index`), unless a separate "Exports" nav entry is wanted
  in addition to the in-page header link.
- Tests: `tests/base.py`, `tests/test_active.py`, `tests/test_config.py`,
  `tests/test_documentview_appconfig.py`, `llime/base/tests.py` — drop
  manifest coverage; add coverage for: latest-write-wins conflict
  (including **replacing a dangling symlink at the destination name**, per
  the reviewer's note); flat hand-created symlinks (including duplicate
  targets) being individually manageable; hidden entries (e.g. `.DS_Store`)
  being fully ignored everywhere `active_dir` is scanned; a non-symlink
  entry being flagged (on the Exports page *and* by `reconcile()` as
  `unexpected_entry`) but surviving both `remove_invalid()` and
  `reconcile --repair`; a hand-created circular symlink being classified
  as `missing` rather than raising `RuntimeError`, everywhere
  `_classify_link()` is used (Exports page, badge scan, `reconcile()`,
  bulk cleanup); the lock file never appearing under
  `DOCUMENT_VIEWER_ACTIVE_DIR` (only under `DOCUMENT_VIEWER_CACHE_DIR`);
  the Exports page; and invalid-link bulk removal, including the new
  `REASON_OUTSIDE_ROOT` case.
- `lib/documentview/AGENTS.md`, `lib/documentview/README.md` — rewrite the
  "Active-Reader Staging" section (§1) and terminology (§2).

## Open questions

1. Should the Python-level identifiers (`active.py`, `add_active`,
   `remove_active`, `DOCUMENT_VIEWER_ACTIVE_DIR`, the `active_add`/
   `active_remove` URL names) be renamed to "export" vocabulary too, or
   stay as internal technical names while the UI says "Exports"? Default:
   leave them as-is.
2. Does the main "Document View" page need a persistent nav-level link to
   Exports (in `llime/base/lib/tools.py`'s nav list, as its own entry), or
   is an in-page header link on the Document View page sufficient? Default:
   in-page link only, no new top-level nav entry.
