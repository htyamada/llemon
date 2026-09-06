# imh — Implementation Notes

Internal behaviour of the `imh` command. See `imhandler-imh-man.md` for
usage and `imhandler-specs.md` for the library API.

---

## Entry point

`bin/imh` is a standalone Python script. It parses the
top-level `-q` / `--qat` flag, then dispatches on the subcommand name to
a handler function. Each handler constructs an `AppConfig`, calls
`appconfig.init()`, and invokes the appropriate library functions.

Config is loaded from `etc/imhandler.conf` in the grove repo. The `-q` flag
switches the variant key from `hty7` to `qat`, selecting the
`[qat.imhandler.core]` section instead of `[hty7.imhandler.core]`.

---

## Scanner — Leaf/Interior Distinction

`scanner.scan()` classifies each directory as either an interior node or a leaf
node. A directory that contains subdirectories is interior; its images are
ignored. A directory with no subdirectories is a leaf; its images are included.
This means only the deepest level of each branch contributes images to the album
tree.

---

## Blacklist enforcement

Every subcommand except `imh list`'s config-free path loads a `blocked`
snapshot once via a shared `_blocked_snapshot(prog, command)` helper before
doing any scanning or database work, and passes it explicitly to the
library call it drives (`scan`, `embed_images`, `cluster_images`,
`get_cluster_members`, `purge`) rather than letting each one call
`blacklist.load_if_configured()` itself — one read per command, not one per
scan. `_blocked_snapshot` calls `blacklist.load_if_configured()`: an
unconfigured `cache_dir` yields an empty snapshot (so an intentionally
unconfigured `imh list DIR` keeps its historical, unfiltered behavior,
exiting 0 and filtering nothing), while a `BlacklistError` (corrupt or
unreadable store) prints to stderr and exits 1 immediately — before any
scan starts, not partway through one.

---

## imh list

Calls `scanner.scan(root, blocked=blocked)` and either walks the resulting
`Album` tree for `--tree` output or calls
`filter_and_sort(album.all_images(), ...)` for flat output. Hidden images
are excluded from both; interior/leaf classification is unaffected (it
depends only on whether a directory has subdirectories, not on which of its
images are hidden). Each leaf album tracks how many of its images were
hidden in `Album.hidden_images`; `Album.hidden_count()` sums that over a
subtree — used by `imh thumb`'s dry-run summary below.

`--tree` is rendered by a recursive function that prints each album with its
`image_count()` in parentheses, indented by depth. `--glob` and `--sort` are
not applied in tree mode.

---

## imh thumb

Thumbnail storage path:

```
cache_dir/thumbs/<xx>/<sha256>-<size>.jpg
```

`<sha256>` is the hex SHA-256 of the absolute image path string (UTF-8
encoded). `<xx>` is the first two hex characters of the digest, used as a
bucket directory to avoid large flat directories.

Cache invalidation: a thumbnail is considered stale if its `mtime` is older
than the source image's `mtime`. Stale thumbnails are regenerated in place;
the destination file is overwritten atomically (write to a temp file in the
same directory, then `os.replace`).

Errors are collected, reported to stderr, and written to
`cache_dir/logs/thumb-errors-<ISO-timestamp>.log` (one path per line). The
log file is only created if at least one error occurred. Exit status is 1 if
the error list is non-empty.

---

## imh purge

Scans `root` (`DIR`, or every configured `image_root` if omitted) with the
blacklist applied, so "live" means present on disk **and** not hidden. A
hidden image is purged from thumbnails/DB/clusters by the same sweep that
already removes a deleted file's — its archive file is never touched, and
restoring it makes it eligible for regeneration again on the next `thumb`/
`embed` run.

**Unscoped (`DIR` omitted):** walks `cache_dir/thumbs/` and, for each `.jpg`
file found, extracts the `<sha256>` from the filename and checks whether any
live image hashes to it; thumbnails with no live match are deleted. The DB
sweep considers every `Images` row.

**Scoped (`DIR` given):** the thumbnail sweep is skipped entirely
(`PurgeResult.thumbs_skipped=True`) — a thumbnail filename carries only a
path hash, not the path itself, so there is no way to tell whether a stale
thumbnail's source was under `DIR` or elsewhere without re-scanning every
configured root anyway. The DB sweep only considers rows whose `path` is
under `DIR`; a stale row elsewhere is left alone.

Cluster collapse: a cluster is deleted if the count of its members *outside*
the stale/purged set drops to 0 or 1. In a scoped run, a cluster with any
member outside `DIR` is left alone entirely — `imh purge` only has
visibility into images under `DIR` on this invocation, and staling that
cluster's in-scope member could otherwise miscount an out-of-scope member as
"the last one left." A `Clusters` row that is already empty (no
`ClusterMembership` rows at all, independent of anything this run does) is
also deleted and counted in `clusters_collapsed`, scoped or not.

