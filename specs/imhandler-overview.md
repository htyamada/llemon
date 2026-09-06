# imhandler — Overview

imhandler is a set of tools for managing a large image collection: browsing,
thumbnail generation, duplicate detection via neural embeddings, and
interactive review of duplicate clusters via a web UI.

---

## Components

### CLI pipeline (`imh`)

Six subcommands covering the full workflow:

| Subcommand | Purpose |
|------------|---------|
| `imh list` | Scan a directory tree and list images; display as album tree or flat list |
| `imh thumb` | Generate and cache JPEG thumbnails |
| `imh embed` | Compute CLIP and SSCD embeddings + quality metrics; store in SQLite |
| `imh cluster` | Group stored embeddings by similarity; write clusters to database |
| `imh report` | Print clusters, best image first within each group |
| `imh purge` | Remove thumbnails and database records for images that no longer exist |

`imh list` and `imh thumb` are self-contained gallery tools; they do not
use the database. `imh embed`, `imh cluster`, and `imh report` form the
dedup pipeline.

### Web UI (`imhandler.djview`)

A shared Django app (`imhandler.djview`) that plugs into a host Django
project. Two host integrations exist:

- **`knip`** (`~/prj/qat/knip/`) — uses the `qat` config variant
- **`llime`** (`~/prj/grove/llime/`) — uses the `hty7` config variant

Both mount `imhandler.djview.urls` at `/image_handler/`; the only
host-specific difference is `IMHANDLER_VARIANT` (`qat` or `hty7`). The app
provides:

- **Browse** — navigate the album tree, view thumbnail grids
- **Similarity** — same as Browse, with per-image embedding indicators and
  an in-browser **Embed** button that streams `imh embed` progress; at the
  virtual multi-root top level it embeds each configured real root
- **Semantic** — CLIP text-to-image search across stored embeddings, with a
  user-selected result count and thumbnail clicks opening the full image
- **Compare** — on-demand re-clustering and side-by-side cluster contact
  sheets, with a Hide button per member for an authorized viewer
- **Similar** — find the most visually similar images in the same directory
- **Hiding workflow** — an authorized viewer can hide an image from every
  viewer surface (and the CLI) without touching its file, and later restore
  it from the Hidden images page. Non-destructive by design: unlike the
  removed session-based Mark/download-a-shell-script flow, nothing is ever
  deleted through the web UI

### Gallery viewer

Album tree navigation and thumbnail grid, implemented as the **Browse** and
**Similarity** sections of the web UI. Interior albums list child albums with
image counts; leaf albums show a sortable thumbnail grid with click-to-view
full-size. The Similarity section additionally indicates which images have
stored embeddings and provides per-image links to the Similar view.

---

## Library structure

```
lib/imhandler/
  appconfig.py       config globals (image_root, cache_dir)
  cache.py           path helpers (thumbs_dir, db_path, etc.)
  models.py          ImageEntry, Album data classes
  scanner.py         recursive directory scan → Album tree
  filter_sort.py     filter and sort lists of ImageEntry
  thumbnailer.py     thumbnail generation, cache, purge
  db.py              SQLite schema, open_db, query helpers
  embedder.py        CLIP/SSCD embedding, quality metrics, find_similar
  clusterer.py       cosine similarity clustering
  blacklist.py       persistent hide/restore store, consulted by every module above
  cli/               imh subcommand handlers
  djview/            Django viewset (ImageHandlerViewSet)
```

The library is imported by both the `imh` CLI and the Django viewset. Gallery
tools (`list`, `thumb`) only use `scanner`, `filter_sort`, and `thumbnailer`.
The dedup pipeline additionally uses `embedder`, `clusterer`, and `db`. Every
one of these except `filter_sort` excludes hidden images by consulting
`blacklist` (directly, or via a caller-supplied `blocked` snapshot).

---

## Document map

| Document | Covers |
|----------|--------|
| `imhandler-overview.md` | This document |
| `imhandler-imh-man.md` | `imh` CLI — all subcommands, options, output, configuration |
| `imhandler-imh-impl.md` | `imh` CLI — internal behaviour, storage formats, algorithms |
| `imhandler-django-man.md` | Web UI — pages, hiding workflow, in-browser embed |
| `imhandler-django-impl.md` | Web UI — integration pattern, view details, SSE, cancellation |
| `imhandler-specs.md` | Library API reference — all public modules and functions |
| `imhandler-theory.md` | Theory — embeddings, cosine similarity, clustering, quality metrics |
| `imhandler-selection-tuning.md` | Practical guide to tuning pipeline parameters |
| `imhandler-goals.md` | Component status |

---

## Configuration

Config is read from `etc/imhandler.conf` in the grove repo. The file uses
TOML.

Two keys drive all path resolution:

| Key | Effect |
|-----|--------|
| `image_root` | Default image directory for commands that scan without an explicit DIR |
| `cache_dir` | Root for all generated files: thumbnails, database, model weights, logs |

Set paths in `etc/imhandler.conf` under `[hty7.imhandler.core]`.
The `-q` flag to `imh` switches both keys to the `[qat.imhandler.core]`
section instead.

---

## Dependencies

The `~/opt/web` venv provides the ML and image-processing stack.
Create it once:

```
set-up-system/250-python-web.sh
```

Packages installed: Pillow, pillow-heif, PyTorch, open_clip_torch, scipy,
tqdm, Django.

On the first run of `imh embed`, model weights are downloaded automatically
and cached under `cache_dir/weights/`:

- CLIP ViT-B/32 — ~605 MB (HuggingFace)
- SSCD disc_mixup — ~90 MB (dl.fbaipublicfiles.com)

### Authentication

To use a Hugging Face API token for private models or higher rate limits, store
the token in `~/etc/imhandler-keys.json`:

```json
{
  "HF_TOKEN": "hf_your_token_here"
}
```

Alternatively, set the `HF_TOKEN` environment variable. If neither is configured,
downloads proceed without authentication.
