# Image Handler Django Views — Implementation Notes

Implementation details for `imhandler.djview`. See
`imhandler-django-man.md` for usage and `imhandler-specs.md` for the API
reference.

---

## Host applications

Two Django projects integrate `djview`:

| Project | Path | Config variant |
|---------|------|----------------|
| `knip` | `~/prj/qat/knip/` | `qat` (`[qat.imhandler.core]`) |
| `llime` | `~/prj/grove/llime/` | `hty7` (`[hty7.imhandler.core]`) |

Both are structured identically: the project installs
`imhandler.djview`, sets `IMHANDLER_VARIANT` for its config section, and
includes the shared URL module at `/image_handler/`. The shared views import
`base.lib.tools` from the host project for navigation.

---

## Integration pattern

The shared `imhandler.djview.views` module constructs a viewset instance
and exposes its methods as module-level view callables:

```python
from imhandler.djview import ImageHandlerViewSet
from base.lib.tools import nav as base_nav, nav_rel as base_nav_rel, specs_nav_item

_nav_suffix = [specs_nav_item('imhandler')]

_vs = ImageHandlerViewSet(
    base_nav=base_nav,
    base_nav_rel=base_nav_rel,
    nav=[],
    nav_suffix=_nav_suffix,
    index_specs_url=_nav_suffix[0]['url'],
)

index                  = _vs.index
browse                 = _vs.browse
similarity_browse      = _vs.similarity_browse
semantic_search        = _vs.semantic_search
compare                = _vs.compare
cluster_detail         = _vs.cluster_detail
hide_image             = _vs.hide_image
restore_image          = _vs.restore_image
hidden_images          = _vs.hidden_images
similar                = _vs.similar
thumb                  = _vs.thumb
image                  = _vs.image
embed_stream           = _vs.embed_stream
embed_cancel           = _vs.embed_cancel   # CSRF-exempt static method
```

The shared `imhandler.djview.urls` module uses the namespace
`image_handler`:

```python
app_name = 'image_handler'

urlpatterns = [
    path('',                         views.index,                  name='index'),
    path('browse/',                  views.browse,                 name='browse'),
    path('similarity/',              views.similarity_browse,      name='similarity_browse'),
    path('semantic/',                views.semantic_search,        name='semantic_search'),
    path('compare/',                 views.compare,                name='compare'),
    path('cluster/<int:cluster_id>/', views.cluster_detail,        name='cluster_detail'),
    path('embed-stream/',            views.embed_stream,           name='embed_stream'),
    path('embed-cancel/',            views.embed_cancel,           name='embed_cancel'),
    path('hide/',                    views.hide_image,             name='hide_image'),
    path('hidden/',                  views.hidden_images,          name='hidden_images'),
    path('restore/',                 views.restore_image,          name='restore_image'),
    path('similar/',                 views.similar,                name='similar'),
    path('thumb/',                   views.thumb,                  name='thumb'),
    path('image/',                   views.image,                  name='image'),
]
```

Both host apps include this under `image_handler/`, so the browse page is
at `/image_handler/browse/`, thumbnails at `/image_handler/thumb/`, etc.

In each host project's `settings.py`:

```python
INSTALLED_APPS = [
    ...,
    'imhandler.djview',
]

IMHANDLER_VARIANT = 'hty7'  # llime; knip uses 'qat'

# Optional -- see "Authorization" below. Falls back to is_staff if unset.
IMHANDLER_BLACKLIST_AUTHORIZER = _authorize_imhandler_blacklist
```

`imhandler.djview.apps.ImageHandlerDjviewConfig.ready()` calls
`appconfig.init_variant(IMHANDLER_VARIANT)`, so the variant difference lives
in configuration rather than in host-local app code.

### Authorization

Hide, Restore, and the Hidden images page are gated by
`settings.IMHANDLER_BLACKLIST_AUTHORIZER(request) -> bool`. If the host
project does not set it, `_default_blacklist_authorizer` is used:
`request.user is not None and request.user.is_authenticated and request.user.is_staff`.
`llime` wraps its existing document-viewer authorization hook (llime is
authenticated by the web server, outside Django, so this is not a plain
`is_staff` check there):

