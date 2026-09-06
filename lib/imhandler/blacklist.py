"""imhandler.blacklist — persistent image blacklist.

The sole persistence and matching implementation for hidden images (see
imhandler-specs.md). Stores absolute, resolved paths beneath a configured
image_root at cache_root()/blacklist.json, with inter-process locking and
atomic replacement so Django workers and CLI processes never lose an update
or observe partial JSON. Contains no Django types.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import AbstractSet

from . import cache, scanner

_STORE_NAME = 'blacklist.json'
_LOCK_NAME = '.blacklist.lock'
_VERSION = 1


class BlacklistError(Exception):
    """The blacklist store is corrupt, unsupported, or cannot be read or
    written because of a filesystem error (e.g. permissions)."""


class BlockedImageError(Exception):
    """An explicit-path operation was asked to act on a blocked image."""


def _store_path() -> Path:
    return cache.cache_root() / _STORE_NAME


def store_path() -> Path:
    """Public path to the blacklist store, for callers (e.g. the CLI export
    command) that must refuse to overwrite it without reaching into a
    private name."""
    return _store_path()


def _lock_path() -> Path:
    return cache.cache_root() / _LOCK_NAME


def _normalize(path: Path | str) -> Path:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ValueError(f'blacklist path must be absolute: {path}')
    normalized = expanded.resolve()
    if not any(
        normalized == root or normalized.is_relative_to(root)
        for root in cache.configured_image_roots()
    ):
        raise ValueError(f'blacklist path is not under a configured image_root: {normalized}')
    if normalized.suffix.lower() not in scanner.IMAGE_SUFFIXES:
        raise ValueError(f'blacklist path has an unsupported suffix: {normalized}')
    return normalized


def _validate_stored_entry(raw: str) -> Path:
    if not raw:
        raise BlacklistError('blacklist entry is empty')
    if '\x00' in raw:
        raise BlacklistError(f'blacklist entry contains a NUL byte: {raw!r}')
    if not Path(raw).is_absolute():
        raise BlacklistError(f'blacklist entry is not absolute: {raw!r}')
    if os.path.normpath(raw) != raw:
        raise BlacklistError(f'blacklist entry is not in canonical form: {raw!r}')
    if raw.startswith('//'):
        # POSIX (and normpath) special-cases exactly two leading slashes and
        # leaves them as-is, but Path.resolve() collapses them to one -- the
        # same string that passes the normpath check above would therefore
        # never match a resolve()-produced identity from _normalize() or the
        # serving endpoints.
        raise BlacklistError(f'blacklist entry has a non-canonical leading slash: {raw!r}')
    if Path(raw).suffix.lower() not in scanner.IMAGE_SUFFIXES:
        raise BlacklistError(f'blacklist entry has an unsupported suffix: {raw!r}')
    return Path(raw)


def load() -> frozenset[Path]:
    path = _store_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise BlacklistError(f'blacklist store cannot be read: {path}: {exc}') from exc
    except UnicodeDecodeError as exc:
        raise BlacklistError(f'blacklist store is not valid UTF-8: {path}') from exc

    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise BlacklistError(f'blacklist store is not valid JSON: {path}') from exc

    if not isinstance(doc, dict) or doc.get('version') != _VERSION:
        raise BlacklistError(f'blacklist store has an unsupported version: {path}')

    paths = doc.get('paths')
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise BlacklistError(f'blacklist store "paths" is not a list of strings: {path}')

    return frozenset(_validate_stored_entry(p) for p in paths)


def is_blocked(path: Path | str, blocked: AbstractSet[Path] | None = None) -> bool:
    if blocked is None:
        blocked = load()
    return Path(path) in blocked


def _write_atomic(blocked: AbstractSet[Path]) -> None:
    cache_dir = cache.cache_root()
    doc = {'version': _VERSION, 'paths': sorted(str(p) for p in blocked)}
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix='.blacklist-')
    except OSError as exc:
        raise BlacklistError(f'cannot create blacklist temp file: {exc}') from exc
    try:
        try:
            fh = os.fdopen(fd, 'w', encoding='utf-8')
        except OSError:
            # mkstemp() returned a raw descriptor, and fdopen() did not
            # take ownership because it failed. Close it before the
            # outer handler unlinks the temporary pathname.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, _store_path())
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Not narrowed to FileNotFoundError: *any* failure to remove
            # the temp file (permissions, a second concurrent cleanup,
            # whatever) must be swallowed here, not raised. The exception
            # this function is about to raise is the one that matters --
            # the original write failure -- and letting an unrelated
            # unlink failure escape instead would replace that diagnostic
            # with a misleading one about a leftover temp file, not fix
            # anything the caller could act on differently.
            pass
        raise BlacklistError(f'cannot write blacklist store: {exc}') from exc


def _update(normalized: Path, *, adding: bool) -> bool:
    cache_dir = cache.cache_root()
    lock_path = _lock_path()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_fh = open(lock_path, 'a+')
    except OSError as exc:
        raise BlacklistError(f'cannot open blacklist lock file: {exc}') from exc

    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise BlacklistError(f'cannot lock blacklist store: {exc}') from exc

        current = load()
        if adding:
            if normalized in current:
                return False
            updated = current | {normalized}
        else:
            if normalized not in current:
                return False
            updated = current - {normalized}
        _write_atomic(updated)
        return True
    finally:
        # Best-effort only, deliberately not raising BlacklistError here:
        # by this point the operation above has already succeeded or
        # already raised on its own terms, and closing lock_fh releases
        # the OS-held flock() regardless of whether LOCK_UN itself
        # succeeds -- so a failure in either of these two calls must be
        # ignored, never allowed to mask or overwrite that outcome.
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_fh.close()
        except OSError:
            pass


def add(path: Path | str) -> bool:
    normalized = _normalize(path)
    return _update(normalized, adding=True)


def remove(path: Path | str) -> bool:
    normalized = _normalize(path)
    return _update(normalized, adding=False)


def remove_stored(raw: str) -> bool:
    """Remove an entry by its exact stored string, bypassing _normalize().

    For the restore endpoint, whose only inputs are strings load() itself
    just returned. Skips _normalize()'s live root-containment check --
    restore only ever narrows the blocked set, so re-validating an entry
    against the *current* configured roots would resurrect the Step 1
    remove() gap: an entry under a root that has since been reconfigured
    away, or is presently offline, would become unrestorable even though
    load() and is_blocked() both still see it correctly. _validate_stored_entry
    still rejects a string that could never have come out of load() in
    the first place (malformed, non-canonical, wrong suffix) -- it just
    does so without touching cache.configured_image_roots(), so a
    never-valid string returns False instead of raising, and a
    genuinely corrupt store still raises BlacklistError via the load()
    inside _update(), which this does not swallow.
    """
    try:
        candidate = _validate_stored_entry(raw)
    except BlacklistError:
        return False
    return _update(candidate, adding=False)


def load_if_configured() -> frozenset[Path]:
    """load(), or empty when no cache_dir is configured at all.

    The single sanctioned fail-open in the whole feature, and it exists for
    exactly one documented case: an `imh list DIR` run against an
    unconfigured variant, which has no cache_dir and therefore no store to
    consult. A *corrupt* store still raises BlacklistError here -- only
    "there is no configured store" is tolerated, never "there is a store
    and it cannot be read."
    """
    try:
        cache.cache_root()
    except EnvironmentError:
        return frozenset()
    return load()
