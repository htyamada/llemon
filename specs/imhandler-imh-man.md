# imh

Tools for scanning, browsing, and deduplicating image collections.

```
imh [-q] COMMAND [options] [DIR]
```

Running `imh` with no arguments prints a summary of available commands.

Pass `-q` (or `--qat`) before the subcommand to use the `[qat]` config
section instead of `[hty7]`:

```
imh -q embed ~/qat/images
```

## Setup

Paths are configured via `etc/imhandler.conf` in the grove repo. The file uses
TOML.

`imh list` requires no configuration. All other subcommands require `cache_dir`
to be set. `imh thumb`, `imh purge`, and `imh embed` (without a DIR argument)
also require `image_root`.

`etc/imhandler.conf` example:

```toml
[hty7.imhandler.core]
image_root = "~/Pictures"
cache_dir  = "~/var/imhandler/cache"
```

For the QAT variant (`imh -q`), add a `[qat.imhandler.core]` section:

```toml
[qat.imhandler.core]
image_root = "~/qat/images"
cache_dir  = "~/var/qat/imhandler/cache"
```

All generated files live under `cache_dir`:

```
cache_dir/
  thumbs/           thumbnail cache
  db/               SQLite database (dedup.db)
  weights/          downloaded model weights
  logs/             per-run error logs
  blacklist.json    persistent hide/restore store
  .blacklist.lock   lock file for concurrent blacklist updates
```

A hidden image is excluded by every subcommand below (`imh list` and `imh
thumb` included) as if it did not exist, without ever touching its file on
disk — see **imh blacklist** below for inspecting and exporting the current
list. The one exception: `imh list DIR` run with `cache_dir` unconfigured
filters nothing (there is no store to consult) rather than treating that as
an error.

The tools require the `~/opt/web` venv. Run
`set-up-system/250-python-web.sh` once to create it.

### Hugging Face authentication (optional)

To use a Hugging Face API token for private models or higher rate limits, store
it in `~/etc/imhandler-keys.json`:

```json
{
  "HF_TOKEN": "hf_your_token_here"
}
```

Alternatively, set the `HF_TOKEN` environment variable. If neither is configured,
model downloads proceed without authentication.

## Quick start

```
imh list ~/Photos
```
List all images under `~/Photos`, one relative path per line.

```
imh thumb ~/Photos
```
Generate 200-pixel thumbnails for every image, storing them in `cache_dir/thumbs/`.

```
imh embed ~/Photos && imh cluster && imh report
```
Compute embeddings, cluster similar images, and print results.

## imh list

Scan a directory tree and list image files. `imh ls` is an alias.

```
imh list [options] DIR
```

| Option | Description |
|--------|-------------|
| `DIR` | Root directory to scan |
| `-t, --tree` | Display as album tree instead of flat list |
| `-g, --glob PATTERN` | Filter filenames by glob (matched against filename only) |
| `--sort KEY` | Sort order: `name` (default), `mtime`, `size` |
| `--count` | Print total image count only and exit |

### Album tree format

```
/home/yamada/Photos/ (143)
  Vacation/ (98)
    Beach/ (42)
      img001.jpg
      img002.jpg
    Mountains/ (56)
      img010.jpg
  Family/ (45)
    img100.jpg
```

Each directory line shows the image count for that subtree in parentheses.

### Notes

- Supported formats: AVIF, BMP, GIF, HEIC, HEIF, JPEG, PNG, TIFF, WebP.
- macOS metadata (`._*` files, `__MACOSX` directories) are silently skipped.
- Images at interior nodes (directories that have subdirectories) are silently
  ignored — only leaf directories are treated as albums.
- `--glob` and `--sort` are ignored when `--tree` is used.
- `--glob` applies before `--count`.

## imh thumb

Generate JPEG thumbnails for images in a directory tree.

```
imh thumb [options] [DIR]
```

| Option | Description |
|--------|-------------|
| `DIR` | Root directory to scan (default: `image_root` from config) |
| `--size N` | Thumbnail long edge in pixels (default: 200) |
| `-n, --dry-run` | Count images without generating thumbnails |
| `-v, --verbose` | Print each thumbnail path as it is created |

Each `--size` value is cached independently. A thumbnail is regenerated if the
source file has been modified since it was cached. Hidden images are
excluded from the scan entirely (no thumbnail is generated, and an existing
one is not removed here — that's `imh purge`'s job); `-n`/`--dry-run`
reports how many were excluded this way: `N image(s) found, M hidden, no
thumbnails generated`.

Thumbnails that cannot be generated are reported to stderr and logged under
`cache_dir/logs/`. Exit status is 1 if any error occurred.

HEIC/HEIF support requires `pillow-heif` (included in the venv).

## imh purge

Remove stale thumbnails and database records for images that no longer exist.

```
imh purge [options] [DIR]
```

| Option | Description |
|--------|-------------|
| `DIR` | Root directory to scan (default: `image_root` from config) |
| `-n, --dry-run` | Report what would be removed without deleting anything |

"Current" means present on disk *and* not hidden — a hidden image is purged
from thumbnails, the database, and cluster membership exactly like a
deleted file, without its archive file ever being touched; restoring it
later makes it eligible for regeneration again.

Without `DIR`, scans every configured `image_root`, removes every thumbnail
in `cache_dir/thumbs/` with no current source, and every database record
(and now-too-small cluster) with no current source. With `DIR`, the
database/cluster sweep is scoped to records under `DIR` only (a stale
record elsewhere is left alone), and the thumbnail sweep is skipped
entirely and reported as such — a thumbnail's filename has no path in it,
so it can't be matched to `DIR` without scanning every root anyway; run
with no `DIR` to sweep thumbnails.

Example output:

