import os
import threading

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from .. import active, documents, paths, views
from .base import DocumentViewTestCase
from .test_download import DocumentViewClientTestCase


class AddActiveTests(DocumentViewTestCase):
    def test_idempotent_add_when_symlink_present_and_correct(self):
        self.touch('a/Book.epub')
        name1 = active.add_active('a/Book.epub')
        name2 = active.add_active('a/Book.epub')
        self.assertEqual(name1, name2)
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_add_replaces_dangling_symlink_at_destination_name(self):
        # Regression: a destination name occupied by a dangling symlink
        # must not be misrouted into "create fresh" (a follow-symlinks
        # existence check reports a dangling symlink as absent) and then
        # fail with FileExistsError -- lexists() sees the directory entry
        # and the dangling link is treated as just another "wrong target"
        # to replace.
        source = self.touch('a/Book.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').symlink_to(self.tmp / 'does-not-exist')

        name = active.add_active('a/Book.epub')

        self.assertEqual(name, 'Book.epub')
        self.assertEqual((self.active / 'Book.epub').resolve(), source.resolve())

    def test_add_replaces_symlink_pointing_elsewhere(self):
        # Conflict resolution: latest write wins, not an error.
        source = self.touch('a/Book.epub')
        other = self.touch('a/Other.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').symlink_to(other)

        active.add_active('a/Book.epub')

        self.assertEqual((self.active / 'Book.epub').resolve(), source.resolve())

    def test_add_refuses_non_symlink_entry_at_computed_name(self):
        self.touch('a/Book.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').write_bytes(b'not a symlink')

        with self.assertRaises(active.ActiveError):
            active.add_active('a/Book.epub')

        self.assertEqual((self.active / 'Book.epub').read_bytes(), b'not a symlink')

    def test_same_filename_different_directory_replaces_link(self):
        # Behavior change from the old manifest model: activating a second
        # document with the same filename as an already-active one now
        # replaces the old link instead of requiring deactivation first.
        self.touch('a/Book.epub')
        self.touch('b/Book.epub')
        active.add_active('a/Book.epub')
        active.add_active('b/Book.epub')

        self.assertEqual((self.active / 'Book.epub').resolve(), (self.root / 'b/Book.epub').resolve())
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_activating_one_variant_leaves_siblings_untouched(self):
        self.touch('a/Book.epub')
        self.touch('a/Book.pdf')
        active.add_active('a/Book.epub')
        self.assertEqual(len(list(self.active.iterdir())), 1)
        self.assertTrue((self.active / 'Book.epub').exists())
        self.assertFalse((self.active / 'Book.pdf').exists())

    def test_add_active_rejects_invalid_source(self):
        with self.assertRaises(paths.PathError):
            active.add_active('../etc/passwd')


