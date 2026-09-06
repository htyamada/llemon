# imhandler — Library API Reference

The `imhandler` package lives in `lib/imhandler/`. It is
safe to import from any frontend. All modules require Python 3.12+.

---

## Quick start

```python
from imhandler.scanner import scan
from imhandler.filter_sort import filter_and_sort, SortKey
from imhandler.thumbnailer import prewarm

album = scan('/home/yamada/Photos')
images = filter_and_sort(album.images, sort=SortKey.MTIME)
prewarm(images)
```

Dedup pipeline:

```python
from imhandler.db import open_db
from imhandler.embedder import embed_images
from imhandler.clusterer import cluster_images

conn = open_db()
embed_images('/home/yamada/Photos', conn)
n = cluster_images(conn, threshold=0.85, model='clip')
```

---

## `imhandler.appconfig` — Path configuration

```python
from hty7.config import AppConfig
from imhandler import appconfig
ac = AppConfig('etc/imhandler.conf', variant='hty7')
appconfig.init(ac)
```

Call `init(ac)` once at frontend startup with an `AppConfig` instance
(from `hty7.config`) before using any other `imhandler` module. The
variant and conf path are baked into `ac` at construction; `init()`
extracts `image_root` and `cache_dir` from the appropriate section.

Module-level globals set by `init()`:

| Name | Type | Description |
|------|------|-------------|
| `image_root` | `str` | Configured image directory (empty string if unset) |
| `cache_dir` | `str` | Configured cache directory (empty string if unset) |

---

## `imhandler.cache` — Cache directory helpers

All paths are resolved under `appconfig.cache_dir`. Every function raises
`EnvironmentError` if the relevant config value is empty.

```python
image_root() -> Path
```
Return `Path(appconfig.image_root)`, resolved to an absolute path. Raises
`EnvironmentError` if `image_root` is unset or the directory does not exist.

```python
cache_root() -> Path
```
Return `Path(appconfig.cache_dir)`.

```python
thumbs_dir() -> Path
```
Return `cache_dir/thumbs`.

```python
db_path() -> Path
```
Return `cache_dir/db/dedup.db`.

```python
weights_dir() -> Path
```
Return `cache_dir/weights`.

---

## `imhandler.models` — Data classes

```python
from imhandler.models import ImageEntry, Album
```

### `ImageEntry`

```python
@dataclass
class ImageEntry:
    path:     Path   # absolute path to the image file
    rel_path: Path   # path relative to the scan root
    mtime:    float  # st_mtime of the file
```

### `Album`

```python
@dataclass
class Album:
    path:     Path         # absolute path to the directory
    rel_path: Path         # path relative to the scan root ('.' for root)
    name:     str          # directory name
    depth:    int          # depth from scan root (0 = root)
    children: list[Album]  # subdirectories (interior nodes only)
    images:   list[ImageEntry]  # images (leaf nodes only)
    hidden_images: int = 0 # count of hidden images excluded from this leaf
```

An `Album` is either an interior node (has `children`, `images` is empty)
or a leaf node (has `images`, `children` is empty). `hidden_images` is only
ever set on a leaf node — the number of images in this directory that were
excluded because they are hidden, not because they're missing (there's
nothing to count for those).

```python
album.hidden_count() -> int
```
Sum of `hidden_images` over this album and all descendants.

```python
album.image_count() -> int
```
Total image count for this album and all descendants.

```python
album.find(rel_path: Path | str) -> Album | None
```
Search the subtree depth-first for the album whose `rel_path` matches.
Returns `None` if not found.

```python
album.first_leaf() -> Album | None
```
Return the first leaf album (one with images) in depth-first order, or
`None` if the subtree contains no images.

```python
album.all_images() -> list[ImageEntry]
```
Return every `ImageEntry` in the subtree, depth-first.

---

## `imhandler.scanner` — Directory scanner

```python
from imhandler.scanner import scan
```

```python
scan(root: Path | str | None = None, *, blocked: AbstractSet[Path] | None = None) -> Album
```

Walk `root` recursively and return an `Album` tree. `root` is resolved to
an absolute path before scanning. If `root` is `None`, `appconfig.image_root`
is used via `image_root()`; an `EnvironmentError` is raised if it is unset.
Images in `imhandler.blacklist`'s hidden set are excluded from the tree.
`blocked` is an explicit snapshot to filter against; if omitted, the current
blacklist is loaded via `blacklist.load_if_configured()` (raises
`blacklist.BlacklistError` if a configured store exists but cannot be read).

