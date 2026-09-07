import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings


class DocumentViewTestCase(TestCase):
    """Points DOCUMENT_VIEWER_* settings at a fresh temp collection per test."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dv-test-'))
        self.root = self.tmp / 'collection'
        self.active = self.tmp / 'active'
        self.cache = self.tmp / 'cache'
        self.root.mkdir()

        override = override_settings(
            DOCUMENT_VIEWER_ROOT=self.root,
            DOCUMENT_VIEWER_ACTIVE_DIR=self.active,
            DOCUMENT_VIEWER_CACHE_DIR=self.cache,
            DOCUMENT_VIEWER_AUTHORIZE=(
                lambda request, action: request.user.is_authenticated
            ),
        )
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        # Undo any restrictive chmods a permission-error test may have left
        # behind so cleanup itself doesn't fail.
        for path in self.tmp.rglob('*'):
            try:
                path.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, rel_path, content=b'x') -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def mkdir(self, rel_path) -> Path:
        p = self.root / rel_path
        p.mkdir(parents=True, exist_ok=True)
        return p