```python
# llime/config/settings.py
def _authorize_imhandler_blacklist(request):
    return _authorize_document_viewer(request, 'blacklist')

IMHANDLER_BLACKLIST_AUTHORIZER = _authorize_imhandler_blacklist
```

`knip` does not currently set `IMHANDLER_BLACKLIST_AUTHORIZER`, so it uses
the `is_staff` default as-is.

---

## View details

### `index`

Renders `image_handler/index.html` with section links (Browse, Similarity,
Semantic, Compare) as relative URLs, plus a **Hidden images** link — only
included in the context (and so only rendered) when
`_can_manage_blacklist(request)` is true for the current request.

### `browse` and `similarity_browse`

Both dispatch to `_browse_impl(request, similarity_mode)`.

- `?album=<rel>` selects the album relative to `image_root`; defaults to `.`
  (root).
- `?sort=name|mtime|size` controls image order; defaults to `name`.
- Interior albums render a child-list template; leaf albums render an image
  grid.
- In `similarity_mode`, the grid annotates each image with `has_similar`
  (True if the path has an embedding in the database), and the template
  shows an **Embed** button linked to `embed_stream`.

### `semantic_search`

Reads `?q=<text>` and optional `?n=<int>`.

1. Parses `n` with default `10`, clamped to `1..200`.
2. Calls `find_semantic(conn, query, n=n)`.
3. Renders the first `n` matches as clickable thumbnails, ordered by CLIP
   cosine similarity.
4. Each result links directly to `image`, so clicking a thumbnail opens the
   full-size image.

### `compare`

Called with `?model=clip|sscd&threshold=0.85`. Calls `cluster_images()` on
every page load (fast; typically < 1 s). Fetches all cluster members in a
single query via `get_cluster_member_rows()`. Clusters with > 100 members
are separated into `large_clusters` and rendered at the bottom of the page.

### `cluster_detail`

Path parameter `cluster_id`. Model and threshold come from GET params (used
only to build `back_url`, the link back to `compare`).

1. Calls `get_cluster_members(conn, cluster_id)` — already excludes hidden
   members.
2. If fewer than 2 rows come back, a direct `SELECT 1 FROM Clusters WHERE
   id = ?` distinguishes "no such cluster" (`Http404`) from "cluster
   collapsed because its members are hidden" (redirect to `compare`,
   nothing deleted) — `get_cluster_members` alone can't tell these apart,
   since both produce the same empty/near-empty result.
3. Calls `cleanup_missing_members(conn, cluster_id)` to delete rows for
   files genuinely missing from disk (blacklist-blind — a hidden-but-present
   file is never treated as missing here). If `remaining_count <= 1`, the
   cluster's rows are deleted and the page redirects to `compare`.
4. A second, blacklist-aware check: of the original `rows`, excluding the
   ones `cleanup_missing_members` just deleted, if fewer than 2 remain
   visible, redirect to `compare` **without deleting anything** — the
   cluster may still be a real pair once a hidden member is restored. This
   is the check step 3's `remaining_count` can't perform, since hiding
   doesn't change `remaining_count` by design.
5. Renders the surviving members plus `can_manage_blacklist =
   _can_manage_blacklist(request)`, which gates the Hide button and the
   shared confirmation modal in the template.

### `hide_image`

POST only. Returns 403 (`JsonResponse`) if `_can_manage_blacklist(request)`
is false. Reads `path` from the POST body; 400 if absent. Calls
`blacklist.add(path)`: `ValueError` (path not absolute/under a root/wrong
suffix) becomes 400, `BlacklistError`/`EnvironmentError` (store unreadable
or unwritable) becomes 500, otherwise `JsonResponse({'ok': True})`. This is
the endpoint the shared `_hide_modal.html` JS posts to via `fetch`.

### `restore_image`

