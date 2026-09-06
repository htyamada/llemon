# UPGRADE: Image Blacklist for imhandler

This document replaces imhandler's deferred deletion plan with a persistent
image blacklist. Django never deletes, moves, or modifies source images in an
archive. Hiding an image records its path, and both the viewer and the
`imh`/imhandler pipeline exclude it. Authorized users can view blacklisted
files and restore them to normal processing. Although the feature does not
provide online file removal, its CLI export is intended to support
operator-controlled offline removal, including removal performed by separate
local scripts.

---

## Part 1 — Requirements

### 1.1 Replace deletion with hiding

Remove the current Mark workflow:

- Remove `mark_toggle`, `deletion_list_download`, and
  `deletion_list_clear`, their URL patterns, and the session-held
  `deletion_list`.
- Remove Mark/Unmark buttons, the fixed deletion bar, `delete.sh` generation,
  and their CSS and JavaScript.
- Add a **Hide** action where Mark is currently offered: cluster contact-sheet
  rows and the focal and closest-match images on the Similar page.

Hiding adds the image's canonical path to a persistent blacklist. It does not
unlink, rename, move, or otherwise modify the source file.

### 1.2 Blacklist behavior

Once blacklisted, an image:

- is absent from Browse, Similarity, Semantic, Compare, and Similar pages;
- receives HTTP 404 from the `image` and `thumb` endpoints, so an old URL
  cannot bypass the viewer filter;
- is excluded by `imh list`, `imh thumb`, `imh embed`, `imh cluster`,
  and `imh report`; and
- is treated by `imh purge` as ineligible derived data: its thumbnails,
  database row, and cluster memberships may be removed, but its source file
  must never be removed.

Existing thumbnails and database rows must not make a blocked image visible.
Blacklist matching is implemented once in the shared library, rather than
independently in Django and the CLI.

### 1.3 Hide and restore interactions

Clicking **Hide** opens a confirmation dialog with a thumbnail, the full path,
and text saying that the archive file remains on disk while imhandler stops
displaying and processing it. The dialog offers **Cancel** and **Hide**.
Submitting is POST-only and CSRF-protected; controls are disabled in flight,
errors remain in the dialog, and adding an existing entry is idempotent.

On success, update the page without a full reload. Remove the contact-sheet row
or replace the Similar panel with a **Hidden** notice. If fewer than two visible
cluster members remain, return to Compare.

Add an authorized **Hidden images** page listing blacklist entries, including
missing files, with a POST-only, CSRF-protected **Show again** action. Restoring
removes the path from the blacklist but does not recreate purged thumbnails,
embeddings, or clusters; normal viewer and `imh` workflows rebuild them.

The blacklist is exportable only through the local `imh` CLI. The web UI has
no download or export action. Export produces data that a separate local
script or manual process can use for explicitly offline file removal. The
export command itself does not delete files or emit an executable shell
script.

### 1.4 Path scope and identity

- Entries are absolute normalized paths beneath a configured `image_root`
  with a suffix in `scanner.IMAGE_SUFFIXES`.
- Reject paths outside all roots, traversal, and textual-prefix lookalikes.
- Identity is path-based. Replacement at the same path stays hidden; a rename
  has a new identity and is not automatically hidden.
- Use the same path representation as scanner output and database rows.
- Validation need not require the file to exist. This supports stale entries
  and keeps a later file at that path hidden until explicitly restored.
- Hiding does not require a writable image root. Archive roots may and normally
  should remain read-only to Django.

### 1.5 Persistence and concurrency

Store the blacklist at `cache_dir/blacklist.json`:

```json
{
  "version": 1,
  "paths": ["/home/yamada/Pictures/Archive/2024/example.jpg"]
}
```

Paths are unique and sorted. A missing file means an empty blacklist; malformed
or unsupported data is an explicit error, never silently empty. Updates use an
inter-process lock and atomic replacement so Django workers and CLI processes
cannot lose changes or observe partial JSON. Create it with owner-only
permissions and never write into a source-image directory.

### 1.6 Authorization and failure policy

- By default, hiding, listing, and restoring require an authenticated staff
  user. Hosts may provide an `IMHANDLER_BLACKLIST_AUTHORIZER` callable.
- Endpoint authorization is authoritative; hiding buttons is not sufficient.
- A blocked image endpoint returns 404 without revealing source-file existence.
- No blacklist operation invokes a source-path delete, move, rename, or shell
  command.
- Django does not expose the complete blacklist as a downloadable file or API
  response. The Hidden images page is for interactive review and restoration,
  not bulk export.
- If the blacklist cannot be read, image-serving and mutation endpoints fail
  closed. CLI commands report the error and exit nonzero instead of processing
  every image.
- Cleanup failure leaves the entry blocked and can be retried with `imh purge`.

### 1.7 Out of scope

- Deleting, moving, renaming, or modifying archive files.
- Trash or filesystem undelete.
- Bulk hide/restore.
- Content-hash or inode tracking across moves.
- Sharing a blacklist between variants that have different `cache_dir` values.
- Performing deletion or generating an executable deletion script from `imh`
  itself. Consuming an export with a separate, operator-invoked offline script
  is supported.

---

## Part 2 — Design

### 2.1 Shared blacklist module

Add `imhandler.blacklist` as the sole persistence and matching implementation:

```python
load() -> frozenset[Path]
is_blocked(path: Path | str, blocked: AbstractSet[Path] | None = None) -> bool
add(path: Path | str) -> bool
remove(path: Path | str) -> bool
```

`add` and `remove` return whether the set changed. They normalize and
validate the path, acquire the update lock, reload under that lock, and
atomically replace the file. The module uses `cache.cache_dir()`, configured
root lookup, and `scanner.IMAGE_SUFFIXES`, and contains no Django types.

### 2.2 Scanner and direct-entry integration

`scanner.scan()` and `scan_all()` accept an optional preloaded blocked set.
Each top-level operation loads the blacklist once and passes that immutable
snapshot through recursive scans. Filter before creating an `ImageEntry`.

This automatically covers Browse, list, thumbnail prewarming, and embedding.
Explicit-path library entry points that bypass scanning must call
`is_blocked` themselves. An album whose leaf images are all hidden follows
the existing empty-album rendering behavior.

### 2.3 Database, clustering, and purge

Old database rows require explicit filtering. Similarity, Semantic, Compare,
cluster detail, Similar, and reports load one blacklist snapshot and omit
matching rows. Clustering excludes blocked rows from its similarity matrix.
Reports omit blocked members and clusters with fewer than two visible members.

`imh purge` removes thumbnails, `ClusterMembership` rows, and `Images`
rows for blocked paths and collapses clusters with fewer than two members.
Every such write is confined to `cache_dir`; tests must assert that the
source archive is unchanged. Restored images regain derived state only through
later thumb/embed/cluster runs.

### 2.4 Django endpoints and templates

Replace deletion-list routes with:

| URL | Name | Method | Purpose |
|---|---|---|---|
| `hide/` | `hide_image` | POST | Add one image path |
| `hidden/` | `hidden_images` | GET | List entries |
| `restore/` | `restore_image` | POST | Remove one image path |

The normal `image` and `thumb` views check the blacklist after canonical
path validation and before opening or generating content — and before any
conditional-response handling, so a revalidating client cannot be answered
`304` for an image that has since been hidden (hiding does not change the
file's mtime, so the validator still matches). Page views filter database
results before building context.

**Media caching.** Blocking in the view is necessary but not sufficient.
Both media endpoints currently answer with `Cache-Control: max-age=3600` and
no validator, so a client that loaded an image before it was hidden keeps
displaying it for up to an hour without contacting Django at all — an old
URL bypassing the viewer filter, which section 1.2 forbids. Therefore:

- `image` and `thumb` send `Cache-Control: private, no-cache`, revalidated
  on every use, together with a `Last-Modified` validator (the source
  file's mtime for `image`, the thumbnail's for `thumb`) so a still-visible
  image costs a conditional request answered `304`, not a re-download.
  `private` also stops a shared proxy from holding a copy that no Django
  check can reach.
- A blocked `404` sends `Cache-Control: no-store`, so the block itself is
  never cached and a later restore takes effect immediately. A blocked
  image answers `404` to every request, conditional or not; `@condition`
  and `@last_modified` are therefore unusable on these views, since they
  answer before the view body runs.
- A contact sheet consequently issues one conditional request per
  thumbnail. That cost is accepted: any nonzero `max-age` is precisely the
  window during which a hidden image stays viewable, and section 1.2 allows
  no window.
- **Already-cached responses cannot be revoked.** Anything a browser stored
  under the old one-hour `max-age` stays usable until it expires, whatever
  the new code sends. At least one hour of wall-clock time — the old
  `max-age` — must pass between deploying the header change and making the
  Hide action reachable; a separate release is not a substitute for the
  elapsed time. Otherwise, accept a bounded window in which an image hidden
  shortly after rollout still renders from a warm cache. Step 5 records
  this as a rollout constraint.

Add a shared `_hide_modal.html` with plain JavaScript to show the existing
thumbnail and path, post the path and CSRF token, display errors, and invoke a
page-specific success callback. Add `hidden_images.html` with path,
source-exists status, and Show again form. Do not show thumbnails there because
media endpoints deliberately block them.

### 2.5 CLI behavior

| Command | Effect |
|---|---|
| `imh list` | Omit blocked paths from counts and output |
| `imh thumb` | Do not generate blocked thumbnails |
| `imh embed` | Do not create or refresh blocked embeddings |
| `imh cluster` | Exclude blocked database rows |
| `imh report` | Omit blocked members and undersized visible clusters |
| `imh purge` | Remove blocked derived state, never source files |
| `imh blacklist export` | Write the blacklist as offline, non-executable data |

Add:

```sh
imh blacklist export [-o FILE] [--format paths|paths0|json]
```

The default `paths` format writes one absolute path per line, sorted exactly as
in the store. `--format json` writes the versioned blacklist document.
`--format paths0` writes each absolute path terminated by a NUL byte instead
of a newline (the `find -print0`/`xargs -0` convention), sorted the same way.
With no `-o`, output goes to stdout so the operator can redirect it locally.
`-o FILE` uses atomic replacement and refuses to use the blacklist store
itself as the destination. Export is read-only: it does not clear entries,
mutate derived data, inspect or modify source files, or invoke another
program.

Unix filenames may contain a literal newline byte, which a line-oriented
format cannot represent: splitting output on newlines would silently turn one
blacklisted path into two apparent removal targets for an offline consumer,
one of which was never actually blacklisted. To keep the `paths` format safe
for its one supported consumption pattern (splitting on newlines), exporting
with `--format paths` must fail explicitly, before writing any output, if any
entry in the store contains a newline — the same "explicit error, never
silently wrong" policy already required of a malformed blacklist file
(section 1.5). `--format paths0` and `--format json` have no such
restriction: NUL cannot appear in a POSIX path, and JSON escapes embedded
newlines in its string encoding, so both represent every accepted path
exactly and are the correct choice for machine consumption of blacklists that
may contain such entries.

The paths formats are deliberately data, not shell syntax: paths are not
quoted as commands, and no shebang, `rm`, or other executable content is
generated. They are suitable as input to a separate, operator-invoked local
script or manual process that reviews and removes archive files offline
(`paths0` piping into `xargs -0` is the safe pattern for arbitrary filenames;
plain `paths` remains convenient for the common case and for manual review).
This assists offline removal without giving the web application or the `imh`
export command file deletion capability.

Each configured variant uses its own `cache_dir` and blacklist. An intentionally
unconfigured `imh list DIR` has no blacklist to load and retains its current
behavior; document this exception.

### 2.6 Normative documentation changes

| File | Required update |
|---|---|
| `specs/imhandler-django-man.md` | Replace Mark/download with Hide and restore |
| `specs/imhandler-specs.md` | Add blacklist API and replace deletion-list state/views |
| `specs/imhandler-django-impl.md` | Update exports, URLs, authorization, templates, media blocking |
| `specs/imhandler-overview.md` | Describe non-destructive viewer hiding and CLI exclusion |
| `specs/imhandler-goals.md` | Replace deletion-list capability with blacklist capability |
| `specs/imhandler-imh-man.md` | Document every command, offline blacklist export, and the unconfigured-list exception |
| `specs/imhandler-imh-impl.md` | Document scan snapshots, DB filtering, clustering, purge, and export serialization |

Changes to format, validation, command coverage, authorization, restoration, or
cleanup semantics must update the affected specifications in the same change.

Step 2's design adds two items to this list beyond the blacklist itself:
`imh purge DIR` now scopes its database sweep to the given directory and
skips the thumbnail sweep entirely (`specs/imhandler-imh-man.md`,
`specs/imhandler-imh-impl.md`), and `--format paths` rejects a carriage
return as well as a newline (section 2.5 above, plus
`specs/imhandler-imh-man.md`).

---

## Part 3 — Implementation Steps

### Step 1 — Persistent store

Add and test `lib/imhandler/blacklist.py`: versioned parsing, normalization,
root validation, missing and stale paths, idempotent updates, locking, atomic
replacement, multiple roots, malformed input, permissions, and concurrent
writers. Document the JSON as persistent user state, not disposable cache.

**Why this is the clean first step:** it is a standalone module under
`lib/imhandler/` with no Django, CLI, scanner, or database dependency — its
only inputs are `imhandler.appconfig`'s module globals (`cache_dir`,
consumed indirectly through `cache.cache_root()`) and `scanner.IMAGE_SUFFIXES`,
both already unit-tested in isolation (`tests/test_appconfig.py` shows the
exact mocking pattern: patch the `appconfig` module globals directly, no
Django settings or `manage.py` needed). It touches no existing code path —
nothing imports it yet — so it can be added, tested, and reviewed on its own
with zero regression risk to Browse, Similarity, or any `imh` command. Steps
2-5 (scanner/CLI enforcement, Django endpoints, spec updates, rollout) all
read this module's public API as a given, so it is also the one piece every
later step is blocked on.

This step also makes one small, additive change outside `blacklist.py`
itself: a new `cache.configured_image_roots()` function (see below), because
neither existing root helper is safe to call from path-validation code —
see the first design bullet.

**Design.**

- **Storage location:** `cache.cache_root() / 'blacklist.json'`, matching
  section 1.5 exactly (`cache_dir/blacklist.json`). The module creates
  `cache_dir` with `mkdir(parents=True, exist_ok=True)` if absent — same
  pattern `db.open_db()` already uses for `cache_dir/db/`.
- **Root-lookup helper (new, additive, in `cache.py`):** neither existing
  root function is safe to call from `_normalize()`. `scanner.py` does not
  even import `image_roots` (it only imports `image_root`/`image_root_entries`
  from `cache.py` — an earlier draft of this plan wrongly referenced
  `scanner.image_roots()`, which does not exist at all). And `cache.image_roots()`
  itself, via `image_root_entries()`, raises `EnvironmentError` for **every**
  configured root the instant *any one* of them fails `p.is_dir()` — correct
  for the existing Django views (`_get_roots()` catches that and renders a
  whole-page error, an accepted all-or-nothing failure mode for *serving*),
  but wrong for blacklist mutation: a user hiding/restoring an image under an
  available root must not fail merely because a second, unrelated archive
  volume happens to be unmounted right now, and section 1.4 explicitly wants
  stale-path support (a root can legitimately be offline). Add:
  ```python
  # cache.py
  def configured_image_roots() -> list[Path]:
      """Configured image_root paths, resolved but never required to exist.

      Unlike image_root_entries()/image_roots(), this does not stat() or
      require each root to currently be a directory -- callers that only
      need root *identity* for path validation (not to actually read files
      under it) use this so an offline/unmounted archive volume doesn't
      block validation for a path under a different, available root, or
      block validating a stale path under the offline root itself. Still
      raises EnvironmentError if image_root is entirely unconfigured, same
      as image_root_entries().
      """
      paths = appconfig.image_roots
      if not paths:
          raise EnvironmentError('image_root is not configured in etc/imhandler.conf')
      return [Path(p).expanduser().resolve() for p in paths]
  ```
  `Path.resolve()` defaults to `strict=False`: it does not raise for a path
  that doesn't exist (confirmed: `Path('/nonexistent/sub/../x').resolve()` →
  `/nonexistent/x`, no error) — it resolves symlinks for whatever prefix of
  the path currently exists and lexically collapses the rest. That is what
  makes this safe to call regardless of whether the root is mounted, and
  it's also *why* `_normalize()` below can use the same call for the
  candidate path without reintroducing an existence requirement.