- Directories that contain subdirectories are **interior nodes**: their
  `children` are populated and any image files they contain are ignored.
- Directories with no subdirectories are **leaf nodes**: their `images` are
  populated.
- macOS metadata is silently skipped: `._*` files and `__MACOSX` directories.
- Symlinks are not followed.
- `PermissionError` on a directory returns an empty album for that node.

Supported suffixes: `.avif`, `.bmp`, `.gif`, `.heic`, `.heif`, `.jpg`,
`.jpeg`, `.png`, `.tif`, `.tiff`, `.webp`.

---

## `imhandler.filter_sort` — Filtering and sorting

```python
from imhandler.filter_sort import filter_images, sort_images, filter_and_sort, SortKey
```

### `SortKey`

```python
class SortKey(Enum):
    NAME  = 'name'
    MTIME = 'mtime'
    SIZE  = 'size'
```

### Functions

```python
filter_images(
    images: Sequence[ImageEntry],
    glob: str | None = None,
    mtime_after: float | None = None,
    mtime_before: float | None = None,
) -> list[ImageEntry]
```

Filter by glob pattern (matched against filename only), and/or mtime
bounds. All criteria are ANDed.

```python
sort_images(images: Sequence[ImageEntry], key: SortKey = SortKey.NAME) -> list[ImageEntry]
```

Return a sorted copy. `SortKey.SIZE` calls `stat()` per entry.

```python
filter_and_sort(
    images: Sequence[ImageEntry],
    glob: str | None = None,
    mtime_after: float | None = None,
    mtime_before: float | None = None,
    sort: SortKey = SortKey.NAME,
) -> list[ImageEntry]
```

Convenience wrapper: filter then sort.

---

## `imhandler.thumbnailer` — Thumbnail cache

```python
from imhandler.thumbnailer import get_or_create, prewarm
```

Requires `cache_dir` to be configured. Thumbnails are stored as JPEG at:

```
cache_dir/thumbs/<xx>/<sha256>-<size>.jpg
```

where `<sha256>` is the SHA-256 of the absolute image path string and
`<xx>` is its first two hex characters.

A cached thumbnail is considered valid if its mtime is ≥ the source
file's mtime. Outdated thumbnails are regenerated in place.

HEIC/HEIF support is enabled automatically if `pillow-heif` is installed.

```python
get_or_create(
    entry: ImageEntry, long_edge: int = 200, *, blocked: AbstractSet[Path] | None = None,
) -> Path
```

Return the path to the thumbnail, generating it if absent or stale.
Creates cache subdirectories as needed. `entry.path` is resolved once and
checked against `blocked` (or the current blacklist, if `blocked` is
omitted); raises `blacklist.BlockedImageError` before touching any file if
the image is hidden.

```python
prewarm(
    entries: list[ImageEntry], long_edge: int = 200, *, blocked: AbstractSet[Path] | None = None,
) -> None
```

Call `get_or_create` for each entry, skipping any entry whose resolved path
is in `blocked` (or the current blacklist, if `blocked` is omitted) instead
of raising `blacklist.BlockedImageError` for it — a caller warming a batch
that may include hidden images doesn't need to catch anything per-entry.
Pass an explicit `blocked` snapshot to share one blacklist read across the
whole batch. Errors from generating a thumbnail for a non-blocked entry
still propagate.

```python
@dataclass
class PurgeResult:
    thumbs_removed: int
    thumb_errors: int
    thumbs_skipped: bool
    db_removed: int
    db_errors: int
    clusters_collapsed: int
```

```python
purge(
    root: Path | str | None = None, *, dry_run: bool = False,
    blocked: AbstractSet[Path] | None = None,
) -> PurgeResult
```

Scan `root` (defaulting to all configured `image_roots`) for images that are
live — present on disk **and** not hidden (`blocked`, or the current
blacklist if `blocked` is omitted; the same exclusion `scanner.scan` already
applies) — then remove every thumbnail and database record for an image
that is no longer live. A hidden image's thumbnail/DB/cluster-membership
rows are therefore removed exactly like a deleted file's, by the same
sweep, without ever touching its source file; restoring it makes it
eligible for regeneration again.

