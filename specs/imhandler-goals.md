## Status

All planned components are complete:

- **Shared backend** — scanner, thumbnailer, filter/sort, embedder, clusterer,
  database, config. See `imhandler-specs.md`.
- **CLI pipeline** (`imh`) — list, thumb, purge, embed, cluster, report.
  See `imhandler-imh-man.md`.
- **Gallery and dedup review web UI** (`imhandler.djview`) — browse,
  similarity browse, compare, cluster detail, similar image search, embed
  from browser, non-destructive hide/restore. See `imhandler-django-man.md`.
- **Persistent image blacklist** (`imhandler.blacklist`) — hide an image
  from every viewer surface and the CLI without touching its file; restore
  it later. See `imhandler-specs.md`.
