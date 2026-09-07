"""documentview.appconfig -- filesystem path configuration.

Call init(ac) once at startup with an AppConfig instance, or init_variant()
to load the repo's own etc/documentview.conf for a given variant (the
normal path, called from apps.py's ready()). config.py reads root and
exports_dir from this module, with a host's DOCUMENT_VIEWER_* Django
settings taking precedence when set (see config.py's docstring).
"""
from __future__ import annotations

import os
from pathlib import Path

from hty7.config import AppConfig

root: str = ''
exports_dir: str = ''
_DEFAULT_CONF = str(Path(__file__).resolve().parents[2] / 'etc' / 'documentview.conf')


def _config_str(value: object) -> str:
    return str(value).strip() if value else ''


def init(ac: AppConfig) -> None:
    """Set module globals from AppConfig (variant already selected)."""
    global root, exports_dir
    root = _config_str(ac.get('documentview', 'core', 'root'))
    exports_dir = _config_str(ac.get('documentview', 'core', 'exports_dir'))


def init_variant(variant: str, conf_path: str = _DEFAULT_CONF) -> None:
    """Load the repo's documentview config for variant and initialize globals."""
    init(AppConfig(os.path.expanduser(conf_path), variant))