If `root` is given, the database sweep is scoped to rows under that root
(a stale row elsewhere survives), and the thumbnail sweep — which cannot
be scoped to a subtree, since a thumbnail filename carries no path — is
skipped entirely (`thumbs_skipped=True`, `thumbs_removed`/`thumb_errors`
both `0`); run with no `root` to sweep thumbnails. Clusters left with zero
or one remaining member are deleted (`clusters_collapsed`). When `dry_run`
is `True`, counts reflect what would be removed/collapsed without deleting
anything (never a partial removal). Requires `cache_dir`, and (when `root`
is omitted) at least one configured `image_root`.

---

## `imhandler.db` — SQLite database

```python
from imhandler.db import open_db
```

Used by the dedup pipeline. Gallery tools do not use the database.

```python
open_db(path: Path | None = None) -> sqlite3.Connection
```

Open (or create) the SQLite database. If `path` is `None`, uses
`cache_dir/db/dedup.db`. Creates parent directories as needed.
Returns a connection with `row_factory = sqlite3.Row`, WAL journal mode,
and foreign keys enabled. Schema is initialised on first open.

### Schema

```
Images(id, path, mtime, width, height,
       clip_embedding BLOB, sscd_embedding BLOB,
       laplacian_score, hf_power_ratio, blocking_score,
       sharpness_consistency, quality_tier,
       UNIQUE(path, mtime))

Clusters(id, threshold_used, model_used, created_at)

ClusterMembership(cluster_id, image_id, quality_rank,
                  PRIMARY KEY(cluster_id, image_id))
```

### Query helpers

```python
get_clusters(conn, *, model=None, threshold=None) -> list[Row]
```
Return `Clusters` rows ordered by `created_at DESC, id`. Both filter
arguments are optional.

```python
get_cluster_member_rows(conn, *, model=None, threshold=None,
                        blocked: AbstractSet[Path] | None = None) -> list[Row]
```
Return a flat join of `Clusters + ClusterMembership + Images` for all
matching clusters, ordered by `cluster_id, quality_rank`. Columns include
all image metric fields. Used by the compare view to load all members in
one query. Rows for hidden images are omitted (filtered in Python against
`blocked`, or the current blacklist if `blocked` is omitted, using the same
path identity `blacklist.is_blocked` uses) — this can leave a cluster with
fewer than two visible rows; collapsing such a cluster is the caller's job.

```python
get_cluster_members(conn, cluster_id, *,
                    blocked: AbstractSet[Path] | None = None) -> list[Row]
```
Return `ClusterMembership + Images` rows for a single cluster, ordered by
`quality_rank`. Columns: `image_id, path, width, height, laplacian_score,
hf_power_ratio, blocking_score, sharpness_consistency, quality_tier,
quality_rank`. Rows for hidden images are omitted (see
`get_cluster_member_rows`); a cluster whose members are all hidden returns
an empty list — the same shape as "no such cluster" — so a caller that must
tell those apart checks row existence before calling this, not after.

```python
cleanup_missing_members(conn, cluster_id) -> tuple[list[int], int]
```
Delete `ClusterMembership` and `Images` rows for files that no longer
exist on disk. Returns `(missing_ids, remaining_count)`. If
`remaining_count <= 1` the caller should delete the cluster itself.
Deliberately blacklist-blind (always operates against the unfiltered
membership, never a `blocked` snapshot): a hidden-but-present member is
never treated as missing here, so hiding an image never deletes cluster
rows — only `imh purge` and this on-disk check do that. Restoring a hidden
image therefore always brings its cluster membership back unchanged.

```python
get_embedded_paths(conn, paths: Iterable[str]) -> set[str]
```
Return the subset of `paths` that have at least one non-null embedding
(`clip_embedding` or `sscd_embedding`) in the database.

---

## `imhandler.embedder` — Embedding and quality metrics

```python
from imhandler.embedder import compute_quality_metrics, embed_images, find_similar, find_semantic
```

Requires the `~/opt/web` venv (PyTorch, open_clip, scipy).

```python
compute_quality_metrics(
    img: Image.Image,
    *,
    lap_lo: float = 0.0005,
    lap_hi: float = 0.002,
    hf_lo: float = 0.65,
    block_hi: float = 2.0,
    sc_hi: float = 1.5,
) -> dict[str, float | int]
```