`dry_run` counts every one of the above without deleting or collapsing
anything.

---

## imh embed

**Skipping**: an image is skipped if the database already contains a row with
matching `(path, mtime)` that has all requested embeddings (`clip_embedding`
and/or `sscd_embedding`) non-null. A partial row (e.g., only CLIP present
when `--model both` was requested) triggers re-processing of the missing
embedding only.

**Batching**: images are grouped into batches of `--batch-size`. Each batch
is processed by the neural model, then committed to the database in a single
transaction. This makes Ctrl-C interruption safe: the next run resumes from
the first unprocessed image.

**Model weights**:
- CLIP ViT-B/32: downloaded via `open_clip` / HuggingFace Hub (~605 MB),
  cached at `cache_dir/weights/clip-vit-b-32/`.
- SSCD disc_mixup: TorchScript checkpoint from
  `dl.fbaipublicfiles.com/sscd-copy-detection/sscd_disc_mixup.torchscript.pt`
  (~90 MB), cached at `cache_dir/weights/sscd_disc_mixup.torchscript.pt`.

**Authentication**: Hugging Face downloads check for an `HF_TOKEN` using the
following fallback chain: `~/etc/imhandler-keys.json` (key `"HF_TOKEN"`),
then the `HF_TOKEN` environment variable. If neither is found, downloads
proceed without authentication.

**Quality tier scoring**: see `imhandler-theory.md` for the scoring formula
and what each metric measures.

---

## imh cluster

1. Load all L2-normalised embeddings for the requested model from the
   database into a numpy matrix `E` of shape `(n, 512)`, excluding hidden
   images (the `blocked` snapshot).
2. Compute the full pairwise cosine similarity matrix: `S = E @ E.T`.
3. Threshold to a boolean adjacency matrix: `A = S >= threshold`.
4. Extract connected components using `scipy.sparse.csgraph.connected_components`.
5. Discard components of size 1 (singletons).
6. Rank members within each component: sort by `(quality_tier ASC,
   laplacian_score DESC)` to produce `quality_rank` values (0 = best).
7. Delete any existing `Clusters` and `ClusterMembership` rows with the same
   `(model_used, threshold_used)`, then insert the new results.

The matrix multiply in step 2 is O(n²) in both time and memory. For
collections larger than ~100 k images this will be slow and memory-intensive;
tiled or approximate NN approaches are not yet implemented.

---

## imh report

Calls `db.get_clusters(conn, model=model, threshold=threshold)`, then for
each cluster calls `db.get_cluster_members(conn, cluster_id, blocked=blocked)`
and formats the output line by line; a cluster left with fewer than 2
visible members after filtering is skipped for this run (its rows are not
touched — that's `imh purge`'s job). The `*` marker is placed on the member
with `quality_rank == 0`. Width/height come from the `Images` table row
stored at embed time.

If `-o FILE` is given, output is written to that file; otherwise to stdout.

---

## imh blacklist export

`cmd_blacklist_export` calls `blacklist.load()` directly (not
`load_if_configured()` — an unconfigured `cache_dir` is a real error for an
export, not silently empty) and sorts the paths, then:

1. **Destination safety** (only when `-o FILE` is given): resolves `FILE`
   and refuses to proceed (exit 1) if it equals the blacklist store or its
   lock file, if it is literally named `-` (use no `-o` for stdout instead),
   or if it falls under any configured `image_root` — an export must never
   become itself a file the scanner or a future blacklist entry could
   reference.
2. **Serialization** (`_write_export`, shared by the `-o` and stdout paths
   so both are covered by the same tests): `paths` (default) writes one
   `os.fsencode`d path per line, `\n`-terminated; `paths0` is the same but
   NUL-terminated (for `find -print0` / `xargs -0`); `json` writes
   `{"version": 1, "paths": [...]}` with `ensure_ascii=True`.
3. **`--format paths` is refused up front**, before writing anything, if
   any path contains a literal `\n` or `\r` — a blacklist entry validated by
   `blacklist.load()` should never contain one, but if it somehow did, the
   newline-delimited format would silently turn one hidden path into two
   apparent removal targets for a downstream script; failing loudly instead
   costs nothing since `paths0`/`json` remain available.
4. **Atomic write to a file**: written to a `tempfile.mkstemp()` sibling of
   the destination, `fsync`ed, then `os.replace()`d into place — the same
   atomicity pattern `blacklist._write_atomic` uses for the store itself.

The export is data only: no format is executable, and none of the accepted
formats produce shell-interpretable output — a consuming script must
explicitly choose to treat the paths as data (e.g. an offline diff or backup
tool), not run them.