POST only. Returns a plain (non-`JsonResponse`) 403 if unauthorized — this
endpoint is reached from an HTML `<form>` on the Hidden images page, not
`fetch`, so a redirect-shaped denial rather than a JSON body is what a
non-JS client actually needs. Reads `path` from the POST body; if present,
calls `blacklist.remove_stored(path)` (not `remove()` — this must succeed
even if `path`'s root has since been reconfigured away). A store failure
returns a plain-text 500 body. On success (or if `path` was already absent,
a no-op), redirects (302) to `hidden_images`.

### `hidden_images`

GET only. Returns the app's `error.html` at 403 if unauthorized, or at 500
if `blacklist.load()` raises (`BlacklistError`/`EnvironmentError` — a
corrupt store or an unconfigured `cache_dir`). Otherwise renders every
stored path together with `p.is_file()`, so the template can flag entries
that are hidden but no longer present on disk.

### `similar`

Reads `?path=` (absolute path) and `?model=clip|sscd`. Validates that the
path is under `image_root`. If `path` itself is hidden, renders
`hidden_focal=True` and a short notice instead of calling `find_similar` at
all. Otherwise calls `find_similar(conn, path, model, blocked=blocked)` (the
same snapshot used for the hidden-focal check, so the two never disagree)
and renders up to 8 results. For an authorized request
(`can_manage_blacklist`), the focal image and the closest match each have a
Hide button wired to the shared modal.

### `thumb`

Reads `?path=` and `?size=200` (int, clamped to 50–800). Before constructing
an `ImageEntry` or calling `get_or_create()`, loads the blacklist
(`blacklist.load_if_configured()`) and 404s immediately if `path` is
hidden — a `BlacklistError` from a corrupt/unreadable store also 404s
(fail closed), never 500. This check runs before the `If-Modified-Since`
comparison, so a client's stale cached validator can never be answered 304
for an image that has since been hidden. On success, returns the JPEG bytes
with `Cache-Control: private, no-cache` (always revalidated, never served
straight from a shared/browser cache without a round trip) and
`Last-Modified`. A 404 response (missing, out-of-root, hidden, or a
generation failure) sets `Cache-Control: no-store` instead, so the negative
result itself is never cached either.

### `image`

Reads `?path=`. Runs the identical hidden-path pre-check as `thumb` (before
the `If-Modified-Since` comparison, for the same cache-poisoning reason),
then streams the full-size original using a chunked generator (64 KB
chunks). Sets `Content-Length` and `Cache-Control: private, no-cache` (a 304
response gets the same header); a 404 sets `Cache-Control: no-store`. Uses
`mimetypes.guess_type` for the content type.

### `embed_stream`

GET, returns `text/event-stream`. Reads `?album=<rel>`.

1. Resolves `album` to one concrete directory, or to all configured roots when
   `album=.` and multiple roots are configured.
2. Creates a `threading.Event` and one flag-file path per concrete target
   (SHA-256 of each target path, stored in `tempfile.gettempdir()`).
3. Stores the event in `_active_embeds` keyed by the sorted target list.
4. Starts a background thread that calls `embed_images()` once per target with
   a cancel token wrapping the shared event and flag files, and an
   `on_progress` callback that puts `{'type': 'progress', 'pct': int, 'dir': str}`
   messages on a queue.
5. The main thread reads from the queue with a 2-second timeout and yields
   SSE frames. Keepalive comments (`: keepalive`) are sent on timeout. The
   stream ends on a `done` or `error` message.

SSE message types:

| Type | Fields | Meaning |
|------|--------|---------|
| `start` | `message` | Embedding begun |
| `output` | `message` | Line printed to stdout by embed_images |
| `progress` | `pct`, `dir` | Batch-level progress |
| `done` | `processed`, `skipped` | Finished successfully |
| `error` | `message` | Fatal error |

### `embed_cancel`

POST, CSRF-exempt (clients may be on a different origin or may lack a CSRF
token). Reads `album` from POST body.

1. Sets the `threading.Event` in `_active_embeds` if the job is running in
   this worker.
2. Touches every flag file for the resolved target set so jobs running in
   other gunicorn workers also stop.

---

## State

`imhandler.djview` holds no session state for hiding/restoring. Hidden-image
state lives entirely in the persistent `imhandler.blacklist` store (see
`imhandler-specs.md`), shared across requests, workers, and the CLI.

---

## URL generation

All internal URL construction uses `reverse('image_handler:<name>')`. The
`_url()` helper in the module wraps this. Media paths are always passed as
`?path=<absolute-path>` query parameters using `urlencode({'path': ...})`,
avoiding any path-segment encoding issues.
