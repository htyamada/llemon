import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))

try:
    from django.conf import settings
except ModuleNotFoundError:
    settings = None


if settings is None:
    class ImhandlerDjviewSemanticSearchTests(unittest.TestCase):
        @unittest.skip('django is not installed')
        def test_django_required(self) -> None:
            pass
else:
    _template_root = Path(tempfile.mkdtemp(prefix='imhandler-djview-templates-'))
    (_template_root / 'base').mkdir(parents=True, exist_ok=True)
    (_template_root / 'base' / 'base.html').write_text(
        '{% block content %}{% endblock %}',
        encoding='utf-8',
    )

    if not settings.configured:
        settings.configure(
            SECRET_KEY='test-secret',
            ROOT_URLCONF=__name__,
            ALLOWED_HOSTS=['*'],
            INSTALLED_APPS=[
                'django.contrib.contenttypes',
                'django.contrib.sessions',
                'imhandler.djview',
            ],
            TEMPLATES=[
                {
                    'BACKEND': 'django.template.backends.django.DjangoTemplates',
                    'DIRS': [str(_template_root)],
                    'APP_DIRS': True,
                }
            ],
            MIDDLEWARE=[],
        )

    import django

    django.setup()

    from django.http import Http404
    from django.test import Client, RequestFactory, override_settings
    from django.urls import include, path
    from django.contrib.sessions.middleware import SessionMiddleware

    from PIL import Image

    from imhandler import appconfig, blacklist
    from imhandler.db import open_db
    from imhandler.djview import ImageHandlerViewSet, _default_blacklist_authorizer
    from imhandler.models import Album

    _vs = ImageHandlerViewSet(base_nav=[], nav_rel=[])
    app_name = 'image_handler'
    _image_handler_patterns = ([
        path('', _vs.index, name='index'),
        path('browse/', _vs.browse, name='browse'),
        path('similarity/', _vs.similarity_browse, name='similarity_browse'),
        path('semantic/', _vs.semantic_search, name='semantic_search'),
        path('compare/', _vs.compare, name='compare'),
        path('cluster/<int:cluster_id>/', _vs.cluster_detail, name='cluster_detail'),
        path('embed-stream/', _vs.embed_stream, name='embed_stream'),
        path('embed-cancel/', _vs.embed_cancel, name='embed_cancel'),
        path('hide/', _vs.hide_image, name='hide_image'),
        path('hidden/', _vs.hidden_images, name='hidden_images'),
        path('restore/', _vs.restore_image, name='restore_image'),
        path('similar/', _vs.similar, name='similar'),
        path('thumb/', _vs.thumb, name='thumb'),
        path('image/', _vs.image, name='image'),
    ], app_name)
    urlpatterns = [
        path('', include(_image_handler_patterns, namespace='image_handler')),
    ]

    class ImhandlerDjviewSemanticSearchTests(unittest.TestCase):
        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.factory = RequestFactory()

        def _with_session(self, request):
            middleware = SessionMiddleware(lambda req: None)
            middleware.process_request(request)
            return request

        def test_semantic_search_renders_top_level_results(self) -> None:
            album = self.root / 'album1'
            album.mkdir()
            image_path = album / 'cat.jpg'
            image_path.write_bytes(b'not-a-real-image')

            request = self.factory.get('/semantic/', {
                'q': 'cat on a chair',
                'n': '17',
            })

            fake_results = [{
                'path': str(image_path),
                'similarity': 0.987,
                'width': 640,
                'height': 480,
            }]

            with mock.patch('imhandler.db.open_db') as open_db_mock:
                conn = open_db_mock.return_value
                with mock.patch('imhandler.embedder.find_semantic',
                                return_value=(fake_results, 1)) as find_semantic_mock:
                    response = _vs.semantic_search(request)

            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn('cat.jpg', html)
            self.assertIn('0.987', html)
            self.assertIn('First 1 result', html)
            self.assertIn('name="n"', html)
            self.assertIn('image/?path=', html)
            find_semantic_mock.assert_called_once_with(
                conn, 'cat on a chair', n=17
            )
            conn.close.assert_called_once_with()

        def test_index_hides_semantic_link_when_route_missing(self) -> None:
            request = self.factory.get('/')
            real_reverse = __import__('django.urls', fromlist=['reverse']).reverse

            def fake_reverse(viewname, *args, **kwargs):
                if viewname == 'image_handler:semantic_search':
                    from django.urls import NoReverseMatch
                    raise NoReverseMatch('missing semantic route')
                return real_reverse(viewname, *args, **kwargs)

            with mock.patch('imhandler.djview.reverse', side_effect=fake_reverse):
                response = _vs.index(request)

            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn('Browse', html)
            self.assertNotIn('Semantic</a>', html)

        def test_browse_corrupt_store_returns_500(self) -> None:
            """scan_all() loads the blacklist internally (imhandler.scanner
            -> blacklist.load_if_configured()); a corrupt store must render
            the app's error.html, not bubble up as an unhandled Django 500."""
            request = self.factory.get('/browse/')
            with mock.patch('imhandler.djview.scan_all', side_effect=blacklist.BlacklistError('bad store')):
                response = _vs.browse(request)
            self.assertEqual(response.status_code, 500)
            self.assertIn('Cannot read the blacklist', response.content.decode('utf-8'))

        def test_compare_corrupt_store_returns_500(self) -> None:
            """cluster_images()/get_cluster_member_rows() both load the
            blacklist internally; a corrupt store must render error.html
            rather than an unhandled Django 500."""
            request = self.factory.get('/compare/')
            conn = mock.Mock()
            with mock.patch('imhandler.db.open_db', return_value=conn):
                with mock.patch('imhandler.clusterer.cluster_images',
                                 side_effect=blacklist.BlacklistError('bad store')):
                    response = _vs.compare(request)
            self.assertEqual(response.status_code, 500)
            self.assertIn('Cannot read the blacklist', response.content.decode('utf-8'))
            conn.close.assert_called_once_with()

        def test_similarity_browse_uses_resolved_album_for_embed_url(self) -> None:
            album = Album(path=self.root, rel_path=Path('.'), name='images', depth=0, images=[])
            request = self.factory.get('/similarity/', {'album': 'missing'})

            with mock.patch('imhandler.djview.scan_all', return_value=album):
                response = _vs.similarity_browse(request)

            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn('/embed\\u002Dstream/?album\\u003D.', html)

        def test_similarity_browse_shows_embed_for_multi_root_virtual_album(self) -> None:
            other_root = Path(self.tmp.name) / 'other-images'
            other_root.mkdir()
            appconfig.image_roots = [str(self.root), str(other_root)]
            appconfig.image_root_names = ['Images', 'Other']
            virtual = Album(path=self.root, rel_path=Path('.'), name='Images', depth=0, images=[])
            request = self.factory.get('/similarity/', {'album': '.'})

            with mock.patch('imhandler.djview.scan_all', return_value=virtual):
                response = _vs.similarity_browse(request)

            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn('id="embed-btn"', html)
            self.assertIn('/embed\\u002Dstream/?album\\u003D.', html)

        def test_embed_stream_virtual_root_runs_all_real_roots(self) -> None:
            other_root = Path(self.tmp.name) / 'other-images'
            other_root.mkdir()
            appconfig.image_roots = [str(self.root), str(other_root)]
            appconfig.image_root_names = ['Images', 'Other']
            request = self._with_session(self.factory.get('/embed-stream/', {'album': '.'}))

            calls = []

            def fake_embed_images(target, conn, *, cancel=None, on_progress=None, blocked=None):
                calls.append(Path(target))
                if on_progress is not None:
                    on_progress(100, Path(target).name)
                return (1, 0, 0)

            conn = mock.Mock()
            with mock.patch('imhandler.db.open_db', return_value=conn):
                with mock.patch('imhandler.embedder.embed_images', side_effect=fake_embed_images):
                    response = _vs.embed_stream(request)
                    body = b''.join(response.streaming_content).decode('utf-8')

            self.assertEqual(calls, [self.root.resolve(), other_root.resolve()])
            self.assertIn('Embedding 2 roots', body)
            self.assertIn('"processed": 2', body)

    class ImhandlerDjviewMediaCachingTests(unittest.TestCase):
        """Section 2.4's caching rules: private, no-cache with a
        Last-Modified validator on 200s, no-store on 404s, and the
        blacklist check (where one already exists in Step 2) running
        before the conditional comparison so a stale validator can never
        be answered 304 for an image that has since been hidden."""

        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.factory = RequestFactory()

            self.image_path = self.root / 'photo.jpg'
            Image.new('RGB', (20, 20), color='red').save(self.image_path, 'JPEG')

        def test_image_sends_private_no_cache_and_last_modified(self) -> None:
            request = self.factory.get('/image/', {'path': str(self.image_path)})
            response = _vs.image(request)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Cache-Control'], 'private, no-cache')
            self.assertIn('Last-Modified', response)

        def test_image_revalidates_to_304(self) -> None:
            request = self.factory.get('/image/', {'path': str(self.image_path)})
            first = _vs.image(request)

            second_request = self.factory.get(
                '/image/', {'path': str(self.image_path)},
                HTTP_IF_MODIFIED_SINCE=first['Last-Modified'],
            )
            second = _vs.image(second_request)

            self.assertEqual(second.status_code, 304)

        def test_image_missing_path_404_has_no_store(self) -> None:
            request = self.factory.get('/image/', {'path': str(self.root / 'missing.jpg')})
            response = _vs.image(request)

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response['Cache-Control'], 'no-store')

        def test_thumb_sends_private_no_cache_and_last_modified(self) -> None:
            request = self.factory.get('/thumb/', {'path': str(self.image_path)})
            response = _vs.thumb(request)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Cache-Control'], 'private, no-cache')
            self.assertIn('Last-Modified', response)

        def test_thumb_revalidates_to_304(self) -> None:
            request = self.factory.get('/thumb/', {'path': str(self.image_path)})
            first = _vs.thumb(request)

            second_request = self.factory.get(
                '/thumb/', {'path': str(self.image_path)},
                HTTP_IF_MODIFIED_SINCE=first['Last-Modified'],
            )
            second = _vs.thumb(second_request)

            self.assertEqual(second.status_code, 304)

        def test_thumb_missing_path_404_has_no_store(self) -> None:
            request = self.factory.get('/thumb/', {'path': str(self.root / 'missing.jpg')})
            response = _vs.thumb(request)

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response['Cache-Control'], 'no-store')

        def test_thumb_hidden_after_cache_returns_404_not_304(self) -> None:
            """The ordering test: hiding an image does not touch its mtime,
            so a client's stale If-Modified-Since would still match if the
            validator check ran before the blacklist check. It must not --
            the explicit blocked pre-check must run first."""
            request = self.factory.get('/thumb/', {'path': str(self.image_path)})
            first = _vs.thumb(request)
            last_modified = first['Last-Modified']

            self.assertTrue(blacklist.add(self.image_path))

            second_request = self.factory.get(
                '/thumb/', {'path': str(self.image_path)},
                HTTP_IF_MODIFIED_SINCE=last_modified,
            )
            second = _vs.thumb(second_request)

            self.assertEqual(second.status_code, 404)
            self.assertEqual(second['Cache-Control'], 'no-store')

        def test_image_hidden_returns_404_with_no_store(self) -> None:
            self.assertTrue(blacklist.add(self.image_path))
            request = self.factory.get('/image/', {'path': str(self.image_path)})
            response = _vs.image(request)

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response['Cache-Control'], 'no-store')

        def test_image_hidden_after_cache_returns_404_not_304(self) -> None:
            """The same ordering test as thumb's above, but for image():
            the blocked check must run before the If-Modified-Since
            comparison, so a stale validator can never be answered 304 for
            an image that has since been hidden."""
            request = self.factory.get('/image/', {'path': str(self.image_path)})
            first = _vs.image(request)
            last_modified = first['Last-Modified']

            self.assertTrue(blacklist.add(self.image_path))

            second_request = self.factory.get(
                '/image/', {'path': str(self.image_path)},
                HTTP_IF_MODIFIED_SINCE=last_modified,
            )
            second = _vs.image(second_request)

            self.assertEqual(second.status_code, 404)
            self.assertEqual(second['Cache-Control'], 'no-store')

        def test_image_and_thumb_fail_closed_on_corrupt_store(self) -> None:
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / 'blacklist.json').write_text('not json', encoding='utf-8')

            image_response = _vs.image(self.factory.get('/image/', {'path': str(self.image_path)}))
            thumb_response = _vs.thumb(self.factory.get('/thumb/', {'path': str(self.image_path)}))

            self.assertEqual(image_response.status_code, 404)
            self.assertEqual(thumb_response.status_code, 404)

        def test_image_and_thumb_fail_closed_on_unreadable_store(self) -> None:
            """Same fail-closed contract as the corrupt-JSON case above, but
            for a store that exists and is structurally fine yet cannot be
            read (e.g. a permissions problem) -- load_if_configured() must
            surface this as BlacklistError too, not a bare PermissionError,
            and both endpoints must still 404 rather than 500."""
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / 'blacklist.json').write_text('[]', encoding='utf-8')
            with mock.patch('builtins.open', side_effect=PermissionError('denied')):
                image_response = _vs.image(self.factory.get('/image/', {'path': str(self.image_path)}))
                thumb_response = _vs.thumb(self.factory.get('/thumb/', {'path': str(self.image_path)}))

            self.assertEqual(image_response.status_code, 404)
            self.assertEqual(thumb_response.status_code, 404)

    class ImhandlerDjviewClusterDetailTests(unittest.TestCase):
        """Viewing a cluster must never destroy cluster metadata just
        because one of its members is hidden (as opposed to genuinely
        missing from disk) -- that is purge's job, not a side effect of a
        GET request."""

        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.factory = RequestFactory()

        def _with_session(self, request):
            middleware = SessionMiddleware(lambda req: None)
            middleware.process_request(request)
            return request

        def _make_cluster(self, paths):
            db = open_db(self.cache / 'db' / 'dedup.db')
            ids = []
            for p in paths:
                db.execute('INSERT INTO Images (path, mtime) VALUES (?, ?)', (str(p), 0.0))
                ids.append(db.execute('SELECT last_insert_rowid()').fetchone()[0])
            db.execute('INSERT INTO Clusters (threshold_used, model_used) VALUES (?, ?)', (0.85, 'clip'))
            cluster_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            for rank, image_id in enumerate(ids):
                db.execute(
                    'INSERT INTO ClusterMembership (cluster_id, image_id, quality_rank) VALUES (?, ?, ?)',
                    (cluster_id, image_id, rank),
                )
            db.commit()
            db.close()
            return cluster_id

        def test_hidden_but_present_member_does_not_delete_cluster(self) -> None:
            a = self.root / 'a.jpg'
            b = self.root / 'b.jpg'
            Image.new('RGB', (10, 10)).save(a, 'JPEG')
            Image.new('RGB', (10, 10)).save(b, 'JPEG')
            cluster_id = self._make_cluster([a.resolve(), b.resolve()])
            self.assertTrue(blacklist.add(a))

            request = self._with_session(self.factory.get(f'/cluster/{cluster_id}/'))
            response = _vs.cluster_detail(request, cluster_id)

            # Down to one visible member -- redirect to Compare, per section
            # 1.3, but the cluster metadata itself must survive: only a
            # collapse driven by genuinely *missing* files (remaining <= 1)
            # deletes rows, and that branch is never reached here.
            self.assertEqual(response.status_code, 302)
            self.assertIn('compare', response['Location'])
            db = open_db(self.cache / 'db' / 'dedup.db')
            self.assertEqual(
                len(db.execute('SELECT id FROM Clusters WHERE id = ?', (cluster_id,)).fetchall()), 1,
            )
            memberships = db.execute(
                'SELECT image_id FROM ClusterMembership WHERE cluster_id = ?', (cluster_id,)
            ).fetchall()
            self.assertEqual(len(memberships), 2)
            db.close()

        def test_nonexistent_cluster_is_404_not_redirect(self) -> None:
            # Regression test for the fix: a bogus cluster_id must not be
            # conflated with "cluster exists but collapsed by hiding".
            request = self._with_session(self.factory.get('/cluster/999999/'))
            with self.assertRaises(Http404):
                _vs.cluster_detail(request, 999999)

        def test_three_member_cluster_with_hidden_and_missing_redirects_without_deleting(self) -> None:
            # A hidden, B missing from disk, C present and visible: rows
            # (blocked-filtered) is [B, C] (len 2, first check doesn't
            # fire); cleanup_missing_members deletes B and returns
            # remaining == 2 (3 real members minus 1 missing -- hiding
            # doesn't reduce this count), so the existing remaining <= 1
            # branch doesn't fire either. Only the second, visible_rows
            # check catches this.
            a = self.root / 'a.jpg'
            c = self.root / 'c.jpg'
            b = self.root / 'b.jpg'  # never created -- stands in for "missing"
            Image.new('RGB', (10, 10)).save(a, 'JPEG')
            Image.new('RGB', (10, 10)).save(c, 'JPEG')
            cluster_id = self._make_cluster([a.resolve(), b.resolve(), c.resolve()])
            self.assertTrue(blacklist.add(a))

            request = self._with_session(self.factory.get(f'/cluster/{cluster_id}/'))
            response = _vs.cluster_detail(request, cluster_id)

            self.assertEqual(response.status_code, 302)
            self.assertIn('compare', response['Location'])
            db = open_db(self.cache / 'db' / 'dedup.db')
            # B (missing) is cleaned up as always; A and C are left alone --
            # the cluster is not collapsed, only navigation changed.
            self.assertEqual(
                len(db.execute('SELECT id FROM Clusters WHERE id = ?', (cluster_id,)).fetchall()), 1,
            )
            memberships = db.execute(
                'SELECT image_id FROM ClusterMembership WHERE cluster_id = ?', (cluster_id,)
            ).fetchall()
            self.assertEqual(len(memberships), 2)
            db.close()

        def test_three_member_cluster_with_two_missing_collapses_cluster(self) -> None:
            # Contrasting variant: no hiding involved, two of three members
            # missing from disk -- remaining == 1, so the existing
            # remaining <= 1 collapse-and-delete path still runs.
            a = self.root / 'a.jpg'
            b = self.root / 'b.jpg'  # missing
            c = self.root / 'c.jpg'  # missing
            Image.new('RGB', (10, 10)).save(a, 'JPEG')
            cluster_id = self._make_cluster([a.resolve(), b.resolve(), c.resolve()])

            request = self._with_session(self.factory.get(f'/cluster/{cluster_id}/'))
            response = _vs.cluster_detail(request, cluster_id)

            self.assertEqual(response.status_code, 302)
            self.assertIn('compare', response['Location'])
            db = open_db(self.cache / 'db' / 'dedup.db')
            self.assertEqual(
                len(db.execute('SELECT id FROM Clusters WHERE id = ?', (cluster_id,)).fetchall()), 0,
            )
            db.close()

        def test_hide_button_rendered_only_for_authorized_request(self) -> None:
            a = self.root / 'a.jpg'
            b = self.root / 'b.jpg'
            Image.new('RGB', (10, 10)).save(a, 'JPEG')
            Image.new('RGB', (10, 10)).save(b, 'JPEG')
            cluster_id = self._make_cluster([a.resolve(), b.resolve()])

            request = self._with_session(self.factory.get(f'/cluster/{cluster_id}/'))
            with mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: False, create=True):
                unauthorized = _vs.cluster_detail(request, cluster_id)
            with mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: True, create=True):
                authorized = _vs.cluster_detail(request, cluster_id)

            unauthorized_html = unauthorized.content.decode('utf-8')
            authorized_html = authorized.content.decode('utf-8')
            self.assertNotIn('<button class="hide-btn"', unauthorized_html)
            self.assertNotIn('mark/', unauthorized_html)
            self.assertNotIn('deletion-list/', unauthorized_html)
            self.assertIn('<button class="hide-btn"', authorized_html)
            self.assertIn('hide/', authorized_html)
            self.assertIn('new FormData(form)', authorized_html)
            self.assertIn('MEMBERS.splice(idx, 1)', authorized_html)


    class ImhandlerDjviewBlacklistAuthorizationTests(unittest.TestCase):
        def test_default_denies_anonymous(self) -> None:
            request = mock.Mock()
            request.user = None
            self.assertFalse(_default_blacklist_authorizer(request))

        def test_default_denies_non_staff_authenticated(self) -> None:
            request = mock.Mock()
            request.user = mock.Mock(is_authenticated=True, is_staff=False)
            self.assertFalse(_default_blacklist_authorizer(request))

        def test_default_allows_staff(self) -> None:
            request = mock.Mock()
            request.user = mock.Mock(is_authenticated=True, is_staff=True)
            self.assertTrue(_default_blacklist_authorizer(request))

    class ImhandlerDjviewHideRestoreTests(unittest.TestCase):
        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.factory = RequestFactory()
            self.image_path = self.root / 'photo.jpg'
            Image.new('RGB', (10, 10)).save(self.image_path, 'JPEG')

        def _authorized(self):
            return mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: True, create=True)

        def _unauthorized(self):
            return mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: False, create=True)

        def _write_corrupt_store(self) -> None:
            self.cache.mkdir(parents=True, exist_ok=True)
            (self.cache / 'blacklist.json').write_text('not json', encoding='utf-8')

        # ── hide_image ──

        def test_hide_image_authorized_post_adds_path(self) -> None:
            with self._authorized():
                response = _vs.hide_image(self.factory.post('/hide/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content), {'ok': True})
            self.assertTrue(blacklist.is_blocked(self.image_path))

        def test_hide_image_unauthorized_returns_403_and_leaves_blacklist_unchanged(self) -> None:
            with self._unauthorized():
                response = _vs.hide_image(self.factory.post('/hide/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 403)
            self.assertFalse(blacklist.is_blocked(self.image_path))

        def test_hide_image_get_returns_405(self) -> None:
            with self._authorized():
                response = _vs.hide_image(self.factory.get('/hide/'))
            self.assertEqual(response.status_code, 405)

        def test_hide_image_missing_path_returns_400(self) -> None:
            with self._authorized():
                response = _vs.hide_image(self.factory.post('/hide/', {}))
            self.assertEqual(response.status_code, 400)

        def test_hide_image_outside_root_returns_400_and_leaves_blacklist_unchanged(self) -> None:
            outside = Path(self.tmp.name) / 'elsewhere' / 'photo.jpg'
            with self._authorized():
                response = _vs.hide_image(self.factory.post('/hide/', {'path': str(outside)}))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(blacklist.load(), frozenset())

        def test_hide_image_is_idempotent(self) -> None:
            with self._authorized():
                first = _vs.hide_image(self.factory.post('/hide/', {'path': str(self.image_path)}))
                second = _vs.hide_image(self.factory.post('/hide/', {'path': str(self.image_path)}))
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)

        def test_hide_image_corrupt_store_returns_500(self) -> None:
            self._write_corrupt_store()
            with self._authorized():
                response = _vs.hide_image(self.factory.post('/hide/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 500)

        # ── restore_image ──

        def test_restore_image_removes_previously_hidden_path_and_redirects(self) -> None:
            blacklist.add(self.image_path)
            with self._authorized():
                response = _vs.restore_image(self.factory.post('/restore/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 302)
            self.assertNotIn('application/json', response.get('Content-Type', ''))
            self.assertFalse(blacklist.is_blocked(self.image_path))

        def test_restore_image_absent_path_is_idempotent(self) -> None:
            with self._authorized():
                response = _vs.restore_image(self.factory.post('/restore/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 302)

        def test_restore_image_root_no_longer_configured_still_succeeds(self) -> None:
            blacklist.add(self.image_path)
            resolved = str(self.image_path.resolve())
            other_root = Path(self.tmp.name) / 'other'
            other_root.mkdir()
            with mock.patch.object(appconfig, 'image_roots', [str(other_root)]):
                with self.assertRaises(ValueError):
                    blacklist.remove(self.image_path)
                with self._authorized():
                    response = _vs.restore_image(self.factory.post('/restore/', {'path': resolved}))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(blacklist.load(), frozenset())

        def test_restore_image_unauthorized_returns_plain_403(self) -> None:
            with self._unauthorized():
                response = _vs.restore_image(self.factory.post('/restore/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 403)

        def test_restore_image_get_returns_405(self) -> None:
            with self._authorized():
                response = _vs.restore_image(self.factory.get('/restore/'))
            self.assertEqual(response.status_code, 405)

        def test_restore_image_corrupt_store_returns_plain_text_500(self) -> None:
            blacklist.add(self.image_path)
            self._write_corrupt_store()
            with self._authorized():
                response = _vs.restore_image(self.factory.post('/restore/', {'path': str(self.image_path)}))
            self.assertEqual(response.status_code, 500)
            self.assertNotIn('application/json', response.get('Content-Type', ''))

        # ── hidden_images ──

        def test_hidden_images_lists_entries_with_exists_status(self) -> None:
            missing = self.root / 'missing.jpg'
            blacklist.add(self.image_path)
            blacklist.add(missing)
            with self._authorized():
                response = _vs.hidden_images(self.factory.get('/hidden/'))
            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn(str(self.image_path.resolve()), html)
            self.assertIn(str(missing.resolve()), html)
            self.assertIn('missing', html)

        def test_hidden_images_unauthorized_returns_403(self) -> None:
            with self._unauthorized():
                response = _vs.hidden_images(self.factory.get('/hidden/'))
            self.assertEqual(response.status_code, 403)

        def test_hidden_images_corrupt_store_returns_500(self) -> None:
            self._write_corrupt_store()
            with self._authorized():
                response = _vs.hidden_images(self.factory.get('/hidden/'))
            self.assertEqual(response.status_code, 500)

        def test_hidden_images_unconfigured_cache_dir_returns_500(self) -> None:
            appconfig.cache_dir = ''
            with self._authorized():
                response = _vs.hidden_images(self.factory.get('/hidden/'))
            self.assertEqual(response.status_code, 500)

    class ImhandlerDjviewSimilarBlacklistTests(unittest.TestCase):
        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.factory = RequestFactory()
            self.image_path = self.root / 'photo.jpg'
            Image.new('RGB', (10, 10)).save(self.image_path, 'JPEG')

        def test_blocked_focal_renders_hidden_notice_not_no_embedding(self) -> None:
            blacklist.add(self.image_path)
            request = self.factory.get('/similar/', {'path': str(self.image_path)})
            response = _vs.similar(request)

            self.assertEqual(response.status_code, 200)
            html = response.content.decode('utf-8')
            self.assertIn('has been hidden', html)
            self.assertNotIn('run the embedder first', html)

        def test_hide_button_and_modal_absent_for_unauthorized(self) -> None:
            request = self.factory.get('/similar/', {'path': str(self.image_path)})
            conn = mock.Mock()
            with mock.patch('imhandler.db.open_db', return_value=conn):
                with mock.patch('imhandler.embedder.find_similar', return_value=(None, [])):
                    with mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: False, create=True):
                        unauthorized = _vs.similar(request)
                    with mock.patch.object(settings, 'IMHANDLER_BLACKLIST_AUTHORIZER', lambda r: True, create=True):
                        authorized = _vs.similar(request)

            unauthorized_html = unauthorized.content.decode('utf-8')
            authorized_html = authorized.content.decode('utf-8')
            self.assertNotIn('id="hide-focal"', unauthorized_html)
            self.assertIn('id="hide-focal"', authorized_html)

    class ImhandlerDjviewHideCsrfTests(unittest.TestCase):
        """Integration-level: goes through the real URLconf and a real
        CsrfViewMiddleware via django.test.Client, unlike every other test
        in this file (which calls view functions directly via
        RequestFactory and so never exercises CSRF at all)."""

        def setUp(self) -> None:
            self._override = override_settings(
                MIDDLEWARE=['django.middleware.csrf.CsrfViewMiddleware'],
                IMHANDLER_BLACKLIST_AUTHORIZER=lambda request: True,
            )
            self._override.enable()
            self.addCleanup(self._override.disable)

            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = Path(self.tmp.name) / 'images'
            self.root.mkdir()
            self.cache = Path(self.tmp.name) / 'cache'
            appconfig.image_roots = [str(self.root)]
            appconfig.image_root_names = ['Images']
            appconfig.cache_dir = str(self.cache)
            self.image_path = self.root / 'photo.jpg'
            Image.new('RGB', (10, 10)).save(self.image_path, 'JPEG')
            self.client = Client(enforce_csrf_checks=True)

        def test_valid_token_hides_the_path(self) -> None:
            # hidden_images.html's {% csrf_token %} lives inside each row's
            # restore form -- with an empty blacklist there are zero rows,
            # so GETting /hidden/ would never call get_token() and Django
            # would never set the csrftoken cookie at all. Seed an
            # unrelated entry first so the page has a row to render.
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

        def test_missing_token_is_rejected_with_a_non_json_response(self) -> None:
            response = self.client.post('/hide/', {'path': str(self.image_path)})
            self.assertEqual(response.status_code, 403)
            self.assertFalse(blacklist.is_blocked(self.image_path))
            # This is what the modal's fetch actually receives on a CSRF
            # failure, and it is not JSON -- confirming the try/catch
            # around response.json() isn't guarding against a
            # hypothetical, it's guarding against Django's real default
            # CSRF_FAILURE_VIEW response.
            self.assertNotIn('application/json', response.get('Content-Type', ''))

    class ImhandlerDjviewUrlsTests(unittest.TestCase):
        """Regression test against the real urls.py/views.py source (not the
        local _image_handler_patterns mirror above), so a stale route left
        behind by a future rename would actually be caught here. A plain
        import of these modules isn't usable in this shared-lib test suite:
        views.py imports `base.lib.tools`, which only exists inside the
        llime host project, not on this test file's sys.path -- so this
        checks source text instead, the same technique already used for the
        cluster_detail HTML sweep above."""

        def test_removed_mark_and_deletion_list_routes_are_gone(self) -> None:
            djview_dir = ROOT / 'lib' / 'imhandler' / 'djview'
            urls_src = (djview_dir / 'urls.py').read_text(encoding='utf-8')
            views_src = (djview_dir / 'views.py').read_text(encoding='utf-8')

            for stale in ('mark_toggle', 'deletion_list_download', 'deletion_list_clear',
                          "'mark/'", "'deletion-list/'"):
                self.assertNotIn(stale, urls_src)
                self.assertNotIn(stale, views_src)

            for expected in ('hide_image', 'restore_image', 'hidden_images'):
                self.assertIn(expected, urls_src)
                self.assertIn(expected, views_src)


if __name__ == '__main__':
    unittest.main()