Compute quality metrics for a PIL image using numpy/scipy only (no neural
models). Returns a dict with keys `laplacian_score`, `hf_power_ratio`,
`blocking_score`, `sharpness_consistency`, `quality_tier`.

`quality_tier` is 0 (clean), 1 (degraded), or 2 (heavily degraded),
derived from the other four metrics using the threshold keyword arguments.
All thresholds are keyword-only and can be tuned empirically.

```python
embed_images(
    root: Path | str,
    conn: sqlite3.Connection,
    *,
    model: str = 'both',
    batch_size: int = 8,
    weights_dir: Path | None = None,
    tier_thresholds: dict[str, float] | None = None,
    cancel=None,
    on_progress=None,
) -> tuple[int, int]
```

Scan `root`, compute embeddings and quality metrics for each image, and
upsert into `conn`. Returns `(processed, skipped)`.

- `model`: `'clip'`, `'sscd'`, or `'both'`.
- Images are keyed by `(path, mtime)`. An image is skipped if it already
  has all requested embeddings in the database; only the missing embedding
  is computed on a partial re-run.
- `weights_dir` defaults to `cache_dir/weights`. Model weights are
  downloaded on first use: CLIP via HuggingFace (~605 MB), SSCD from
  `dl.fbaipublicfiles.com` (~90 MB). For HuggingFace downloads, an optional
  `HF_TOKEN` is loaded from `~/etc/imhandler-keys.json` (key `"HF_TOKEN"`)
  or the `HF_TOKEN` environment variable if set.
- `tier_thresholds` is forwarded to `compute_quality_metrics` as keyword
  arguments; pass `None` to use defaults.
- `cancel`: optional object with `.is_set() -> bool`; checked before each
  batch. A `_CancelToken` combining a `threading.Event` and a flag file is
  used by `djview` to support cross-worker cancellation.
- `on_progress`: optional callable `(pct: int, dir_label: str) -> None`;
  called at batch boundaries with completion percentage and current directory.
- Safe to interrupt with ^C: each batch is committed atomically and will be
  skipped on the next run.

```python
find_similar(
    conn: sqlite3.Connection,
    path: Path | str,
    model: str,
    *,
    n: int = 8,
    blocked: AbstractSet[Path] | None = None,
) -> tuple[Row | None, list[dict]]
```

Find the `n` most similar images in the same directory as `path` using
cosine similarity of stored embeddings. Returns `(target_row, neighbors)`:

- `target_row` — the `Images` row for `path` (columns: embedding blob,
  `width`, `height`), or `None` if no embedding exists for this image,
  **including when `path` itself is hidden** (checked against `blocked`,
  or the current blacklist if `blocked` is omitted).
- `neighbors` — list of dicts with keys `path`, `similarity` (float,
  rounded to 3 dp), `width`, `height`, ordered by descending similarity.
  Excludes `path` itself, hidden images, and is restricted to the
  immediate directory (no subdirectories).

```python
find_semantic(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: Path | str | None = None,
    n: int = 24,
    weights_dir: Path | None = None,
    blocked: AbstractSet[Path] | None = None,
) -> tuple[list[dict], int]
```

Find the `n` CLIP-nearest images to a text `query`, across every embedded
image in the database (or under `scope`, if given — restricted to that
directory and its subdirectories). Returns `(results, candidate_count)`:

- `results` — list of dicts with keys `path`, `similarity` (float, rounded
  to 3 dp), `width`, `height`, ordered by descending similarity, truncated
  to `n`.
- `candidate_count` — the number of embedded, non-hidden images considered
  (before truncation to `n`), for a UI to show e.g. "showing 24 of 311
  matches."

An empty (or all-whitespace) `query` returns `([], 0)` without touching the
database or loading the CLIP text model. Hidden images are excluded from
the candidate set (checked against `blocked`, or the current blacklist if
`blocked` is omitted) before ranking, so a hidden image can never appear in
results regardless of `n`.

### Models

**CLIP ViT-B/32** (`open_clip`, pretrained `'openai'`): general-purpose
image-text embedding. 512-dim L2-normalised output. Good for finding
images of the same subject regardless of framing.

**SSCD disc_mixup** (TorchScript checkpoint from Facebook Research): trained
specifically for copy detection. 512-dim L2-normalised output. More
sensitive to near-duplicates with minor edits (crop, colour grade, etc.)
than CLIP.

