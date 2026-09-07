"""Exports-directory staging (directory-is-authority model).

Whatever symlink physically exists in `DOCUMENT_VIEWER_EXPORTS_DIR` *is* an
export link -- full stop. There is no separate manifest recording
ownership or intent; the directory's actual symlinks are the sole source
of truth. add/remove/reconcile operations acquire an `fcntl.flock` lock on
a sibling lock file (under `DOCUMENT_VIEWER_CACHE_DIR`, never inside the
exports directory itself -- see `config.active_lock_path()`) before
touching the directory.
"""
import contextlib
import fcntl
import dataclasses
import logging
import os
import stat
from pathlib import Path

from . import config, documents, paths

logger = logging.getLogger('documentview')

REASON_MISSING = 'missing'
REASON_OUTSIDE_ROOT = 'outside_root'
REASON_NOT_A_FILE = 'not_a_file'
REASON_UNREADABLE = 'unreadable'
REASON_UNSUPPORTED = 'unsupported_type'

REASON_LABELS = {
    REASON_MISSING: 'the source file is missing or the link could not be resolved (e.g. a symlink loop)',
    REASON_OUTSIDE_ROOT: 'the link points outside the collection',
    REASON_NOT_A_FILE: 'the source is no longer a regular file',
    REASON_UNREADABLE: 'the source is not readable',
    REASON_UNSUPPORTED: 'the source no longer has a supported suffix',
}


class ActiveError(Exception):
    pass


@dataclasses.dataclass
class RemoveResult:
    link_name: str
    reason: 'str | None'  # None means the link was valid when removed


@dataclasses.dataclass
class ReconcileIssue:
    link_name: str
    kind: str  # 'invalid_link' | 'unexpected_entry'
    detail: str
    repaired: bool = False


@contextlib.contextmanager
def _locked():
    config.validate_live()
    lock_path = config.active_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a+b') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _validate_link_name(link_name: str) -> None:
    if not link_name or '/' in link_name or link_name in ('.', '..') or '\x00' in link_name:
        raise ActiveError('invalid export link name')


def _classify_link(link_path: Path):
    """Classify a symlink under `exports_dir` from its own resolved target
    (not a trusted rel_path -- a hand-created symlink can point anywhere).

    Returns `(reason, real_path)`; `reason is None` means the link is a
    fully valid, in-collection, supported document, and `real_path` is
    then its real absolute path.
    """
    try:
        real = link_path.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: a dangling target. RuntimeError: resolve()'s documented
        # exception for a symlink loop -- a hand-created circular symlink
        # is possible and must not propagate as an unhandled exception.
        # Both fold into REASON_MISSING: neither has a resolvable real
        # path, and both are handled identically everywhere.
        return REASON_MISSING, None

    try:
        real.relative_to(config.root())
    except ValueError:
        return REASON_OUTSIDE_ROOT, None

    try:
        st = real.stat()
    except OSError:
        return REASON_MISSING, None

    if not stat.S_ISREG(st.st_mode):
        return REASON_NOT_A_FILE, None
    if not os.access(real, os.R_OK):
        return REASON_UNREADABLE, None
    if real.suffix.lower() not in documents.SUPPORTED_SUFFIXES:
        return REASON_UNSUPPORTED, None
    return None, real


def active_badge_paths() -> set:
    """Real paths of currently-exported documents, for browse/detail-page
    "exported" badges only -- not a link registry. Multiple links
    (including hand-created duplicates) resolving to the same real file
    collapse to one set entry, which is correct for an existence check.
    A plain, display-only `Path.resolve()`, not the hardened O_NOFOLLOW
    resolver used to actually open files.
    """
    try:
        exports_dir = config.exports_dir()
        entries = list(os.scandir(exports_dir))
    except OSError:
        return set()

    found = set()
    for entry in entries:
        name = entry.name
        if name.startswith('.'):
            continue
        try:
            if not entry.is_symlink():
                continue
        except OSError:
            continue
        reason, real = _classify_link(exports_dir / name)
        if reason is None:
            found.add(real)
    return found


