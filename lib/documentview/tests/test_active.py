import os
import threading

from .. import active, documents, views
from .base import DocumentViewTestCase
from .test_download import DocumentViewClientTestCase


def _seed_manifest_entry(self, link_name, source_rel_path):
    """Directly inject a manifest entry + matching symlink, bypassing
    `add_active()`. Used only to reach states `add_active()` itself would
    never produce (e.g. a manifest entry recording a source whose suffix
    is no longer supported) -- reachable in practice only via a hand-edited
    manifest or a config change, not through the app's own add path.
    """
    manifest = active._read_manifest()
    manifest[link_name] = {'source': source_rel_path}
    active._write_manifest(manifest)
    self.active.mkdir(parents=True, exist_ok=True)
    (self.active / link_name).symlink_to(self.root / source_rel_path)


class AddActiveTests(DocumentViewTestCase):
    def test_idempotent_add_when_symlink_present_and_correct(self):
        self.touch('a/Book.epub')
        name1 = active.add_active('a/Book.epub')
        name2 = active.add_active('a/Book.epub')
        self.assertEqual(name1, name2)
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_registered_but_symlink_missing_is_reported_not_repaired(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()
        with self.assertRaises(active.MismatchError):
            active.add_active('a/Book.epub')
        # No implicit repair: still nothing on disk.
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_registered_but_symlink_points_elsewhere_is_reported(self):
        self.touch('a/Book.epub')
        other = self.touch('a/Other.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()
        (self.active / 'Book.epub').symlink_to(other)
        with self.assertRaises(active.MismatchError):
            active.add_active('a/Book.epub')

    def test_foreign_symlink_at_computed_name_rejected_without_adoption(self):
        self.touch('a/Book.epub')
        elsewhere = self.tmp / 'elsewhere.epub'
        elsewhere.write_bytes(b'x')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').symlink_to(elsewhere)

        with self.assertRaises(active.CollisionError):
            active.add_active('a/Book.epub')

        # Untouched: still points at the foreign target, and no manifest entry.
        self.assertEqual(os.readlink(self.active / 'Book.epub'), str(elsewhere))
        self.assertEqual(active.load_manifest_sources(), {})

    def test_same_filename_different_directory_is_collision(self):
        self.touch('a/Book.epub')
        self.touch('b/Book.epub')
        active.add_active('a/Book.epub')
        with self.assertRaises(active.CollisionError):
            active.add_active('b/Book.epub')

    def test_activating_one_variant_leaves_siblings_untouched(self):
        self.touch('a/Book.epub')
        self.touch('a/Book.pdf')
        active.add_active('a/Book.epub')
        self.assertIsNone(active.find_link_for_source('a/Book.pdf'))
        self.assertEqual(active.find_link_for_source('a/Book.epub'), 'Book.epub')
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_add_active_rejects_invalid_source(self):
        from .. import paths

        with self.assertRaises(paths.PathError):
            active.add_active('../etc/passwd')


class RemoveActiveTests(DocumentViewTestCase):
    def test_remove_valid_link(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        result = active.remove_active('Book.epub')
        self.assertEqual(result.reason, None)
        self.assertFalse((self.active / 'Book.epub').exists())
        self.assertEqual(active.load_manifest_sources(), {})

    def test_remove_refuses_unregistered_name(self):
        self.active.mkdir(parents=True, exist_ok=True)
        # Not registered at all -> ActiveError before any filesystem check.
        with self.assertRaises(active.ActiveError):
            active.remove_active('not-registered.epub')

    def test_remove_refuses_foreign_symlink_even_if_named_like_a_source(self):
        # A symlink exists on disk under this name, but it was never
        # created through add_active -- the manifest has no entry for it,
        # so remove must refuse rather than treating "is a symlink" alone
        # as sufficient authorization.
        target = self.touch('a/Foreign.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Foreign.epub').symlink_to(target)
        with self.assertRaises(active.ActiveError):
            active.remove_active('Foreign.epub')
        self.assertTrue((self.active / 'Foreign.epub').is_symlink())

    def test_remove_refuses_registered_name_that_is_now_a_plain_file(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()
        (self.active / 'Book.epub').write_bytes(b'not a symlink')
        with self.assertRaises(active.ActiveError):
            active.remove_active('Book.epub')

    def test_remove_cannot_smuggle_arbitrary_path(self):
        with self.assertRaises(active.ActiveError):
            active.remove_active('../etc/passwd')
        with self.assertRaises(active.ActiveError):
            active.remove_active('sub/dir/name')

    def test_remove_succeeds_when_source_missing(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()
        result = active.remove_active('Book.epub')
        self.assertEqual(result.reason, active.REASON_MISSING)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_remove_succeeds_when_source_replaced_by_directory(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()
        source.mkdir()
        result = active.remove_active('Book.epub')
        self.assertEqual(result.reason, active.REASON_NOT_A_FILE)

    def test_remove_succeeds_when_source_unreadable(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.chmod(0o000)
        try:
            result = active.remove_active('Book.epub')
        finally:
            source.chmod(0o644)
        self.assertEqual(result.reason, active.REASON_UNREADABLE)

    def test_remove_succeeds_when_source_no_longer_supported_suffix(self):
        # A recorded source's suffix can't change via a rename (the rel_path
        # string is what's recorded), so reaching this classification means
        # seeding a manifest entry directly, as a hand-edited manifest or a
        # config change to SUPPORTED_SUFFIXES might produce.
        self.touch('a/Book.exe')
        _seed_manifest_entry(self, 'Book.exe', 'a/Book.exe')
        result = active.remove_active('Book.exe')
        self.assertEqual(result.reason, active.REASON_UNSUPPORTED)

    def test_remove_never_follows_or_removes_target(self):
        source = self.touch('a/Book.epub', b'original content')
        active.add_active('a/Book.epub')
        active.remove_active('Book.epub')
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), b'original content')


class ConcurrencyTests(DocumentViewTestCase):
    def test_concurrent_add_same_source_stays_consistent(self):
        self.touch('a/Book.epub')
        errors = []

        def worker():
            try:
                active.add_active('a/Book.epub')
            except active.ActiveError as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every thread either succeeded (idempotent) or hit a reported
        # mismatch; none silently corrupted the manifest/filesystem pair.
        self.assertEqual(active.load_manifest_sources(), {'a/Book.epub': 'Book.epub'})
        self.assertTrue((self.active / 'Book.epub').is_symlink())
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_concurrent_add_then_remove_leaves_consistent_state(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        results = []

        def remover():
            try:
                results.append(active.remove_active('Book.epub'))
            except active.ActiveError as e:
                results.append(e)

        threads = [threading.Thread(target=remover) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one remove can succeed; the rest see a clean "not
        # registered" error. Either way, disk and manifest end up agreeing.
        successes = [r for r in results if isinstance(r, active.RemoveResult)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(active.load_manifest_sources(), {})
        self.assertFalse((self.active / 'Book.epub').exists())


class ReconcileTests(DocumentViewTestCase):
    def test_reports_missing_symlink_with_valid_source(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()

        issues = active.reconcile(repair=False)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, 'missing_symlink')
        self.assertFalse(issues[0].repaired)
        # No repair happened.
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_repair_recreates_missing_symlink_for_valid_source(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()

        issues = active.reconcile(repair=True)
        self.assertTrue(issues[0].repaired)
        self.assertTrue((self.active / 'Book.epub').is_symlink())

    def test_reports_broken_source_for_each_classification(self):
        cases = [
            ('missing', 'epub', lambda p: p.unlink()),
            ('not_a_file', 'epub', lambda p: (p.unlink(), p.mkdir())),
            ('unreadable', 'epub', lambda p: p.chmod(0o000)),
            ('unsupported_type', 'exe', lambda p: None),
        ]
        for suffix_case, ext, mutate in cases:
            with self.subTest(suffix_case):
                source = self.touch(f'a/{suffix_case}.{ext}')
                if ext == 'exe':
                    _seed_manifest_entry(self, f'{suffix_case}.exe', f'a/{suffix_case}.exe')
                else:
                    active.add_active(f'a/{suffix_case}.epub')
                    mutate(source)
                issues = active.reconcile(repair=False)
                broken = [i for i in issues if i.kind == 'broken_source']
                self.assertEqual(len(broken), 1, issues)
                self.assertFalse(broken[0].repaired)
                try:
                    source.chmod(0o644)
                except OSError:
                    pass
                active.remove_active(f'{suffix_case}.{ext}')

    def test_repair_removes_broken_source_link_and_entry(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        issues = active.reconcile(repair=True)
        self.assertTrue(issues[0].repaired)
        self.assertFalse((self.active / 'Book.epub').exists())
        self.assertEqual(active.load_manifest_sources(), {})

    def test_foreign_symlink_reported_but_never_removed(self):
        target = self.touch('a/Foreign.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Foreign.epub').symlink_to(target)

        issues = active.reconcile(repair=True)
        foreign = [i for i in issues if i.kind == 'foreign']
        self.assertEqual(len(foreign), 1)
        self.assertFalse(foreign[0].repaired)
        self.assertTrue((self.active / 'Foreign.epub').is_symlink())

    def test_no_issues_when_everything_consistent(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        self.assertEqual(active.reconcile(repair=False), [])

    def test_repair_never_creates_a_link_escaping_the_collection_root(self):
        # A manifest `source` is untrusted (hand-edited or corrupted); a
        # traversal path must never be treated as a valid, relinkable
        # source just because a file happens to exist there.
        outside = self.tmp / 'outside.pdf'
        outside.write_bytes(b'not part of the collection')
        manifest = active._read_manifest()
        manifest['Escape.pdf'] = {'source': '../outside.pdf'}
        active._write_manifest(manifest)

        issues = active.reconcile(repair=True)
        self.assertFalse((self.active / 'Escape.pdf').exists())
        self.assertNotIn('Escape.pdf', active._read_manifest())
        self.assertTrue(any(i.link_name == 'Escape.pdf' for i in issues))

    def test_repair_never_acts_on_an_invalid_link_name(self):
        # A manifest key containing `/` or `..` can never name a real
        # app-managed link (active_dir / link_name would escape active_dir)
        # -- it must be reported/dropped, not used to build a path.
        self.touch('a/Book.epub')
        manifest = active._read_manifest()
        manifest['../evil'] = {'source': 'a/Book.epub'}
        active._write_manifest(manifest)

        issues = active.reconcile(repair=True)
        invalid = [i for i in issues if i.kind == 'invalid_entry']
        self.assertEqual(len(invalid), 1)
        self.assertTrue(invalid[0].repaired)
        self.assertNotIn('../evil', active._read_manifest())
        self.assertFalse((self.tmp / 'evil').exists())

    def test_reports_symlink_pointing_at_the_wrong_target(self):
        self.touch('a/Book.epub')
        other = self.touch('a/Other.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()
        (self.active / 'Book.epub').symlink_to(other)

        issues = active.reconcile(repair=False)
        wrong = [i for i in issues if i.kind == 'wrong_target']
        self.assertEqual(len(wrong), 1)
        self.assertFalse(wrong[0].repaired)
        self.assertEqual(os.readlink(self.active / 'Book.epub'), str(other))

    def test_repair_relinks_a_symlink_pointing_at_the_wrong_target(self):
        source = self.touch('a/Book.epub')
        other = self.touch('a/Other.epub')
        active.add_active('a/Book.epub')
        (self.active / 'Book.epub').unlink()
        (self.active / 'Book.epub').symlink_to(other)

        issues = active.reconcile(repair=True)
        wrong = [i for i in issues if i.kind == 'wrong_target']
        self.assertEqual(len(wrong), 1)
        self.assertTrue(wrong[0].repaired)
        self.assertEqual(os.readlink(self.active / 'Book.epub'), str(source))


class ActiveHttpTests(DocumentViewClientTestCase):
    def test_add_then_remove_round_trip_through_views(self):
        self.touch('a/Book.epub')
        r = self.post('/documents/active/add/', {'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Remove from reader', r.content)
        self.assertEqual(active.find_link_for_source('a/Book.epub'), 'Book.epub')

        r2 = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r2.status_code, 200)
        self.assertIn(b'Add to reader', r2.content)
        self.assertIsNone(active.find_link_for_source('a/Book.epub'))

    def test_active_add_requires_mutate_authorization(self):
        from django.test import Client

        self.touch('a/Book.epub')
        anon = Client()
        r = anon.post('/documents/active/add/', {'rel_path': 'a/Book.epub'}, HTTP_HOST='localhost')
        self.assertEqual(r.status_code, 403)

    def test_active_add_get_not_allowed(self):
        self.touch('a/Book.epub')
        r = self.get('/documents/active/add/?rel_path=a/Book.epub')
        self.assertEqual(r.status_code, 405)

    def test_browse_shows_reader_badge_after_activation(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        r = self.get('/documents/browse/a/')
        self.assertIn(b'reader', r.content)

    def test_browse_offers_per_format_reader_controls_in_both_views(self):
        self.touch('a/Book.epub')
        self.touch('a/Book.pdf')
        for mode in ('cover', 'title'):
            r = self.get(f'/documents/browse/a/?view={mode}')
            self.assertContains(r, 'Add EPUB to reader')
            self.assertContains(r, 'Add PDF to reader')

    def test_add_and_remove_from_browse_return_to_the_listing(self):
        self.touch('a/Book.epub')
        listing = '/documents/browse/a/?view=cover'

        added = self.post(
            '/documents/active/add/',
            {'rel_path': 'a/Book.epub', 'return_to': listing},
        )
        self.assertRedirects(added, listing, fetch_redirect_response=False)
        r = self.get(listing)
        self.assertContains(r, 'Remove EPUB from reader')

        removed = self.post(
            '/documents/active/remove/',
            {
                'link_name': 'Book.epub',
                'rel_path': 'a/Book.epub',
                'return_to': listing,
            },
        )
        self.assertRedirects(removed, listing, fetch_redirect_response=False)
        self.assertIsNone(active.find_link_for_source('a/Book.epub'))

    def test_mutation_does_not_redirect_to_an_external_host(self):
        self.touch('a/Book.epub')
        r = self.post(
            '/documents/active/add/',
            {'rel_path': 'a/Book.epub', 'return_to': 'https://example.com/'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Remove from reader', r.content)

    def test_remove_succeeds_through_the_view_when_source_is_missing(self):
        # Regression: the endpoint must operate on link_name (manifest
        # identity) first. Resolving rel_path back to a document is only
        # for choosing a page to render -- it must never gate the removal
        # itself, since "the source is gone" is exactly the case this
        # endpoint exists to clean up (spec 1.5/4.5).
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'the source file is missing', r.content)
        self.assertIsNone(active.find_link_for_source('a/Book.epub'))
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_remove_succeeds_through_the_view_when_source_replaced_by_directory(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()
        source.mkdir()

        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(active.find_link_for_source('a/Book.epub'))

    def test_remove_renders_fallback_page_when_no_rel_path_resolves(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        # No sibling variant left to fall back to at all -- rel_path itself
        # won't resolve to any logical document any more.
        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Back to Document View', r.content)
        self.assertIsNone(active.find_link_for_source('a/Book.epub'))


class SymlinkedSourceActiveStateTests(DocumentViewTestCase):
    """Regression: the real collection curates books by symlinking them
    into `humble-bundle/selected/` from sibling directories. Browse lists
    such a document under the *symlink's* path, while `add_active()`
    records the resolved *target* path -- so a naive reverse lookup keyed
    only on the canonical source never matched, and the reader badge /
    "Remove from reader" button never appeared for exactly the directory
    the user curates.
    """

    def _selected_layout(self):
        target = self.touch('real/Book.epub', b'epub-bytes')
        self.mkdir('selected')
        (self.root / 'selected' / 'Book.epub').symlink_to(target)
        return target

    def test_activating_via_symlink_marks_both_paths_active(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')

        sources = active.load_manifest_sources()
        self.assertEqual(sources.get('selected/Book.epub'), 'Book.epub')
        self.assertEqual(sources.get('real/Book.epub'), 'Book.epub')

    def test_badge_lookup_matches_the_listed_symlink_path(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')

        _, docs = documents.scan_directory(self.root / 'selected', 'selected')
        variant = documents.representative_variant(docs[0])
        self.assertEqual(variant.rel_path, 'selected/Book.epub')
        self.assertEqual(active.find_link_for_source(variant.rel_path), 'Book.epub')

    def test_manifest_records_canonical_source_as_identity(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')
        manifest = active._read_manifest()
        self.assertEqual(manifest['Book.epub']['source'], 'real/Book.epub')
        self.assertEqual(manifest['Book.epub']['requested'], 'selected/Book.epub')

    def test_activating_target_directly_is_the_same_link(self):
        # Same underlying file, reached two ways -- must collapse to one
        # idempotent link, not collide.
        self._selected_layout()
        first = active.add_active('selected/Book.epub')
        second = active.add_active('real/Book.epub')
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_activating_canonical_then_alias_still_records_the_alias(self):
        # Regression: activating the canonical path first used to return
        # early on the later alias activation (already active, same
        # source) without ever recording the alias -- so the curated
        # `selected/` directory kept showing the document as inactive.
        self._selected_layout()
        active.add_active('real/Book.epub')
        active.add_active('selected/Book.epub')

        manifest = active._read_manifest()
        self.assertEqual(manifest['Book.epub']['requested'], 'selected/Book.epub')
        self.assertEqual(active.find_link_for_source('selected/Book.epub'), 'Book.epub')

    def test_direct_activation_needs_no_requested_key(self):
        self.touch('real/Book.epub')
        active.add_active('real/Book.epub')
        self.assertNotIn('requested', active._read_manifest()['Book.epub'])

    def test_removal_still_works_and_clears_both_lookups(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')
        active.remove_active('Book.epub')
        self.assertEqual(active.load_manifest_sources(), {})

    def test_view_page_offers_remove_for_a_symlinked_document(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')
        _, docs = documents.scan_directory(self.root / 'selected', 'selected')
        rows = views._variant_rows(docs[0])
        self.assertEqual(rows[0]['active_link'], 'Book.epub')


class SymlinkedSourceLogicalGroupingTests(DocumentViewClientTestCase):
    """Regression: a document reached through an in-hierarchy symlink must
    regroup with the *other files actually listed alongside it* (the
    symlink's own directory), not with whatever happens to share a
    basename in the symlink target's real directory. Otherwise
    `selected/Alias.epub -> real/Canonical.epub` sitting next to
    `selected/Alias.pdf` shows one "Alias" document with both formats when
    browsed, but its own detail page turns into "Canonical" with only the
    EPUB variant.
    """

    def _aliased_layout(self):
        self.touch('real/Canonical.epub', b'epub-bytes')
        self.mkdir('selected')
        (self.root / 'selected' / 'Alias.epub').symlink_to(self.root / 'real' / 'Canonical.epub')
        self.touch('selected/Alias.pdf', b'pdf-bytes')

    def test_resolve_logical_groups_with_the_requested_directorys_sibling(self):
        self._aliased_layout()
        document, resolved = views._resolve_logical('selected/Alias.epub')
        self.assertIsNotNone(document)
        self.assertEqual(document.basename, 'Alias')
        self.assertEqual(set(document.variants), {'epub', 'pdf'})
        self.assertEqual(document.variants['epub'].rel_path, 'selected/Alias.epub')
        self.assertEqual(document.variants['pdf'].rel_path, 'selected/Alias.pdf')
        self.assertEqual(resolved.rel_path, 'selected/Alias.epub')

    def test_view_page_shows_both_variants_for_the_symlinked_alias(self):
        self._aliased_layout()
        r = self.get('/documents/view/selected/Alias.epub/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Alias.epub', r.content)
        self.assertIn(b'Alias.pdf', r.content)
        self.assertIn(b'PDF', r.content)

    def test_activating_through_the_view_records_the_alias_not_the_canonical_path(self):
        self._aliased_layout()
        r = self.post('/documents/active/add/', {'rel_path': 'selected/Alias.epub'})
        self.assertEqual(r.status_code, 200)
        manifest = active._read_manifest()
        self.assertEqual(manifest['Canonical.epub']['source'], 'real/Canonical.epub')
        self.assertEqual(manifest['Canonical.epub']['requested'], 'selected/Alias.epub')
        self.assertEqual(active.find_link_for_source('selected/Alias.epub'), 'Canonical.epub')


class CorruptManifestTests(DocumentViewClientTestCase):
    def _corrupt_manifest(self):
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text('this is not valid json {{{')

    def test_add_active_raises_manifest_error_rather_than_silently_colliding(self):
        self.touch('a/Book.epub')
        self._corrupt_manifest()
        with self.assertRaises(active.ManifestError):
            active.add_active('a/Book.epub')

    def test_remove_active_raises_manifest_error_rather_than_reporting_foreign(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        self._corrupt_manifest()
        with self.assertRaises(active.ManifestError):
            active.remove_active('Book.epub')

    def test_reconcile_raises_manifest_error(self):
        self._corrupt_manifest()
        with self.assertRaises(active.ManifestError):
            active.reconcile(repair=False)

    def test_load_manifest_sources_degrades_gracefully_for_display(self):
        self._corrupt_manifest()
        # Display-only lookups must not break browsing; they log and
        # degrade to "nothing known active" instead of raising.
        self.assertEqual(active.load_manifest_sources(), {})

    def test_missing_manifest_file_is_legitimately_empty_not_an_error(self):
        self.assertFalse(self.manifest.exists())
        self.assertEqual(active.load_manifest_sources(), {})
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')  # must not raise
        self.assertEqual(active.find_link_for_source('a/Book.epub'), 'Book.epub')

    def test_active_add_view_surfaces_manifest_error_instead_of_500(self):
        self.touch('a/Book.epub')
        self._corrupt_manifest()
        r = self.post('/documents/active/add/', {'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'not valid JSON', r.content)
