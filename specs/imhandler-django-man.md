# Image Handler Web UI

The web UI is provided by `imhandler.djview`, which supplies the shared
Django views, URL patterns, templates, and app config. It plugs into an
existing Django project; there is no standalone server command.

See `imhandler-django-impl.md` for integration instructions and
`imhandler-specs.md` for the full API.

---

## Pages

### Index

Root page with links to the four sections: Browse, Similarity, Semantic,
Compare. A fifth link, **Hidden images**, appears only for a request
authorized to manage the blacklist (see "Hiding workflow" below).

### Browse

The gallery. Navigation is breadcrumb-based: the current album path is shown
as a trail of links from the collection root down to the current level.

**Interior albums** (directories that contain sub-albums) show a list of
child albums, each with its total image count. Click an album to descend into
it. A parent link navigates up one level.

**Leaf albums** (directories that contain images directly) show a thumbnail
grid. Each thumbnail links to the full-size image. A filename label appears
below each thumbnail.

Sort the grid with `?sort=name` (default), `?sort=mtime`, or `?sort=size`.
Sort links are provided in the page header.

If `cache_dir` is not configured, a warning is shown and thumbnails cannot
be generated.

### Similarity

The gallery with embedding awareness. Identical layout and navigation to
Browse, with two additions on leaf album pages:

- Images that have stored embeddings show a visual indicator. Each such
  image has a **Similar** link that opens the Similar view for that image.
- An **Embed** button appears at the top of the page. Clicking it starts
  the embedding process for the current album and streams progress line by
  line via Server-Sent Events. At the virtual top-level album in a multi-root
  setup, Embed runs once for each configured real root. A **Cancel** button
  stops an in-progress embed; completed batches are already saved and will be
  skipped on the next run.

### Compare

Loads all clusters for a given model and similarity threshold — re-clustering
on each page load. Adjust the model (`clip` / `sscd`) and threshold via links
or the URL query string (`?model=clip&threshold=0.85`). Clusters with more
than 100 members are listed separately at the bottom.

Each cluster entry shows a thumbnail strip of its members. Click a cluster to
open the contact sheet.

### Contact sheet (cluster detail)

Shows all members of a single cluster as a grid, ordered by quality rank
(best first). Each cell shows the thumbnail, filename, pixel dimensions, and
quality tier. Clicking the thumbnail opens the full-size image in a new tab.

Members missing from disk are cleaned up automatically when the page is
loaded: their rows are deleted, and if only one member remains, the cluster
itself is deleted and the page redirects back to Compare. Hidden members
(see below) are never deleted this way — they are only filtered out of the
view — but if fewer than two members remain visible after applying both the
missing-file cleanup and the hidden-image filter, the page still redirects
back to Compare rather than showing a one-image contact sheet.

For an authorized request (see "Hiding workflow" below), a **Hide** button
beneath each image opens a confirmation modal; confirming removes that image
from the grid immediately, without a full page reload.

### Similar

Shows the most similar images in the same directory as a given image,
ranked by cosine similarity. Switch between CLIP and SSCD results with the
model links. From any thumbnail in Browse or Similarity view, use the
**Similar** link to reach this page.

If the focal image is itself hidden, the page shows a short notice and a
link back to the directory instead of any similarity results. For an
authorized request, a **Hide** button is shown beneath the focal image and
beneath the closest match.

### Semantic search

The top-level **Semantic** page runs a CLIP text-to-image search across all
embedded images in the library. Choose how many matches to show with the
`results` field. The page returns the first `N` matches as clickable
thumbnails. Selecting a result opens the full-size image directly.

---

## Hiding workflow

Hiding is a display-layer decision, not a deletion. A hidden image's archive
file is left on disk untouched; imhandler simply stops showing it, thumbnailing
it, embedding it, or clustering it, until it is restored.

Hide and Restore, and the Hidden images page, are only available to a request
the host project's authorization hook approves (by default, `is_staff` on an
authenticated Django user; a host project such as `llime` may substitute its
own check — see `imhandler-django-impl.md`). An unauthorized request never
sees a Hide button, the Hidden images nav link, or the confirmation modal, and
a direct POST to the Hide/Restore endpoints is rejected.

1. From a cluster contact sheet or the Similar page, click **Hide** beneath
   an image. A confirmation modal shows the thumbnail and full path and warns
   that the file on disk is left alone.
2. Confirming removes the image from every viewer surface immediately: the
   Browse/Similarity grids, Compare's clustering, Similar's neighbor lists,
   and the direct `image`/`thumb` URLs, which now 404. A client that
   revalidates against the server (the normal case, since these URLs are
   served `private, no-cache`) gets that 404 on its very next request. A
   client whose only copy predates the deploy of that header — cached under
   the old unvalidated `max-age=3600` policy — can keep rendering it from
   its own cache until that copy expires; see `imhandler-specs.md`'s caching
   rules and the rollout note in `upgrades/blacklist.md`.
3. Visit **Hidden images** (linked from the Index page) to see every
   currently hidden path, flagged if it is missing from disk. Click
   **Show again** next to a path to restore it: it becomes visible and
   eligible for thumbnailing/embedding/clustering again on the next scan.

No deletion is performed by the web UI, and hiding an image never modifies
its file, mtime, or any cluster/database row for another, still-visible
image.

---

## Embedding from the web UI

On any Similarity view page, click **Embed** to run `imh embed` for the
current album directly from the browser. At the virtual top-level album in a
multi-root setup, **Embed** runs once for each configured real root. Progress
is streamed line by line. Click **Cancel** to stop; already-completed batches
are saved and will be skipped on the next run.

This is equivalent to running `imh embed <album-path>` from the command line,
or one `imh embed` run per configured root for the multi-root virtual top
level.
