"""Django view logic for the image handler.

Deployed Django projects normally include ``imhandler.djview.urls``.
``ImageHandlerViewSet`` remains available for direct reuse by other front ends.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import mimetypes
import queue
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings  # type: ignore[import-untyped]
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse  # type: ignore[import-untyped]
from django.shortcuts import redirect, render  # type: ignore[import-untyped]
from django.urls import NoReverseMatch, reverse  # type: ignore[import-untyped]
from django.utils.http import http_date, parse_http_date_safe  # type: ignore[import-untyped]
from django.views.decorators.csrf import csrf_exempt  # type: ignore[import-untyped]
from django.views.decorators.http import require_POST  # type: ignore[import-untyped]

from imhandler import appconfig, blacklist
from imhandler.cache import image_root_entries as _image_root_entries
from imhandler.filter_sort import SortKey, filter_and_sort
from imhandler.models import ImageEntry
from imhandler.scanner import scan_all
from imhandler.thumbnailer import get_or_create

_NS = 'image_handler'
_T  = 'image_handler'

_active_embeds: dict[str, threading.Event] = {}


def _cancel_flag_path(album_path: Path) -> Path:
    h = hashlib.sha256(str(album_path).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f'imh_cancel_{h}'


class _CancelToken:
    """Checked by embed_images each batch; works across gunicorn workers via flag file."""
    def __init__(self, event: threading.Event, flag: Path) -> None:
        self._event = event
        self._flag = flag

    def is_set(self) -> bool:
        return self._event.is_set() or self._flag.exists()


class _MultiCancelToken:
    """Checked by embed_images for multi-root runs."""
    def __init__(self, event: threading.Event, flags: list[Path]) -> None:
        self._event = event
        self._flags = flags

    def is_set(self) -> bool:
        return self._event.is_set() or any(flag.exists() for flag in self._flags)


def _get_roots() -> 'list[tuple[Path, str]] | None':
    try:
        return _image_root_entries()
    except EnvironmentError:
        return None


def _default_blacklist_authorizer(request) -> bool:
    user = getattr(request, 'user', None)
    return bool(user is not None and user.is_authenticated and user.is_staff)


def _can_manage_blacklist(request) -> bool:
    hook = getattr(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', _default_blacklist_authorizer)
    return bool(hook(request))


def _path_to_album_rel(dir_path: Path, roots: list[Path]) -> str:
    """Convert an absolute directory path to an album rel string for URL building."""
    for root in roots:
        try:
            sub_rel = dir_path.relative_to(root)
            if len(roots) == 1:
                return str(sub_rel)
            return root.name if str(sub_rel) == '.' else str(Path(root.name) / sub_rel)
        except ValueError:
            continue
    return '.'


def _resolve_album_path(album_rel: str, roots: list[Path]) -> Path | None:
    """Resolve an album_rel URL parameter to an absolute filesystem path.

    For a single root, album_rel is directly relative to that root.
    For multiple roots the first path component is the root's basename.
    Returns None when the rel cannot be resolved.
    """
    if len(roots) == 1:
        root = roots[0]
        if album_rel == '.':
            return root
        candidate = (root / album_rel).resolve()
        return candidate if candidate.is_relative_to(root) else None
    if album_rel == '.':
        return None
    parts = Path(album_rel).parts
    root_name, *rest = parts
    for root in roots:
        if root.name == root_name:
            if not rest:
                return root
            candidate = (root / Path(*rest)).resolve()
            return candidate if candidate.is_relative_to(root) else None
    return None


def _build_breadcrumb(album_rel, root_name, browse_url):
    """Return list of {name, url} dicts from root down to album_rel."""
    crumbs = [{'name': root_name, 'url': browse_url + '?album=.', 'rel': '.'}]
    rel = Path(album_rel)
    if str(rel) == '.':
        return crumbs
    for i, part in enumerate(rel.parts):
        ancestor_rel = str(Path(*rel.parts[:i + 1]))
        crumbs.append({
            'name': part,
            'url': browse_url + '?' + urlencode({'album': ancestor_rel}),
            'rel': ancestor_rel,
        })
    return crumbs


def _url(name, **kwargs):
    return reverse(f'{_NS}:{name}', kwargs=kwargs or None)


def _safe_url(name, **kwargs):
    try:
        return _url(name, **kwargs)
    except NoReverseMatch:
        return None


def _embed_targets(album_rel: str, roots: list[Path]) -> list[Path]:
    """Resolve an embed request to one or more concrete directories."""
    if album_rel == '.' and len(roots) > 1:
        return roots
    album_path = _resolve_album_path(album_rel, roots)
    if album_path is None or not album_path.is_dir():
        return []
    return [album_path]


def _embed_job_key(targets: list[Path]) -> str:
    return '||'.join(sorted(str(path) for path in targets))


def _not_found(message: str = 'Not found') -> HttpResponse:
    """A 404 for the image/thumb media endpoints.

    Returned rather than raised as Http404 so it can carry
    Cache-Control: no-store -- the block itself must never be cached, or a
    later restore would not take effect immediately for a client holding a
    cached 404.
    """
    resp = HttpResponse(message, status=404, content_type='text/plain; charset=utf-8')
    resp['Cache-Control'] = 'no-store'
    return resp


def _is_not_modified(request, last_modified_epoch: float) -> bool:
    """True if the request's If-Modified-Since matches last_modified_epoch.

    Compared by hand, in the view body, rather than via Django's @condition
    decorator: that decorator answers before the view runs, so a blacklist
    check inside the view would never execute for a revalidating request --
    exactly the bypass that would let a client's cached copy of a
    since-hidden image keep rendering.
    """
    header = request.META.get('HTTP_IF_MODIFIED_SINCE')
    if not header:
        return False
    since = parse_http_date_safe(header)
    if since is None:
        return False
    return int(last_modified_epoch) <= since


class ImageHandlerViewSet:

    def __init__(self, *, base_nav, nav=None, nav_suffix=None,
                 nav_rel=None, base_nav_rel=None,
                 index_specs_url=None):
        self._base_nav = base_nav
        self._base_nav_rel = (
            base_nav_rel if base_nav_rel is not None
            else nav_rel if nav_rel is not None
            else base_nav
        )
        self._nav_prefix = list(nav) if nav else []
        self._nav_suffix = list(nav_suffix) if nav_suffix else []
        self._index_specs_url = index_specs_url
        self._nav_index = self._nav_prefix + [
            {'name': 'Browse',     'url': 'browse/'},
            {'name': 'Similarity', 'url': 'similarity/'},
            {'name': 'Semantic',   'url': 'semantic/'},
            {'name': 'Compare',    'url': 'compare/'},
        ] + self._nav_suffix
        self._nav = [self._relative_nav_item(n) for n in self._nav_index]
        self._nav_rel = [self._relative_nav_item(n) for n in self._nav]

    @staticmethod
    def _relative_nav_item(item):
        url = item['url']
        if url.startswith('/') or '://' in url:
            return {'name': item['name'], 'url': url}
        return {'name': item['name'], 'url': '../' + url}

    def _ctx(self, extra):
        return {'base_nav': self._base_nav, 'nav': self._nav, **extra}

    def _t(self, name):
        return f'{_T}/{name}'

    # ── index ──────────────────────────────────────────────────────────────

    def index(self, request):
        semantic_url = _safe_url('semantic_search')
        hidden_url = _url('hidden_images') if _can_manage_blacklist(request) else None
        return render(request, self._t('index.html'),
                      self._ctx({
                          'title': 'Image Handler',
                          'nav': self._nav_index,
                          'semantic_url': semantic_url,
                          'specs_url': self._index_specs_url,
                          'hidden_url': hidden_url,
                      }))

    # ── compare / cluster ──────────────────────────────────────────────────

    def compare(self, request):
        from imhandler.clusterer import cluster_images
        from imhandler.db import open_db, get_cluster_member_rows

        model = request.GET.get('model', 'clip')
        if model not in ('clip', 'sscd'):
            model = 'clip'
        try:
            threshold = float(request.GET.get('threshold', '0.85'))
            threshold = max(0.0, min(1.0, threshold))
        except ValueError:
            threshold = 0.85

        try:
            conn = open_db()
        except EnvironmentError as e:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Compare',
                'message': 'Cannot open image database.',
                'detail': str(e),
            }), status=500)

        try:
            num_clusters = cluster_images(conn, model=model, threshold=threshold)
            rows = get_cluster_member_rows(conn, model=model, threshold=threshold)
        except blacklist.BlacklistError as e:
            conn.close()
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Compare', 'message': 'Cannot read the blacklist.', 'detail': str(e),
            }), status=500)
        conn.close()

        thumb_base = _url('thumb')
        image_base = _url('image')
        clusters = []
        cluster_map = {}
        for row in rows:
            cid = row['cluster_id']
            if cid not in cluster_map:
                entry = {'id': cid, 'members': []}
                cluster_map[cid] = entry
                clusters.append(entry)
            cluster_map[cid]['members'].append({
                'name': Path(row['path']).name,
                'thumb_url': thumb_base + '?' + urlencode({'path': row['path']}),
                'image_url': image_base + '?' + urlencode({'path': row['path']}),
            })

        clusters = [c for c in clusters if len(c['members']) > 1]
        large_threshold = 100
        normal = [c for c in clusters if len(c['members']) <= large_threshold]
        large  = [c for c in clusters if len(c['members']) >  large_threshold]
        compare_base = _url('compare')
        return render(request, self._t('compare.html'), self._ctx({
            'title': 'Compare',
            'num_clusters': num_clusters,
            'clusters': normal,
            'large_clusters': large,
            'model': model,
            'threshold': threshold,
            'clip_url': compare_base + '?' + urlencode({'model': 'clip', 'threshold': threshold}),
            'sscd_url': compare_base + '?' + urlencode({'model': 'sscd', 'threshold': threshold}),
        }))

    def cluster_detail(self, request, cluster_id):
        from imhandler.db import open_db, get_cluster_members, cleanup_missing_members

        model = request.GET.get('model', 'clip')
        if model not in ('clip', 'sscd'):
            model = 'clip'
        try:
            threshold = float(request.GET.get('threshold', '0.85'))
            threshold = max(0.0, min(1.0, threshold))
        except ValueError:
            threshold = 0.85

        compare_base = _url('compare')
        back_url = compare_base + '?' + urlencode({'model': model, 'threshold': threshold})

        try:
            conn = open_db()
        except EnvironmentError as e:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Cluster',
                'message': 'Cannot open image database.',
                'detail': str(e),
            }), status=500)

        try:
            rows = get_cluster_members(conn, cluster_id)
        except blacklist.BlacklistError as e:
            conn.close()
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Cluster', 'message': 'Cannot read the blacklist.', 'detail': str(e),
            }), status=500)

        if len(rows) < 2:
            # rows is already blocked-filtered (get_cluster_members(),
            # db.py), so a cluster whose members are all/mostly hidden
            # collapses to the same shape as "no such cluster" -- tell them
            # apart with a direct existence check before deciding.
            cluster_exists = conn.execute(
                'SELECT 1 FROM Clusters WHERE id = ?', (cluster_id,)
            ).fetchone() is not None
            conn.close()
            if not cluster_exists:
                raise Http404('Cluster not found')
            return redirect(_url('compare'))

        missing_ids, remaining = cleanup_missing_members(conn, cluster_id)
        if remaining <= 1:
            # Too few *real files* left, regardless of blacklist status --
            # collapse the cluster's DB rows entirely.
            conn.execute('DELETE FROM ClusterMembership WHERE cluster_id = ?', (cluster_id,))
            conn.execute('DELETE FROM Clusters WHERE id = ?', (cluster_id,))
            conn.commit()
            conn.close()
            return redirect(_url('compare'))

        missing_id_set = set(missing_ids)
        visible_rows = [r for r in rows if r['image_id'] not in missing_id_set]
        if len(visible_rows) < 2:
            # Blacklist-aware, unlike `remaining` above: catches "too few
            # members are actually displayable" without deleting anything --
            # the cluster may still be a real pair once the hidden member is
            # restored.
            conn.close()
            return redirect(_url('compare'))

        conn.close()

        thumb_base = _url('thumb')
        image_base = _url('image')
        can_manage_blacklist = _can_manage_blacklist(request)
        members = []
        for row in visible_rows:
            members.append({
                'path': row['path'],
                'name': Path(row['path']).name,
                'thumb_url': thumb_base + '?' + urlencode({'path': row['path']}),
                'image_url': image_base + '?' + urlencode({'path': row['path']}),
                'width': row['width'],
                'height': row['height'],
                'laplacian_score': row['laplacian_score'],
                'hf_power_ratio': row['hf_power_ratio'],
                'blocking_score': row['blocking_score'],
                'sharpness_consistency': row['sharpness_consistency'],
                'quality_tier': row['quality_tier'],
            })

        return render(request, self._t('cluster_detail.html'), {
            'title': f'Cluster {cluster_id}',
            'cluster_id': cluster_id,
            'members': members,
            'can_manage_blacklist': can_manage_blacklist,
            'back_url': back_url,
            'base_nav': self._base_nav_rel,
            'nav': self._nav_rel,
        })

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

    # ── browse / similarity ────────────────────────────────────────────────

    def _browse_impl(self, request, similarity_mode):
        root_entries = _get_roots()
        if root_entries is None:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Image Handler',
                'message': 'image_root is not configured or does not exist.',
                'detail': ', '.join(appconfig.image_roots) or '(not set in etc/imhandler.conf)',
            }), status=500)

        album_rel = request.GET.get('album', '.').strip() or '.'
        sort_key = request.GET.get('sort', 'name')
        if sort_key not in ('name', 'mtime', 'size'):
            sort_key = 'name'

        root_paths = [p for p, _ in root_entries]
        try:
            root_album = scan_all()
        except blacklist.BlacklistError as e:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Image Handler', 'message': 'Cannot read the blacklist.', 'detail': str(e),
            }), status=500)
        root_name = root_entries[0][1] if len(root_entries) == 1 else 'Images'
        current = root_album if album_rel == '.' else root_album.find(album_rel)
        if current is None:
            current = root_album

        section_url = _url('similarity_browse' if similarity_mode else 'browse')
        breadcrumb = _build_breadcrumb(str(current.rel_path), root_name, section_url)
        section_label = 'Similarity' if similarity_mode else 'Browse'
        title = f'{section_label}: {breadcrumb[-1]["name"]}'
        parent_url = breadcrumb[-2]['url'] if len(breadcrumb) > 1 else None

        thumb_base = _url('thumb')
        image_base = _url('image')
        current_album_rel = str(current.rel_path)
        embed_targets = _embed_targets(current_album_rel, root_paths)

        embed_stream_url = (
            _url('embed_stream') + '?' + urlencode({'album': current_album_rel})
            if similarity_mode and embed_targets else None
        )
        if current.children:
            children = [
                {
                    'name': child.name,
                    'rel': str(child.rel_path),
                    'count': child.image_count(),
                    'url': section_url + '?' + urlencode({'album': str(child.rel_path)}),
                }
                for child in current.children
            ]
            return render(request, self._t('browse.html'), self._ctx({
                'title': title,
                'breadcrumb': breadcrumb,
                'parent_url': parent_url,
                'children': children,
                'images': None,
                'sort_key': sort_key,
                'cache_missing': False,
                'similarity_mode': similarity_mode,
                'embed_stream_url': embed_stream_url,
            }))
        else:
            similar_base = _url('similar')
            images = filter_and_sort(current.images, sort=SortKey(sort_key))
            image_list = [
                {
                    'name': img.path.name,
                    'path': str(img.path),
                    'thumb_url': thumb_base + '?' + urlencode({'path': str(img.path)}),
                    'image_url': image_base + '?' + urlencode({'path': str(img.path)}),
                    'similar_url': similar_base + '?' + urlencode({'path': str(img.path)}),
                    'has_similar': False,
                }
                for img in images
            ]
            if similarity_mode and image_list:
                try:
                    from imhandler.db import open_db, get_embedded_paths
                    conn = open_db()
                    has_similar_paths = get_embedded_paths(conn, [img['path'] for img in image_list])
                    conn.close()
                    for img in image_list:
                        img['has_similar'] = img['path'] in has_similar_paths
                except Exception:
                    pass
            return render(request, self._t('browse.html'), self._ctx({
                'title': title,
                'breadcrumb': breadcrumb,
                'parent_url': parent_url,
                'children': None,
                'images': image_list,
                'sort_key': sort_key,
                'cache_missing': not appconfig.cache_dir,
                'similarity_mode': similarity_mode,
                'embed_stream_url': embed_stream_url,
            }))

    def browse(self, request):
        return self._browse_impl(request, similarity_mode=False)

    def similarity_browse(self, request):
        return self._browse_impl(request, similarity_mode=True)

    # ── similar ────────────────────────────────────────────────────────────

    def similar(self, request):
        from imhandler.db import open_db
        from imhandler.embedder import find_similar

        path_str = request.GET.get('path', '')
        if not path_str:
            raise Http404('No path given')

        model = request.GET.get('model', 'clip')
        if model not in ('clip', 'sscd'):
            model = 'clip'

        root_entries = _get_roots()
        if root_entries is None:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Similar',
                'message': 'image_root is not configured or does not exist.',
                'detail': ', '.join(appconfig.image_roots) or '(not set in etc/imhandler.conf)',
            }), status=500)

        root_paths = [p for p, _ in root_entries]
        path = Path(path_str).resolve()
        if not any(path.is_relative_to(r) for r in root_paths):
            raise Http404('Path not under any configured root')
        if not path.is_file():
            raise Http404('Image not found')

        album_rel = _path_to_album_rel(path.parent, root_paths)
        browse_url = _url('similarity_browse') + '?' + urlencode({'album': album_rel})
        thumb_base = _url('thumb')
        image_base = _url('image')
        similar_base = _url('similar')
        can_manage_blacklist = _can_manage_blacklist(request)

        try:
            blocked = blacklist.load_if_configured()
            if path in blocked:
                return render(request, self._t('similar.html'), self._ctx({
                    'title': path.name, 'name': path.name,
                    'hidden_focal': True, 'browse_url': browse_url,
                    'can_manage_blacklist': can_manage_blacklist,
                }))
            conn = open_db()
            target_row, raw_neighbors = find_similar(conn, path, model, blocked=blocked)
            conn.close()
        except (EnvironmentError, blacklist.BlacklistError) as e:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Similar',
                'message': 'Cannot load image data.',
                'detail': str(e),
            }), status=500)

        no_embedding = target_row is None or target_row[f'{model}_embedding'] is None
        neighbors = [
            {
                'path': nb['path'],
                'name': Path(nb['path']).name,
                'similarity': nb['similarity'],
                'width': nb['width'],
                'height': nb['height'],
                'thumb_url': thumb_base + '?' + urlencode({'path': nb['path']}),
                'image_url': image_base + '?' + urlencode({'path': nb['path']}),
                'similar_url': similar_base + '?' + urlencode({'path': nb['path'], 'model': model}),
            }
            for nb in raw_neighbors
        ]

        closest = neighbors[0] if neighbors else None
        other_neighbors = neighbors[1:]
        focal_width  = target_row['width']  if target_row and not no_embedding else None
        focal_height = target_row['height'] if target_row and not no_embedding else None

        return render(request, self._t('similar.html'), self._ctx({
            'title': path.name,
            'path': str(path),
            'name': path.name,
            'focal_width': focal_width,
            'focal_height': focal_height,
            'image_url': image_base + '?' + urlencode({'path': str(path)}),
            'thumb_url': thumb_base + '?' + urlencode({'path': str(path)}),
            'closest': closest,
            'neighbors': other_neighbors,
            'no_embedding': no_embedding,
            'model': model,
            'clip_url': similar_base + '?' + urlencode({'path': str(path), 'model': 'clip'}),
            'sscd_url': similar_base + '?' + urlencode({'path': str(path), 'model': 'sscd'}),
            'browse_url': browse_url,
            'can_manage_blacklist': can_manage_blacklist,
        }))

    def semantic_search(self, request):
        from imhandler.db import open_db
        from imhandler.embedder import find_semantic

        query = request.GET.get('q', '').strip()
        try:
            limit = int(request.GET.get('n', '10'))
        except ValueError:
            limit = 10
        limit = max(1, min(200, limit))

        root_entries = _get_roots()
        if root_entries is None:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Semantic Search',
                'message': 'image_root is not configured or does not exist.',
                'detail': ', '.join(appconfig.image_roots) or '(not set in etc/imhandler.conf)',
            }), status=500)

        thumb_base = _url('thumb')
        image_base = _url('image')

        try:
            conn = open_db()
            raw_results, candidate_count = find_semantic(conn, query, n=limit)
            conn.close()
        except EnvironmentError as e:
            return render(request, self._t('error.html'), self._ctx({
                'title': 'Semantic Search',
                'message': 'Cannot open image database.',
                'detail': str(e),
            }), status=500)

        results = [
            {
                'path': row['path'],
                'name': Path(row['path']).name,
                'similarity': row['similarity'],
                'width': row['width'],
                'height': row['height'],
                'thumb_url': thumb_base + '?' + urlencode({'path': row['path']}),
                'image_url': image_base + '?' + urlencode({'path': row['path']}),
            }
            for row in raw_results
        ]

        return render(request, self._t('semantic.html'), self._ctx({
            'title': 'Semantic Search',
            'query': query,
            'limit': limit,
            'candidate_count': candidate_count,
            'results': results,
        }))

    # ── thumb / image ──────────────────────────────────────────────────────

    @staticmethod
    def thumb(request):
        path_str = request.GET.get('path', '')
        if not path_str:
            return _not_found('No path given')
        root_entries = _get_roots()
        if root_entries is None:
            return _not_found('image_root not configured')
        root_paths = [p for p, _ in root_entries]
        path = Path(path_str).resolve()
        if not any(path.is_relative_to(r) for r in root_paths):
            return _not_found('Path not under any configured root')
        if not path.is_file():
            return _not_found('Image not found')

        try:
            size = int(request.GET.get('size', '200'))
        except ValueError:
            size = 200
        size = max(50, min(800, size))

        try:
            blocked = blacklist.load_if_configured()
        except blacklist.BlacklistError:
            return _not_found('Image not found')
        if path in blocked:
            return _not_found('Image not found')

        root = next(r for r in root_paths if path.is_relative_to(r))
        entry = ImageEntry(path=path, rel_path=path.relative_to(root), mtime=path.stat().st_mtime)
        try:
            thumb_path = get_or_create(entry, long_edge=size, blocked=blocked)
        except EnvironmentError as e:
            return _not_found(f'Cache unavailable: {e}')
        except Exception:
            return _not_found('Thumbnail generation failed')

        # The blocked check above runs before this conditional check: a
        # blocked image never reaches here to be compared against
        # If-Modified-Since, so a stale validator can never be answered 304
        # for an image that has since been hidden.
        last_modified = thumb_path.stat().st_mtime
        header_date = http_date(last_modified)
        if _is_not_modified(request, last_modified):
            resp = HttpResponse(status=304)
        else:
            resp = HttpResponse(thumb_path.read_bytes(), content_type='image/jpeg')
        resp['Cache-Control'] = 'private, no-cache'
        resp['Last-Modified'] = header_date
        return resp

    @staticmethod
    def image(request):
        path_str = request.GET.get('path', '')
        if not path_str:
            return _not_found('Missing path')
        root_entries = _get_roots()
        if root_entries is None:
            return _not_found('image_root not configured')
        root_paths = [p for p, _ in root_entries]
        path = Path(path_str).resolve()
        if not any(path.is_relative_to(r) for r in root_paths):
            return _not_found('Path not under any configured root')
        if not path.is_file():
            return _not_found('Image not found')

        try:
            blocked = blacklist.load_if_configured()
        except blacklist.BlacklistError:
            return _not_found('Image not found')
        if path in blocked:
            return _not_found('Image not found')

        content_type, _ = mimetypes.guess_type(str(path))
        content_type = content_type or 'application/octet-stream'

        last_modified = path.stat().st_mtime
        header_date = http_date(last_modified)
        if _is_not_modified(request, last_modified):
            resp = HttpResponse(status=304)
            resp['Cache-Control'] = 'private, no-cache'
            resp['Last-Modified'] = header_date
            return resp

        def _iter(p, chunk=65536):
            with open(p, 'rb') as f:
                while True:
                    data = f.read(chunk)
                    if not data:
                        break
                    yield data

        resp = HttpResponse(_iter(path), content_type=content_type)
        resp['Content-Length'] = path.stat().st_size
        resp['Cache-Control'] = 'private, no-cache'
        resp['Last-Modified'] = header_date
        return resp

    # ── embed ──────────────────────────────────────────────────────────────

    @staticmethod
    def embed_stream(request):
        from imhandler.db import open_db
        from imhandler.embedder import embed_images

        def _event(obj):
            return f'data: {json.dumps(obj)}\n\n'

        def _error_stream(msg):
            yield _event({'type': 'error', 'message': msg})

        root_entries = _get_roots()
        if root_entries is None:
            return StreamingHttpResponse(_error_stream('image_root not configured'),
                                         content_type='text/event-stream')

        album_rel = request.GET.get('album', '.').strip() or '.'
        targets = _embed_targets(album_rel, [p for p, _ in root_entries])
        if not targets:
            return StreamingHttpResponse(_error_stream('Album cannot be resolved'),
                                         content_type='text/event-stream')

        q: queue.Queue = queue.Queue()
        cancel_event = threading.Event()
        cancel_flags = [_cancel_flag_path(path) for path in targets]
        for flag in cancel_flags:
            flag.unlink(missing_ok=True)
        cancel = (
            _CancelToken(cancel_event, cancel_flags[0])
            if len(cancel_flags) == 1
            else _MultiCancelToken(cancel_event, cancel_flags)
        )
        key = _embed_job_key(targets)
        _active_embeds[key] = cancel_event

        class _Writer(io.RawIOBase):
            def writable(self):
                return True

            def write(self, b):
                s = b.decode() if isinstance(b, (bytes, bytearray)) else b
                for line in s.splitlines():
                    if line.strip():
                        q.put({'type': 'output', 'message': line.rstrip()})
                return len(b)

        def _run():
            try:
                writer = io.TextIOWrapper(_Writer(), line_buffering=True)
                with contextlib.redirect_stdout(writer):
                    conn = open_db()
                    processed = 0
                    skipped = 0
                    total_targets = len(targets)
                    for index, target in enumerate(targets, start=1):
                        if cancel.is_set():
                            print('Cancelled.', flush=True)
                            break
                        q.put({
                            'type': 'output',
                            'message': f'[{index}/{total_targets}] {target}',
                        })
                        p, s, _excluded = embed_images(
                            target, conn,
                            cancel=cancel,
                            on_progress=lambda pct, d, i=index, n=total_targets:
                                q.put({
                                    'type': 'progress',
                                    'pct': ((i - 1) * 100 + pct) // n,
                                    'dir': d,
                                }),
                        )
                        processed += p
                        skipped += s
                    conn.close()
                q.put({'type': 'done', 'processed': processed, 'skipped': skipped})
            except Exception as exc:
                q.put({'type': 'error', 'message': str(exc)})
            finally:
                _active_embeds.pop(key, None)
                for flag in cancel_flags:
                    flag.unlink(missing_ok=True)

        def _stream():
            if len(targets) == 1:
                start_msg = f'Embedding {targets[0]} …'
            else:
                start_msg = f'Embedding {len(targets)} roots …'
            yield _event({'type': 'start', 'message': start_msg})
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            while True:
                try:
                    msg = q.get(timeout=2)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                yield _event(msg)
                if msg['type'] in ('done', 'error'):
                    break

        resp = StreamingHttpResponse(_stream(), content_type='text/event-stream')
        resp['Cache-Control'] = 'no-cache'
        resp['X-Accel-Buffering'] = 'no'
        return resp

    @staticmethod
    @csrf_exempt
    def embed_cancel(request):
        if request.method != 'POST':
            return JsonResponse({'error': 'POST required'}, status=405)
        album_rel = request.POST.get('album', '.').strip() or '.'
        root_entries = _get_roots()
        if root_entries is None:
            return JsonResponse({'error': 'image_root not configured'}, status=500)
        targets = _embed_targets(album_rel, [p for p, _ in root_entries])
        if not targets:
            return JsonResponse({'error': 'Album cannot be resolved'}, status=400)
        # Set event if the job is running in this worker
        event = _active_embeds.get(_embed_job_key(targets))
        if event:
            event.set()
        # Create flag files so the other worker also sees the cancel
        for target in targets:
            _cancel_flag_path(target).touch()
        return JsonResponse({'ok': True})