```
$ imh purge
12 thumbnail(s) removed, 0 error(s)
7 DB record(s) removed, 0 error(s)
2 cluster(s) collapsed (fewer than two members remaining)
$ imh purge ~/Photos/Vacation
thumbnail sweep skipped (DIR given; run with no DIR to sweep thumbnails)
3 DB record(s) removed, 0 error(s)
1 cluster(s) collapsed (fewer than two members remaining)
```

Exit status is 1 if any removal failed.

## imh embed

Compute CLIP and/or SSCD embeddings and quality metrics for each image and
store results in the dedup database.

```
imh embed [options] [DIR]
```

| Option | Description |
|--------|-------------|
| `DIR` | Root directory to scan (default: `image_root` from config) |
| `--model MODEL` | `clip`, `sscd`, or `both` (default: `both`) |
| `--db PATH` | Database path (default: `cache_dir/db/dedup.db`) |
| `--weights DIR` | Model weights directory (default: `cache_dir/weights`) |
| `--batch-size N` | Images per embedding batch (default: 32) |

Quality tier threshold overrides (calibrated for natural photographs; see
`imhandler-selection-tuning.md` before changing these):

| Option | Default | Description |
|--------|---------|-------------|
| `--lap-lo F` | 0.0005 | Laplacian score below this → heavily blurry |
| `--lap-hi F` | 0.002 | Laplacian score below this → slightly blurry |
| `--hf-lo F` | 0.65 | HF power ratio below this → possible upscaling |
| `--block-hi F` | 2.0 | Blocking score above this → JPEG blocking |
| `--sc-hi F` | 1.5 | Sharpness consistency above this → inconsistent |

Already-processed images are skipped; re-running after adding new images only
processes the new ones. `DIR`, when given, must be under `image_root`.
Interrupt with Ctrl-C at any time; completed batches are committed and will be
skipped on the next run.

On first run, model weights are downloaded automatically (~700 MB total) and
cached under `cache_dir/weights/` for reuse.

### Quality metrics

Four float metrics are computed per image (no neural model required):

| Metric | High | Low |
|--------|------|-----|
| `laplacian_score` | Sharp, detailed | Blurry or out-of-focus |
| `hf_power_ratio` | Natural sharpness | Possible upscaling or filtering |
| `blocking_score` | JPEG blocking artifacts | Clean |
| `sharpness_consistency` | Spatially inconsistent sharpness | Uniformly sharp |

`quality_tier` (0 = clean, 1 = degraded, 2 = heavily degraded) is derived
from these metrics using the threshold parameters. It is used to rank images
within clusters. See `tuning.md` for guidance on interpreting and tuning.

## imh cluster

Group stored embeddings by visual similarity and write cluster results to the
database.

```
imh cluster [options]
```

| Option | Description |
|--------|-------------|
| `--model MODEL` | `clip` or `sscd` (default: `clip`) |
| `--threshold F` | Similarity threshold (default: 0.85) |
| `--db PATH` | Database path (default: `cache_dir/db/dedup.db`) |

Re-running with the same `--model` and `--threshold` replaces those results.
Different threshold values coexist in the database. Images with no similar
peer are not written to the database. Within each cluster, members are ranked
best-first: quality_tier ascending, then laplacian_score descending.

See `tuning.md` for guidance on choosing a threshold.

## imh report

Print clusters from the database, ordered by most recent first. Best image in
each cluster is marked `*`.

```
imh report [options]
```

| Option | Description |
|--------|-------------|
| `--model MODEL` | Filter to clusters from this model (default: all) |
| `--threshold F` | Filter to clusters with this threshold (default: all) |
| `--db PATH` | Database path (default: `cache_dir/db/dedup.db`) |
| `-o, --output FILE` | Write report to FILE instead of stdout |

### Output format

```
--- cluster 1 model=clip threshold=0.850 2026-04-15 14:32:01 ---
  *[0] clean              lap=0.0142   1920x1280  /home/yamada/Photos/img001.jpg
   [1] degraded           lap=0.0031   1280x853   /home/yamada/Photos/img001b.jpg

```

## imh blacklist

Inspect and export the persistent image blacklist — the same store the
web UI's Hide/Restore buttons and every other `imh` subcommand consult.
There is no `imh blacklist hide`/`restore`; hiding and restoring are web-UI
actions (see `imhandler-django-man.md`) — this subcommand is read-only.

```
imh blacklist export [options]
```

| Option | Description |
|--------|-------------|
| `-o, --output FILE` | Write to `FILE` instead of stdout |
| `--format FORMAT` | `paths` (default, one per line), `paths0` (NUL-terminated, for `find -print0`/`xargs -0`), or `json` (`{"version": 1, "paths": [...]}`) |

The export is plain data, never executable, regardless of format. `-o`
refuses to write to the blacklist store or lock file, to a path under any
configured `image_root`, or to a file literally named `-` (omit `-o` to
write to stdout instead) — an export must never itself become a path the
scanner (or a future blacklist entry) could reference. Requires
`cache_dir`; exits 1 if the store is corrupt or unreadable, or if any
refused-destination case above applies.

`--format paths` additionally refuses to run (exit 1, before writing
anything) if any path contains a literal newline or carriage return, since
that format's consumer splits on newlines — one hidden path could otherwise
be misread as two entries. This should not occur for any path `imh
blacklist export` can actually produce; use `--format paths0` or `--format
json` if it ever does.

## Configuration

| Key | Required by | Effect |
|-----|-------------|--------|
| `image_root` | `imh thumb`, `imh purge`, `imh embed` (default DIR) | Default image directory |
| `cache_dir` | all except `imh list` | Root for thumbnails, database, weights, and logs |

Set these in `etc/imhandler.conf` under `[hty7.imhandler.core]`
(or `[qat.imhandler.core]` for `imh -q`). See **Setup** above.