---

## `imhandler.clusterer` — Similarity clustering

```python
from imhandler.clusterer import cluster_images
```

```python
cluster_images(
    conn: sqlite3.Connection,
    *,
    threshold: float = 0.85,
    model: str = 'clip',
    blocked: AbstractSet[Path] | None = None,
) -> int
```

Load L2-normalised embeddings from `conn`, excluding hidden images
(`blocked`, or the current blacklist if `blocked` is omitted), compute the
full pairwise cosine similarity matrix (numpy dot product), threshold to an
adjacency matrix, extract connected components (scipy sparse graph),
discard singletons, rank members by quality (quality_tier ascending,
laplacian_score descending), and write results to the `Clusters` and
`ClusterMembership` tables. Because singletons are discarded, a cluster is
never written with only hidden-then-excluded members.

Any existing clusters with the same `threshold` and `model` are replaced
before writing new results. Clusters from other threshold/model combinations
are left intact.

Returns the number of clusters written.

---

## `imhandler.blacklist` — Persistent hide/restore store

```python
from imhandler import blacklist
```

The sole persistence and matching implementation for hidden images. An
image is *hidden* (not deleted) by adding its resolved path to a JSON store
at `cache_root()/blacklist.json`; every reader in this package (`scanner`,
`thumbnailer`, `db`, `embedder`, `clusterer`) consults it and excludes
hidden images, so a single `add()` call is enough to remove an image from
every viewer surface. The archive file itself is never touched, and no
function in this module ever opens, deletes, or requires write access to
an image root — every write this module makes is confined to `cache_dir`,
so a read-only archive root works with hiding exactly as well as a
writable one. Contains no Django types; `imhandler.djview` is the only
caller with `path` values sourced from HTTP requests rather than a
previous `load()`.

