"""Runtime (real-DOM) regression test for llemon_video/video.html's
Segmind start-image picker + data-handling-warning consent UI
(see specs/mediagen-video-user-interface-spec.md, "Segmind image-to-video
integration").

Mirrors tests/test_llemon_image_edit_dom.py: node --check only validates
JS syntax and tests/test_image_creator_render.py only string-matches the
rendered source, so neither can catch a runtime ReferenceError, a stale
closure variable, or an event handler that silently never fires -- or,
specific to this feature, a picker that renders enabled when its
transport metadata is entirely absent (the fail-closed requirement this
design was reviewed twice over). This file renders the template against a
fixture set of Segmind models covering every scenario, then hands the
HTML to tests/js/video_dom_test.js, a jsdom harness that drives it end to
end.

Requires Node and this repo's own JS dependency, installed with
`npm install` inside tests/js/ (shared with test_llemon_image_edit_dom.py;
no separate install needed). Skipped -- not failed -- when either is
unavailable.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import test_image_creator_render as _render_mod  # noqa: E402

_JS_DIR = _TESTS_DIR / 'js'
_HARNESS = _JS_DIR / 'video_dom_test.js'


def _node_available() -> bool:
    return shutil.which('node') is not None


def _jsdom_installed() -> bool:
    if not _node_available():
        return False
    result = subprocess.run(
        ['node', '-e', "require.resolve('jsdom')"],
        cwd=_JS_DIR, capture_output=True,
    )
    return result.returncode == 0


def _django_available() -> bool:
    return getattr(_render_mod, 'settings', None) is not None


_PICKER_ITEMS = [
    {'fname': 'cat.png', 'url': '/img/cat.png', 'thumb_url': '/thumb/cat.png'},
    {'fname': 'dog.png', 'url': '/img/dog.png', 'thumb_url': '/thumb/dog.png'},
]


def _video_presentation(**overrides):
    base = {
        'mode': 'image-to-video',
        'reference_image_request_family': 'none',
        'allows_start_image': True,
        'allows_end_image': False,
        'allows_reference_images': False,
        'shows_scene_images': False,
        'is_upscale': False,
    }
    base.update(overrides)
    return base


def _video_model_option(model_id, *, known_caveat=None, **presentation_overrides):
    capabilities = {'presentation': _video_presentation(**presentation_overrides)}
    if known_caveat:
        capabilities['known_caveat'] = known_caveat
    return {
        'id': model_id, 'display': model_id, 'description': '',
        'capabilities': capabilities,
    }


def _build_model_options():
    return [
        _video_model_option(
            'wan-warned',
            known_caveat=(
                'This model has been observed to ignore the requested aspect '
                'ratio in image-to-video mode and return square (1:1) output '
                'instead. This is Segmind provider behavior, not a Grove bug.'
            ),
            required_backend_transports={'data_url': 'provider_upload'},
            available_backend_transports=['provider_upload'],
            transport_warnings={
                'provider_upload': 'Uploads your image to Segmind for hosting.',
            },
        ),
        _video_model_option(
            'wan-unwarned',
            required_backend_transports={'data_url': 'provider_upload'},
            available_backend_transports=['provider_upload'],
            transport_warnings={},
        ),
        _video_model_option(
            'wan-unavailable',
            required_backend_transports={'data_url': 'provider_upload'},
            available_backend_transports=[],
        ),
        # Deliberately carries neither required_backend_transports nor
        # available_backend_transports at all -- the fail-closed case:
        # absent metadata must render identically to a declared-unavailable
        # transport, never as an enabled-by-default picker.
        _video_model_option('wan-missing-metadata'),
        _video_model_option('t2v-only', allows_start_image=False),
    ]


def _render_page() -> str:
    models = _build_model_options()
    presentation = {
        'provider': 'segmind',
        'api': 'segmind',
        'target': {
            'provider': 'segmind', 'api': 'segmind',
            'operation': 'provider', 'model': None,
        },
        'operations': {'generate': {
            'operation': 'generate',
            'model_options': models,
            'selected_model': 'wan-warned',
            'default_model': 'wan-warned',
            'defaults': {'duration': '5s'},
            'controls': {},
            'availability': {'enabled': True},
            'selected_target': {
                'target': {
                    'provider': 'segmind', 'api': 'segmind',
                    'operation': 'generate', 'model': 'wan-warned',
                },
                'controls': {'capabilities': models[0]['capabilities']},
            },
            'notes': {'provider': 'segmind', 'model_tag_states': {}},
        }},
    }
    context = {
        'site_name': 'Test', 'title': 'Video Creator',
        'providers': ['segmind'], 'provider': 'segmind',
        'model_options': models,
        'model_tag_states': {}, 'reverse_tags': [],
        'presentation': presentation,
        'default_model': 'wan-warned', 'default_duration': '5s',
        'available_tags': [], 'gallery_images': _PICKER_ITEMS,
        'output_subdir': '',
        'notes_load_errors': [],
        'generate_url': '/generate/',
        'model_note_url': '/note/',
        'models_json_url': '/models.json',
        'video_file_url': '/file/PLACEHOLDER',
        'video_large_thumbnail_url': '/thumb/PLACEHOLDER',
        'source_dirs_json_url': '',
    }
    from django.template.loader import get_template
    with _render_mod.override_settings(**_render_mod._DJANGO_TEST_OVERRIDES):
        html = get_template('llemon_video/video.html').render(context)
    return html


@unittest.skipUnless(_django_available(), 'django is not installed')
@unittest.skipUnless(
    _jsdom_installed(),
    'node/jsdom not available under tests/js/ -- run `npm install` there to enable '
    '(see tests/js/package.json)',
)
class VideoStartImageConsentDomTests(unittest.TestCase):
    def test_segmind_start_image_picker_and_consent_runtime_behavior(self) -> None:
        html = _render_page()
        with tempfile.NamedTemporaryFile(
            'w', suffix='.html', delete=False, encoding='utf-8',
        ) as f:
            f.write(html)
            html_path = f.name
        try:
            result = subprocess.run(
                ['node', str(_HARNESS), html_path],
                cwd=_JS_DIR, capture_output=True, text=True, timeout=60,
            )
        finally:
            Path(html_path).unlink(missing_ok=True)
        if result.returncode != 0:
            self.fail(
                'jsdom harness reported a failure (see tests/js/video_dom_test.js):\n'
                + result.stdout + '\n' + result.stderr
            )


if __name__ == '__main__':
    unittest.main()