- **Path identity:** a `_normalize(path: Path | str) -> Path` helper —
  - Expands `~`, then **checks absoluteness before resolving**:
    `expanded = Path(path).expanduser(); if not expanded.is_absolute(): raise
    ValueError(...)`. This ordering matters and is easy to get backwards —
    `Path.resolve()` always returns an absolute path (a relative input
    resolves against the current working directory), so checking
    `is_absolute()` *after* calling `resolve()` can never reject anything:
    if the process's CWD happens to be inside a configured root (plausible
    for a CLI invocation, e.g. `imh` run from within an archive directory),
    a relative input like `photo.jpg` would resolve to a real in-root,
    correct-suffix path and sail through every later check even though
    section 1.4 requires "absolute normalized paths" as the accepted input
    shape, not merely as the output shape. Only once the pre-resolve
    absoluteness check passes does `_normalize()` call `Path(...).resolve()`
    — the same non-strict resolution `cache.configured_image_roots()` now
    uses for roots, and, importantly, the same call the existing
    `image`/`thumb`/`similar` endpoints already use on the client-supplied
    `path` query parameter (`lib/imhandler/djview/__init__.py:492,621,656`:
    `path = Path(path_str).resolve()`). Reusing that exact convention (not
    the lexical-only `os.path.normpath` an earlier draft of this plan used)
    is what keeps stored identity and served identity from disagreeing: if
    a candidate path passes through an in-root symlink, `.resolve()`
    collapses it to the same real target the serving endpoints would
    independently resolve to, so a blacklist entry for the alias and a
    request for the target (or vice versa) are the same stored value — the
    alias/target split an earlier draft left open cannot arise, because
    both call sites now agree there is only one identity: the resolved
    path.
  - Checks containment with `any(normalized == root or
    normalized.is_relative_to(root) for root in cache.configured_image_roots())`
    — `Path.is_relative_to` compares path *segments*, so
    `/home/yamada/Pictures/ArchiveEvil/x.jpg` is correctly rejected against
    a configured root of `/home/yamada/Pictures/Archive` (the textual-prefix
    lookalike section 1.4 calls out; a `str.startswith()` check would wrongly
    accept it). Both sides of the comparison went through the same
    `.resolve()`, so this is also symlink-consistent, not just
    string-consistent. Raises `ValueError` if no configured root contains
    the path.
  - Checks `normalized.suffix.lower() in scanner.IMAGE_SUFFIXES`, raising
    `ValueError` otherwise.
  - `add()` and `remove()` both call this, so they share one rejection
    path for traversal, out-of-root, and wrong-suffix inputs. `is_blocked()`
    deliberately does **not** call it: it's the hot path (once per file
    during a scan of potentially thousands of images), and every caller
    already guarantees a normalized, in-root, correct-suffix path before
    checking it — the scanner only ever calls it with a `Path` it just built
    under a resolved root, and section 2.4's endpoints check the blacklist
    "after canonical path validation" has already run (and, per the point
    above, will have already run the identical `.resolve()`). `is_blocked`
    is a plain `Path(path) in blocked` lookup.
- **On-disk format:** exactly the section-1.5 shape, `paths` always written
  sorted for a stable diff/`git`-friendly file even though it's not meant to
  be hand-edited. `load()` raises a new `BlacklistError` (module-level
  exception, not a bare `ValueError`, so callers can distinguish "bad input
  path" from "store is corrupt") when the file exists but `json.load` fails,
  `version` is missing or not `1`, `paths` is not a list of strings, or **any
  individual entry fails structural validation** — checked with a
  `_validate_stored_entry(raw: str) -> Path` helper applied to every entry:
  non-empty, absolute, no embedded NUL byte (`'\x00' not in raw` — NUL can
  never appear in a real POSIX path and always indicates corruption), a
  suffix in `scanner.IMAGE_SUFFIXES`, and — the check that closes the actual
  gap — `os.path.normpath(raw) == raw`, i.e. the string is already in its
  own canonical lexical form. This is what catches a malformed entry like
  `/archive/sub/../photo.jpg` sitting next to a scanner-produced
  `/archive/photo.jpg`: today's plain "list of strings" schema check would
  accept it, and since `is_blocked()` is a bare set-membership check (by
  design, for speed), the two strings would never compare equal and the
  intended file would silently stay visible. This check is **deliberately
  lexical only** — it does not call `.resolve()` or re-check root
  containment against the *current* `appconfig.image_roots` at load time:
  re-resolving would touch the filesystem and could legitimately disagree
  with what was stored if something on disk changed since, and re-checking
  containment would reject a historically-valid entry purely because
  `image_root` was reconfigured afterward, which section 1.4 does not ask
  for (a stale/offline root must not evict its existing entries). It only
  rejects strings that could never have come out of `_normalize()` in the
  first place, regardless of today's config. Per section 1.5, "malformed or
  unsupported data is an explicit error, never silently empty." A
  **missing** file is the one case that legitimately means an empty
  blacklist: `load()` catches `FileNotFoundError` specifically and returns
  `frozenset()`, nothing broader.
- **Locking:** a dedicated `cache_dir/.blacklist.lock` file (never the data
  file itself), taken with `fcntl.flock(fd, fcntl.LOCK_EX)` for the
  reload-under-lock / compute / atomic-replace critical section in `add()`
  and `remove()`. This is the first inter-process lock in the codebase
  (`storage.py`'s `_replace_or_move` handles atomic replacement but not
  locking, and `db.py` relies on SQLite's own locking) — `fcntl` is
  Linux/POSIX-only, consistent with the rest of `imhandler`'s existing
  filesystem assumptions and this deployment's environment, so no
  cross-platform fallback is planned.
- **Atomic replacement:** `tempfile.mkstemp(dir=cache_dir, prefix='.blacklist-')`
  (created `0600` by `mkstemp` itself, satisfying section 1.5's "owner-only
  permissions" with no extra `chmod`), write + `flush()` + `os.fsync()`,
  then `os.replace(tmp, store_path)` — same-filesystem rename, so no
  `EXDEV` fallback is needed the way `storage.py._replace_or_move` needs one
  for cross-media-root moves. On any exception during the write, the temp
  file is unlinked before re-raising so a failed write never leaves stray
  `.blacklist-*.tmp` files behind.
- **Public API** (signatures as already fixed in section 2.1):
  ```python
  class BlacklistError(Exception):
      """The blacklist store exists but is corrupt or unsupported."""

  def load() -> frozenset[Path]: ...
  def is_blocked(path: Path | str, blocked: AbstractSet[Path] | None = None) -> bool: ...
  def add(path: Path | str) -> bool: ...     # raises ValueError on an invalid path
  def remove(path: Path | str) -> bool: ...  # raises ValueError on an invalid path
  ```
  `add`/`remove` both: normalize+validate first (outside the lock — no
  reason to hold it just to raise `ValueError`), then acquire the lock,
  reload the current on-disk set fresh (never trust an in-memory copy across
  the lock acquisition — that's precisely the lost-update race the lock
  exists to prevent), compute the new set, write it only if it actually
  changed, and return whether it changed. An `add()` of an
  already-present path or a `remove()` of an absent one is a no-op that
  still returns `False` — this is what makes hiding idempotent (section 1.3)
  and gives the Django view a simple truthy check for "did anything happen."
  **Known limitation, deliberately not solved in Step 1:** `remove()` uses
  the same `_normalize()` as `add()` — including the current-config root
  containment check — so a stored path that would no longer validate against
  the *current* configured roots (e.g. `image_root` was reconfigured after
  the entry was added, or the entry's root is a currently-offline volume
  outside every *currently* configured root) cannot be removed through this
  API, even though `load()` (see above) still surfaces it correctly and
  `is_blocked()` still blocks it. This is a real, tested gap, not a
  hypothetical one: covered by a test below rather than left implicit. Out
  of scope to solve until Step 3's restore endpoint makes "remove an entry
  `_normalize()` would now reject" a real product question (e.g. whether
  restore should bypass containment entirely, since restoring only ever
  narrows the blocked set).

**Tests** — new `tests/test_imhandler_blacklist.py`, following
`tests/test_appconfig.py`'s pattern (insert `lib/` onto `sys.path`, import
`imhandler.blacklist` and `imhandler.appconfig` directly, no Django). Each
test sets `appconfig.cache_dir` and `appconfig.image_roots` to a
`tempfile.TemporaryDirectory()` via `mock.patch.object`, so nothing touches
a real archive or a real cache dir:

- `load()` on a missing file returns `frozenset()`.
- `add()` then `load()` round-trips one path; `add()` of the same path again
  returns `False` and leaves the file's mtime/content unchanged (idempotent,
  no spurious write).
- `add()` rejects: a relative path, a path outside every configured root, a
  path under a root only as a textual prefix (e.g. sibling directory whose
  name extends the root's, proving `is_relative_to` and not
  `startswith` is used), a `..`-traversal path that lexically escapes the
  root, and a wrong-suffix file — each as a distinct `ValueError` case.
- **Relative input with an in-root working directory** (the absoluteness
  ordering bug): `chdir()` into a configured root's temp directory, then
  call `add('photo.jpg')` (no leading `/`) and assert it still raises
  `ValueError`. Checking `is_absolute()` *after* `resolve()` would pass this
  case silently — `Path('photo.jpg').resolve()` becomes an in-root,
  correct-suffix absolute path from the CWD alone — so this test only passes
  once the absoluteness check runs on the pre-resolve, `expanduser()`-only
  path.
- `add()` accepts a path that does not exist on disk (stale-entry support,
  section 1.4) and `load()` still returns it.
- `remove()` of a present path returns `True` and drops it from `load()`;
  `remove()` of an absent path returns `False` and writes nothing.
- Multiple configured roots: `add()` accepts a path under the second root
  when only the first root would reject it.
