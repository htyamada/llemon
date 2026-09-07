from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase


class DocumentViewerIntegrationTests(TestCase):
    def test_library_uses_llime_upstream_authorization_policy(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / 'library'
            active = Path(temp_dir) / 'reader'
            root.mkdir()
            with self.settings(
                DOCUMENT_VIEWER_ROOT=root,
                DOCUMENT_VIEWER_ACTIVE_DIR=active,
                DOCUMENT_VIEWER_CACHE_DIR=Path(temp_dir) / 'cache',
            ):
                response = self.client.get('/documents/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<link rel="stylesheet" href="/zorf/llime/static/base/css/documentview.css">',
            html=True,
        )

# Create your tests here.