class RemoveActiveTests(DocumentViewTestCase):
    def test_remove_valid_link(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        result = active.remove_active('Book.epub')
        self.assertIsNone(result.reason)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_remove_refuses_unregistered_name(self):
        self.active.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(active.ActiveError):
            active.remove_active('not-there.epub')

    def test_remove_succeeds_for_a_hand_created_symlink(self):
        # Directory-is-authority: presence as a symlink in the exports
        # directory is the only authorization needed, whether or not the
        # app itself created it -- nothing is "foreign" any more.
        target = self.touch('a/HandCreated.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'HandCreated.epub').symlink_to(target)

        result = active.remove_active('HandCreated.epub')
        self.assertIsNone(result.reason)
        self.assertFalse((self.active / 'HandCreated.epub').exists())

    def test_remove_refuses_non_symlink_entry(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').write_bytes(b'not a symlink')
        with self.assertRaises(active.ActiveError):
            active.remove_active('Book.epub')
        self.assertTrue((self.active / 'Book.epub').exists())

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

    def test_remove_succeeds_when_target_outside_root(self):
        # New, first-class case a manifest-recorded source (always
        # collection-relative by construction) could never reach: a
        # hand-created symlink pointing entirely outside the collection.
        outside = self.tmp / 'outside.pdf'
        outside.write_bytes(b'not part of the collection')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Escape.pdf').symlink_to(outside)

        result = active.remove_active('Escape.pdf')
        self.assertEqual(result.reason, active.REASON_OUTSIDE_ROOT)

    def test_remove_succeeds_when_target_unsupported_suffix(self):
        target = self.touch('a/Notes.exe')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Notes.exe').symlink_to(target)

        result = active.remove_active('Notes.exe')
        self.assertEqual(result.reason, active.REASON_UNSUPPORTED)

    def test_remove_never_follows_or_removes_target(self):
        source = self.touch('a/Book.epub', b'original content')
        active.add_active('a/Book.epub')
        active.remove_active('Book.epub')
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), b'original content')


class ClassifyLinkTests(DocumentViewTestCase):
    def test_missing_target(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Gone.epub').symlink_to(self.tmp / 'nope')
        reason, real = active._classify_link(self.active / 'Gone.epub')
        self.assertEqual(reason, active.REASON_MISSING)
        self.assertIsNone(real)

    def test_symlink_loop_classified_as_missing_not_an_unhandled_exception(self):
        self.active.mkdir(parents=True, exist_ok=True)
        loop_a = self.active / 'LoopA'
        loop_b = self.active / 'LoopB'
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        reason, real = active._classify_link(loop_a)
        self.assertEqual(reason, active.REASON_MISSING)
        self.assertIsNone(real)

    def test_outside_root(self):
        outside = self.tmp / 'outside.pdf'
        outside.write_bytes(b'x')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Escape.pdf').symlink_to(outside)

        reason, real = active._classify_link(self.active / 'Escape.pdf')
        self.assertEqual(reason, active.REASON_OUTSIDE_ROOT)
        self.assertIsNone(real)

    def test_valid_link(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        reason, real = active._classify_link(self.active / 'Book.epub')
        self.assertIsNone(reason)
        self.assertEqual(real, source.resolve())


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

        self.assertEqual(errors, [])
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
        # present" error. Either way, the directory ends up consistent.
        successes = [r for r in results if isinstance(r, active.RemoveResult)]
        self.assertEqual(len(successes), 1)
        self.assertFalse((self.active / 'Book.epub').exists())


class ReconcileTests(DocumentViewTestCase):
    def test_no_issues_when_everything_consistent(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        self.assertEqual(active.reconcile(repair=False), [])

    def test_hand_created_symlink_is_a_normal_valid_link_not_foreign(self):
        target = self.touch('a/Book.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Book.epub').symlink_to(target)
        self.assertEqual(active.reconcile(repair=False), [])

    def test_reports_invalid_link_when_source_missing(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        issues = active.reconcile(repair=False)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, 'invalid_link')
        self.assertFalse(issues[0].repaired)
        self.assertTrue((self.active / 'Book.epub').is_symlink())  # not repaired

    def test_repair_removes_invalid_link(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        issues = active.reconcile(repair=True)
        self.assertTrue(issues[0].repaired)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_reports_link_pointing_outside_root(self):
        outside = self.tmp / 'outside.pdf'
        outside.write_bytes(b'x')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Escape.pdf').symlink_to(outside)

        issues = active.reconcile(repair=False)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, 'invalid_link')
        self.assertIn('outside the collection', issues[0].detail)

    def test_unexpected_non_symlink_entry_reported_and_never_repaired(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'stray.txt').write_bytes(b'not from this app')

        issues = active.reconcile(repair=True)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, 'unexpected_entry')
        self.assertFalse(issues[0].repaired)
        self.assertTrue((self.active / 'stray.txt').exists())

    def test_hidden_entries_are_never_reported(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / '.DS_Store').write_bytes(b'macos metadata')
        self.assertEqual(active.reconcile(repair=True), [])
        self.assertTrue((self.active / '.DS_Store').exists())


class RemoveInvalidTests(DocumentViewTestCase):
    def test_removes_every_invalid_link(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        outside = self.tmp / 'outside.pdf'
        outside.write_bytes(b'x')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'Escape.pdf').symlink_to(outside)

        removed = active.remove_invalid()
        self.assertEqual(removed, 2)
        self.assertEqual(list(self.active.iterdir()), [])

    def test_leaves_valid_links_and_unexpected_files_alone(self):
        self.touch('a/Good.epub')
        active.add_active('a/Good.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'stray.txt').write_bytes(b'not from this app')

        removed = active.remove_invalid()
        self.assertEqual(removed, 0)
        self.assertTrue((self.active / 'Good.epub').exists())
        self.assertTrue((self.active / 'stray.txt').exists())

    def test_no_invalid_links_returns_zero(self):
        self.active.mkdir(parents=True, exist_ok=True)
        self.assertEqual(active.remove_invalid(), 0)


class ActiveHttpTests(DocumentViewClientTestCase):
    def test_add_hides_the_add_control_and_shows_a_notice(self):
        self.touch('a/Book.epub')
        r = self.post('/documents/active/add/', {'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'Add to exports', r.content)
        self.assertIn(b'Added', r.content)
        self.assertIn(b'Exports', r.content)

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

    def test_browse_shows_export_badge_after_activation(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        r = self.get('/documents/browse/a/')
        self.assertIn(b'exported', r.content)

    def test_browse_offers_add_control_only_for_non_exported_formats(self):
        self.touch('a/Book.epub')
        self.touch('a/Book.pdf')
        active.add_active('a/Book.epub')
        for mode in ('cover', 'title'):
            r = self.get(f'/documents/browse/a/?view={mode}')
            self.assertNotContains(r, 'Add EPUB to exports')
            self.assertContains(r, 'Add PDF to exports')

    def test_browse_never_offers_a_remove_control(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        r = self.get('/documents/browse/a/')
        self.assertNotIn(b'Remove from exports', r.content)

    def test_view_page_never_offers_a_remove_control(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        r = self.get('/documents/view/a/Book.epub/')
        self.assertNotIn(b'Remove from exports', r.content)
        self.assertNotIn(b'Add to exports', r.content)  # already exported

    def test_add_from_browse_returns_to_the_listing(self):
        self.touch('a/Book.epub')
        listing = '/documents/browse/a/?view=cover'

        added = self.post('/documents/active/add/', {'rel_path': 'a/Book.epub', 'return_to': listing})
        self.assertRedirects(added, listing, fetch_redirect_response=False)
        r = self.get(listing)
        self.assertContains(r, 'exported')

    def test_mutation_does_not_redirect_to_an_external_host(self):
        self.touch('a/Book.epub')
        r = self.post(
            '/documents/active/add/',
            {'rel_path': 'a/Book.epub', 'return_to': 'https://example.com/'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Added', r.content)

    def test_remove_succeeds_through_the_view_when_source_is_missing(self):
        # Regression: the endpoint must operate on link_name alone.
        # Resolving rel_path back to a document is only for choosing a
        # page to render -- it must never gate the removal itself, since
        # "the source is gone" is exactly the case this endpoint exists to
        # clean up.
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'the source file is missing', r.content)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_remove_succeeds_through_the_view_when_source_replaced_by_directory(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()
        source.mkdir()

        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_remove_renders_fallback_page_when_no_rel_path_resolves(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        # No sibling variant left to fall back to at all -- rel_path itself
        # won't resolve to any logical document any more.
        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'rel_path': 'a/Book.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Back to Document View', r.content)
        self.assertFalse((self.active / 'Book.epub').exists())


class SymlinkedSourceActiveStateTests(DocumentViewTestCase):
    """Regression: the real collection curates books by symlinking them
    into `humble-bundle/selected/` from sibling directories. Browse lists
    such a document under the *symlink's* path, while `add_active()`
    creates the export link at the resolved target's filename. Badge
    lookup must still recognize the document as exported under both the
    curated path and its real path, since both resolve to the same real
    file -- no separate alias bookkeeping needed.
    """

    def _selected_layout(self):
        target = self.touch('real/Book.epub', b'epub-bytes')
        self.mkdir('selected')
        (self.root / 'selected' / 'Book.epub').symlink_to(target)
        return target

    def test_activating_via_symlink_badges_both_paths(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')

        exported = active.active_badge_paths()
        real = (self.root / 'real' / 'Book.epub').resolve()
        self.assertIn(real, exported)

        _, selected_docs = documents.scan_directory(self.root / 'selected', 'selected')
        self.assertEqual(views._variant_real_path(selected_docs[0].variants['epub']), real)

        _, real_docs = documents.scan_directory(self.root / 'real', 'real')
        self.assertEqual(views._variant_real_path(real_docs[0].variants['epub']), real)

    def test_activating_target_directly_is_the_same_link(self):
        # Same underlying file, reached two ways -- must collapse to one
        # idempotent link, not collide.
        self._selected_layout()
        first = active.add_active('selected/Book.epub')
        second = active.add_active('real/Book.epub')
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.active.iterdir())), 1)

    def test_view_page_offers_no_remove_control_for_a_symlinked_document(self):
        self._selected_layout()
        active.add_active('selected/Book.epub')
        _, docs = documents.scan_directory(self.root / 'selected', 'selected')
        rows = views._variant_rows(docs[0])
        self.assertTrue(rows[0]['exported'])


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

    def test_activating_through_the_view_exports_the_canonical_target(self):
        self._aliased_layout()
        r = self.post('/documents/active/add/', {'rel_path': 'selected/Alias.epub'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.active / 'Canonical.epub').is_symlink())
        self.assertEqual(
            (self.active / 'Canonical.epub').resolve(), (self.root / 'real/Canonical.epub').resolve()
        )


class ExportsPageTests(DocumentViewClientTestCase):
    def test_lists_valid_export(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        r = self.get('/documents/exports/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Book')
        self.assertContains(r, 'Remove from exports')

    def test_lists_invalid_link_with_reason(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()
        r = self.get('/documents/exports/')
        self.assertContains(r, 'Book.epub')
        self.assertContains(r, 'the source file is missing')
        self.assertContains(r, 'Delete all invalid links')

    def test_lists_unexpected_non_symlink_entry(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'stray.txt').write_bytes(b'not from this app')
        r = self.get('/documents/exports/')
        self.assertContains(r, 'Unexpected files')
        self.assertContains(r, 'stray.txt')

    def test_hidden_entries_are_never_listed(self):
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / '.DS_Store').write_bytes(b'macos metadata')
        r = self.get('/documents/exports/')
        self.assertNotContains(r, '.DS_Store')

    def test_prune_deletes_all_invalid_links(self):
        source = self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        source.unlink()

        r = self.post('/documents/exports/prune/', {})
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.active / 'Book.epub').exists())
        self.assertContains(r, 'Deleted 1 invalid link')

    def test_prune_with_nothing_invalid_reports_none_deleted(self):
        r = self.post('/documents/exports/prune/', {})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'No invalid links to delete')

    def test_remove_control_removes_and_returns_to_the_listing(self):
        self.touch('a/Book.epub')
        active.add_active('a/Book.epub')
        listing = '/documents/exports/'

        r = self.post('/documents/active/remove/', {'link_name': 'Book.epub', 'return_to': listing})
        self.assertRedirects(r, listing, fetch_redirect_response=False)
        self.assertFalse((self.active / 'Book.epub').exists())

    def test_exports_index_requires_browse_authorization(self):
        from django.test import Client

        anon = Client()
        r = anon.get('/documents/exports/', HTTP_HOST='localhost')
        self.assertEqual(r.status_code, 403)

    def test_prune_requires_mutate_authorization(self):
        from django.test import Client

        anon = Client()
        r = anon.post('/documents/exports/prune/', {}, HTTP_HOST='localhost')
        self.assertEqual(r.status_code, 403)

    def test_exports_link_appears_on_the_collection_page(self):
        r = self.get('/documents/')
        self.assertContains(r, 'dv-exports-link')
        self.assertContains(r, '/documents/exports/')

    def test_duplicate_links_to_the_same_target_are_each_individually_removable(self):
        # Regression: two link names resolving to the same real file must
        # not collide on a rel_path-keyed lookup -- each row's "Remove"
        # form must carry its own link_name, not whichever one happened to
        # be processed last.
        target = self.touch('a/Book.epub')
        self.active.mkdir(parents=True, exist_ok=True)
        (self.active / 'LinkA.epub').symlink_to(target)
        (self.active / 'LinkB.epub').symlink_to(target)

        r = self.get('/documents/exports/')
        self.assertContains(r, 'name="link_name" value="LinkA.epub"')
        self.assertContains(r, 'name="link_name" value="LinkB.epub"')

        removed = self.post('/documents/active/remove/', {'link_name': 'LinkA.epub'})
        self.assertEqual(removed.status_code, 200)
        self.assertFalse((self.active / 'LinkA.epub').exists())
        self.assertTrue((self.active / 'LinkB.epub').exists())

    def test_exports_index_validates_live_config_before_scanning(self):
        # Regression: _exports_context() used to scan exports_dir directly
        # without ever calling config.validate_live() -- the check
        # browse()/view() get for free via paths.resolve_*(). A missing or
        # misconfigured root would otherwise render a silently-empty
        # Exports page instead of surfacing the configuration error.
        missing_root = self.tmp / 'does-not-exist'
        with override_settings(DOCUMENT_VIEWER_ROOT=missing_root):
            with self.assertRaises(ImproperlyConfigured):
                self.get('/documents/exports/')
