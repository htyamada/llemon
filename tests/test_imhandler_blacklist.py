import fcntl
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'lib'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from imhandler import appconfig, blacklist, cache


class ImhandlerBlacklistTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)

        self.root1 = base / 'root1'
        self.root2 = base / 'root2'
        self.root1.mkdir()
        self.root2.mkdir()
        self.cache_dir = base / 'cache'

        self._patches = [
            mock.patch.object(appconfig, 'image_roots', [str(self.root1), str(self.root2)]),
            mock.patch.object(appconfig, 'cache_dir', str(self.cache_dir)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def store_path(self) -> Path:
        return self.cache_dir / 'blacklist.json'

    def write_raw_store(self, doc) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store_path().write_text(json.dumps(doc), encoding='utf-8')


class LoadTests(ImhandlerBlacklistTestCase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(blacklist.load(), frozenset())

    def test_invalid_json_raises(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store_path().write_text('{not json', encoding='utf-8')
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_wrong_version_raises(self) -> None:
        self.write_raw_store({'version': 2, 'paths': []})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_non_string_path_raises(self) -> None:
        self.write_raw_store({'version': 1, 'paths': [123]})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_rejects_traversal_entry(self) -> None:
        p = str(self.root1 / 'sub' / '..' / 'photo.jpg')
        self.write_raw_store({'version': 1, 'paths': [p]})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_rejects_relative_entry(self) -> None:
        self.write_raw_store({'version': 1, 'paths': ['photo.jpg']})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_rejects_embedded_nul(self) -> None:
        p = str(self.root1 / 'photo.jpg').replace('photo', 'pho\x00to')
        self.write_raw_store({'version': 1, 'paths': [p]})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_rejects_wrong_suffix(self) -> None:
        p = str(self.root1 / 'notes.txt')
        self.write_raw_store({'version': 1, 'paths': [p]})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_rejects_double_leading_slash(self) -> None:
        # normpath() special-cases exactly two leading slashes and leaves
        # them alone, but Path.resolve() collapses them to one -- so this
        # entry would otherwise load successfully yet never match the
        # resolved identity is_blocked() is actually asked about.
        p = '//' + str(self.root1 / 'photo.jpg').lstrip('/')
        self.write_raw_store({'version': 1, 'paths': [p]})
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_invalid_utf8_raises_blacklist_error(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store_path().write_bytes(b'{"version": 1, "paths": ["\xff\xfe"]}')
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.load()

    def test_structural_validation_preserves_entry_under_reconfigured_root(self) -> None:
        other_root = Path(self._tmp.name) / 'gone-root'
        stored = str(other_root / 'photo.jpg')
        self.write_raw_store({'version': 1, 'paths': [stored]})

        loaded = blacklist.load()

        self.assertIn(Path(stored), loaded)
        self.assertTrue(blacklist.is_blocked(stored, blocked=loaded))
        with self.assertRaises(ValueError):
            blacklist.remove(stored)


class AddRemoveTests(ImhandlerBlacklistTestCase):
    def test_add_then_load_round_trip_and_idempotent(self) -> None:
        target = self.root1 / 'photo.jpg'

        self.assertTrue(blacklist.add(target))
        self.assertEqual(blacklist.load(), frozenset({target.resolve()}))

        mtime_before = self.store_path().stat().st_mtime_ns
        self.assertFalse(blacklist.add(target))
        self.assertEqual(self.store_path().stat().st_mtime_ns, mtime_before)

    def test_add_rejects_relative_path(self) -> None:
        with self.assertRaises(ValueError):
            blacklist.add('photo.jpg')

    def test_add_rejects_path_outside_root(self) -> None:
        outside = Path(self._tmp.name) / 'elsewhere' / 'photo.jpg'
        with self.assertRaises(ValueError):
            blacklist.add(outside)

    def test_add_rejects_textual_prefix_lookalike(self) -> None:
        lookalike = self.root1.with_name(self.root1.name + 'Evil') / 'photo.jpg'
        with self.assertRaises(ValueError):
            blacklist.add(lookalike)

    def test_add_rejects_traversal_that_escapes_root(self) -> None:
        escaping = self.root1 / '..' / 'photo.jpg'
        with self.assertRaises(ValueError):
            blacklist.add(escaping)

    def test_add_rejects_wrong_suffix(self) -> None:
        with self.assertRaises(ValueError):
            blacklist.add(self.root1 / 'notes.txt')

    def test_add_rejects_relative_path_with_in_root_cwd(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.root1)
        try:
            with self.assertRaises(ValueError):
                blacklist.add('photo.jpg')
        finally:
            os.chdir(cwd)

    def test_add_accepts_stale_path(self) -> None:
        stale = self.root1 / 'gone.jpg'
        self.assertTrue(blacklist.add(stale))
        self.assertIn(stale.resolve(), blacklist.load())

    def test_remove_present_and_absent(self) -> None:
        target = self.root1 / 'photo.jpg'
        blacklist.add(target)

        self.assertTrue(blacklist.remove(target))
        self.assertEqual(blacklist.load(), frozenset())

        mtime_before = self.store_path().stat().st_mtime_ns
        self.assertFalse(blacklist.remove(target))
        self.assertEqual(self.store_path().stat().st_mtime_ns, mtime_before)

    def test_add_accepts_path_under_second_root(self) -> None:
        target = self.root2 / 'photo.jpg'
        self.assertTrue(blacklist.add(target))
        self.assertIn(target.resolve(), blacklist.load())


class UnavailableRootTests(ImhandlerBlacklistTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.missing_root = Path(self._tmp.name) / 'missing-root'
        self._patches2 = [
            mock.patch.object(appconfig, 'image_roots', [str(self.root1), str(self.missing_root)]),
        ]
        for p in self._patches2:
            p.start()
            self.addCleanup(p.stop)

    def test_configured_image_roots_tolerates_missing_root(self) -> None:
        roots = cache.configured_image_roots()
        self.assertEqual(roots, [self.root1.resolve(), self.missing_root.resolve()])

    def test_add_remove_under_available_root_when_other_root_missing(self) -> None:
        target = self.root1 / 'photo.jpg'
        self.assertTrue(blacklist.add(target))
        self.assertTrue(blacklist.remove(target))

    def test_add_remove_under_missing_root_itself(self) -> None:
        target = self.missing_root / 'photo.jpg'
        self.assertTrue(blacklist.add(target))
        self.assertTrue(blacklist.remove(target))


class SymlinkIdentityTests(ImhandlerBlacklistTestCase):
    def test_symlink_resolves_to_real_target(self) -> None:
        real = self.root1 / 'real.jpg'
        real.write_bytes(b'')
        alias_dir = self.root1 / 'aliases'
        alias_dir.mkdir()
        alias = alias_dir / 'alias.jpg'
        alias.symlink_to(real)

        blacklist.add(alias)

        self.assertEqual(blacklist.load(), frozenset({alias.resolve()}))
        self.assertEqual(alias.resolve(), real.resolve())


class MalformedStoreDoesNotShortCircuitMutationTests(ImhandlerBlacklistTestCase):
    def test_add_raises_blacklist_error_on_corrupt_store(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.store_path().write_text('not json', encoding='utf-8')
        with self.assertRaises(blacklist.BlacklistError):
            blacklist.add(self.root1 / 'photo.jpg')


class PermissionsAndLayoutTests(ImhandlerBlacklistTestCase):
    def test_file_mode_is_owner_only_and_parent_created(self) -> None:
        self.assertFalse(self.cache_dir.exists())
        blacklist.add(self.root1 / 'photo.jpg')

        self.assertTrue(self.cache_dir.is_dir())
        mode = self.store_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class ConcurrencyTests(ImhandlerBlacklistTestCase):
    def test_concurrent_add_does_not_lose_updates(self) -> None:
        targets = [self.root1 / f'photo{i}.jpg' for i in range(20)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(blacklist.add, targets))

        loaded = blacklist.load()
        for target in targets:
            self.assertIn(target.resolve(), loaded)


class IsBlockedTests(ImhandlerBlacklistTestCase):
    def test_explicit_blocked_set_skips_disk(self) -> None:
        target = self.root1 / 'photo.jpg'
        with mock.patch.object(blacklist, 'load', side_effect=AssertionError('load() should not be called')):
            self.assertTrue(blacklist.is_blocked(target, blocked=frozenset({target})))
            self.assertFalse(blacklist.is_blocked(self.root1 / 'other.jpg', blocked=frozenset({target})))


class RemoveStoredTests(ImhandlerBlacklistTestCase):
    def test_removes_present_entry(self) -> None:
        target = self.root1 / 'photo.jpg'
        blacklist.add(target)

        self.assertTrue(blacklist.remove_stored(str(target.resolve())))
        self.assertEqual(blacklist.load(), frozenset())

    def test_absent_entry_is_a_noop(self) -> None:
        target = self.root1 / 'photo.jpg'
        self.assertFalse(blacklist.remove_stored(str(target.resolve())))

    def test_malformed_string_is_a_noop_not_an_error(self) -> None:
        self.assertFalse(blacklist.remove_stored('relative.jpg'))
        self.assertFalse(blacklist.remove_stored(''))
        self.assertFalse(blacklist.remove_stored(str(self.root1 / 'notes.txt')))

    def test_removes_entry_whose_root_is_no_longer_configured(self) -> None:
        # The direct regression test for the Step 1 remove() gap: add()
        # while the root is configured, then reconfigure it away, then
        # confirm remove() would ValueError but remove_stored() still works.
        target = self.root1 / 'photo.jpg'
        blacklist.add(target)

        with mock.patch.object(appconfig, 'image_roots', [str(self.root2)]):
            with self.assertRaises(ValueError):
                blacklist.remove(target)
            self.assertTrue(blacklist.remove_stored(str(target.resolve())))

        self.assertEqual(blacklist.load(), frozenset())


class FilesystemErrorNormalizationTests(ImhandlerBlacklistTestCase):
    """Step 3's fix: any OSError touching the store or lock file becomes
    BlacklistError, never a bare PermissionError/OSError -- every caller in
    the CLI and Django code is written to expect only BlacklistError."""

    def test_load_permission_error_becomes_blacklist_error(self) -> None:
        self.write_raw_store({'version': 1, 'paths': []})
        with mock.patch('builtins.open', side_effect=PermissionError('denied')):
            with self.assertRaises(blacklist.BlacklistError):
                blacklist.load()

    def test_lock_open_permission_error_becomes_blacklist_error(self) -> None:
        target = self.root1 / 'photo.jpg'
        with mock.patch('builtins.open', side_effect=PermissionError('denied')):
            with self.assertRaises(blacklist.BlacklistError):
                blacklist.add(target)
        self.assertEqual(blacklist.load(), frozenset())

    def test_remove_stored_lock_open_permission_error_becomes_blacklist_error(self) -> None:
        target = self.root1 / 'photo.jpg'
        blacklist.add(target)
        with mock.patch('builtins.open', side_effect=PermissionError('denied')):
            with self.assertRaises(blacklist.BlacklistError):
                blacklist.remove_stored(str(target.resolve()))

    def test_write_failure_survives_unlink_cleanup_also_failing(self) -> None:
        # The direct regression test for the fix: a failed cleanup unlink
        # must never mask the original write failure with its own OSError.
        target = self.root1 / 'photo.jpg'
        with mock.patch('os.replace', side_effect=OSError('replace failed')):
            with mock.patch('os.unlink', side_effect=PermissionError('cleanup denied')):
                with self.assertRaises(blacklist.BlacklistError) as cm:
                    blacklist.add(target)
        self.assertIn('replace failed', str(cm.exception))

    def test_lock_acquire_failure_becomes_blacklist_error_and_leaves_store_unchanged(self) -> None:
        target = self.root1 / 'photo.jpg'
        with mock.patch('fcntl.flock', side_effect=OSError('no locks available')):
            with self.assertRaises(blacklist.BlacklistError):
                blacklist.add(target)
        self.assertEqual(blacklist.load(), frozenset())

    def test_lock_release_failure_does_not_discard_a_successful_update(self) -> None:
        target = self.root1 / 'photo.jpg'
        real_flock = fcntl.flock

        def fake_flock(fd, op):
            if op == fcntl.LOCK_UN:
                raise OSError('unlock failed')
            return real_flock(fd, op)

        with mock.patch('fcntl.flock', side_effect=fake_flock):
            result = blacklist.add(target)

        self.assertTrue(result)
        self.assertIn(target.resolve(), blacklist.load())


if __name__ == '__main__':
    unittest.main()