def add_active(source_rel_path: str) -> str:
    """Create (or idempotently confirm) an export link for the exact
    selected source format. Returns the link name.

    Raises `paths.PathError` if `source_rel_path` isn't a valid document,
    or `ActiveError` if the computed name is occupied by a non-symlink
    entry (refused, since that might be the user's own data) or the
    filesystem operation itself fails.
    """
    with paths.resolve_document(source_rel_path) as resolved:
        source_abs = resolved.abs_path
        link_name = source_abs.name

    with _locked():
        exports_dir = config.exports_dir()
        exports_dir.mkdir(parents=True, exist_ok=True)
        link_path = exports_dir / link_name

        if not os.path.lexists(link_path):
            try:
                os.symlink(source_abs, link_path)
            except OSError as e:
                raise ActiveError(f'failed to create export link "{link_name}": {e}') from e
            return link_name

        if link_path.is_symlink():
            try:
                current_target = link_path.resolve(strict=True)
            except (OSError, RuntimeError):
                current_target = None  # dangling, or a symlink loop
            if current_target == source_abs:
                return link_name  # idempotent no-op
            # Conflict: latest write wins. A dangling link is just another
            # "wrong target" to replace, not a special case.
            try:
                link_path.unlink()
                os.symlink(source_abs, link_path)
            except OSError as e:
                raise ActiveError(f'failed to replace export link "{link_name}": {e}') from e
            return link_name

        raise ActiveError(
            f'"{link_name}" already exists in the exports directory and is not a symlink; refusing to replace it'
        )


def remove_active(link_name: str) -> RemoveResult:
    """Remove an export link. Only ever unlinks a symlink directly inside
    `DOCUMENT_VIEWER_EXPORTS_DIR` -- never an arbitrary path, and never the
    link's target. Presence as a symlink is the only authorization needed;
    there is no separate registry to check membership against.

    Removal succeeds regardless of the link's current validity;
    `RemoveResult.reason` reports *why* if it no longer validates.
    """
    _validate_link_name(link_name)

    with _locked():
        exports_dir = config.exports_dir()
        dir_fd = os.open(exports_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                st = os.lstat(link_name, dir_fd=dir_fd)
            except OSError as e:
                raise ActiveError(f'"{link_name}" is not present in the exports directory: {e}') from e
            if not stat.S_ISLNK(st.st_mode):
                raise ActiveError(f'"{link_name}" is not a symlink; refusing to remove it')

            reason, _real = _classify_link(exports_dir / link_name)

            try:
                os.unlink(link_name, dir_fd=dir_fd)
            except OSError as e:
                raise ActiveError(f'failed to remove export link "{link_name}": {e}') from e
        finally:
            os.close(dir_fd)

    return RemoveResult(link_name=link_name, reason=reason)


def remove_invalid() -> int:
    """Delete every currently-invalid export symlink (any REASON_*,
    including outside-root). Returns the number removed. Never even calls
    `_classify_link()` on a non-symlink entry (enumeration rule), so this
    can't touch a stray non-symlink file.
    """
    removed = 0
    with _locked():
        exports_dir = config.exports_dir()
        try:
            entries = list(os.scandir(exports_dir))
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if name.startswith('.'):
                continue
            try:
                if not entry.is_symlink():
                    continue
            except OSError:
                continue
            reason, _real = _classify_link(exports_dir / name)
            if reason is None:
                continue
            try:
                os.unlink(exports_dir / name)
            except OSError:
                continue
            removed += 1
    return removed


def reconcile(repair: bool = False) -> list:
    """Report (and, with `repair=True`, prune) invalid export symlinks.

    A visible non-symlink entry is never classified as valid/invalid --
    it's reported as its own informational `unexpected_entry` issue,
    never touched by `--repair`. Hidden entries (name starting with `.`)
    are always skipped entirely.
    """
    issues = []
    with _locked():
        exports_dir = config.exports_dir()
        exports_dir.mkdir(parents=True, exist_ok=True)
        try:
            entries = list(os.scandir(exports_dir))
        except OSError:
            entries = []
        entries.sort(key=lambda e: e.name)

        for entry in entries:
            name = entry.name
            if name.startswith('.'):
                continue
            try:
                is_symlink = entry.is_symlink()
            except OSError:
                continue

            if not is_symlink:
                issues.append(
                    ReconcileIssue(name, 'unexpected_entry', 'unexpected non-symlink entry in the exports directory')
                )
                continue

            link_path = exports_dir / name
            reason, _real = _classify_link(link_path)
            if reason is None:
                continue
            label = REASON_LABELS.get(reason, reason)
            if repair:
                try:
                    os.unlink(link_path)
                    issues.append(ReconcileIssue(name, 'invalid_link', f'{label}; removed', repaired=True))
                except OSError as e:
                    issues.append(ReconcileIssue(name, 'invalid_link', f'{label}; remove failed: {e}'))
            else:
                issues.append(ReconcileIssue(name, 'invalid_link', label))

    return issues
