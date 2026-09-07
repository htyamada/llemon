"""Settings, defaults, and validation for the documentview app.

`root`, `active_dir`, and `active_manifest` normally come from the repo's
own `etc/documentview.conf` (loaded into the `appconfig` module by
`apps.py`'s `ready()`, following the same convention as
`etc/imhandler.conf`/`etc/llemon_djview.conf`). A host's
`DOCUMENT_VIEWER_ROOT`/`DOCUMENT_VIEWER_ACTIVE_DIR`/
`DOCUMENT_VIEWER_ACTIVE_MANIFEST` Django setting, when present *and
non-empty*, wins over the conf file (see `_configured()`) -- this is what
lets tests point each of these at a fresh temp directory via
`override_settings` without touching the real conf. A setting explicitly
set to `''` is treated as not set, rather than being resolved as
`Path('')` -- the process's working directory; `root()`/`active_dir()`
raise `ImproperlyConfigured` outright if nothing configures them.

`AppConfig.ready()` calls `validate_shape()`, which only checks that each
value is present and of the right type (str/Path / callable / dict) using
pure string/PurePath comparison -- no filesystem access. That way
`manage.py check`, migrations, and unrelated management commands never
fail just because a deployment mount happens to be absent.

The equivalent live check (that the configured roots exist, and that the
cache dir / manifest / manifest lock / active dir all resolve outside
DOCUMENT_VIEWER_ROOT) runs lazily, once per process, the first time a view
or management command actually touches the filesystem -- see
`validate_live()`, called from `paths.py`.

Note: `~` expands under the account running `manage.py` / the WSGI process,
which may differ from the developer's own home directory.
"""
from pathlib import Path, PurePath

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static

from . import appconfig

DEFAULT_CACHE_DIR = '~/var/documentview/cache'
DEFAULT_ACTIVE_MANIFEST = '~/var/documentview/state/active_manifest.json'

DEFAULT_COVER_SIZES = {
    'thumb': (150, 220),
    'detail': (300, 440),
}

_LIMIT_DEFAULTS = {
    'DOCUMENT_VIEWER_MAX_ARCHIVE_ENTRIES': 2000,
    'DOCUMENT_VIEWER_MAX_ENTRY_BYTES': 64 * 1024 * 1024,
    'DOCUMENT_VIEWER_MAX_TOTAL_BYTES': 256 * 1024 * 1024,
    'DOCUMENT_VIEWER_MAX_COMPRESSION_RATIO': 100,
    'DOCUMENT_VIEWER_MAX_XML_BYTES': 4 * 1024 * 1024,
    'DOCUMENT_VIEWER_MAX_IMAGE_PIXELS': 40_000_000,
    'DOCUMENT_VIEWER_MAX_PREVIEW_SECTIONS': 3,
    'DOCUMENT_VIEWER_MAX_PREVIEW_BYTES': 200 * 1024,
    'DOCUMENT_VIEWER_MAX_CBZ_PREVIEW_IMAGES': 10,
    'DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES': 10,
    'DOCUMENT_VIEWER_PDF_RENDER_DPI': 96,
    'DOCUMENT_VIEWER_MAX_PDF_RENDER_DIMENSION': 2048,
    'DOCUMENT_VIEWER_SUBPROCESS_TIMEOUT': 10,
    'DOCUMENT_VIEWER_MAX_SYMLINK_HOPS': 2,
}

_validated_configs = set()


def _default_authorize(request, action):
    return request.user.is_authenticated


def _configured(setting_name: str, appconfig_value: str):
    """The host Django setting wins when it's set to a truthy value;
    otherwise the `etc/documentview.conf`-loaded value. A setting that's
    merely *present but falsy* (`DOCUMENT_VIEWER_ROOT = ''`) is treated the
    same as absent -- matching `validate_shape()` -- rather than being used
    as-is, which would let `Path('')` (the process's working directory)
    silently stand in for a real, configured root.
    """
    value = getattr(settings, setting_name, None)
    return value if value else appconfig_value


def root() -> Path:
    value = _configured('DOCUMENT_VIEWER_ROOT', appconfig.root)
    if not value:
        raise ImproperlyConfigured(
            'DOCUMENT_VIEWER_ROOT is not configured (set root in etc/documentview.conf, '
            'or the DOCUMENT_VIEWER_ROOT setting)'
        )
    return Path(value).expanduser().resolve()


def active_dir() -> Path:
    value = _configured('DOCUMENT_VIEWER_ACTIVE_DIR', appconfig.active_dir)
    if not value:
        raise ImproperlyConfigured(
            'DOCUMENT_VIEWER_ACTIVE_DIR is not configured (set active_dir in etc/documentview.conf, '
            'or the DOCUMENT_VIEWER_ACTIVE_DIR setting)'
        )
    return Path(value).expanduser().resolve()


def cache_dir() -> Path:
    value = getattr(settings, 'DOCUMENT_VIEWER_CACHE_DIR', DEFAULT_CACHE_DIR)
    return Path(value).expanduser().resolve()


def active_manifest_path() -> Path:
    value = _configured('DOCUMENT_VIEWER_ACTIVE_MANIFEST', appconfig.active_manifest) or DEFAULT_ACTIVE_MANIFEST
    return Path(value).expanduser().resolve()


def active_manifest_lock_path() -> Path:
    return active_manifest_path().with_suffix(active_manifest_path().suffix + '.lock')