- **Unavailable configured root** (`cache.configured_image_roots()`, Finding
  1): a direct test of the new function itself — two configured roots, one
  pointed at a path that does not exist on disk at all — asserts it returns
  both resolved paths without raising (unlike `image_roots()`, exercised
  separately in `tests/test_appconfig.py`'s style but for `cache.py`, kept
  in this same new test file since the function is small and only exists
  for `blacklist.py`'s sake for now). Plus, at the `blacklist.py` level with
  the same two-roots-one-missing fixture: (a) `add()`/`remove()` for a path
  under the *other*, present root still succeeds, proving root validation no
  longer calls the existence-requiring `image_root_entries()`/`image_roots()`;
  (b) `add()`/`remove()` for a path nominally under the *missing* root itself
  also succeeds (containment doesn't require the root to currently exist),
  directly exercising the "compatible with stale paths" requirement, not
  just the "other root is fine" half.
- **Symlink identity consistency** (Finding 2): inside a temp root, create a
  real file and a symlink to it elsewhere under the same root; `add()` the
  symlink's path; assert the entry `load()` returns is the *real* file's
  resolved path, not the symlink's own path — i.e. asserting the stored
  value equals `Path(symlink_path).resolve()`, the same computation
  `lib/imhandler/djview/__init__.py`'s `image`/`thumb` endpoints perform on
  a request path. This is the test that would have caught the original
  divergence: with the old lexical-only normalization, this assertion would
  have failed because the stored entry would have been the symlink's own
  (unresolved) path instead.
- **Load-time structural validation** (Finding 3): hand-write a store whose
  `paths` list contains one syntactically-valid-JSON-string entry that
  `_normalize()` itself could never have produced —
  `/archive/sub/../photo.jpg` (a non-canonical `..` segment) is the
  motivating case, plus a relative path, a path with an embedded `\x00`,
  and a wrong-suffix path, each as a separate test — and assert `load()`
  raises `BlacklistError` for each, never silently returning a set that
  contains the bad string as-is. A companion test proves this check stops
  at syntax, not config: write a store containing one *well-formed*,
  `_normalize()`-producible entry (absolute, canonical, correct suffix)
  whose root is **not** among the currently-configured `image_roots` (i.e.
  reconfigured away since it was added) and assert `load()` still returns
  it without raising — this is the "preserving valid historical entries
  after root reconfiguration" half of the requirement, and it's what
  distinguishes this from simply re-running `_normalize()` at load time
  (which would have rejected it and thereby actively evicted a real
  historical entry, the opposite of what section 1.4 wants). Chain that
  same fixture into the already-planned "known limitation" test: `remove()`
  of that same now-unreachable-by-root path raises `ValueError`, while
  `is_blocked()` against the `load()`-returned snapshot still correctly
  reports it as blocked — pinning down, in one test, that the entry is
  fully visible/enforced but only removal is affected.
- A hand-written malformed store — invalid JSON, `"version": 2`, and
  `"paths"` containing a non-string — each raises `BlacklistError` from
  `load()` (and therefore from `add()`/`remove()`, which call it under the
  lock), never returns an empty/partial result.
- The written file has mode `0600` (via `stat().st_mode`) and its parent
  directory is created if missing.
- A concurrency test: use a `ThreadPoolExecutor` to call `add()` for N
  distinct valid paths at once against the same store; assert all N survive
  in the final `load()` (proves the lock's reload-under-lock actually
  prevents a lost update rather than just serializing on paper).
- `is_blocked()` with an explicit `blocked` set skips disk entirely (assert
  via a `mock.patch.object(blacklist, 'load')` that raises if called) —
  confirms callers can pass a cached snapshot for the hot path described
  above.

**Verification:**
```sh
python3 -m py_compile lib/imhandler/blacklist.py lib/imhandler/cache.py
python3 -m unittest tests.test_imhandler_blacklist -v
python3 -m unittest discover -s tests -t .
```
No `manage.py check`/`manage.py test` needed for this step — nothing Django
imports it yet.

### Step 2 — Shared and CLI enforcement

Make every non-Django code path blacklist-aware: the scanner, thumbnailer,
embedder, clusterer, database query helpers, `purge`, and all `imh`
commands, plus the new `imh blacklist export`. Django is left alone with two
contained exceptions, each justified below: `embed_stream`'s tuple unpack
(2d) and the media caching headers (2i). See "Intermediate state" below,
because a correct Step 2 also changes what the running site displays without
any `djview` edit at all.

**Why this is the right next step:** Step 1 delivered the store; nothing
reads it. The scope boundary that makes this step coherent is "everything
with no Django import" — which is also exactly the boundary section 1.2
draws when it requires that "blacklist matching is implemented once in the
shared library, rather than independently in Django and the CLI." If the
Django endpoints were built first they would have to filter results
themselves, and Step 2 would then be a refactor of freshly written code
rather than an addition. Done in this order, Step 3 has no filtering left
to write: it adds authorization, three endpoints, and UI on top of library
functions that already exclude blocked images.

**Sub-steps,** each separately compilable and testable, in dependency
order: (2a) small additions to `blacklist.py`; (2b) `scanner`; (2c)
`thumbnailer` (including the `purge` changes); (2d) `embedder`; (2e) `db`
and `clusterer`; (2f) existing `imh` commands; (2g) `imh blacklist
export`; (2h) tests; (2i) the media caching headers, which depend on
nothing else here and can land first.

**Design.**

- **2a — three small additions to `blacklist.py`:**
  ```python
  class BlockedImageError(Exception):
      """An explicit-path operation was asked to act on a blocked image."""

  def store_path() -> Path: ...          # public alias for _store_path()
  def load_if_configured() -> frozenset[Path]: ...
  ```
  `store_path()` exists because `imh blacklist export -o FILE` must refuse
  to overwrite the store, and reaching into `blacklist._store_path()` from
  the CLI would make a private name part of the interface by accident.
  `BlockedImageError` is a distinct class, not `ValueError`, for the same
  reason `BlacklistError` is: callers must be able to tell "you asked me to
  thumbnail a hidden image" (a policy stop, HTTP 404) apart from "that path
  is not a valid blacklist path" (a bad request) and from "the store is
  corrupt" (fail closed, HTTP 500).
  `load_if_configured()` is `load()` except that it returns `frozenset()`
  when `cache_dir` is entirely unconfigured:
  ```python
  def load_if_configured() -> frozenset[Path]:
      """load(), or empty when no cache_dir is configured at all.

      The single sanctioned fail-open in the whole feature, and it exists
      for exactly one documented case: section 2.5's `imh list DIR` run
      against an unconfigured variant, which has no cache_dir and therefore
      no store to consult.  A *corrupt* store still raises BlacklistError
      here -- only "there is no configured store" is tolerated, never
      "there is a store and it cannot be read."
      """
      try:
          cache.cache_root()
      except EnvironmentError:
          return frozenset()
      return load()
  ```
  Keeping this in `blacklist.py` rather than inlining a `try/except
  EnvironmentError` at the `imh list` call site means the feature has one
  fail-open, in one function, with one docstring explaining it and one test
  pinning that corruption is still an error. An inlined version would be
  copied the next time someone adds a command.

- **The `blocked=` parameter convention.** Every function that reaches
  images gains a keyword-only `blocked: AbstractSet[Path] | None = None`
  with exactly one meaning:
  - `None` → load one snapshot now (via `load_if_configured()`), fail
    closed if the store is corrupt;
  - an explicit set → use it and do not touch disk;
  - `frozenset()` → "nothing is blocked", the only way to get unfiltered
    results, and it must be written out at the call site.

  There is deliberately **no** `enforce=False` / `skip_blacklist=True`
  escape hatch: an opt-out flag is the mechanism by which a future caller
  quietly leaves the policy, and section 1.2 admits no exceptions.
  The important half of this convention is that the default is *load*, not
  *don't filter*. The tempting alternative — `None` means no filtering, so
  callers must opt in — is faster and more explicit, and it is wrong here:
  the failure mode of a caller forgetting the parameter would be a hidden
  image becoming visible again, silently, which section 1.2 treats as the
  one outcome the feature must never produce. With a loading default, the
  cost of forgetting is a redundant few-microsecond JSON read, not a policy
  breach. Callers that scan several roots in one operation still pass an
  explicit snapshot so that the whole operation sees one consistent view
  (and so a two-root `imh thumb` does not load twice).

- **Circular import (a real one, not a hypothetical).** `blacklist.py`
  already does `from . import cache, scanner` at module level, so
  `scanner.py` must **not** do `from . import blacklist` at module level:
  importing `imhandler.scanner` first would re-enter it through
  `blacklist`. Use a function-local import inside `scan`/`scan_all`,
  matching the precedent already in `thumbnailer.purge` (`from .scanner
  import scan  # local import to avoid circular at module level`). The same
  applies to `thumbnailer.py`, which `blacklist.py` does not import but
  which imports `scanner` transitively; keep every `blacklist` import in
  `scanner`/`thumbnailer` function-local and the dependency graph stays
  one-directional on paper as well as in practice.

- **2b — scanner.** `scan(root=None, *, blocked=None)` and `scan_all(*,
  blocked=None)` resolve the snapshot once at entry and hand the resolved
  set down to `_scan_dir(path, root, depth, blocked)`, which drops a
  candidate *before* constructing its `ImageEntry` (section 2.2). The check
  is `entry not in blocked` — a plain set membership on a `Path`, not
  `blacklist.is_blocked()`, because the enclosing loop runs once per file
  in the archive and the snapshot is already in hand.

  Why plain membership is *correct* here and not merely fast: `_scan_dir`
  skips every symlink (`not entry.is_symlink()` guards both the directory
  and the file branch) and `scan()` resolves its root, so every path the
  scanner builds is an absolute, symlink-free, fully resolved path — the
  same identity `_normalize()` produces and the same one the `image`/`thumb`
  endpoints compute with `Path(path_str).resolve()`. Hiding an image through
  a symlinked alias therefore stores the real file's path, and the scanner,
  which only ever walks to the real file, excludes it. That agreement is an
  invariant worth a test rather than a comment, because it is the one thing
  that would break silently if the scanner ever started following symlinks.

  Emergent, intended, and worth pinning down: a leaf album whose images are
  all blocked becomes an empty leaf `Album`. `image_count()` returns 0,
  `first_leaf()` skips past it (it tests `if self.images`), and Browse
  renders it with the existing empty-album path — exactly the "follows the
  existing empty-album rendering behavior" section 2.2 asks for, with no
  new code.

  **Exclusion statistics come from the scan, not from the store.** `imh
  thumb` and `imh embed` must report how many images they skipped, and no
  arithmetic on `len(blocked)` can answer that: the store legitimately holds
  entries for missing files, for other roots, for directories outside the
  requested `DIR`, and for images the scanner would never have listed. The
  count has to be taken where the discard happens. `Album` gains
  `hidden_images: int = 0` and a recursive `hidden_count()` beside
  `image_count()`; `_scan_dir` increments a local counter each time it drops
  a candidate and assigns it to the album **in the leaf branch only**,
  exactly where it assigns `album.images`. The leaf-only placement is what
  keeps the number honest: `_scan_dir` already discards the images it
  collected in a directory that also has subdirectories ("interior node —
  images silently ignored"), so a blocked image there was never going to be
  processed and must not be reported as excluded. The counter is bumped
  before the `ImageEntry` is built, so a blocked file still costs no
  `stat()`, and `scan_all()`'s virtual album aggregates its roots through
  the same recursion `image_count()` uses. Adding a defaulted field to a
  dataclass keeps every existing `Album` construction site working
  unchanged.

- **2c — thumbnailer.** `get_or_create(entry, long_edge=200, *,
  blocked=None)` raises `BlockedImageError` before computing the thumbnail
  path, so no file is created and no source image is opened. This is
  section 2.2's "explicit-path library entry points that bypass scanning
  must call `is_blocked` themselves": Django builds an `ImageEntry` by hand
  from a query parameter and never goes through the scanner, so the
  scanner-level filter does not cover it. `prewarm(entries, long_edge=200,
  *, blocked=None)` filters its list instead of raising, since it is a bulk
  convenience; note that it currently has no callers anywhere in the repo,
  and gets the parameter only so it cannot become a hole later.

  **Canonicalize at the direct-entry boundary.** `ImageEntry` is a plain
  dataclass that accepts any `Path`, so `entry.path` carries none of the
  guarantees a scanner-built path does, and `blocked` membership is a
  literal comparison by design. A caller handing `get_or_create` an in-root
  symlink, or a path containing `..`, would therefore miss the match and
  thumbnail a hidden image — the scanner's canonicality argument covers the
  scanner, not this entry point. `get_or_create` resolves `entry.path` once
  on entry and uses the resolved path for **both** the membership check and
  the `sha256` thumbnail digest. Using it for the digest too heads off the
  second bug the first fix would otherwise introduce, where an alias and its
  target produce two cache files for one image. Scanner-produced paths are
  already resolved, so no existing thumbnail's digest changes and no cache
  is invalidated; the cost is one path resolution per thumbnail, against
  opening and decoding a JPEG.

  The rule generalizes: canonicalize at every public entry point that
  accepts a caller-supplied path — `get_or_create` and `find_similar`'s
  `path` argument in this step, the endpoints in Step 3 (which already
  resolve) — and keep the bare membership test inside `_scan_dir`, which
  builds its own canonical paths and runs once per file in the archive.
  `is_blocked()` itself stays the plain lookup Step 1 settled on.

- **2c — purge.** Three changes, only the first of which is about the
  blacklist:

  1. **Blocked images become non-live, for free.** `purge` derives
     `live_paths`/`live_hashes` from `scan()`, so once the scan filters,
     a blocked image's thumbnails and `Images` row are already treated as
     stale and removed, and its `ClusterMembership` rows go with them
     through the existing cascade — precisely section 1.2's "its
     thumbnails, database row, and cluster memberships may be removed, but
     its source file must never be removed." No new deletion code is
     written, which is the strongest available argument that no new
     deletion risk is introduced: every write `purge` performs is still
     confined to `cache_dir`. `purge` passes an explicit snapshot into
     `scan()` for each root so that a multi-root purge cannot see two
     different blacklists.
  2. **Collapse clusters with fewer than two members.** Section 2.3
     requires it and the current code does not do it: it deletes only
     clusters with *zero* remaining memberships (`DELETE FROM Clusters
     WHERE id NOT IN (SELECT DISTINCT cluster_id FROM ClusterMembership)`),
     so hiding one of a two-image cluster leaves a one-member cluster in
     the database. The viewer hides those (`compare` filters
     `len(members) > 1`) but `imh report` does not. Add a step that deletes
     the memberships of, and then the rows for, clusters with exactly one
     remaining member, before the existing empty-cluster sweep.
  3. **Scope `purge DIR` (pre-existing defect, fixed here).** Today
     `purge(root=DIR)` builds its live set from `DIR` alone and then sweeps
     *every* thumbnail and *every* `Images` row, so `imh purge
     /archive/2024` destroys the thumbnails and database rows of every
     image outside `2024`. This is not blacklist-related, but Step 2's
     tests must assert an exact removal set, and encoding the current
     behavior as expected would bake the bug into the suite. Fix:
     - the `Images` sweep only considers rows whose path is under one of
       the scanned roots (`Path(r['path']).is_relative_to(scanned_root)`);
     - the thumbnail sweep runs **only when no `DIR` is given**. A
       thumbnail filename is `sha256(str(path))`, which cannot be mapped
       back to a path, so thumbnails cannot be scoped to a subtree; with
       `DIR` given the sweep is skipped and reported as skipped rather than
       run against a partial keep-set. Purging thumbnails remains available
       by running `imh purge` with no `DIR`.

     This changes documented CLI behavior, so Step 4's specification table
     gains `imh purge DIR` scoping.
  4. **Return type.** These additions make the current 4-tuple unreadable,
     so `purge` returns a small `PurgeResult` dataclass
     (`thumbs_removed`, `thumb_errors`, `thumbs_skipped: bool`,
     `db_removed`, `db_errors`, `clusters_collapsed`). `cmd_purge` is the
     only caller in this repo (`djview` never calls it), so the change is
     contained; `imh purge`'s summary gains a collapsed-cluster line and,
     when `DIR` was given, an explicit "thumbnail sweep skipped" line.

- **2d — embedder.** `embed_images(root, conn, *, blocked=None, ...)`
  passes the snapshot into its `scan(root)` call, so blocked images never
  enter `all_entries` and no embedding is created or refreshed for them.
  One accounting subtlety: `skipped` is computed as `len(all_entries) -
  len(todo)` and means "already up to date", so blocked images must not be
  folded into it — they are absent from `all_entries` entirely. Return
  `(processed, skipped, excluded)`, where `excluded` is the scanned album's
  `hidden_count()` from 2b rather than anything derived from the store, so
  `imh embed` can report the exclusion
  honestly instead of silently processing fewer images than the archive
  contains. This is the one place Step 2 cannot stay entirely out of
  `djview`: `embed_stream` unpacks the result as `p, s = embed_images(...)`,
  which a 3-tuple breaks, and no return shape avoids that (a `namedtuple`
  still fails to unpack into two names). Update that single call site here
  rather than deferring the third field to Step 3 — a one-line edit is a
  smaller breach of the step boundary than shipping a step that leaves
  `manage.py test` failing.

  `find_similar(conn, path, model, *, n=8, blocked=None)` resolves `path`
  first, per the canonicalization rule in 2c — the `Images` table stores
  resolved paths, so an unresolved alias would silently match neither the
  target row nor the blocked set. It then drops blocked
  neighbors, and returns `(None, [])` when the *target itself* is blocked —
  the same shape it already returns when the target has no embedding, which
  the Similar view already handles. Step 3's view checks `is_blocked`
  explicitly before calling, so it can render the "Hidden" notice section
  1.3 asks for; the library's job is only to never leak a blocked image
  into a result set.

  `find_semantic(conn, query, *, scope=None, n=24, weights_dir=None,
  blocked=None)` filters `rows` **before** loading the CLIP text model.
  Ordering matters: the existing `if not rows: return [], 0` early return
  then also covers "every candidate is blocked", so an all-hidden scope
  costs no model load at all.

- **2e — database helpers and clusterer.** `get_clusters`,
  `get_cluster_member_rows`, and `get_cluster_members` gain `blocked=None`
  and drop matching rows; `clusterer._load_embeddings` drops blocked rows
  before building the similarity matrix, so `cluster_images` never writes a
  cluster containing a hidden image (and, since it already discards
  components of size 1, never writes an undersized one either).

  **Filter in Python, never in SQL.** Every one of these filters is
  `Path(row['path']) not in blocked` applied to fetched rows, not a `NOT IN
  (?, ?, …)` clause. Three reasons, in order of importance: a large
  blacklist would exceed SQLite's bound-parameter limit and fail at some
  archive-dependent size rather than in testing; the comparison must be the
  same `Path`-identity comparison `is_blocked` uses, and SQL would compare
  raw strings under SQLite's collation instead; and it keeps exactly one
  matching implementation, as section 1.2 requires. The cost is that
  `cluster_images` still reads blocked rows' embedding blobs before
  discarding them — measured in a few hundred bytes per hidden image, and
  paid only by `cluster`/Compare.

  **Where the "fewer than two visible members" rule lives:** in the
  consumers, not in the `db` helpers. The helpers drop blocked *members*
  only; `cmd_report` and (already) `compare` drop the resulting undersized
  clusters. Putting it in `get_cluster_members` would make the function
  return "no such cluster" and "cluster you may not usefully see" as the
  same empty list, and `cluster_detail` needs to tell those apart.

  `cleanup_missing_members` is deliberately **unchanged**. It deletes rows
  for files that no longer exist on disk; a blocked file does exist, and
  conflating the two would make a plain `GET` of a cluster page delete
  database rows for images the user only hid — reversible only by
  re-embedding, and surprising as a side effect of viewing. Removing
  blocked derived state is `purge`'s job, on the operator's schedule, which
  is also what section 1.6's "cleanup failure leaves the entry blocked and
  can be retried with `imh purge`" assumes.

- **2f — existing `imh` commands.** Each command loads its snapshot
  explicitly at the top, through one helper, so that a corrupt store
  produces a uniform message and stops the command *before* it does any
  work (section 1.6: "report the error and exit nonzero instead of
  processing every image") rather than surfacing from inside a recursive
  scan:
  ```python
  def _blocked_snapshot(prog: str, command: str) -> frozenset[Path]:
      from imhandler import blacklist
      try:
          return blacklist.load_if_configured()
      except blacklist.BlacklistError as exc:
          print(f'{prog} {command}: {exc}', file=sys.stderr)
          sys.exit(1)
  ```
  It is called after each command's existing configuration validation and
  before any scan. The scanner's loading default remains the safety net for
  every *other* caller; the CLI does not rely on it.

  | Command | Change |
  |---|---|
  | `list`, `ls` | `scan(root, blocked=snap)`; blocked paths absent from output and from `--count` |
  | `thumb` | one snapshot for all `DIR`s; hidden count added to the summary and the `--dry-run` line |
  | `embed` | snapshot passed to every `embed_images` call; `excluded` reported |
  | `cluster` | snapshot passed to `cluster_images` |
  | `report` | blocked members omitted; clusters with fewer than two visible members omitted |
  | `purge` | as above; collapsed-cluster and skipped-sweep lines added |
  | `blacklist export` | new; see 2g |

  Exclusion counts go **only** into existing human-readable summary lines.
  `imh list` prints nothing but paths, because its output is script input;
  a "3 hidden" line on stdout would corrupt every consumer.

- **2g — `imh blacklist export`.** A nested subparser (`blacklist` →
  `export`) rather than a flat `blacklist-export`, leaving room for a
  future `blacklist list`/`add`/`remove` without a second top-level name;
  `imh blacklist` with no subcommand prints its usage and exits nonzero.
  `main()`'s hand-written help block gains a `blacklist` line, and the
  `match args.command` dispatch a `'blacklist'` case that switches on
  `args.blacklist_command`.

  ```sh
  imh blacklist export [-o FILE] [--format paths|paths0|json]
  ```

  - **Testability shape:** `cmd_blacklist_export` resolves the destination
    to a binary stream and delegates to a pure
    `_write_export(paths: list[str], fmt: str, stream: BinaryIO) -> None`.
    That split exists because `paths0` writes NUL bytes and must go to
    `sys.stdout.buffer`, which `contextlib.redirect_stdout(io.StringIO())`
    does not provide; with the split, format behavior is tested against a
    `BytesIO` and only the wiring needs the CLI.
  - **Ordering** is re-derived as `sorted(str(p) for p in load())`, the
    identical operation `_write_atomic` uses when writing the store, so the
    output order matches the store's `paths` array exactly (section 2.5)
    and never depends on `frozenset` iteration order.
  - **Bytes, not text.** A filename can contain bytes that are not valid
    UTF-8; Python surfaces those as lone surrogates via `surrogateescape`,
    and `str(p).encode('utf-8')` raises `UnicodeEncodeError` on them. Such
    a path can genuinely reach the store — `_validate_stored_entry` has no
    reason to reject it, and `json.dump`'s default `ensure_ascii=True`
    writes it as a `\udcXX` escape that `json.load` reads back unchanged.
    So `paths` and `paths0` emit `os.fsencode(s)`, which reproduces the
    original bytes exactly, and file destinations open in `'wb'`. `json`
    writes text with `encoding='utf-8'` and default `ensure_ascii=True`,
    matching the store's own encoding.
  - **`paths` rejects newline *and* carriage return**, before writing any
    output, listing every offending entry. Section 2.5 requires the
    newline check; CR is added for the same reason it gives — Python's
    `splitlines()`, CRLF-aware readers, and `read` in most shells all treat
    CR as a break, and in a terminal a CR overwrites the line, hiding part
    of the path from the human review that this format also exists for.
    `paths0` and `json` accept both characters and round-trip them exactly,
    so nothing becomes unexportable. Step 4's specification update carries
    this widening.
  - **`--format json` re-derives** the document from `load()` instead of
    copying the store file, so a corrupt store fails the export rather than
    being propagated. Because the derivation is identical to
    `_write_atomic`'s, exporting a store written by `add()` is byte-for-byte
    identical to that store — a cheap, sharp test.
  - **Destination safety.** `-o FILE` is refused when the resolved
    destination lies under any configured image root, when it is
    `blacklist.store_path()` or the lock file, and when it is a bare `-`
    (refused with a message pointing at the no-`-o` form rather than
    creating a file literally named `-`). The image-root rejection is the
    one that matters most: without it, `imh blacklist export -o
    /archive/photo.jpg` overwrites an archive image with export data —
    exactly the outcome sections 1.1 and 1.7 exist to prevent, reached
    through the only command in the feature that writes a file whose name
    the user chooses. Containment uses `is_relative_to` against
    `cache.configured_image_roots()`, the non-stat'ing helper, so an
    unmounted root still protects its subtree; if no root is configured at
    all that check is skipped, since there is then no archive for imhandler
    to protect. Refusing such a destination also keeps the `mkstemp` temp
    file out of the archive, honoring section 1.5's "never write into a
    source-image directory". Writing uses `tempfile.mkstemp` in the
    *destination's* parent directory plus `os.replace`, so the replacement
    is atomic and no `EXDEV` case arises. The result keeps `mkstemp`'s
    `0600`: the export enumerates the paths a user chose to hide, and it is
    derived from a `0600` store, so it should not be created world-readable
    by umask.
  - **Read-only** in the strong sense: no lock is taken (readers never need
    one — `os.replace` means a reader sees the old file or the new one,
    never a partial one), `cache_dir` is not created, the store's bytes and
    mtime are untouched, and no other program is invoked.
  - **Exit codes.** Corrupt store, newline/CR in `paths`, destination
    equals the store, and an unwritable destination each print to stderr
    and exit 1, having written nothing. An empty blacklist is exit 0 with
    empty output (`paths`, `paths0`) or `{"version": 1, "paths": []}`
    (`json`).

- **2i — media caching headers (the one Django change with no blacklist
  dependency).** Apply section 2.4's caching rules to `image` and `thumb`
  now, ahead of the endpoints that need them: `private, no-cache` plus a
  `Last-Modified` validator in place of today's `max-age=3600`, and
  `no-store` on 404s. This is a defect in code already running — an
  unvalidated hour-long `max-age` on a media endpoint is wrong on its own
  terms — and it currently has no victim only because nothing can be hidden
  yet. Landing it here rather than in Step 3 is a scheduling decision, and
  the schedule is the whole point: a cache entry created under the old
  header cannot be revoked by any later deploy, so if the header change and
  the Hide button ship together the operator has to hold Hide back for an
  hour by hand to get a clean boundary. Shipping it a step early lets the
  old entries expire on their own, and by Step 3 the constraint has
  dissolved. The cost is paid early too: thumbnails become conditional
  requests before the feature that requires it exists. That is the right
  side to err on — the alternative trades a measurable, bounded latency
  cost for an unbounded correctness window.

  **Enforcement runs before conditional-response handling.** The blacklist
  check must be the first thing either endpoint does after canonical path
  validation, and the `If-Modified-Since` comparison must happen after it,
  inside the view body. Getting this backwards reopens the bypass through
  the revalidation path the headers just added: a client holding a stale
  validator revalidates, the view answers `304 Not Modified` because the
  file's mtime has not changed — hiding an image does not touch it, by
  design — and the browser renders its cached copy of an image that is now
  blocked. Concretely, this rules out Django's `@condition` /
  `@last_modified` decorators on these two views: they answer before the
  view body runs, so a blacklist check written inside the body would never
  execute. Compare the validator by hand in the view, after the check.
  A hidden image must yield `404` on every request, conditional or not.

  Because this sub-step depends on nothing else in Step 2, it can be
  implemented, tested, and reviewed first, and its tests belong with the
  Django tests rather than the two new library test files:

  - request an image and a thumbnail, assert `Cache-Control: private,
    no-cache` and a `Last-Modified` header;
  - re-request with the returned `If-Modified-Since` and assert `304`;
  - **load the media, hide it, then re-request with the same
    `If-Modified-Since` the first response supplied, and assert `404` —
    never `304`.** This is the ordering test, and it is the one that fails
    if the conditional handling is hoisted above the blacklist check or
    moved into a decorator. It needs something blockable, so it lands with
    Step 3's `hide` endpoint, or earlier against a hand-written store
    entry if 2i is implemented first;
  - assert the blocked `404` carries `Cache-Control: no-store` (Step 3,
    once 404s exist).

- **Snapshot consistency.** Each operation reads one snapshot and no reader
  takes the lock. A hide that lands mid-run is not seen until the next run;
  a restore that lands just after `purge` took its snapshot may cost the
  restored image its derived data, which the next `thumb`/`embed`/`cluster`
  rebuilds — the behavior section 1.3 already specifies for restore. The
  only ordering that would matter is a hide racing a `purge` that is about
  to *keep* the image, and that resolves in the safe direction: the image
  stays visible in derived data until the next purge, while every read path
  already blocks it.

- **Intermediate state (read before deploying Step 2 alone).** Because the
  default is to load, Step 2 changes Django's behavior without editing
  `djview`: `browse` (`scan_all`), `compare` (`cluster_images` plus
  `get_cluster_member_rows`), `cluster_detail` (`get_cluster_members`),
  `similar` (`find_similar`), `semantic` (`find_semantic`) and
  `embed_stream` (`embed_images`) all begin excluding blocked images
  immediately. `thumb` even returns the right status by accident — it wraps
  `get_or_create` in `except Exception: raise Http404(...)`, so
  `BlockedImageError` becomes a 404. But the `image` endpoint still serves
  a blocked file to anyone with its URL, and there is still no way to hide
  anything from the UI. **Step 2 is therefore not a policy boundary and
  must not be presented to users as "hiding works"**; only Step 3 closes
  the `image` endpoint. The caching side is already handled by 2i, which is
  exactly why it is in this step: the accidental `thumb` 404 would
  otherwise be invisible to any client holding a cached copy. Two rough edges are accepted for the interval
  between the steps and fixed in Step 3: a cluster whose members are all
  blocked yields `cluster_detail`'s existing "Cluster not found" 404, and a
  corrupt store surfaces as a Django 500 rather than a rendered error page.
  Both fail closed.

**Tests.** A new shared fixture module `tests/_imhandler_fixture.py`
(private-helper naming already used by `tests/_hty7_install_check.py`)
provides: a temp two-root archive builder that writes real, tiny JPEGs with
PIL so thumbnailing actually runs, the `mock.patch.object(appconfig, …)`
setup Step 1's tests established, and `tree_fingerprint(root)` returning
`{relpath: (size, st_mtime_ns, st_mode, sha256)}`. Every test that runs a
mutating command asserts the archive fingerprint is unchanged — that single
helper is how "the source archive is unchanged" (section 2.3) is proven
rather than asserted in prose.

`tests/test_imhandler_enforcement.py`:

- **scanner:** a blocked image is absent while its siblings remain;
  `scan_all()` filters across both roots without disturbing `rel_path`
  prefixes; `scan(root, blocked=frozenset())` returns everything even
  though the store has entries, with `mock.patch.object(blacklist, 'load')`
  set to raise, proving an explicit snapshot never re-reads disk; a
  deep tree scanned with `blocked=None` calls `load` **exactly once**
  (counting mock), proving the snapshot threads through the recursion; a
  corrupt store makes `scan()` raise `BlacklistError` rather than return
  unfiltered results; an unconfigured `cache_dir` lets `scan(DIR)` succeed
  unfiltered (the section 2.5 exception, pinned so it cannot be widened by
  accident).
- **scanner identity:** create a real file plus an in-root symlink to it,
  `add()` the symlink path, and assert the scan of the real directory
  excludes the file — the alias/target agreement argued above.
- **empty leaf:** a leaf whose every image is blocked has `image_count() ==
  0` and is skipped by `first_leaf()`.
- **scan statistics:** `hidden_count()` equals the number of blocked images
  actually discarded in the scanned scope — asserted against a tree that
  makes every wrong answer distinguishable: a blocked image in a leaf
  (counted), a blocked image in an *interior* directory that also has
  subdirectories (**not** counted, because the scanner discards interior
  images anyway), a blocked entry for a file that does not exist, a blocked
  entry under the other root, and a blocked entry outside the scanned
  `DIR` (none counted). A count taken from `len(blocked)` fails every one
  of the last four.
- **thumbnailer:** `get_or_create` on a blocked entry raises
  `BlockedImageError` and leaves `thumbs_dir()` empty; with an explicit
  snapshot it does not call `load`; `prewarm` skips blocked entries and
  generates the rest.
- **thumbnailer canonicalization:** a hand-built `ImageEntry` whose `path`
  is an in-root symlink to a blocked file, and another whose path reaches a
  blocked file through a `..` segment, both raise `BlockedImageError` and
  create no file — the two bypasses a literal membership test would let
  through. A companion test proves the digest did not move: the thumbnail
  produced for a scanner-built (already canonical) entry lands at the same
  path it did before this change, so no existing cache is invalidated, and
  an alias and its target resolve to one shared thumbnail file rather than
  two.
- **purge:** a blocked image's thumbnail and `Images` row are removed while
  the archive fingerprint is unchanged; its `ClusterMembership` rows go
  with it; a cluster left with one member is collapsed (membership *and*
  `Clusters` row deleted) while a cluster left with two survives; `--dry-run`
  reports identical counts and removes nothing; `purge(DIR)` deletes only
  rows under `DIR` (a stale row under the other root survives) and reports
  the thumbnail sweep as skipped; purging a root whose images are *all*
  blocked still leaves every source file in place.
- **embedder:** a blocked image gets no `Images` row; when every candidate
  is blocked, `embed_images` returns `(0, 0, n)` without loading a model
  (`load_clip_model`/`load_sscd_model` patched to raise); `find_similar`
  omits blocked neighbors and returns `(None, [])` for a blocked target;
  `find_semantic` omits blocked rows and, when all are blocked, returns
  `([], 0)` with `_load_clip_text_model` patched to raise.
- **db/clusterer:** `get_cluster_member_rows`/`get_cluster_members` omit
  blocked members *and still return a one-member cluster*, pinning that the
  undersized rule lives in the consumer; `cluster_images` writes no cluster
  when one of a near-identical pair is blocked; `cleanup_missing_members`
  does not delete a blocked-but-present file's row.

`tests/test_imhandler_cli_blacklist.py` (calling `cmd_*` directly with an
`argparse.Namespace`, as `main()` only adds argument parsing):

- `imh list` output and `--count` exclude blocked paths and stdout carries
  nothing else; `imh thumb` generates no blocked thumbnail and reports the
  hidden count; `imh embed`, `imh cluster`, `imh report` exclude blocked
  images, and `report` drops clusters with fewer than two visible members.
- A corrupt store makes every command exit 1 with `imh CMD: …` on stderr
  **and do no work** — asserted by an empty `thumbs_dir()` and an unchanged
  database, not just by the exit code.
- `imh list DIR` with `cache_dir` unconfigured exits 0 and filters nothing.
- **export:** all three formats to stdout and to `-o FILE`; order identical
  to the store's `paths` array; `--format json` byte-identical to a store
  written by `add()`; empty store; corrupt store exits 1 with the
  destination never created; a stored path containing `\n` and one
  containing `\r` each make `--format paths` exit 1 with **no** output
  written while `paths0` and `json` round-trip them byte-exactly; a
  filename containing an undecodable byte (`b'bad\xff.jpg'`) survives
  `add()` → `load()` → export and is compared with `os.fsencode`;
  adversarial names (spaces, single and double quotes, backslash,
  `$(rm -rf /)`, a leading `-`, `;`) appear as literal bytes with no
  quoting or escaping, and no output begins with a shebang or is created
  with an execute bit; `-o` pointing at a path under a configured image root
  exits 1 with nothing written and the archive fingerprint unchanged —
  including the case where the named file already exists, which is the
  overwrite this rejection exists to stop, and the case where the root is
  configured but not mounted; `-o` pointing at the store or the lock file
  exits 1 with the store unmodified; `-o -` is refused; after every export the
  store's bytes and `st_mtime_ns` are unchanged and `cache_dir` contains no
  `.blacklist-*` temp file and no `.blacklist.lock` (export must not even
  create the lock); an unwritable destination directory exits 1 leaving no
  temp file behind.

**Verification:**
```sh
python3 -m py_compile lib/imhandler/*.py lib/imhandler/cli/*.py lib/imhandler/djview/*.py bin/imh
python3 -m unittest tests.test_imhandler_enforcement -v
python3 -m unittest tests.test_imhandler_cli_blacklist -v
python3 -m unittest discover -s tests -t .
cd llime && ./manage.py check && ./manage.py test
```
`manage.py check`/`test` *are* needed this time, unlike Step 1: the shared
signatures Django imports change, and two `djview` sites are edited here —
`embed_stream`'s `p, s = embed_images(...)` unpack (2d) and the media
caching headers (2i).

### Step 3 — Django replacement

Add `_can_manage_blacklist(request)` and the three endpoints; close the
`image` endpoint and make `thumb`'s block an explicit pre-check rather than a
caught exception (the caching half of section 2.4 already landed in Step
2i, so Step 3 only adds `no-store` to the new blocked 404s); replace Mark and deletion-bar UI with Hide; add the Hidden
images page; and remove old exports, routes, context, session state, and
shell-script generation. Page and query filtering is already done by Step 2's
library changes — Step 3 only needs the surfaces Step 2 could not reach:
authorization, mutation endpoints, the `image` endpoint, a rendered error page
for a corrupt store, and the `cluster_detail` case where every member is
hidden. Step 3 is also where Step 1's known `remove()` limitation (an entry
outside the currently configured roots cannot be removed) becomes a product
question, since restore is the operation it blocks.

**Why this order:** every sub-step below reads from real, current code
(`lib/imhandler/djview/__init__.py`, `urls.py`, `views.py`, and the
`cluster_detail.html`/`similar.html` templates), so the design fixes exact
line references rather than describing the shape of a rewrite. Sub-steps run
authorization and the shared library addition first, since the endpoints and
the templates that call them both depend on those; the two media endpoints
(`image`/`thumb`) are independent of the mutation endpoints and can land in
parallel; template/UI work comes last because it is the only part that needs
the endpoints to already exist.

**Sub-steps,** each separately compilable and testable, in dependency order:
(3a) one addition and one latent-bug fix to `blacklist.py`; (3b)
authorization; (3c) the three endpoints and their routes; (3d) `image`/`thumb`
enforcement; (3e) `cluster_detail`; (3f) `similar`; (3g) shared modal, Hidden
images template, and nav; (3h) removing the old Mark/deletion-list code; (3i)
tests.

**Design.**

- **3a — one addition to `blacklist.py` (`remove_stored`) and one
  latent-bug fix (normalizing filesystem errors to `BlacklistError`).**
  First, `remove_stored`. This is the
  concrete resolution of Step 1's known `remove()` gap, not just a
  restatement of it. `remove()` calls `_normalize()`, which re-validates
  root containment against the *currently* configured `image_roots` — right
  for `add()`, since hiding an unreachable path makes no sense, but wrong
  for restore: an entry can legitimately be listed and blocked (`load()`
  and `is_blocked()` both still see it) while no longer validating under
  `_normalize()`, because its root was reconfigured away or is presently
  offline. A restore endpoint that reused `remove()` would 400 on exactly
  the entries an operator most needs to restore.

  The fix is not to relax `_normalize()` — that would reopen add-time
  validation for a case it exists to close — but to recognize that restore
  never needs it in the first place. The Hidden images page's only source
  of input is `load()`'s own output: every string the restore form can ever
  submit is one `load()` just produced, which means it already passed
  `_validate_stored_entry()` (Step 1's *structural*, config-independent
  canonical-form check: absolute, no NUL, canonical `normpath`, no
  non-canonical leading `//`, correct suffix — everything except live root
  containment). Restore only needs to confirm the submitted string is
  still exactly a member of the current set; it never needs to ask whether
  that string is a path someone could *newly* add today.

  ```python
  def remove_stored(raw: str) -> bool:
      """Remove an entry by its exact stored string, bypassing _normalize().

      For the restore endpoint, whose only inputs are strings load() itself
      just returned. Skips _normalize()'s live root-containment check --
      restore only ever narrows the blocked set, so re-validating an entry
      against the *current* configured roots would resurrect the Step 1
      remove() gap: an entry under a root that has since been reconfigured
      away, or is presently offline, would become unrestorable even though
      load() and is_blocked() both still see it correctly. _validate_stored_entry
      still rejects a string that could never have come out of load() in
      the first place (malformed, non-canonical, wrong suffix) -- it just
      does so without touching cache.configured_image_roots(), so a
      never-valid string returns False instead of raising, and a
      genuinely corrupt store still raises BlacklistError via the load()
      inside _update(), which this does not swallow.
      """
      try:
          candidate = _validate_stored_entry(raw)
      except BlacklistError:
          return False
      return _update(candidate, adding=False)
  ```

  A malformed or already-absent `raw` is a silent no-op (matching `remove()`'s
  own idempotency contract), never an error; only a corrupt on-disk store
  (raised from `load()` inside `_update()`) propagates. `remove_stored` never
  calls the filesystem beyond `blacklist.json` itself, so a hand-crafted POST
  that doesn't match any stored entry can do nothing worse than a no-op —
  there is no injection surface here worth defending against beyond that.

  **The same sub-step also fixes a latent Step 1/2 bug this design exposes:
  `load()` and `_update()` only convert specific, anticipated failures into
  `BlacklistError`** (`FileNotFoundError`, `json.JSONDecodeError`, the
  structural checks) **and let any other `OSError` — a permission error
  opening `blacklist.json` or the lock file, a read-only `cache_dir` — pass
  through unconverted.** Every caller in this document, from Step 2's
  `imh`'s `_blocked_snapshot` (which only catches `blacklist.BlacklistError`
  before printing a clean message and exiting nonzero) through this step's
  own `image`/`thumb`/`hidden_images`, is written to expect `BlacklistError`
  as the one exception type that means "the blacklist could not be
  consulted." A raw `PermissionError` defeats that contract at every one of
  those sites simultaneously, producing an unhandled traceback (a raw
  Django 500, or an unfiltered CLI stack trace) instead of the fail-closed
  behavior section 1.6 specifies — worth fixing once, here, at the source,
  rather than adding a matching `except OSError` at every call site this
  step and Step 2 already wrote. `load()`:
  ```python
  def load() -> frozenset[Path]:
      path = _store_path()
      try:
          with open(path, 'r', encoding='utf-8') as fh:
              raw_text = fh.read()
      except FileNotFoundError:
          return frozenset()
      except OSError as exc:
          raise BlacklistError(f'blacklist store cannot be read: {path}: {exc}') from exc
      except UnicodeDecodeError as exc:
          raise BlacklistError(f'blacklist store is not valid UTF-8: {path}') from exc
      ...
  ```
  (`FileNotFoundError` is itself an `OSError` subclass, so it must stay the
  first, more specific `except`.)

  **Every filesystem call on the write path needs the same treatment, not
  only the ones already anticipated** — this is worth being exhaustive
  about, since the entire point of this fix is "one exception type, no
  exceptions to that rule." `_write_atomic()`'s cleanup-on-failure path is
  itself a filesystem call and can itself fail:
  ```python
  def _write_atomic(blocked: AbstractSet[Path]) -> None:
      cache_dir = cache.cache_root()
      doc = {'version': _VERSION, 'paths': sorted(str(p) for p in blocked)}
      try:
          cache_dir.mkdir(parents=True, exist_ok=True)
          fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix='.blacklist-')
      except OSError as exc:
          raise BlacklistError(f'cannot create blacklist temp file: {exc}') from exc
      try:
          try:
              fh = os.fdopen(fd, 'w', encoding='utf-8')
          except OSError:
              # mkstemp() returned a raw descriptor, and fdopen() did not
              # take ownership because it failed. Close it before the
              # outer handler unlinks the temporary pathname.
              try:
                  os.close(fd)
              except OSError:
                  pass
              raise
          with fh:
              json.dump(doc, fh)
              fh.flush()
              os.fsync(fh.fileno())
          os.replace(tmp_name, _store_path())
      except OSError as exc:
          try:
              os.unlink(tmp_name)
          except OSError:
              # Not narrowed to FileNotFoundError: *any* failure to remove
              # the temp file (permissions, a second concurrent cleanup,
              # whatever) must be swallowed here, not raised. The exception
              # this function is about to raise is the one that matters --
              # the original write failure -- and letting an unrelated
              # unlink failure escape instead would replace that diagnostic
              # with a misleading one about a leftover temp file, not fix
              # anything the caller could act on differently.
              pass
          raise BlacklistError(f'cannot write blacklist store: {exc}') from exc
  ```
  `_update()`'s lock handling needs the equivalent for both ends of the
  lock, not just acquiring the lock file itself. `cache_dir.mkdir(...)` and
  `open(lock_path, 'a+')` becoming `BlacklistError` was already specified
  above; `fcntl.flock()` can independently raise `OSError` on *both* the
  exclusive-lock acquisition and the unlock, and the two need different
  treatment, not the same `except OSError: raise BlacklistError` reflex
  applied twice: a failed *acquisition* means the critical section never
  ran at all, so it must raise (the caller's `add()`/`remove()` genuinely
  did nothing and needs to know that); a failed *release*, in contrast, is
  reached only after `_write_atomic()` has already returned successfully —
  the operation is done — and closing `lock_fh` immediately afterward
  releases any OS-held `flock()` on that descriptor regardless of whether
  the explicit `LOCK_UN` call itself succeeded, so raising from a failed
  unlock would report a real success as a failure and give the caller
  nothing correct to retry:
  ```python
  def _update(normalized: Path, *, adding: bool) -> bool:
      cache_dir = cache.cache_root()
      lock_path = _lock_path()
      try:
          cache_dir.mkdir(parents=True, exist_ok=True)
          lock_fh = open(lock_path, 'a+')
      except OSError as exc:
          raise BlacklistError(f'cannot open blacklist lock file: {exc}') from exc

      try:
          try:
              fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
          except OSError as exc:
              raise BlacklistError(f'cannot lock blacklist store: {exc}') from exc

          current = load()
          if adding:
              if normalized in current:
                  return False
              updated = current | {normalized}
          else:
              if normalized not in current:
                  return False
              updated = current - {normalized}
          _write_atomic(updated)
          return True
      finally:
          # Best-effort only, deliberately not raising BlacklistError here:
          # by this point the operation above has already succeeded or
          # already raised on its own terms, and closing lock_fh releases
          # the OS-held flock() regardless of whether LOCK_UN itself
          # succeeds -- so a failure in either of these two calls must be
          # ignored, never allowed to mask or overwrite that outcome.
          try:
              fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
          except OSError:
              pass
          try:
              lock_fh.close()
          except OSError:
              pass
  ```
  `BlacklistError`'s docstring widens from "the blacklist store exists but
  is corrupt or unsupported" to "...corrupt, unsupported, or cannot be
  read/written because of a filesystem error" to match. `cache.cache_root()`
  raising plain `EnvironmentError` for an *entirely unconfigured* `cache_dir`
  is deliberately left alone — that is a distinct, already-correct condition
  (`load_if_configured()` already treats it as the one sanctioned fail-open,
  Step 2, 2a), not a store I/O failure, and must not be swallowed into
  `BlacklistError`. `tests/test_imhandler_blacklist.py` gains
  `mock.patch('builtins.open', side_effect=PermissionError(...))`-style
  cases for `load()`, `add()`/`remove()`/`remove_stored()` (lock-open
  failure), a `mock.patch('os.unlink', side_effect=PermissionError(...))`
  case that fails a write *and* its own cleanup unlink simultaneously
  (asserting `BlacklistError` from the original write failure is what
  surfaces, not the unlink's `PermissionError` — the direct regression test
  for the fix above), and a `mock.patch('fcntl.flock', side_effect=...)`
  pair covering both a failed acquisition (asserting `add()`/`remove()`
  raise `BlacklistError` and the store is unchanged) and a failed release
  (asserting `add()`/`remove()` still return their normal result and the
  store *is* correctly updated, proving the swallowed `LOCK_UN` failure
  didn't discard a real success).

- **3b — authorization.** Section 1.6 specifies "an authenticated staff
  user" by default with an `IMHANDLER_BLACKLIST_AUTHORIZER` override —
  `lib/documentview/config.py`'s `DOCUMENT_VIEWER_AUTHORIZE(request, action)`
  is the precedent already in this repo, but imhandler's blacklist has only
  one gated action (manage: hide/list/restore together, per section 1.6's
  own wording), so the hook takes just `request`:

  ```python
  # lib/imhandler/djview/__init__.py, near _get_roots()
  def _default_blacklist_authorizer(request) -> bool:
      user = getattr(request, 'user', None)
      return bool(user is not None and user.is_authenticated and user.is_staff)

  def _can_manage_blacklist(request) -> bool:
      hook = getattr(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', _default_blacklist_authorizer)
      return bool(hook(request))
  ```

  Add `from django.conf import settings` and `from imhandler import blacklist`
  to the existing top-level import block (`lib/imhandler/djview/__init__.py:20-32`);
  unlike the library modules imported function-locally throughout this file
  to dodge circular imports (`imhandler.clusterer`, `imhandler.db`,
  `imhandler.embedder`), `blacklist` has no import-cycle with `djview`, so a
  top-level import is simplest and matches `appconfig`/`cache`'s existing
  top-level treatment.

  **This default silently disables the feature for llime.**
  `llime/config/settings.py:152-154` already documents why:
  `_authorize_document_viewer` returns `True` unconditionally because "llime
  is authenticated by the web server, outside Django" — there is no real
  Django-session staff user in that deployment, so `_default_blacklist_authorizer`
  would return `False` for every request and Hide/Restore/Hidden-images would
  403 for everyone. This is not a hypothetical edge case to note for later;
  it is the actual configuration of the repo's own primary consumer, so this
  sub-step must also add, right next to `DOCUMENT_VIEWER_AUTHORIZE` in
  `llime/config/settings.py`, a **one-argument wrapper** — not a direct
  assignment. `_authorize_document_viewer(request, action)` takes two
  positional arguments; `IMHANDLER_BLACKLIST_AUTHORIZER` is called as
  `hook(request)` (one argument, per 3b's `_can_manage_blacklist` above), so
  `IMHANDLER_BLACKLIST_AUTHORIZER = _authorize_document_viewer` would raise
  `TypeError: _authorize_document_viewer() missing 1 required positional
  argument: 'action'` on the very first Hide/Restore/Hidden-images request —
  not merely fail to authorize, but crash the endpoint outright:
  ```python
  def _authorize_imhandler_blacklist(request):
      """Same reasoning as _authorize_document_viewer: llime is authenticated
      by the web server, outside Django. Wrapped rather than assigned
      directly because that hook takes (request, action) and this one is
      called with (request) alone."""
      return _authorize_document_viewer(request, 'blacklist')

  IMHANDLER_BLACKLIST_AUTHORIZER = _authorize_imhandler_blacklist
  ```
  This still reuses `_authorize_document_viewer`'s actual logic (and, via its
  docstring, its rationale) rather than duplicating an unconditional `return
  True` a second time with no explanation. `../qat/knip` runs real `django.contrib.auth`
  with no override configured today, so the staff-only default applies
  there unchanged; Step 5's rollout note should confirm knip has at least
  one staff account before Hide ships, since otherwise the feature is present
  but unreachable there for a different reason (no staff user exists) than
  in llime (the authorizer always says no).

  **Reading "authorized users can view blacklisted files" correctly.** The
  document's opening paragraph and section 1.6 both use "view"/"authorized
  users can view" language that could be misread as image/thumb making an
  exception for staff. They do not, anywhere in sections 1.2/1.6/2.4: a
  blocked image 404s unconditionally, for every requester. What section 1.3
  actually grants authorized users is visibility into the *blacklist itself*
  — the Hidden images page shows path and existence status, explicitly
  *without* thumbnails ("media endpoints deliberately block them"). No
  sub-step below adds a staff bypass to `image`/`thumb`; this is called out
  because it is exactly the kind of implicit exception a first implementation
  pass could plausibly add by accident while wiring up authorization.

- **3c — the three endpoints.** `hide_image` and `restore_image` are added
  as `@staticmethod`s (matching `mark_toggle`/`thumb`/`image`'s existing
  convention) and *must* stay static: `require_POST` (from
  `django.views.decorators.http.require_http_methods`) wraps a plain
  `def view(request, *args, **kwargs)` and reads `request` as its first
  positional argument. Applied to an unbound instance method
  (`def hide_image(self, request):`) inside the class body, the decorator
  would instead receive `self` as that first argument and check `self.method`
  — which doesn't exist — rather than `request.method`; `@staticmethod`
  strips `self` entirely before `require_POST` ever sees the function, the
  same reason `embed_cancel` (`__init__.py:839-840`) is static under its own
  `@csrf_exempt`. `hidden_images` is the one exception: it needs
  `self._ctx()`/`self._t()` for nav rendering like every other page view, so
  it stays an instance method and is not decorated with `require_POST` (a
  `GET` listing page has no method to restrict). `require_POST` is imported
  at `lib/imhandler/djview/__init__.py:25` today but never used — `hide_image`
  and `restore_image` are its first callers. CSRF protection itself is the
  Django middleware default and needs no decorator, matching
  `mark_toggle`/`deletion_list_clear` today.

  ```python
  @staticmethod
  @require_POST
  def hide_image(request):
      if not _can_manage_blacklist(request):
          return JsonResponse({'error': 'Forbidden'}, status=403)
      path = request.POST.get('path', '')
      if not path:
          return JsonResponse({'error': 'No path given'}, status=400)
      try:
          blacklist.add(path)
      except ValueError as e:
          return JsonResponse({'error': str(e)}, status=400)
      except (blacklist.BlacklistError, EnvironmentError) as e:
          return JsonResponse({'error': str(e)}, status=500)
      return JsonResponse({'ok': True})

  @staticmethod
  @require_POST
  def restore_image(request):
      if not _can_manage_blacklist(request):
          return HttpResponse(status=403)
      path = request.POST.get('path', '')
      if path:
          try:
              blacklist.remove_stored(path)
          except (blacklist.BlacklistError, EnvironmentError) as e:
              return HttpResponse(f'Cannot update the blacklist: {e}',
                                   status=500, content_type='text/plain; charset=utf-8')
      return redirect(_url('hidden_images'))

  def hidden_images(self, request):
      if not _can_manage_blacklist(request):
          return render(request, self._t('error.html'), self._ctx({
              'title': 'Hidden images', 'message': 'Not authorized.', 'detail': '',
          }), status=403)
      try:
          blocked = blacklist.load()
      except (blacklist.BlacklistError, EnvironmentError) as e:
          return render(request, self._t('error.html'), self._ctx({
              'title': 'Hidden images',
              'message': 'Cannot read the blacklist.',
              'detail': str(e),
          }), status=500)
      entries = [{'path': str(p), 'exists': p.is_file()} for p in sorted(blocked)]
      return render(request, self._t('hidden_images.html'), self._ctx({
          'title': 'Hidden images', 'entries': entries,
      }))
  ```

  `hide_image` returns `{'ok': True}` on both the changed and
  already-idempotent case (`blacklist.add`'s boolean return is not
  surfaced) — the client only needs to know the path is no longer visible,
  matching section 1.3's "adding an existing entry is idempotent." It is
  JSON because the shared modal (3g) calls it via `fetch`, exactly as
  `mark_toggle` used to be.

  **`restore_image` is deliberately *not* JSON, unlike `hide_image`.** Its
  only caller is a plain HTML `<form>` on the Hidden images page (3g) — a
  normal, non-JS form submission navigates the browser directly to whatever
  the response is, so a `JsonResponse` here would leave the user staring at
  raw `{"ok": true}` text instead of back on the Hidden images page. It
  follows the same shape `deletion_list_clear` already used
  (`__init__.py:393-398`: `return redirect(...)` after a plain POST) rather
  than the JSON shape of the two endpoints meant for `fetch`. A missing
  `path`, like `deletion_list_clear`'s tolerance of a missing `next`, is not
  an error worth surfacing — it silently redirects back with nothing
  changed, matching `remove_stored`'s own idempotent-no-op-on-absent
  contract from 3a. A genuine failure (corrupt store, unconfigured
  `cache_dir`) returns a plain-text 500 rather than a rendered `error.html`,
  since `restore_image` is a `@staticmethod` (required so `@require_POST`
  wraps a plain `(request)` signature rather than an unbound `(self,
  request)` — see 3c's opening paragraph) and so has no `self._ctx()`/`self._t()`
  available; a rare, staff-only failure page does not need the app's nav
  chrome to be useful.

  `hidden_images` (an instance method, unlike the two mutation endpoints —
  it needs `self._ctx()`/`self._t()` for nav rendering) catches
  `EnvironmentError` too (unconfigured `cache_dir`/`image_root`), the same
  pattern every other page view in this file already uses for
  `open_db()`/`_get_roots()` failures, rather than inventing a second
  convention for this one view.

  Routes (`lib/imhandler/djview/urls.py:17-19` currently hold the three
  Mark/deletion-list routes being replaced):
  ```python
  path('hide/', views.hide_image, name='hide_image'),
  path('hidden/', views.hidden_images, name='hidden_images'),
  path('restore/', views.restore_image, name='restore_image'),
  ```
  and `lib/imhandler/djview/views.py:23-25`'s three aliases become
  `hide_image = _vs.hide_image`, `hidden_images = _vs.hidden_images`,
  `restore_image = _vs.restore_image`.

- **3d — `image`/`thumb` enforcement.** `image()` (`__init__.py:691-729`)
  has no blacklist check at all today — section 2.4's "Intermediate state"
  note already flags this as the one surface Step 2 could not close. Add,
  immediately after the existing `path.is_file()` check and *before*
  `last_modified = path.stat().st_mtime` (the ordering section 2.4 requires,
  identical to `thumb`'s existing comment at `__init__.py:677-680`):
  ```python
  try:
      blocked = blacklist.load_if_configured()
  except blacklist.BlacklistError:
      return _not_found('Image not found')
  if path in blocked:
      return _not_found('Image not found')
  ```
  Both the corrupt-store and the blocked case return the identical message
  and status through `_not_found()` (section 1.6: never reveal source-file
  existence, and `_not_found()` already stamps `Cache-Control: no-store` at
  `__init__.py:152-162`, so nothing new is needed there) — a corrupt store
  fails closed exactly like a blocked entry, rather than a 500 that would
  distinguish the two cases for an unauthenticated caller.

  `thumb()` (`__init__.py:644-689`) already blocks correctly today, but only
  because its `except Exception:` at line 671 happens to also catch
  `blacklist.BlockedImageError` raised from inside `get_or_create` — the
  comment at lines 672-675 says as much. Replace the accident with an
  explicit pre-check using the same snapshot passed into `get_or_create`,
  so one `load_if_configured()` call serves both the check and the
  downstream filtering (matching Step 2's "Snapshot consistency" principle
  rather than loading twice):
  ```python
  try:
      blocked = blacklist.load_if_configured()
  except blacklist.BlacklistError:
      return _not_found('Image not found')
  if path in blocked:
      return _not_found('Image not found')
  ...
  try:
      thumb_path = get_or_create(entry, long_edge=size, blocked=blocked)
  except EnvironmentError as e:
      return _not_found(f'Cache unavailable: {e}')
  except Exception:
      return _not_found('Thumbnail generation failed')
  ```
  The remaining bare `except Exception` now only ever means "thumbnailing
  itself failed" — a decode error, a permissions problem — not "hidden",
  which is what "an explicit pre-check rather than a caught exception" in
  this step's opening paragraph means concretely: policy and implementation
  failure are no longer reported through the same catch-all.

- **3e — `cluster_detail`.** `get_cluster_members()` already defaults to
  `load_if_configured()` and drops blocked rows (Step 2, `db.py`), so a
  member appearing in this view's `rows`/`members` was never blocked to
  begin with — there is no "already hidden" state to render here, unlike
  the old Mark/Unmark toggle. Delete `marked_set` (`__init__.py:333`), the
  `'marked': row['path'] in marked_set` field (line 351), and
  `deletion_count` (line 358) from the context; delete `mark_toggle`,
  `deletion_list_download`, `deletion_list_clear` (`__init__.py:364-398`)
  entirely — nothing else in the view references them once the template
  stops calling `mark_toggle`.

  **A server-side "fewer than two visible members" check is required here,
  not only the client-side one below.** `get_cluster_members`'s own
  docstring already states the gap precisely (`db.py:125-128`): "A cluster
  whose members are all blocked returns an empty list, the same shape as
  'no such cluster' -- callers that must tell those apart check row
  existence before calling this, not after." `cluster_detail` does not do
  that today, and the *partially*-blocked case is worse than the docstring's
  fully-blocked example: take a two-member cluster where one member was
  hidden a moment ago from another tab, by the CLI, or by a different staff
  user — not through this page's own modal. `rows = get_cluster_members(conn,
  cluster_id)` (`__init__.py:315`) already excludes it, so `rows` has length
  1; `if not rows: raise Http404(...)` (line 317) does not fire, since 1 is
  truthy; `cleanup_missing_members` (line 321) is deliberately
  blacklist-blind (`db.py:158-165` — it must not treat a hidden-but-present
  file as "missing," or a plain page view would delete cluster metadata for
  an image the user only hid) and so still counts the hidden member as
  present, leaving `remaining == 2` and the `remaining <= 1` collapse (line
  322) never firing either. The page renders with exactly one visible row —
  precisely the state section 1.3 says must instead redirect to Compare, and
  this can happen on a plain `GET` with no Hide click in this browser tab at
  all, so the client-side DOM-count check in this same sub-step cannot catch
  it.

  Fix by checking `len(rows) < 2` immediately after computing `rows` (before
  running `cleanup_missing_members` at all), distinguishing "no such cluster"
  from "cluster exists but is down to 0 or 1 visible members" with one cheap
  existence query — exactly the check `get_cluster_members`'s docstring asks
  callers to do "before calling this, not after." **That single check is not
  enough by itself, though**, because `rows` is only blocked-filtered, and
  `cleanup_missing_members` can shrink the visible count *further* without
  ever touching `rows` again: take a three-member cluster with A hidden, B
  missing from disk, C visible and present. `rows` (blocked-filtered) is
  `[B, C]` — length 2, so the upfront check does not fire. `cleanup_missing_members`
  is blacklist-blind by design (`db.py:158-165`), so it counts `[A, B, C]`,
  finds only `B` missing, deletes it, and returns `remaining = 3 - 1 = 2` —
  which also does not trigger the existing `remaining <= 1` collapse. The
  page then builds `members` from `rows` minus `missing_ids` (`{B}`), leaving
  exactly `[C]`: one visible row rendered, with neither check having caught
  it. `remaining` and "visible row count" are answering two different
  questions — `remaining` is blacklist-blind on purpose, so it must stay
  that way for the *DB-collapse* decision (deleting `Clusters`/`ClusterMembership`
  rows only because real files are gone, never because of hiding) — and
  conflating them was the bug. A **second**, blacklist-*aware* check is
  needed after `missing_ids` is known, computed from the same `rows` the
  page is about to render from:
  ```python
  try:
      rows = get_cluster_members(conn, cluster_id)
  except blacklist.BlacklistError as e:
      conn.close()
      return render(request, self._t('error.html'), self._ctx({
          'title': 'Cluster', 'message': 'Cannot read the blacklist.', 'detail': str(e),
      }), status=500)

  if len(rows) < 2:
      cluster_exists = conn.execute(
          'SELECT 1 FROM Clusters WHERE id = ?', (cluster_id,)
      ).fetchone() is not None
      conn.close()
      if not cluster_exists:
          raise Http404('Cluster not found')
      return redirect(_url('compare'))

  missing_ids, remaining = cleanup_missing_members(conn, cluster_id)
  if remaining <= 1:
      # Existing behavior, unchanged: too few *real files* left, regardless
      # of blacklist status -- collapse the cluster's DB rows entirely.
      conn.execute('DELETE FROM ClusterMembership WHERE cluster_id = ?', (cluster_id,))
      conn.execute('DELETE FROM Clusters WHERE id = ?', (cluster_id,))
      conn.commit()
      conn.close()
      return redirect(_url('compare'))

  missing_id_set = set(missing_ids)
  visible_rows = [r for r in rows if r['image_id'] not in missing_id_set]
  if len(visible_rows) < 2:
      # Blacklist-aware, unlike `remaining` above: rows is already
      # blocked-filtered, so this catches "too few members are actually
      # displayable" without deleting anything -- the cluster may still be
      # a real pair once the hidden member is restored, and restoring must
      # not require re-creating destroyed Cluster/ClusterMembership rows.
      conn.close()
      return redirect(_url('compare'))

  conn.close()
  ...
  # `members` is now built directly from `visible_rows`, which already
  # excludes both blocked and missing entries -- the old
  # `if row['image_id'] in missing_id_set: continue` guard inside the loop
  # is no longer needed, since visible_rows was filtered before the loop.
  ```
  A cluster ID that never existed still gets `rows == []` at the first check
  and resolves to the direct `SELECT` finding nothing, so it still 404s as
  "Cluster not found" rather than silently redirecting to Compare — the
  fully-blocked case is no longer indistinguishable from "no such cluster,"
  closing both halves of the docstring's own warning, not just the one this
  document's "Intermediate state" section originally flagged as an accepted
  rough edge. The two-member "one hidden, one present" case from the
  existing Step 2 test (below) is still caught by the *first* check alone
  (`rows` is already `[C]`, length 1); the second check exists specifically
  for the three-member case above, where blocking alone doesn't shrink `rows`
  below 2 but shrinks it below 2 once the independently-missing member is
  also excluded. Note that the `try/except blacklist.BlacklistError` shown
  above around `get_cluster_members()` is a *separate* block from the
  existing `try/except EnvironmentError` around `open_db()`
  (`__init__.py:306-313`), not merged into it — `open_db()` can only raise
  `EnvironmentError`, `get_cluster_members()` can only raise
  `BlacklistError`, and giving each its own `except` lets the rendered
  message stay precise ("Cannot open image database." vs. "Cannot read the
  blacklist.") instead of one generic message covering two unrelated
  failures. This is the "rendered error page for a corrupt store" the intro
  paragraph calls out as one of the surfaces Step 2 left unreached (today an
  uncaught `BlacklistError` would surface as a bare Django 500, not the
  app's own `error.html`).

  **This changes the expected result of an existing Step 2 test.**
  `tests/test_djview.py:344-364`
  (`test_hidden_but_present_member_does_not_delete_cluster`) hides one
  member of a two-member cluster and asserts `response.status_code == 200`
  with both memberships still in the database. Per the fix above, that
  exact scenario (2 members, 1 hidden, 1 visible) now redirects to Compare
  (`302`) instead of rendering — but it must still leave the `Clusters` and
  `ClusterMembership` rows untouched, since this new `len(rows) < 2` branch
  never runs a `DELETE`, only the (unrelated, unchanged) `remaining <= 1`
  branch does that, and that branch is never reached here. Update this test
  to assert the redirect (`302`, `Location` pointing at `compare/`) while
  keeping its real invariant — the two `ClusterMembership` rows and the
  `Clusters` row still present — intact; the test's own docstring ("must
  never destroy cluster metadata just because one of its members is
  hidden") was always about the DB rows, not the status code, so this is a
  correction to an incidental assertion, not a weakening of what the test
  actually guards.

  In `cluster_detail.html`: replace the Mark/Unmark button (lines 85-89, and
  its `.mark-btn`/`.mark-btn.marked`/`tr.is-marked` CSS at lines 14, 23-29)
  with a single Hide button carrying the data the shared modal needs, shown
  only to a user who can actually use it — section 1.6: "hiding buttons is
  not sufficient" for authorization, but that is a floor, not a reason to
  skip the UX of not showing a control that will just 403:
  ```html
  {% if can_manage_blacklist %}
  <button class="hide-btn" data-path="{{ m.path }}"
          data-thumb="{{ m.thumb_url }}" data-name="{{ m.name }}">Hide</button>
  {% endif %}
  ```
  which needs `'can_manage_blacklist': _can_manage_blacklist(request)` added
  to this view's context alongside the other template variables
  (`__init__.py:354-362`); the shared modal include at the bottom of the
  template (3g) is wrapped in the same `{% if can_manage_blacklist %}` so an
  unauthorized viewer's page carries neither the button nor the modal's
  markup/CSRF form. Remove the `#del-bar` block (lines 114-123) and its CSS
  (lines 31-46, including the now-unnecessary `body { padding-bottom: 3em; }`),
  the `MARK_URL`/`CSRF` constants and the `.mark-btn` click handler (lines
  137-165) — CSRF now comes from the shared modal's own token. Wire Hide
  buttons to the modal and implement section 1.3's "If fewer than two
  visible cluster members remain, return to Compare" as a client-side count
  after DOM removal too — this handles the *live*, same-tab case (hide
  clicked just now) that the server-side check above cannot, since that
  check only runs on a fresh `GET`.

  **The `onSuccess` callback must also retire the hidden member from the
  page's lightbox state, not only remove its table row.** The existing
  lightbox (`cluster_detail.html:134-213`, unchanged by this step) navigates
  by index into a `MEMBERS` array parsed once from `{{ members|json_script:
  "member-data" }}`, and each `.cd-thumb`'s `data-idx` attribute is that
  same index, fixed at render time. Removing only the `<tr>` leaves both of
  those stale: `MEMBERS` still contains the hidden entry, so `lbNav()`'s
  `(lbIdx + delta) % MEMBERS.length` wraparound can still land on it and
  request `m.image_url` for a path that now 404s (per 3d); and every
  `.cd-thumb` originally indexed *after* the removed one now points at the
  wrong array slot as soon as one entry is spliced out, so clicking a
  thumbnail that visually shifted up a row would open the neighboring
  image instead of the one actually clicked. Fix by keeping `MEMBERS` (and
  each thumbnail's `data-idx`) in sync with the DOM on every successful
  hide, and by closing the lightbox defensively if it happens to be open at
  all (it cannot legitimately be mid-hide today — the confirmation modal
  and the lightbox are mutually exclusive overlays, since the lightbox
  covers the Hide buttons underneath it while open — but closing
  unconditionally is one line and removes the need to reason about it ever
  becoming possible after a future template change):
  ```js
  document.querySelectorAll('.hide-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      imhandlerHideModal.open({
        path: btn.dataset.path, thumbUrl: btn.dataset.thumb, name: btn.dataset.name,
        onSuccess: () => {
          const row = btn.closest('tr');
          const idx = parseInt(row.querySelector('.cd-thumb').dataset.idx, 10);
          row.remove();
          if (lb.classList.contains('open')) lbClose();
          MEMBERS.splice(idx, 1);
          // Keep the inert JSON blob consistent too, in case anything
          // ever re-reads it (e.g. a restored bfcache page) rather than
          // relying solely on the live MEMBERS variable already in scope.
          document.getElementById('member-data').textContent = JSON.stringify(MEMBERS);
          document.querySelectorAll('.cd-thumb').forEach(el => {
            const i = parseInt(el.dataset.idx, 10);
            if (i > idx) el.dataset.idx = i - 1;
          });
          if (document.querySelectorAll('.cd-table tbody tr').length < 2) {
            window.location.href = "{{ back_url }}";
          }
        },
      });
    });
  });
  ```
  This block is placed among the Hide-button wiring (roughly where the old
  `.mark-btn` handler was, `cluster_detail.html:141-165`), textually *before*
  `MEMBERS`/`lb`/`lbClose` are declared further down in the same script
  (`cluster_detail.html:136,168,190`) — that is fine, not a bug to fix: the
  handler only runs later, on a user click, by which point the whole
  top-level script (including those `const` declarations) has already
  executed once, so the closure sees them normally. It is called out here
  only so whoever implements this doesn't reflexively reorder the script
  block looking for a reference-before-declaration error that isn't real.

  The two checks are complementary, not redundant: the server-side one
  covers a fresh request arriving already below two visible members (direct
  URL, another tab, another user, the CLI); the client-side one covers a
  hide happening during this page's own lifetime, which no request has
  observed yet.

- **3f — `similar`.** Section 1.1 offers Hide on both the focal and
  closest-match images here, and section 1.3 requires that hiding either
  updates the page without a reload; hiding the *focal* image, though,
  removes the entire premise of the page (there is nothing left to be
  "similar to"), so it redirects rather than leaving an empty page. Give
  the two buttons stable IDs and the closest panel its own ID:
  ```html
  <button id="hide-focal" class="hide-btn" data-path="{{ path }}"
          data-thumb="{{ thumb_url }}" data-name="{{ name }}">Hide</button>
  ...
  <div id="closest-panel" class="sim-panel">
    ...
    <button id="hide-closest" class="hide-btn" data-path="{{ closest.path }}"
            data-thumb="{{ closest.thumb_url }}"
            data-name="{{ closest.name }}">Hide</button>
  </div>
  ```
  Wire them through one null-safe helper. The null guard is required because
  the closest-match panel is optional, and neither button exists on the
  server-rendered `hidden_focal` branch below:
  ```js
  const focalHideBtn = document.getElementById('hide-focal');
  const closestHideBtn = document.getElementById('hide-closest');
  const closestPanel = document.getElementById('closest-panel');

  function wireHide(btn, onSuccess) {
    if (!btn) return;
    btn.addEventListener('click', () => {
      imhandlerHideModal.open({
        path: btn.dataset.path,
        thumbUrl: btn.dataset.thumb,
        name: btn.dataset.name,
        onSuccess,
      });
    });
  }

  wireHide(focalHideBtn, () => { window.location.href = "{{ browse_url }}"; });
  wireHide(closestHideBtn, () => {
    if (closestPanel) {
      closestPanel.innerHTML = '<p class="hidden-notice">Hidden.</p>';
    }
  });
  ```
  A second, server-rendered case section 1.3 requires: the *focal* image
  can arrive already blocked (a bookmarked/stale `similar/?path=...` URL,
  or another tab/CLI hiding it while this page is open) — today `find_similar`
  already returns `(None, [])` for a blocked target (Step 2, 2d), but the
  view can't currently tell that apart from "this image just has no
  embedding yet," so it renders the same "run the embedder first" message
  for both, which section 1.3's "replace the Similar panel with a Hidden
  notice" asks to distinguish. Check explicitly, before calling
  `find_similar`, using one snapshot shared with the call (again, one load
  per request, not two):
  ```python
  try:
      blocked = blacklist.load_if_configured()
      if path in blocked:
          return render(request, self._t('similar.html'), self._ctx({
              'title': path.name, 'name': path.name,
              'hidden_focal': True, 'browse_url': browse_url,
              'can_manage_blacklist': _can_manage_blacklist(request),
          }))
      conn = open_db()
      target_row, raw_neighbors = find_similar(conn, path, model, blocked=blocked)
      conn.close()
  except (EnvironmentError, blacklist.BlacklistError) as e:
      return render(request, self._t('error.html'), self._ctx({
          'title': 'Similar', 'message': 'Cannot load image data.', 'detail': str(e),
      }), status=500)
  ```
  The error message is deliberately generic ("Cannot load image data.", not
  the original "Cannot open image database.") since this one `except` now
  covers two unrelated causes (`open_db()`'s `EnvironmentError`,
  `load_if_configured()`'s `BlacklistError`) — `detail` still carries the
  real exception text for whoever reads the page. `can_manage_blacklist` is
  set on the `hidden_focal` branch too even though that branch renders no
  Hide button itself (nothing there needs authorizing) — Django would
  otherwise treat the missing key as falsy and skip the modal include by
  accident rather than by an explicit decision, and passing it keeps every
  render of this template following the same context contract.
  In `similar.html`, wrap the existing focus/closest/neighbors markup in
  `{% if hidden_focal %}<p>This image has been hidden.</p><p><a
  href="{{ browse_url }}">&larr; Back to directory</a></p>{% else %} ...
  {% endif %}`.

  Remove `marked_set` (`__init__.py:535`), the two `'marked': ...` fields
  (lines 559, 584) and `deletion_count` (line 585) from the view; add
  `'can_manage_blacklist': _can_manage_blacklist(request)` to the context
  instead (the same context key and rationale as 3e — a non-staff or
  anonymous visitor should not see a control that only 403s). In the
  template, wrap both Mark/Unmark buttons (lines 76-78, 88-90 — focal and
  closest) in `{% if can_manage_blacklist %}...{% endif %}` and replace them
  with the ID-bearing Hide buttons above, carrying
  `data-path`/`data-thumb`/`data-name` the same way as 3e; add
  `id="closest-panel"` to the optional closest panel and wrap the shared
  modal include (3g) in the same condition.
  Delete `#del-bar` (lines 114-123) and its CSS (lines 37-50), delete
  `.mark-btn` CSS (lines 18-24) and its click handler (`MARK_URL`, lines
  131, 135-155). `browse.html`, `compare.html`, and `semantic.html` need no
  change: none of them ever offered Mark (confirmed by `grep -rn
  "mark\|deletion" lib/imhandler/djview/templates/image_handler/{compare,browse,semantic}.html`
  returning nothing), and their data is already blocked-filtered by Step 2,
  so there is no authorization-gated control on those pages either.

- **3g — shared modal, Hidden images template, nav.** New
  `templates/image_handler/_hide_modal.html`: one hidden overlay `<div>`
  per page (not one per row — `cluster_detail` can have many rows), wrapping
  a `<form id="hide-modal-form">` containing `{% csrf_token %}` so the modal
  never depends on another form surviving on the page to supply a CSRF
  field, a thumbnail `<img>` and path text populated at open-time, and an
  inline error area (`hidden` by default):
  ```html
  <div id="hide-modal" class="hm-overlay" hidden>
    <form id="hide-modal-form" class="hm-box">
      {% csrf_token %}
      <img id="hm-thumb" src="" alt="">
      <div id="hm-path"></div>
      <p>The archive file remains on disk; imhandler will stop displaying and processing it.</p>
      <div id="hm-error" class="hm-error" hidden></div>
      <div class="hm-actions">
        <button type="button" id="hm-cancel">Cancel</button>
        <button type="submit" id="hm-confirm">Hide</button>
      </div>
    </form>
  </div>
  ```
  **The whole script below is wrapped in an IIFE**, `(function() { ... })();`
  — not for style, but because two real things go wrong without it. First,
  a top-level `function close() { ... }` (used below) is also the name of
  the built-in `window.close()`; a top-level function *declaration* in a
  `<script>` tag becomes a property of `window`, so an unwrapped version
  would silently overwrite the browser's own tab-closing function for the
  rest of the page. Second, `cluster_detail.html` and `similar.html` each
  already carry their own inline `<script>` block (the lightbox code, 3e's
  addition above) — top-level `let`/`const`/`class` bindings across
  multiple `<script>` tags on one page share a single global lexical
  environment, so a same-named `const` in two of them is a `SyntaxError`
  ("already declared"), not a harmless shadow the way `var` would be. An
  IIFE gives every name below (`overlay`, `form`, `close`, ...) its own
  function scope, immune to both problems regardless of what any other
  script on the page happens to declare.

  **CSRF and error handling need a fully specified flow, not just "post the
  path and CSRF token" — two things go wrong with the naive version.**
  First, `fetch` does not automatically include a form's fields; a bare
  `fetch(url, {method: 'POST', body: JSON.stringify({path})})` would never
  send the `{% csrf_token %}` field at all, so Django's `CsrfViewMiddleware`
  would 403 every submission regardless of the token rendered in the DOM.
  Second, a CSRF failure — or any non-2xx response that isn't `hide_image`
  itself — comes back as Django's own HTML `CSRF_FAILURE_VIEW` page, not
  JSON; a `response.json()` call on that response rejects with a
  `SyntaxError`, and if that rejection isn't caught, the `catch` for the
  *fetch* never runs, the button-disable from `setBusy(true)` is never
  undone, and the dialog is left disabled with no inline error shown — the
  "error path" section 2.4 asks for, and 1.3's "controls are disabled in
  flight" would be permanently true instead of temporarily true. Fix both
  with an explicit `async`/`try`/`catch`/`finally` handler, submitting the
  form's own fields (which already include `csrfmiddlewaretoken`) via
  `FormData` rather than hand-building a JSON body:
  ```js
  (function() {
  const HIDE_URL = "{% url 'image_handler:hide_image' %}";
  const overlay = document.getElementById('hide-modal');
  const form = document.getElementById('hide-modal-form');
  const thumbEl = document.getElementById('hm-thumb');
  const pathEl = document.getElementById('hm-path');
  const errorEl = document.getElementById('hm-error');
  const cancelBtn = document.getElementById('hm-cancel');
  const confirmBtn = document.getElementById('hm-confirm');
  let currentPath = null, currentOnSuccess = null;

  function setBusy(busy) { cancelBtn.disabled = busy; confirmBtn.disabled = busy; }
  function showError(msg) { errorEl.textContent = msg; errorEl.hidden = false; }
  function close() { overlay.hidden = true; errorEl.hidden = true; currentPath = null; currentOnSuccess = null; }

  window.imhandlerHideModal = {
    open({path, thumbUrl, name, onSuccess}) {
      currentPath = path; currentOnSuccess = onSuccess;
      thumbEl.src = thumbUrl; thumbEl.alt = name; pathEl.textContent = path;
      errorEl.hidden = true; overlay.hidden = false;
    },
  };
  cancelBtn.addEventListener('click', close);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!currentPath) return;
    setBusy(true);
    errorEl.hidden = true;
    try {
      const body = new FormData(form);  // carries csrfmiddlewaretoken from {% csrf_token %}
      body.set('path', currentPath);
      const response = await fetch(HIDE_URL, {method: 'POST', body});
      let data;
      try {
        data = await response.json();
      } catch (parseErr) {
        // Django's CSRF-failure page (and any other non-JSON error page,
        // e.g. a 500 from an app server in front of Django) lands here.
        throw new Error(
          response.status === 403
            ? 'Request rejected (missing or expired CSRF token). Reload the page and try again.'
            : `Unexpected server response (HTTP ${response.status}).`
        );
      }
      if (!response.ok || data.error) {
        throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
      }
      const onSuccess = currentOnSuccess;
      close();
      if (onSuccess) onSuccess();
    } catch (err) {
      showError(err.message || 'Network error -- please try again.');
    } finally {
      setBusy(false);
    }
  });
  })();
  ```
  `finally` is what guarantees Cancel/Hide are usable again after *any*
  outcome — a JSON error body, a non-JSON error page, or `fetch` itself
  rejecting (offline, DNS failure, aborted request) — rather than only after
  the one "clean JSON error" path the naive version handles. `FormData`
  needs no `Content-Type` header (the browser sets the correct
  `multipart/form-data` boundary itself), and `request.POST.get('path')` on
  the Django side reads a `FormData`-submitted field exactly like a native
  form post, so `hide_image`'s implementation in 3c needs no change to
  accept this. Included once near the end of `cluster_detail.html` and
  `similar.html`, each inside the `{% if can_manage_blacklist %}` guard from
  3e/3f.

  New `templates/image_handler/hidden_images.html`: a table of `path` /
  `exists` (rendered as e.g. "missing" when `False`, section 1.3: "including
  missing files") / a per-row `<form method="post" action="{% url
  'image_handler:restore_image' %}">` with `{% csrf_token %}`, a hidden
  `path` input, and a plain "Show again" submit button — no modal, no
  confirmation, and no thumbnail (section 2.4: "Do not show thumbnails
  there because media endpoints deliberately block them" — an `<img
  src="{{ thumb_url }}">` here would just render as a broken image, since
  `thumb` 404s every blocked path unconditionally per 3d). A full
  POST-redirect-GET back to `hidden/` is sufficient; section 1.3's
  "without a full reload" requirement is scoped to the Hide action on
  `cluster_detail`/`similar`, not to Show again.

  `index.html` gains a conditional nav entry, following the existing
  `{% if semantic_url %}`/`{% if specs_url %}` pattern (`index.html:6,9`)
  rather than baking a new item into the `ImageHandlerViewSet.__init__`
  nav list, which is built once at process start and has no access to
  per-request authorization:
  ```python
  # index(), alongside the existing semantic_url lookup
  hidden_url = _url('hidden_images') if _can_manage_blacklist(request) else None
  ```
  ```html
  {% if hidden_url %}<li><a href="{{ hidden_url }}">Hidden images</a></li>{% endif %}
  ```

- **3h — removing the old code.** Once 3c-3g land, delete: `mark_toggle`,
  `deletion_list_download`, `deletion_list_clear` (`__init__.py:364-398`);
  the three routes at `urls.py:17-19` and aliases at `views.py:23-25`; every
  remaining `deletion_list`/`marked_set` reference (already covered above,
  listed here for the final `rg` sweep); the `.mark-btn`/`#del-bar` CSS in
  both templates. Confirm nothing else in the repo still references
  `mark_toggle`/`deletion_list_download`/`deletion_list_clear` before
  deleting (`rg -n "mark_toggle|deletion_list"` — Step 4 runs the fuller
  version of this search across specs too, but Step 3's own diff should
  already be clean going in).

**Tests.** Added to `tests/test_djview.py`, following its existing
per-concern-class structure and its established fixture shape (temp
`appconfig.image_roots`/`cache_dir`, `RequestFactory`, `_with_session` for
session-touching views). `test_djview.py:67-82` maintains its own local
mirror of `urls.py`'s route list (used by the URL-name `{% url %}` template
tags this step's templates now rely on, and by the CSRF test below, which
must resolve real routes rather than calling view functions directly) — it
gains `hide/`/`hidden/`/`restore/` and drops `mark/`/`deletion-list/`/
`deletion-list/clear/` in lockstep with `urls.py`.

- **Authorization:** `_default_blacklist_authorizer` denies an anonymous
  request and a non-staff authenticated one, allows an `is_staff=True` one
  (a `SimpleNamespace`/`mock.Mock` standing in for `request.user` is enough
  — no real `django.contrib.auth` user model round-trip needed); an
  `IMHANDLER_BLACKLIST_AUTHORIZER` set via `mock.patch.object(settings,
  'IMHANDLER_BLACKLIST_AUTHORIZER', ...)` overrides the default and is
  consulted instead.
- **`hide_image`:** authorized POST adds the path and returns
  `{'ok': True}`; unauthorized returns 403 and leaves the blacklist
  unchanged; GET returns 405; missing `path` returns 400; a path outside
  every configured root returns 400 with the blacklist unchanged; hiding
  the same path twice both return `{'ok': True}` (idempotent); a
  hand-written corrupt store returns 500 rather than a stack trace.
- **`restore_image`:** authorized POST removes a previously hidden path and
  redirects (`302`) to `hidden/`, **not** a JSON body — this is the direct
  regression test for the plain-`<form>`-vs-`JsonResponse` bug this revision
  fixes, so assert `response['Content-Type']` is not `application/json` (or
  simply that `response.status_code == 302` and there is no parseable JSON
  body) in addition to checking the blacklist changed; restoring an absent
  path still redirects with nothing changed (idempotent, no error); restoring
  an entry whose root is no longer configured succeeds (the concrete
  regression test for 3a's fix — `add()` it while the root is configured,
  remove the root from `appconfig.image_roots`, confirm `remove()` would
  `ValueError` here but `restore_image` still redirects successfully and the
  entry is gone from `load()`); unauthorized returns a plain 403 (not a
  redirect, so a non-staff user gets an unambiguous denial rather than being
  bounced back to a page they also can't use); GET returns 405; a
  hand-written corrupt store returns a plain-text 500 (via the
  `PermissionError`-simulating case from 3a's fix, not just a hand-corrupted
  JSON file) rather than an unhandled exception.
- **`hidden_images`:** lists a mix of an existing and a missing-from-disk
  path with correct `exists` values; unauthorized returns 403 via
  `error.html`; a corrupt store returns 500 via `error.html` (not a bare
  Django 500); an unconfigured `cache_dir` renders the same 500 path via
  the caught `EnvironmentError` rather than an unhandled exception.
- **`image`/`thumb` enforcement:** a hidden path 404s from `image()` with
  `Cache-Control: no-store` (mirrors the existing `thumb` coverage at
  `test_djview.py:285-303`); the same revalidation-ordering test as that
  existing one, but against `image()`: load, hide, re-request with the
  stale `If-Modified-Since` from the first response, assert 404 not 304; a
  store made unreadable via `PermissionError` (not just structurally
  malformed JSON — the 3a regression case) makes both `image()` and
  `thumb()` 404 (fail closed) rather than 500.
- **`similar`:** a blocked focal path renders `hidden_focal` truthy and the
  "hidden" copy in the response body, not the "no embedding" message; an
  unblocked focal path with a blocked closest neighbor never shows that
  neighbor (already covered at the library level by Step 2's embedder
  tests — this is only a thin page-level smoke check that the view doesn't
  reintroduce it via a stale `blocked` snapshot); the Hide button and the
  shared modal include are both absent from the rendered HTML for a
  non-staff/anonymous request (`can_manage_blacklist=False`), and both
  present for a staff request.
- **`cluster_detail`:** the rendered page contains a `hide/` form action and
  a `data-path` per member for an authorized request, and contains neither
  `mark/` nor `deletion-list/` anywhere in its HTML (a `rg`-style substring
  assertion, the template-level companion to the repo-wide sweep in Step 4);
  for an unauthorized request, the rendered page contains no `hide-btn`
  element and no `hide/` form action at all, only the read-only table.
- **Cluster collapse on partial hide, no live click involved (the direct
  fix for the gap this revision found):** build a two-member cluster where
  both files exist on disk, `blacklist.add()` one member directly (standing
  in for "hidden from another tab, another user, or the CLI" — no `hide/`
  POST in this test), then `GET` `cluster_detail` and assert a `302` to
  `compare/` with the `Clusters`/`ClusterMembership` rows for the *other*,
  still-visible member left completely alone (only Hide/`purge` remove rows;
  a `GET` never does). A companion test requests a `cluster_id` that was
  never created at all and asserts `Http404`, not a `302` — proving the new
  `SELECT 1 FROM Clusters` check actually distinguishes "no such cluster"
  from "cluster collapsed by hiding," per `get_cluster_members`'s own
  docstring warning, rather than conflating the two the way the raw
  `len(rows)` check alone would. **A third variant is the actual regression
  test for the second check** (the upfront `len(rows) < 2` alone does not
  catch this): a three-member cluster with A hidden, B missing from disk,
  and C present and visible. `rows` is `[B, C]` (length 2, upfront check
  does not fire) and `cleanup_missing_members` returns `remaining == 2`
  (`3` real members minus `1` missing — hiding doesn't reduce this count by
  design — so the `remaining <= 1` branch does not fire either); assert the
  view still redirects (`302` to `compare/`) via the second,
  `visible_rows`-based check, that `B`'s `Images`/`ClusterMembership` rows
  were deleted by `cleanup_missing_members` as always, and that `A`'s and
  `C`'s rows are both left alone (the cluster is not collapsed — only
  navigation changed, since `remaining == 2` never authorized a `DELETE`).
  A fourth, contrasting variant keeps the existing `remaining <= 1`
  collapse-and-delete path covered on its own terms: three members, none
  hidden, two missing from disk — `remaining == 1` — and asserts the
  `Clusters`/`ClusterMembership` rows are actually deleted, unlike the third
  variant's redirect-without-deleting.
- **Existing test update:** `test_hidden_but_present_member_does_not_delete_cluster`
  (`test_djview.py:344-364`) is updated in place to assert `302`/`compare/`
  instead of `200`, while its existing DB assertions (`Clusters` row present,
  both `ClusterMembership` rows present) are kept exactly as they are — this
  is the same scenario as the bullet above, already present in the suite
  before this step, just asserting the wrong status code for what section
  1.3 actually requires.
- **Removed routes:** requesting `mark/`, `deletion-list/`, or
  `deletion-list/clear/` against the URLconf returns `Resolver404`/404, not
  a working (if unreachable-from-the-UI) endpoint left wired up by mistake.
- **CSRF enforcement, end-to-end (the direct regression test for 3g's fix)
  — a new `ImhandlerDjviewHideCsrfTests` class.** Every other test in this
  file calls view functions directly through `RequestFactory`
  (`request = self.factory.post(...); _vs.hide_image(request)`), which never
  passes through Django's middleware chain at all — `CsrfViewMiddleware`
  never runs, so none of those tests could ever have caught a modal that
  forgets to send its token, which is exactly the bug this revision fixes.
  This class instead goes through the real URLconf and a real
  `CsrfViewMiddleware`, using `django.test.Client(enforce_csrf_checks=True)`
  (the default test `Client` disables CSRF checks entirely, which would
  make this test pass regardless of whether the fix was applied):
  ```python
  @override_settings(
      MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'],
      IMHANDLER_BLACKLIST_AUTHORIZER=lambda request: True,  # authorization is 3b/3e's concern, not this test's
  )
  class ImhandlerDjviewHideCsrfTests(unittest.TestCase):
      def setUp(self):
          ...  # same temp image_roots/cache_dir fixture as the other classes
          self.client = Client(enforce_csrf_checks=True)

      def test_valid_token_hides_the_path(self):
          # hidden_images.html's {% csrf_token %} lives inside each row's
          # restore form (3g) -- with an empty blacklist there are zero
          # rows, so GETting /hidden/ would never call get_token() and
          # Django would never set the csrftoken cookie at all. Seed an
          # unrelated entry first so the page actually has a row to render.
          seed_path = self.root / 'seed.jpg'
          Image.new('RGB', (5, 5)).save(seed_path, 'JPEG')
          blacklist.add(seed_path)

          self.client.get('/hidden/')
          token = self.client.cookies['csrftoken'].value
          response = self.client.post('/hide/', {
              'path': str(self.image_path), 'csrfmiddlewaretoken': token,
          })
          self.assertEqual(response.status_code, 200)
          self.assertTrue(blacklist.is_blocked(self.image_path))

      def test_missing_token_is_rejected_with_a_non_json_response(self):
          response = self.client.post('/hide/', {'path': str(self.image_path)})
          self.assertEqual(response.status_code, 403)
          self.assertFalse(blacklist.is_blocked(self.image_path))
          # The assertion that matters for 3g: this is what the modal's
          # fetch actually receives on a CSRF failure, and it is not JSON --
          # confirming the try/catch around response.json() isn't guarding
          # against a hypothetical, it's guarding against Django's real
          # default CSRF_FAILURE_VIEW response.
          self.assertNotIn('application/json', response.get('Content-Type', ''))
  ```
  `test_valid_token_hides_the_path` posts the token as `csrfmiddlewaretoken`
  in the body, matching the field name Django's `{% csrf_token %}` actually
  renders. **This proves the server accepts that field; it does not prove
  the JavaScript ever sends it** — the test client submits the POST body
  directly and never executes `_hide_modal.html`'s `<script>` at all, so a
  future edit that rewrites the modal's `fetch` call to a hand-built JSON
  body (silently dropping the form's fields) would leave this test passing
  unchanged while the real UI started 403ing on every Hide click. That
  regression needs a different kind of test — a static assertion on the
  rendered template source, not on server behavior:
  ```python
  def test_hide_modal_submits_via_formdata(self):
      request = self.factory.get(f'/cluster/{self._make_cluster([...])}/')
      # ... _with_session, IMHANDLER_BLACKLIST_AUTHORIZER True as elsewhere ...
      html = _vs.cluster_detail(request, cluster_id).content.decode('utf-8')
      self.assertIn('new FormData(form)', html)
  ```
  This is a coupling-to-exact-source-text check, not a behavioral one — it
  would need updating if the modal's JS were ever legitimately rewritten —
  but it is the cheapest thing that actually breaks when the submission
  mechanism regresses, which `test_valid_token_hides_the_path` does not.
  The same style of check covers the lightbox-reindexing fix: `cluster_detail`'s
  rendered HTML must contain `MEMBERS.splice(idx, 1)` (proving the `onSuccess`
  handler still retires the hidden entry from the navigation array, not just
  the table row) and must **not** contain a bare `btn.closest('tr').remove()`
  with nothing else in the same handler (the exact shape of the bug this
  revision fixes) — a regression that drops the `MEMBERS`/`data-idx` upkeep
  while keeping the row-removal line would otherwise pass every other test
  in this file, since none of them open the lightbox or click a second
  thumbnail after a hide.
  **What no test in this suite can cover, static or otherwise:** the
  modal's actual rendered error text after a 403 (`showError(...)`'s DOM
  update), and whether the lightbox actually opens the correct image after
  a hide reindexes the thumbnails, both require executing the JS in a
  browser-like environment; this repo has no such harness (per `CLAUDE.md`:
  "plain browser JavaScript," no `package.json`/Jest/Playwright anywhere in
  the tree), so both are verified manually as part of Step 4's existing
  manual-verification pass — specifically, hide the *first* of three
  contact-sheet rows, then click each remaining thumbnail and confirm the
  lightbox opens the image actually clicked, not its neighbor.
  The CSRF test class above proves the *server* half of the bug precisely —
  CSRF failures are real, frequent (any expired session), and not JSON —
  which is what makes the client-side `try`/`catch` in 3g something other
  than defensive boilerplate; it does not, by itself, prove the client
  actually exercises that path correctly.

**Verification:**
```sh
python3 -m py_compile lib/imhandler/*.py lib/imhandler/cli/*.py lib/imhandler/djview/*.py bin/imh
python3 -m unittest tests.test_imhandler_blacklist tests.test_djview -v
python3 -m unittest discover -s tests -t .
cd llime && ./manage.py check && ./manage.py test
```

### Step 4 — Specifications and verification

Apply section 2.6 and search for stale workflow language:

```sh
rg -n "deletion list|deletion_list|delete\.sh|mark_toggle|deletion-list|Mark button|marked for deletion|immediate deletion"
```

Then run:

```sh
python3 -m py_compile lib/imhandler/*.py lib/imhandler/cli/*.py lib/imhandler/djview/*.py bin/imh
python3 -m unittest discover -s tests -t .
cd llime && ./manage.py check
cd llime && ./manage.py test
```

Manually hide an image, confirm it appears on the Hidden images page, and
confirm that all viewer surfaces and old media URLs
block it while the source remains unchanged. Include the caching case
explicitly: load an image URL, hide the image, then reload that same URL in
the same browser session without clearing the cache and confirm a 404 rather
than a cached render; check the same for a thumbnail inside a contact sheet. Run every `imh` command, restore
the image, and confirm normal regeneration makes it eligible again. Also test a
missing path, concurrent updates, and a malformed blacklist. Export both
formats from the local CLI, confirm that neither output is executable, verify
that the paths output can be consumed safely as data by an offline script, and
confirm that no web route offers the same export.

### Step 5 — Rollout and rollback

Deploy the shared library and Django changes together, checking both `llime`
and `../qat/knip`. No archive write permission is required. Back up
`cache_dir/blacklist.json` as user-maintained state. Old code ignores the
file and would expose hidden images, so rolling back while the old viewer is
reachable is not policy-safe.

Section 2.4's media caching rules impose a hard timing constraint, and Step
2i only creates the opportunity to satisfy it — it does not satisfy it by
itself. Client caches populated under the old `max-age=3600` cannot be
revoked by any later deploy, so:

**At least one hour of wall-clock time must elapse between deploying the
Step 2i header change and making the Hide action reachable.** One hour is
the old `max-age`; if that value has ever been raised in a deployment, use
whatever the largest previously served value was instead.

Separate releases do not discharge this on their own — two releases can be
minutes apart, and a Step 2 deploy followed promptly by a Step 3 deploy
leaves the window wide open. The release boundary is not the control; the
clock is. An operator who cannot wait must accept that an image hidden
inside that window may still render from a warm browser cache until the
entry expires, and should say so explicitly rather than assume the split
release handled it. Rollback re-opens the window in the other direction,
since the old code resumes handing out hour-long `max-age` responses for
images the blacklist still lists — one more reason rolling back a reachable
viewer is not policy-safe.