Identity is path-based, not content- or inode-based: `add()`/`remove()`
never `stat()` or read the target file, so a path can be hidden whether or
not anything currently exists there (useful for hiding a path in advance,
or leaving a stale entry for a file that's since been deleted elsewhere).
A new file later written to that same path stays hidden, since nothing
distinguishes it from the file that was there when it was hidden; a file
moved or renamed to a *different* path is a different identity and is not
automatically hidden, even if the renamed file's bytes are identical to a
hidden one.

```python
class BlacklistError(Exception)
```
The store is corrupt, unsupported, or cannot be read or written because of
a filesystem error (e.g. permissions). Every filesystem/parse failure in
this module is normalized to this one type — callers never see a bare
`OSError`/`PermissionError`/`json.JSONDecodeError`.

```python
class BlockedImageError(Exception)
```
Raised by an explicit-path operation (e.g. `thumbnailer.get_or_create`)
asked to act on an image that is currently hidden.

```python
add(path: Path | str) -> bool
```
Hide `path`. `path` is expanded, resolved, and validated: it must be
absolute (after expansion), under a currently configured `image_root`, and
have a supported image suffix — raises `ValueError` otherwise. Returns
`True` if this call changed the store, `False` if `path` was already
hidden (idempotent).

```python
remove(path: Path | str) -> bool
```
Restore `path`, using the same live-root validation as `add()` — raises
`ValueError` if no configured root presently contains `path`, even if it is
in the store. Returns `True`/`False` the same way as `add()`.

```python
remove_stored(raw: str) -> bool
```
Restore an entry by the exact string `load()` returned for it, bypassing
`remove()`'s live-root check. This is what the Hidden images page's
restore button uses, so an entry under a root that has since been
reconfigured away (or is temporarily offline) can still be restored — the
one thing `remove()` cannot do. A string that could never have come from
`load()` (malformed, non-canonical, wrong suffix) is a silent no-op
(`False`), matching `remove()`'s already-absent case; a genuinely corrupt
store still raises `BlacklistError`.

```python
load() -> frozenset[Path]
```
Return every currently hidden path as a resolved, absolute `Path`. An
absent store is an empty result; a store that exists but fails to parse or
read raises `BlacklistError`.

```python
load_if_configured() -> frozenset[Path]
```
`load()`, or an empty set when `cache_dir` is not configured at all. The
one sanctioned fail-open in this module, for callers (e.g. `imh list DIR`
against an unconfigured variant) that have no store to consult in the
first place. A store that exists but is corrupt or unreadable still raises
`BlacklistError` — only "there is no configured store" is tolerated.

```python
is_blocked(path: Path | str, blocked: AbstractSet[Path] | None = None) -> bool
```
Return whether `path` is hidden. Pass an explicit `blocked` snapshot (from
a prior `load()`/`load_if_configured()`) to check many paths without
re-reading the store each time; omitted, this calls `load()` itself.

```python
store_path() -> Path
```
Public path to the blacklist store (`cache_root()/blacklist.json`), for
callers — e.g. the CLI's export command — that must refuse to overwrite it
without reaching into a private module attribute.

### Store format

```json
{"version": 1, "paths": ["/abs/path/one.jpg", "/abs/path/two.png"]}
```

Paths are absolute, resolved, sorted, and de-duplicated — an invariant
`add()`/`_normalize()` maintain when writing, not one `load()` re-verifies
against the filesystem: entry validation is deliberately lexical only (see
`_validate_stored_entry`), so a store hand-edited to contain a canonical-
looking path that is itself a symlink to another in-root file is accepted
as that path, not resolved to its target. This is intentional — re-resolving
at load time would let an entry's *symlink chain* changing after it was
written spuriously invalidate the whole store — and assumes the store file
is not hand-edited; it is generated and consumed only by this module. A
missing file means an empty blacklist; a malformed file (wrong version,
non-list `paths`, non-canonical or unsupported-suffix entries, invalid
JSON/UTF-8) raises `BlacklistError` rather than silently treating any
subset of entries as valid — "hidden" and "visible" must never both look
like a plausible read of the same corrupt file.

### Concurrency and durability

Updates take an exclusive `fcntl.flock()` on a dedicated
`cache_root()/.blacklist.lock` file (never the data file itself) and write
via `tempfile.mkstemp()` + `os.fsync()` + `os.replace()`, so concurrent
Django workers and CLI processes never lose an update or observe partial
JSON. A failure to release the lock (`LOCK_UN` or `close()`) after a
successful write is swallowed, not raised — the write already succeeded,
and closing the descriptor releases the OS-held lock regardless.

---

## `imhandler.djview` — Django view set

```python
from imhandler.djview import ImageHandlerViewSet
```

`ImageHandlerViewSet` is a class whose methods are Django view callables.
The deployed Django front ends normally use the shared
`imhandler.djview.views` and `imhandler.djview.urls` modules rather
than carrying local wrapper apps. The URL namespace is `image_handler`.

```python
ImageHandlerViewSet(
    *,
    base_nav,
    nav=None,
    nav_suffix=None,
    nav_rel=None,
    base_nav_rel=None,
    index_specs_url=None,
)
```

- `base_nav`: navigation dict/list used by the base template (host-app specific).
- `nav`: right-side navbar items.
- `nav_suffix`: right-side navbar items appended after `nav`.
- `nav_rel`: relative-path variant of `nav`.
- `base_nav_rel`: relative-path variant of `base_nav`; defaults to `base_nav`.
- `index_specs_url`: optional specs link for the index page.

### Views

All media paths are passed as `?path=<absolute-path>` query parameters.

| Method | HTTP | Query params | Description |
|--------|------|--------------|-------------|
| `index` | GET | — | Root page with section links |
| `browse` | GET | `album=<rel>`, `sort=name\|mtime\|size` | Album tree + image grid |
| `similarity_browse` | GET | `album=<rel>`, `sort=` | Same as browse; marks embedded images; shows embed button |
| `semantic_search` | GET | `q=`, `n=` | CLIP text-to-image search across all embedded images; returns the first `n` thumbnail matches |
| `compare` | GET | `model=clip\|sscd`, `threshold=0.85` | Re-clusters on each load and shows contact sheets |
| `cluster_detail` | GET | `model=`, `threshold=` | Single cluster; auto-cleans missing members, filters hidden ones |
| `hide_image` | POST | `path=` (body) | Hide an image; authorized only |
| `restore_image` | POST | `path=` (body) | Restore a hidden image; authorized only |
| `hidden_images` | GET | — | List every currently hidden path; authorized only |
| `similar` | GET | `path=`, `model=clip\|sscd` | Most similar images in the same directory |
| `thumb` | GET | `path=`, `size=200` | Serve JPEG thumbnail; `size` clamped to 50–800 |
| `image` | GET | `path=` | Stream full-size original |
| `embed_stream` | GET | `album=<rel>` | SSE stream; runs `embed_images` in a background thread, or across all configured roots for multi-root `album=.` |
| `embed_cancel` | POST | — | Cancel running embed (`album` in POST body); CSRF-exempt |

`thumb` and `image` both 404 (with `Cache-Control: no-store`, so the 404
itself is never cached) for a hidden path, and fail closed the same way —
404, not 500 — if the blacklist store itself cannot be read. Normal (non-404)
responses from both views send `Cache-Control: private, no-cache` with a
`Last-Modified` validator, forcing revalidation on every use rather than
letting a client serve a stale copy unconditionally; a request that
revalidates gets the 404 as soon as the image is hidden. This does not
retroactively invalidate a response a client cached before this header was
deployed — a copy stored under the previous `max-age=3600` policy remains
servable, unvalidated, until that copy's hour expires regardless of when the
image is later hidden — a deployment that changes this header and makes
Hide reachable at the same time should expect that window, rather than
assume the header change alone closes it immediately.

`thumb` and `image` also give the *identical* 404 (same status, same
`Image not found` body, same `no-store` header) for a hidden path and a
path that was never blacklisted at all but simply doesn't exist — the
response never reveals whether a given in-root path is hidden or just
absent. (A path outside every configured root is rejected earlier with a
different message, `Path not under any configured root`, since that
distinction reveals nothing about any specific archive file.)

### Authorization

`hide_image`, `restore_image`, and `hidden_images` are gated by
`settings.IMHANDLER_BLACKLIST_AUTHORIZER`, an optional callable
`(request) -> bool`. If unset, the default requires an authenticated,
`is_staff` Django user. A host project may substitute its own check — see
`imhandler-django-impl.md`. `index`'s "Hidden images" link and the Hide
button on `cluster_detail`/`similar` are shown only when this check passes
for the current request; an unauthorized POST to `hide_image` returns
`{'error': ...}` with status 403, `restore_image` returns a plain
(non-redirect) 403, and `hidden_images` renders the app's `error.html` with
status 403.

### `hide_image` / `restore_image` / `hidden_images`

`hide_image` calls `blacklist.add(path)` and returns
`JsonResponse({'ok': True})` on success, `{'error': ...}` (400) for a
missing/invalid/out-of-root path, or `{'error': ...}` (500) if the store
cannot be read or written. `restore_image` calls
`blacklist.remove_stored(path)` (so an entry under a since-reconfigured
root can still be restored) and always redirects (302) to `hidden_images`
on success — deliberately a plain redirect, not JSON, since the request
comes from a plain HTML `<form>` on the Hidden images page, not the fetch
call `hide_image` uses. A store failure renders a plain-text 500 body
instead. `hidden_images` lists every stored path together with whether it
still exists on disk (a path missing from disk can still be restored; it
simply has nothing to restore *to* until it reappears).

### Cancellation

`embed_stream` creates a cancel token combining a `threading.Event` (for
same-worker cancels) and one or more tempfile flags (for cross-worker cancels
under gunicorn). `embed_cancel` sets both. The token is passed to
`embed_images` via its `cancel` parameter. For multi-root `album=.`, the
stream iterates all configured roots and aggregates the counts.

### Templates

Templates live at `templates/image_handler/<name>.html`. The Django app
label is `imhandler_djview`; add `'imhandler.djview'` to
`INSTALLED_APPS` so Django's template loader finds them. Host projects mount
the shared URL module with:

```python
path('image_handler/', include('imhandler.djview.urls')),
```

The package `AppConfig.ready()` calls
`appconfig.init_variant(settings.IMHANDLER_VARIANT)`, defaulting to `hty7`
when the setting is absent.

---

## Configuration

Paths come from `appconfig.init(ac)`, which extracts values from an `AppConfig`
instance. Call `init()` at frontend startup before using any module that reads
paths.

| Config key | Required by | Effect |
|------------|-------------|--------|
| `image_root` | `cache.image_root`, `scanner.scan` | Default image directory |
| `cache_dir` | `thumbnailer`, `db`, `embedder`, `clusterer` | Root for all generated files |

For `scan(root=None)` and `thumbnailer.purge(root=None)`, the `root`
argument defaults to `appconfig.image_root` via `cache.image_root()`.
