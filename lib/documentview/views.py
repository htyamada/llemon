import os

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import active, config, covers, documents, paths, previews, subresources


# Content-Security-Policy (spec 1.6), defense in depth on top of the
# sanitization in previews.py -- not the only control, but the layer that
# still holds if the sanitizer is ever bypassed.
#
# EPUB/CBZ previews may load images only from this origin's own validated
# subresource routes. Markdown previews additionally allow http(s) images,
# because a locally-authored Markdown file legitimately embeds remote
# diagrams (spec 1.3, 4.4). Nothing may load script, frames, objects, or
# fonts from anywhere, in either case.
_CSP_HTML_SELF_IMAGES = "'self'"
_CSP_HTML_REMOTE_IMAGES = "'self' https: http:"

# Served bytes (covers, preview subresources, downloads) are not documents
# we render, but a viewer can navigate straight to one; `sandbox` puts any
# such navigation in an opaque origin with scripting disabled.
_CSP_RESOURCE = "default-src 'none'; sandbox"


def _html_csp(img_src: str) -> str:
    return (
        "default-src 'none'; "
        f"img-src {img_src}; "
        "style-src 'self'; "
        "font-src 'self'; "
        "script-src 'none'; "
        "object-src 'none'; "
        "frame-src 'none'; "
        "frame-ancestors 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )


def _secure_resource(response):
    """Headers for served document bytes: covers, preview subresources, downloads."""
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = _CSP_RESOURCE
    return response


def _render_page(request, template, context, *, allow_remote_images=False):
    context.setdefault('documentview_stylesheet_url', config.stylesheet_url())
    response = render(request, template, context)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = _html_csp(
        _CSP_HTML_REMOTE_IMAGES if allow_remote_images else _CSP_HTML_SELF_IMAGES
    )
    return response


def _render_view_page(request, context):
    """The detail page's CSP depends on which preview it embeds: only a
    Markdown preview may pull remote images.
    """
    preview_ctx = context.get('preview') or {}
    return _render_page(
        request,
        'documentview/view.html',
        context,
        allow_remote_images=preview_ctx.get('kind') == 'markdown',
    )


def _require(request, action):
    if not config.authorize(request, action):
        raise PermissionDenied()


def _breadcrumbs(rel_path):
    parts = [p for p in rel_path.split('/') if p] if rel_path else []
    crumbs = []
    acc = []
    for part in parts:
        acc.append(part)
        crumbs.append({'name': part, 'rel_path': '/'.join(acc)})
    return crumbs


def _view_mode(request):
    mode = request.GET.get('view', 'cover')
    return mode if mode in ('cover', 'title') else 'cover'


def _return_after_mutation(request):
    target = request.POST.get('return_to', '')
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(target)
    return None


def _variant_real_path(variant):
    """Plain, display-only resolved path for an "exported" badge lookup --
    not the hardened O_NOFOLLOW resolver used to actually open files.
    """
    return config.root().joinpath(*variant.rel_path.split('/')).resolve()


def _resolve_logical(rel_path):
    """Resolve a collection-relative path to `(document, resolved)`, or
    `(None, None)` if it doesn't name a valid document. `resolved` (already
    closed) carries the exact requested variant's metadata; regrouping to
    find the rest of `document`'s variants happens by rescanning the
    *requested* directory entry's own directory listing (spec 2.2) --
    deliberately not the resolved target's directory, since an
    in-hierarchy symlink (e.g. a curated `selected/` directory) can point
    at a file whose real directory has entirely different siblings. Using
    the target's directory there would regroup the document under the
    wrong basename/variant set and disagree with what browse() just showed
    for this same listing.
    """
    try:
        requested_rel = paths.normalize_rel_path(rel_path)
    except paths.PathError:
        return None, None
    try:
        resolved = paths.resolve_document(rel_path)
    except paths.PathError:
        return None, None
    resolved.close()

    *dir_parts, filename = requested_rel.split('/')
    directory_rel = '/'.join(dir_parts)
    try:
        dir_resolved = paths.resolve_directory(directory_rel)
    except paths.PathError:
        return None, None
    _, docs = documents.scan_directory(dir_resolved.abs_path, dir_resolved.rel_path)

    split = documents.strip_supported_suffix(filename)
    if split is None:
        return None, None
    basename, _suffix = split
    document = next(
        (
            d
            for d in docs
            if d.basename == basename
            and resolved.suffix in d.variants
            and d.variants[resolved.suffix].rel_path == requested_rel
        ),
        None,
    )
    if document is None:
        return None, None
    # Downstream code (display, re-resolution on the next request, and
    # active.add_active()) must see the path the caller actually acted on,
    # not the symlink-resolved canonical path -- otherwise activating a
    # document reached through an in-hierarchy symlink would silently
    # record the wrong alias (or none at all).
    resolved.rel_path = requested_rel
    resolved.abs_path = config.root().joinpath(*requested_rel.split('/'))
    return document, resolved


def index(request):
    return browse(request, '')


def browse(request, rel_path=''):
    _require(request, 'browse')
    try:
        resolved = paths.resolve_directory(rel_path)
    except paths.PathError:
        raise Http404('directory not found')
    subdirs, docs = documents.scan_directory(resolved.abs_path, resolved.rel_path)
    exported_paths = active.active_badge_paths()
    active_links = {
        doc.rel_path: {
            suffix
            for suffix, variant in doc.variants.items()
            if _variant_real_path(variant) in exported_paths
        }
        for doc in docs
    }
    context = {
        'rel_path': resolved.rel_path,
        'breadcrumbs': _breadcrumbs(resolved.rel_path),
        'subdirs': subdirs,
        'documents': docs,
        'active_links': active_links,
        'view_mode': _view_mode(request),
        'format_preference': documents.FORMAT_PREFERENCE,
        'exports_mode': False,
    }
    return _render_page(request, 'documentview/browse.html', context)


def _exports_context(request, *, notice=None, error=None):
    """Scan `active_dir` directly, one row per **symlink** entry -- not
    through `active.active_badge_paths()`, which is badge-only (§1). Hidden
    entries are skipped entirely; a non-symlink entry is never classified,
    it's surfaced separately as an "unexpected file".
    """
    config.validate_live()  # same live config check browse()/view() get via paths.resolve_*()
    active_dir = config.active_dir()
    try:
        entries = list(os.scandir(active_dir))
    except OSError:
        entries = []
    entries.sort(key=lambda e: e.name)

    docs = []
    invalid_links = []
    unexpected_entries = []

    for entry in entries:
        name = entry.name
        if name.startswith('.'):
            continue
        try:
            is_symlink = entry.is_symlink()
        except OSError:
            continue
        if not is_symlink:
            unexpected_entries.append(name)
            continue

        reason, real = active._classify_link(active_dir / name)
        if reason is not None:
            invalid_links.append({'link_name': name, 'label': active.REASON_LABELS[reason]})
            continue

        rel_parts = real.relative_to(config.root()).parts
        rel_path = '/'.join(rel_parts)
        suffix = real.suffix.lower()[1:]
        try:
            st = real.stat()
        except OSError:
            invalid_links.append({'link_name': name, 'label': active.REASON_LABELS[active.REASON_MISSING]})
            continue

        variant = documents.Variant(
            suffix=suffix, filename=real.name, rel_path=rel_path,
            mtime_ns=st.st_mtime_ns, size=st.st_size,
        )
        basename, _ = documents.strip_supported_suffix(real.name)
        doc = documents.LogicalDocument(
            basename=basename, directory='/'.join(rel_parts[:-1]), variants={suffix: variant},
        )
        # Two link names can resolve to the same real file (a hand-created
        # duplicate), which would collide as a dict key on doc.rel_path --
        # each row needs its own tile-to-link_name pairing, so it's carried
        # directly on the one-off doc instance instead.
        doc.link_name = name
        docs.append(doc)

    docs.sort(key=lambda d: d.sort_key())

    context = {
        'rel_path': '',
        'breadcrumbs': [],
        'subdirs': [],
        'documents': docs,
        'active_links': {},
        'view_mode': _view_mode(request),
        'format_preference': documents.FORMAT_PREFERENCE,
        'exports_mode': True,
        'invalid_links': invalid_links,
        'unexpected_entries': unexpected_entries,
    }
    if notice:
        context['active_notice'] = notice
    if error:
        context['active_error'] = error
    return context


def exports_index(request):
    _require(request, 'browse')
    return _render_page(request, 'documentview/browse.html', _exports_context(request))


@require_POST
def exports_prune(request):
    _require(request, 'mutate')
    removed = active.remove_invalid()
    return_response = _return_after_mutation(request)
    if return_response is not None:
        return return_response
    if removed:
        notice = f'Deleted {removed} invalid link{"s" if removed != 1 else ""}.'
    else:
        notice = 'No invalid links to delete.'
    return _render_page(request, 'documentview/browse.html', _exports_context(request, notice=notice))


def _build_preview(variant):
    """Format-appropriate fast-preview context for the `view` page (spec
    1.3). Never lets a preview failure break the detail page -- returns
    `None` on any resolution problem, same as a missing/malformed cover.
    """
    try:
        resolved = paths.resolve_document(variant.rel_path)
    except paths.PathError:
        return None
    try:
        if resolved.suffix == 'epub':
            data = previews.epub_preview(resolved, resolved.rel_path)
            return {'kind': 'epub', 'rel_path': resolved.rel_path, **data}
        if resolved.suffix == 'pdf':
            return {
                'kind': 'pdf',
                'rel_path': resolved.rel_path,
                'pages': list(range(1, previews.pdf_preview_pages(resolved) + 1)),
            }
        if resolved.suffix == 'cbz':
            return {
                'kind': 'cbz',
                'rel_path': resolved.rel_path,
                'pages': list(range(1, previews.cbz_preview_page_count(resolved) + 1)),
            }
        if resolved.suffix == 'md':
            return {'kind': 'markdown', 'html': previews.markdown_preview(resolved)}
        if resolved.suffix == 'txt':
            return {'kind': 'text', 'text': previews.text_preview(resolved)}
        return None
    finally:
        resolved.close()


def _variant_rows(document):
    exported_paths = active.active_badge_paths()
    rows = []
    for suffix in documents.FORMAT_PREFERENCE:
        variant = document.variants.get(suffix)
        if variant is None:
            continue
        rows.append({'variant': variant, 'exported': _variant_real_path(variant) in exported_paths})
    return rows


def _view_context(document, rel_path):
    preview_variant = documents.representative_variant(document)
    return {
        'document': document,
        'variant_rows': _variant_rows(document),
        'breadcrumbs': _breadcrumbs(document.directory),
        'rel_path': rel_path,
        'preview': _build_preview(preview_variant),
    }


def view(request, rel_path):
    _require(request, 'browse')
    document, resolved = _resolve_logical(rel_path)
    if document is None:
        raise Http404('document not found')
    return _render_view_page(request, _view_context(document, resolved.rel_path))


def cover(request, rel_path):
    _require(request, 'browse')
    document, _resolved = _resolve_logical(rel_path)
    if document is None:
        raise Http404('document not found')
    size_name = request.GET.get('size', 'thumb')
    try:
        data = covers.cover_for(document, size_name)
    except covers.CoverError:
        raise Http404('unknown cover size')
    response = HttpResponse(data, content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=3600'
    return _secure_resource(response)


@require_POST
def cover_refresh(request):
    _require(request, 'mutate')
    document, _resolved = _resolve_logical(request.POST.get('rel_path', ''))
    if document is None:
        raise Http404('document not found')
    covers.invalidate(document)
    return HttpResponseRedirect(reverse('documentview:view', kwargs={'rel_path': document.rel_path}))


def _int_param(request, name):
    try:
        return int(request.GET[name])
    except (KeyError, ValueError, TypeError):
        return None


def preview(request, rel_path):
    """Bounded format-specific preview subresource (spec 2.2): the exact
    variant image/resource an EPUB/PDF/CBZ preview `<img>` on the `view`
    page points at. Markdown/text previews are rendered inline on `view`
    and never hit this route.
    """
    _require(request, 'browse')
    try:
        resolved = paths.resolve_document(rel_path)
    except paths.PathError:
        raise Http404('document not found')
    try:
        kind = request.GET.get('kind')

        if resolved.suffix == 'epub' and kind == 'epub-image':
            try:
                content_type, data = previews.epub_image_subresource(resolved, request.GET.get('id', ''))
            except subresources.StaleSubresourceError:
                return HttpResponse(status=409)
            except (subresources.SubresourceError, previews.PreviewError):
                return HttpResponse(status=400)
            response = HttpResponse(data, content_type=content_type)
            return _secure_resource(response)

        if resolved.suffix == 'pdf' and kind == 'pdf-page':
            page = _int_param(request, 'page')
            if page is None:
                return HttpResponse(status=400)
            try:
                data = previews.pdf_preview_page(resolved, page)
            except previews.PreviewError:
                raise Http404('preview page unavailable')
            response = HttpResponse(data, content_type='image/jpeg')
            return _secure_resource(response)

        if resolved.suffix == 'cbz' and kind == 'cbz-page':
            page = _int_param(request, 'page')
            if page is None:
                return HttpResponse(status=400)
            try:
                data = previews.cbz_preview_page(resolved, page)
            except previews.PreviewError:
                raise Http404('preview page unavailable')
            response = HttpResponse(data, content_type='image/jpeg')
            return _secure_resource(response)

        raise Http404('unsupported preview subresource')
    finally:
        resolved.close()


def download(request, rel_path):
    """Exact-original download: preserves bytes and filename, attachment
    disposition. One route per format variant -- `rel_path` names the
    exact variant, never a bare/ambiguous logical-document basename.
    """
    _require(request, 'download')
    try:
        resolved = paths.resolve_document(rel_path)
    except paths.PathError:
        raise Http404('document not found')
    fh = os.fdopen(resolved.fd, 'rb')  # FileResponse closes fh (and so fd) when done
    response = FileResponse(fh, as_attachment=True, filename=resolved.abs_path.name)
    return _secure_resource(response)


@require_POST
def active_add(request):
    _require(request, 'mutate')
    document, resolved = _resolve_logical(request.POST.get('rel_path', ''))
    if document is None:
        raise Http404('document not found')
    try:
        active.add_active(resolved.rel_path)
    except active.ActiveError as e:
        context = _view_context(document, resolved.rel_path)
        context['active_error'] = str(e)
    else:
        return_response = _return_after_mutation(request)
        if return_response is not None:
            return return_response
        context = _view_context(document, resolved.rel_path)
        context['active_notice'] = f'Added "{resolved.abs_path.name}" to Exports.'
    return _render_view_page(request, context)


@require_POST
def active_remove(request):
    """Removal is driven by `link_name` (the export directory entry) alone:
    it must succeed and unlink the symlink even when the source document no
    longer resolves (missing, unreadable, no longer a supported type, ...)
    -- that's precisely the case this endpoint exists to clean up.
    Resolving `rel_path` back to a document is only for choosing which page
    to render the result on; its failure must never block the removal
    itself. Reachable in practice only from the Exports page, since
    collection browse/detail pages no longer offer a remove control.
    """
    _require(request, 'mutate')
    link_name = request.POST.get('link_name', '')
    try:
        result = active.remove_active(link_name)
    except active.ActiveError as e:
        error, notice = str(e), None
    else:
        error = None
        if result.reason:
            label = active.REASON_LABELS[result.reason]
            notice = f'Removed "{result.link_name}" from Exports ({label}).'
        else:
            notice = f'Removed "{result.link_name}" from Exports.'

    if error is None:
        return_response = _return_after_mutation(request)
        if return_response is not None:
            return return_response

    document, resolved = _resolve_logical(request.POST.get('rel_path', ''))
    if document is not None:
        context = _view_context(document, resolved.rel_path)
        if error:
            context['active_error'] = error
        if notice:
            context['active_notice'] = notice
        return _render_view_page(request, context)

    return _render_page(
        request, 'documentview/active_removed.html',
        {'active_error': error, 'active_notice': notice},
    )