def cover_sizes() -> dict:
    return getattr(settings, 'DOCUMENT_VIEWER_COVER_SIZES', DEFAULT_COVER_SIZES)


def stylesheet_url() -> str:
    return getattr(
        settings,
        'DOCUMENT_VIEWER_STYLESHEET_URL',
        static('documentview/documentview.css'),
    )


def authorize(request, action: str) -> bool:
    hook = getattr(settings, 'DOCUMENT_VIEWER_AUTHORIZE', _default_authorize)
    return hook(request, action)


def limit(name: str):
    """A host-configured value wins; otherwise the built-in default. A name
    that is neither configured nor known raises `KeyError`.
    """
    if hasattr(settings, name):
        return getattr(settings, name)
    return _LIMIT_DEFAULTS[name]


def validate_shape() -> None:
    """Startup-safe validation: types and non-overlap only, no filesystem access.

    `root`/`active_dir` normally come from `etc/documentview.conf` (already
    loaded into the `appconfig` module by the time `ready()` calls this),
    with the DOCUMENT_VIEWER_ROOT/DOCUMENT_VIEWER_ACTIVE_DIR Django
    settings, when set, taking precedence -- same lookup order as
    `root()`/`active_dir()`.
    """
    root_value = _configured('DOCUMENT_VIEWER_ROOT', appconfig.root)
    if not root_value:
        return  # app installed but not configured (e.g. a host with no collection)
    if not isinstance(root_value, (str, PurePath)):
        raise ImproperlyConfigured('DOCUMENT_VIEWER_ROOT must be a path')

    active_value = _configured('DOCUMENT_VIEWER_ACTIVE_DIR', appconfig.active_dir)
    if not active_value:
        raise ImproperlyConfigured(
            'DOCUMENT_VIEWER_ACTIVE_DIR is required when DOCUMENT_VIEWER_ROOT is set '
            '(set active_dir in etc/documentview.conf or the DOCUMENT_VIEWER_ACTIVE_DIR setting)'
        )
    if not isinstance(active_value, (str, PurePath)):
        raise ImproperlyConfigured('DOCUMENT_VIEWER_ACTIVE_DIR must be a path')

    cache_value = getattr(settings, 'DOCUMENT_VIEWER_CACHE_DIR', DEFAULT_CACHE_DIR)
    if not isinstance(cache_value, (str, PurePath)):
        raise ImproperlyConfigured('DOCUMENT_VIEWER_CACHE_DIR must be a path')

    manifest_value = _configured('DOCUMENT_VIEWER_ACTIVE_MANIFEST', appconfig.active_manifest) or DEFAULT_ACTIVE_MANIFEST
    if not isinstance(manifest_value, (str, PurePath)):
        raise ImproperlyConfigured('DOCUMENT_VIEWER_ACTIVE_MANIFEST must be a path')

    authorize_hook = getattr(settings, 'DOCUMENT_VIEWER_AUTHORIZE', _default_authorize)
    if not callable(authorize_hook):
        raise ImproperlyConfigured('DOCUMENT_VIEWER_AUTHORIZE must be callable')

    sizes = getattr(settings, 'DOCUMENT_VIEWER_COVER_SIZES', DEFAULT_COVER_SIZES)
    if not isinstance(sizes, dict) or not sizes:
        raise ImproperlyConfigured('DOCUMENT_VIEWER_COVER_SIZES must be a non-empty dict')

    # Pure string/PurePath comparison; no expanduser()/resolve(), no filesystem access.
    root_pure = PurePath(root_value)
    active_pure = PurePath(active_value)
    if active_pure == root_pure or root_pure in active_pure.parents:
        raise ImproperlyConfigured('DOCUMENT_VIEWER_ACTIVE_DIR must not be inside DOCUMENT_VIEWER_ROOT')


def validate_live() -> None:
    """Filesystem-touching validation, run lazily and once per process (per
    distinct configuration -- keyed on the resolved paths, so tests that
    override settings to point at different temporary directories are each
    validated in turn rather than only the first).
    """
    resolved_root = root()
    resolved_active = active_dir()
    resolved_cache = cache_dir()
    resolved_manifest = active_manifest_path()
    resolved_lock = active_manifest_lock_path()

    cache_key = (resolved_root, resolved_active, resolved_cache, resolved_manifest)
    if cache_key in _validated_configs:
        return

    if not resolved_root.is_dir():
        raise ImproperlyConfigured(f'DOCUMENT_VIEWER_ROOT does not exist or is not a directory: {resolved_root}')

    for name, path in (
        ('DOCUMENT_VIEWER_ACTIVE_DIR', resolved_active),
        ('DOCUMENT_VIEWER_CACHE_DIR', resolved_cache),
        ('DOCUMENT_VIEWER_ACTIVE_MANIFEST', resolved_manifest),
        ('DOCUMENT_VIEWER_ACTIVE_MANIFEST lock file', resolved_lock),
    ):
        if path == resolved_root or resolved_root in path.parents:
            raise ImproperlyConfigured(f'{name} must resolve outside DOCUMENT_VIEWER_ROOT: {path}')

    resolved_active.mkdir(parents=True, exist_ok=True)
    resolved_cache.mkdir(parents=True, exist_ok=True)
    resolved_manifest.parent.mkdir(parents=True, exist_ok=True)

    _validated_configs.add(cache_key)
