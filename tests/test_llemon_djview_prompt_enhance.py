"""Prompt-enhancement and image-edit-control tests for llemon_djview.

Covers the step-15 Django integration of the prompt-enhancement upgrade:

- media initialization goes through top-level ``mediagen.init()`` and a
  configuration error is fatal — no media backend is initialized after a
  failed init, so no media request can follow;
- image and video generation pass ``generated_prompt`` and
  ``prompt_enhancement`` through to metadata writers, sidecars, and
  summaries, and an enhanced image result forces the client-side canonical
  metadata path;
- every media action requires an explicit provider, and image editing requires
  an explicit live-discovered model with no static/default fallback;
- the image-edit endpoint enforces the per-provider aspect-ratio and size
  policies (OpenRouter: explicit fixed ratio, explicit size forwarded;
  Venice: ``auto`` source ratio, no size accepted).
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GROVE_LIB = ROOT / 'lib'
HTY7_LIB = Path.home() / 'src' / 'hty7' / 'python3' / 'lib'
for lib in (GROVE_LIB, HTY7_LIB):
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))


def _stash_djview_modules() -> dict:
    """Remove any already-imported llemon_djview modules from sys.modules."""
    return {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == 'llemon_djview' or name.startswith('llemon_djview.')
    }


def _restore_djview_modules(stashed: dict) -> None:
    for name in list(sys.modules):
        if name == 'llemon_djview' or name.startswith('llemon_djview.'):
            del sys.modules[name]
    sys.modules.update(stashed)


class FakeJsonResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status


def _fake_django_modules():
    return {
        'django': types.ModuleType('django'),
        'django.conf': types.SimpleNamespace(
            settings=types.SimpleNamespace(
                LLEMON_IMAGEGEN_MEDIA_DIR='',
                LLEMON_IMAGEGEN_LOG_DIR='',
                LLEMON_VIDEOGEN_MEDIA_DIR='',
                LLEMON_VIDEOGEN_LOG_DIR='',
            ),
        ),
        'django.http': types.SimpleNamespace(
            FileResponse=object,
            Http404=RuntimeError,
            JsonResponse=FakeJsonResponse,
            StreamingHttpResponse=object,
        ),
        'django.shortcuts': types.SimpleNamespace(
            redirect=object,
            render=lambda request, template, context: context,
        ),
        'django.urls': types.SimpleNamespace(reverse=lambda *args, **kwargs: ''),
        'django.views': types.ModuleType('django.views'),
        'django.views.decorators': types.ModuleType('django.views.decorators'),
        'django.views.decorators.csrf': types.SimpleNamespace(
            csrf_exempt=lambda f: f,
        ),
        'django.views.decorators.http': types.SimpleNamespace(
            require_POST=lambda f: f,
        ),
    }


class _DjviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._stashed_djview_modules = _stash_djview_modules()

    def tearDown(self) -> None:
        _restore_djview_modules(self._stashed_djview_modules)


class MediaInitFatalTests(_DjviewTestCase):
    def test_media_settings_uses_top_level_mediagen_init(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            import llemon_djview as djview
        fake_imagegen = types.SimpleNamespace(
            init=mock.Mock(),
            get_media_dir=mock.Mock(return_value='~/img'),
            get_log_dir=mock.Mock(return_value=''),
        )
        fake_videogen = types.SimpleNamespace(
            init=mock.Mock(),
            get_media_dir=mock.Mock(return_value='~/vid'),
            get_log_dir=mock.Mock(return_value=''),
        )
        fake_mediagen = types.SimpleNamespace(
            init=mock.Mock(),
            imagegen=fake_imagegen,
            videogen=fake_videogen,
        )
        appconfig = object()
        with mock.patch.dict(sys.modules, {'hty7.llemon.mediagen': fake_mediagen}):
            djview.media_settings(appconfig)
        fake_mediagen.init.assert_called_once_with(appconfig)
        # The subpackage inits run inside mediagen.init(); media_settings
        # must not bypass the top-level seam by calling them directly.
        fake_imagegen.init.assert_not_called()
        fake_videogen.init.assert_not_called()

    def test_media_settings_init_error_is_fatal_and_stops_media_setup(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            import llemon_djview as djview
        fake_imagegen = types.SimpleNamespace(
            init=mock.Mock(),
            get_media_dir=mock.Mock(return_value='~/img'),
            get_log_dir=mock.Mock(return_value=''),
        )
        fake_videogen = types.SimpleNamespace(
            init=mock.Mock(),
            get_media_dir=mock.Mock(return_value='~/vid'),
            get_log_dir=mock.Mock(return_value=''),
        )
        fake_mediagen = types.SimpleNamespace(
            init=mock.Mock(side_effect=RuntimeError('invalid rewrite selector')),
            imagegen=fake_imagegen,
            videogen=fake_videogen,
        )
        with mock.patch.dict(sys.modules, {'hty7.llemon.mediagen': fake_mediagen}):
            with self.assertRaises(RuntimeError):
                djview.media_settings(object())
        # A failed initialization propagates out of the Django settings
        # import, so startup aborts: no media directory is configured and no
        # media backend can serve a request afterwards.
        fake_imagegen.get_media_dir.assert_not_called()
        fake_videogen.get_media_dir.assert_not_called()


class RequiredProviderTests(_DjviewTestCase):
    def test_image_generation_requires_provider(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({'prompt': 'draw this'}))
        response = view._generate(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'provider is required')

    def test_image_generation_does_not_add_presentation_lookup(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet
        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'example', 'prompt': 'draw this', 'model': 'off',
        }))
        presentation_lookup = mock.Mock(
            side_effect=RuntimeError('catalog unavailable'),
        )
        with mock.patch.dict(view._generate.__globals__, {
            'normalize_provider_api': lambda *a: ('example', 'images'),
            'resolve_action_model': lambda model, *a, **k: model,
            'default_aspect_ratio': lambda *a, **k: '1:1',
            'default_image_size': lambda *a, **k: '1K',
            'aspect_ratios': lambda *a, **k: ['1:1'],
            'image_sizes': lambda *a, **k: ['1K'],
            'extract_extra_params': lambda *a, **k: {},
            'model_scoped_parameters': lambda *a, **k: False,
            'preflight_request': mock.Mock(side_effect=
                view._generate.__globals__['LLemonImageParamError'](
                    'preflight rejected',
                )),
            'model_presentation': presentation_lookup,
        }):
            response = view._generate(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'preflight rejected')
        presentation_lookup.assert_not_called()

    def test_image_generation_rejects_membership_before_preflight(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet
        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'example', 'prompt': 'draw this', 'model': 'm1',
            'aspect_ratio': 'bad', 'image_size': '1K',
        }))
        preflight = mock.Mock()
        with mock.patch.dict(view._generate.__globals__, {
            'normalize_provider_api': lambda *a: ('example', 'images'),
            'resolve_action_model': lambda model, *a, **k: model,
            'aspect_ratios': lambda *a, **k: ['1:1'],
            'default_aspect_ratio': lambda *a, **k: '1:1',
            'image_sizes': lambda *a, **k: ['1K'],
            'default_image_size': lambda *a, **k: '1K',
            'preflight_request': preflight,
        }):
            response = view._generate(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'invalid aspect_ratio')
        preflight.assert_not_called()

    def test_image_generation_missing_model_fails_before_model_information(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from hty7.llemon.mediagen.imagegen import LLemonImageParamError
            from llemon_djview.imagegen import LLemonImageGenViewSet
        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'segmind', 'prompt': 'draw this', 'model': '   ',
        }))
        ratios = mock.Mock()
        backend = mock.Mock()
        with mock.patch.dict(view._generate.__globals__, {
            'normalize_provider_api': lambda *a: ('segmind', 'inference'),
            'resolve_action_model': mock.Mock(side_effect=LLemonImageParamError(
                "provider 'segmind' has no default model for image generation; provide a model explicitly"
            )),
            'aspect_ratios': ratios,
            'make_imagegen_backend': backend,
        }):
            response = view._generate(request)
        self.assertEqual(response.status_code, 400)
        ratios.assert_not_called()
        backend.assert_not_called()

    def test_image_generation_model_information_failure_is_brief_502(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet
        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'segmind', 'prompt': 'draw this', 'model': 'm1',
        }))
        backend = mock.Mock()
        with mock.patch.dict(view._generate.__globals__, {
            'normalize_provider_api': lambda *a: ('segmind', 'inference'),
            'resolve_action_model': lambda model, *a, **k: model,
            'aspect_ratios': mock.Mock(side_effect=RuntimeError('secret record body')),
            'make_imagegen_backend': backend,
        }):
            response = view._generate(request)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data['error'],
            'could not validate request against model information',
        )
        backend.assert_not_called()

    def test_image_upscale_requires_provider(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({'filename': 'a.png'}))
        response = view._do_upscale(request, '/tmp')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'provider is required')

    def test_video_generation_requires_provider(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.videogen import LLemonVideoGenViewSet

        view = LLemonVideoGenViewSet('llemon_video', 'llemon_video')
        request = types.SimpleNamespace(body=json.dumps({'prompt': 'animate this'}))
        response = view._generate(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'provider is required')


class EditDiscoveryTests(_DjviewTestCase):
    """list_edit_models_with_metadata() failure/empty listing now raises;
    _edit_metadata() no longer degrades that into a dict with a warning."""

    def _edit_metadata(self, side_effect):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            import llemon_djview.imagegen as imagegen

        list_edit_models_with_metadata = (
            mock.Mock(side_effect=side_effect)
            if isinstance(side_effect, Exception)
            else mock.Mock(return_value=side_effect)
        )
        with mock.patch.object(imagegen, 'supports_edit', return_value=True), \
                mock.patch.object(
                    imagegen, 'list_edit_models_with_metadata',
                    list_edit_models_with_metadata,
                ):
            return imagegen._edit_metadata('openrouter', 'images')

    def test_empty_discovery_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._edit_metadata(RuntimeError(
                "provider 'openrouter' declares edit support but the "
                "edit-model listing returned no models"
            ))

    def test_failed_discovery_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._edit_metadata(RuntimeError('catalog unavailable'))


class _FakeImageBackend:
    """Minimal imagegen backend double for _generate_result tests."""

    embeds_metadata_in_exif = False
    result: dict = {}
    instances: list = []

    def __init__(self, model=None, log_dir=None):
        self.model = model
        self.shutdown_called = False
        type(self).instances.append(self)

    def generate(self, prompt, **kwargs):
        return dict(type(self).result)

    def shutdown(self):
        self.shutdown_called = True

    @staticmethod
    def write_images(images, save_dir, stamp):
        raise AssertionError('write_images is patched out via save_operation_images')


class ImageEnhancementPassthroughTests(_DjviewTestCase):
    _ENHANCEMENT = {
        'provider': 'openrouter',
        'model': 'text-model',
        'prompt': 'rewrite instruction',
        'request_id': 'req-1',
        'usage': {},
    }

    def _run_generate_result(self, result):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        backend_cls = _FakeImageBackend
        backend_cls.result = result
        backend_cls.instances = []
        writers = {
            'make_imagegen_backend': mock.Mock(return_value=backend_cls),
            'save_operation_images': mock.Mock(return_value=(['out.png'], 'out.json')),
            'write_image_generation_exif_with_sidecar_fallback':
                mock.Mock(return_value=None),
            'write_image_metadata': mock.Mock(),
            'image_generation_summary_lines': mock.Mock(return_value=[]),
            'model_display': lambda model, *a, **k: model,
        }
        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(view, '_gallery_dir', return_value=tmp), \
                    mock.patch.object(view, '_log_dir', return_value=''), \
                    mock.patch.object(view, '_ensure_large_thumbnail'), \
                    mock.patch.dict(view._generate_result.__globals__, writers):
                payload, status = view._generate_result(
                    'original prompt', 'image-model', '1:1', '1024x1024',
                    None, None, 'openrouter', 'images',
                )
        return payload, status, writers

    def test_enhanced_result_forces_canonical_metadata_and_summary_line(self) -> None:
        payload, status, writers = self._run_generate_result({
            'model': 'image-model',
            'images': ['fake'],
            'usage': None,
            'generated_prompt': 'rewritten prompt',
            'prompt_enhancement': self._ENHANCEMENT,
        })
        self.assertEqual(status, 200)
        # embeds_metadata_in_exif is False, but the enhanced result must
        # still use the client-side canonical EXIF writer.
        exif_writer = writers['write_image_generation_exif_with_sidecar_fallback']
        exif_writer.assert_called_once()
        kwargs = exif_writer.call_args.kwargs
        self.assertEqual(kwargs['prompt'], 'original prompt')
        self.assertEqual(kwargs['generated_prompt'], 'rewritten prompt')
        self.assertEqual(kwargs['prompt_enhancement'], self._ENHANCEMENT)
        writers['write_image_metadata'].assert_not_called()
        summary_kwargs = writers['image_generation_summary_lines'].call_args.kwargs
        self.assertEqual(summary_kwargs['prompt'], 'original prompt')
        self.assertEqual(summary_kwargs['generated_prompt'], 'rewritten prompt')
        self.assertEqual(payload['generated_prompt'], 'rewritten prompt')

    def test_unenhanced_result_uses_plain_metadata_and_omits_fields(self) -> None:
        payload, status, writers = self._run_generate_result({
            'model': 'image-model',
            'images': ['fake'],
            'usage': None,
        })
        self.assertEqual(status, 200)
        writers['write_image_generation_exif_with_sidecar_fallback'] \
            .assert_not_called()
        metadata_writer = writers['write_image_metadata']
        metadata_writer.assert_called_once()
        self.assertIsNone(metadata_writer.call_args.kwargs['generated_prompt'])
        self.assertIsNone(metadata_writer.call_args.kwargs['prompt_enhancement'])
        summary_kwargs = writers['image_generation_summary_lines'].call_args.kwargs
        self.assertIsNone(summary_kwargs['generated_prompt'])
        self.assertNotIn('generated_prompt', payload)


class _FakeVideoBackend:
    result: dict = {}

    def __init__(self, **kwargs):
        pass

    def generate(self, prompt, **kwargs):
        return dict(type(self).result)

    def shutdown(self):
        pass


class VideoEnhancementPassthroughTests(_DjviewTestCase):
    _ENHANCEMENT = {
        'provider': 'openrouter',
        'model': 'text-model',
        'prompt': 'rewrite instruction',
        'request_id': 'req-2',
        'usage': {},
    }

    def _run_generate(self, result):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.videogen import LLemonVideoGenViewSet

        _FakeVideoBackend.result = result
        sidecar_writer = mock.Mock()
        patches = {
            'normalize_provider_api': lambda *a, **k: ('openrouter', 'openrouter'),
            'default_video_model': lambda *a, **k: 'video-model',
            'default_duration': lambda *a, **k: 5,
            'make_videogen_backend': mock.Mock(return_value=_FakeVideoBackend),
            'model_presentation': mock.Mock(return_value={
                'reference_image_request_family': 'none',
                'allows_start_image': False,
                'allows_end_image': False,
                'allows_reference_images': True,
                'allows_scene_images': False,
            }),
            'save_generated_videos': mock.Mock(return_value=['out.mp4']),
            'write_video_sidecar': sidecar_writer,
            'model_display': lambda model, *a, **k: model,
        }
        view = LLemonVideoGenViewSet('llemon_video', 'llemon_video')
        request = types.SimpleNamespace(
            body=json.dumps({
                'provider': 'openrouter',
                'prompt': 'original prompt',
                'duration': 5,
            }),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(view, '_gallery_dir', return_value=tmp), \
                    mock.patch.object(view, '_log_dir', return_value=''), \
                    mock.patch.object(view, '_u', return_value=''), \
                    mock.patch.dict(view._generate.__globals__, patches):
                resp = view._generate(request)
        return resp, sidecar_writer

    def test_enhanced_video_sidecar_and_summary_carry_generated_prompt(self) -> None:
        resp, sidecar_writer = self._run_generate({
            'model': 'video-model',
            'videos': ['fake'],
            'generated_prompt': 'rewritten prompt',
            'prompt_enhancement': self._ENHANCEMENT,
        })
        self.assertEqual(resp.status_code, 200)
        meta = sidecar_writer.call_args.args[2]
        self.assertEqual(meta['prompt'], 'original prompt')
        self.assertEqual(meta['generated_prompt'], 'rewritten prompt')
        self.assertEqual(meta['prompt_enhancement'], self._ENHANCEMENT)
        summary = resp.data['summary']
        self.assertIn(['Prompt', 'original prompt'], summary)
        self.assertIn(['Generated prompt', 'rewritten prompt'], summary)
        prompt_index = summary.index(['Prompt', 'original prompt'])
        self.assertEqual(summary[prompt_index + 1],
                         ['Generated prompt', 'rewritten prompt'])

    def test_unenhanced_video_sidecar_omits_enhancement_fields(self) -> None:
        resp, sidecar_writer = self._run_generate({
            'model': 'video-model',
            'videos': ['fake'],
        })
        self.assertEqual(resp.status_code, 200)
        meta = sidecar_writer.call_args.args[2]
        self.assertNotIn('generated_prompt', meta)
        self.assertNotIn('prompt_enhancement', meta)
        summary = resp.data['summary']
        self.assertIn(['Prompt', 'original prompt'], summary)
        self.assertNotIn('Generated prompt', [row[0] for row in summary])


class _RecordingVideoBackend:
    recorded_kwargs: dict = {}

    def __init__(self, **kwargs):
        pass

    def generate(self, prompt, **kwargs):
        type(self).recorded_kwargs = dict(kwargs)
        return {'model': 'wan-2.2-i2v-fast', 'videos': ['fake']}

    def shutdown(self):
        pass


def _segmind_presentation(**overrides):
    base = {
        'reference_image_request_family': 'none',
        'allows_start_image': True,
        'allows_end_image': False,
        'allows_reference_images': False,
        'allows_scene_images': False,
        'required_backend_transports': {'data_url': 'provider_upload'},
        'available_backend_transports': ['provider_upload'],
        'transport_warnings': {'provider_upload': 'Uploads your image to Segmind for hosting.'},
    }
    base.update(overrides)
    return base


class SegmindVideoStartImageConsentTests(_DjviewTestCase):
    """Covers videogen.py's Segmind start-image accept_data_handling_warnings
    gate (see specs/mediagen-video-user-interface-spec.md, "Segmind
    image-to-video integration"): a picker-sourced (or any
    other) data: image_url for wan-2.2-i2v-fast's provider_upload transport
    may reach the backend only with consent; a public https:// URL never
    resolves to data: and so never needs it.
    """

    def _run_generate(self, body_extra, presentation, *, gallery_dir=None):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.videogen import LLemonVideoGenViewSet

        _RecordingVideoBackend.recorded_kwargs = {}
        patches = {
            'normalize_provider_api': lambda *a, **k: ('segmind', 'segmind'),
            'default_video_model': lambda *a, **k: 'wan-2.2-i2v-fast',
            'default_duration': lambda *a, **k: 5,
            'make_videogen_backend': mock.Mock(return_value=_RecordingVideoBackend),
            'model_presentation': mock.Mock(return_value=presentation),
            'save_generated_videos': mock.Mock(return_value=['out.mp4']),
            'write_video_sidecar': mock.Mock(),
            'model_display': lambda model, *a, **k: model,
        }
        view = LLemonVideoGenViewSet('llemon_video', 'llemon_video')
        request = types.SimpleNamespace(
            body=json.dumps({
                'provider': 'segmind',
                'prompt': 'a video',
                'duration': 5,
                **body_extra,
            }),
            get_host=lambda: 'testserver',
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(view, '_gallery_dir', return_value=gallery_dir or tmp), \
                    mock.patch.object(view, '_log_dir', return_value=''), \
                    mock.patch.object(view, '_u', return_value=''), \
                    mock.patch.dict(view._generate.__globals__, patches):
                resp = view._generate(request)
        return resp, dict(_RecordingVideoBackend.recorded_kwargs)

    def test_public_url_dispatches_without_consent(self) -> None:
        resp, recorded = self._run_generate(
            {'image_url': 'https://example.com/start.png'},
            _segmind_presentation(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(recorded.get('image_url'), 'https://example.com/start.png')
        self.assertNotIn('accept_data_handling_warnings', recorded)

    def test_data_url_without_consent_is_rejected_before_dispatch(self) -> None:
        resp, recorded = self._run_generate(
            {'image_url': 'data:image/png;base64,AAAA'},
            _segmind_presentation(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('data-handling warning', resp.data['error'])
        self.assertEqual(recorded, {})

    def test_data_url_with_consent_dispatches_and_forwards_flag(self) -> None:
        resp, recorded = self._run_generate(
            {
                'image_url': 'data:image/png;base64,AAAA',
                'accept_data_handling_warnings': True,
            },
            _segmind_presentation(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(recorded.get('image_url'), 'data:image/png;base64,AAAA')
        self.assertIs(recorded.get('accept_data_handling_warnings'), True)

    def test_non_boolean_accept_flag_is_rejected(self) -> None:
        resp, recorded = self._run_generate(
            {
                'image_url': 'data:image/png;base64,AAAA',
                'accept_data_handling_warnings': 'true',
            },
            _segmind_presentation(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('must be a boolean', resp.data['error'])
        self.assertEqual(recorded, {})

    def test_data_url_rejected_when_transport_unavailable(self) -> None:
        resp, recorded = self._run_generate(
            {
                'image_url': 'data:image/png;base64,AAAA',
                'accept_data_handling_warnings': True,
            },
            _segmind_presentation(available_backend_transports=[]),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not supported', resp.data['error'])
        self.assertEqual(recorded, {})

    def test_unwarned_model_dispatches_with_data_url_and_no_flag(self) -> None:
        resp, recorded = self._run_generate(
            {'image_url': 'data:image/png;base64,AAAA'},
            _segmind_presentation(transport_warnings={}),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(recorded.get('image_url'), 'data:image/png;base64,AAAA')
        self.assertNotIn('accept_data_handling_warnings', recorded)


class _MetadataVideoBackend:
    @staticmethod
    def list_video_models_with_metadata():
        return [
            {'id': 'wan-2.2-i2v-fast', 'name': 'WAN 2.2 i2v Fast', 'description': ''},
            {'id': 'other-model', 'name': 'Other Model', 'description': ''},
        ]


class VideoModelCaveatTests(_DjviewTestCase):
    """Covers videogen.py's static known-caveat note (upgrades/
    segmind-image-to-video.md §1.3): wan-2.2-i2v-fast's capabilities carry a
    Grove-only 'known_caveat' key about its aspect-ratio quirk, while every
    other model's capabilities dict has no such key at all.
    """

    def test_known_caveat_present_only_for_flagged_model(self) -> None:
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.videogen import LLemonVideoGenViewSet

        view = LLemonVideoGenViewSet('llemon_video', 'llemon_video')
        patches = {
            'make_videogen_backend': mock.Mock(return_value=_MetadataVideoBackend),
            'model_presentation': mock.Mock(return_value={}),
        }
        with mock.patch.dict(view._model_options.__globals__, patches):
            options = view._model_options('segmind', 'segmind')
        by_id = {opt['id']: opt for opt in options}
        self.assertEqual(
            by_id['wan-2.2-i2v-fast']['capabilities']['known_caveat'],
            'This model has been observed to ignore the requested aspect '
            'ratio in image-to-video mode and return square (1:1) output '
            'instead. This is Segmind provider behavior, not a Grove bug.',
        )
        self.assertNotIn('known_caveat', by_id['other-model']['capabilities'])


def _edit_option(model_id):
    operation = lambda available: {
        'available': available, 'unavailable_reason': None if available else 'unavailable',
        'designation': None, 'designation_reason': None,
    }
    controls = {
        'aspect_ratios': [], 'default_aspect_ratio': None,
        'image_sizes': [], 'default_image_size': None,
        'qualities': [], 'default_quality': None, 'extra_fields': [],
    }
    edit_input = {'accepted_source_kinds': ['data_url'],
                  'required_backend_transports': {},
                  'available_backend_transports': [],
                  'transport_warnings': {}}
    return {'id': model_id, 'name': model_id, 'display': model_id,
            'presentation': {
                'id': model_id, 'name': model_id, 'description': None,
                'detail': 'complete',
                'operations': {
                    'generate': operation(False), 'edit': operation(True),
                    # Ordered schema, effective_max_count == 1: edit_images
                    # mirrors edit exactly here, per specs/mediagen-image-
                    # spec.md's "Agreement with edit_input is scoped, not
                    # universal" (Task 13).
                    'edit_images': operation(True),
                },
                'controls': {'generate': dict(controls), 'edit': dict(controls)},
                'edit_input': edit_input,
                'edit_inputs': {
                    'shape': 'ordered', 'min_count': 1, 'max_count': None,
                    'effective_max_count': 1,
                    'accepted_source_kinds': edit_input['accepted_source_kinds'],
                    'required_backend_transports': edit_input['required_backend_transports'],
                    'available_backend_transports': edit_input['available_backend_transports'],
                    'transport_warnings': edit_input['transport_warnings'],
                    'roles': [],
                }}}


_OPENROUTER_EDIT_META = {
    'edit_models':               ['vendor/edit-model'],
    'edit_model_options':        [_edit_option('vendor/edit-model')],
    'selected_edit_model':       'vendor/edit-model',
    'default_edit_model':        'vendor/edit-model',
    'edit_aspect_ratios':        ['1:1', '16:9'],
    'default_edit_aspect_ratio': '1:1',
    'edit_image_sizes':          ['1024x1024', '2048x2048'],
    'default_edit_image_size':   '1024x1024',
}

_VENICE_EDIT_META = {
    'edit_models':               ['qwen-edit'],
    'edit_model_options':        [_edit_option('qwen-edit')],
    'selected_edit_model':       'qwen-edit',
    'default_edit_model':        'qwen-edit',
    'edit_aspect_ratios':        ['auto', '1:1'],
    'default_edit_aspect_ratio': 'auto',
    'edit_image_sizes':          [],
    'default_edit_image_size':   '',
}


class ImageEditControlTests(_DjviewTestCase):
    def _run_edit(self, body, edit_meta, provider='openrouter', *, include_provider=True):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request_body = dict(body)
        if include_provider:
            request_body['provider'] = provider
        request = types.SimpleNamespace(body=json.dumps(request_body))
        edit_result = mock.Mock(return_value=({'files': ['out.png']}, 200))
        edit_meta = __import__('copy').deepcopy(edit_meta)
        row_controls = edit_meta['edit_model_options'][0]['presentation']['controls']['edit']
        row_controls.update({
            'aspect_ratios': edit_meta['edit_aspect_ratios'],
            'image_sizes': edit_meta['edit_image_sizes'],
        })
        patches = {
            'normalize_provider_api': lambda *a, **k: (provider, provider),
            'supports_edit': lambda *a, **k: True,
            '_edit_metadata': lambda *a, **k: dict(edit_meta),
            'default_aspect_ratio': lambda *a, **k: edit_meta['default_edit_aspect_ratio'],
            'default_image_size': lambda *a, **k: edit_meta['default_edit_image_size'],
            'preflight_request': lambda *a, **k: None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                        view, '_read_image_as_data_url',
                        return_value=('data:image/png;base64,x', None),
                    ), \
                    mock.patch.object(view, '_edit_result', edit_result), \
                    mock.patch.dict(view._do_edit_image.__globals__, patches):
                resp = view._do_edit_image(request, tmp)
        return resp, edit_result

    def test_openrouter_edit_requires_explicit_fixed_aspect_ratio(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('fixed aspect ratio', resp.data['error'])
        edit_result.assert_not_called()

    def test_edit_requires_explicit_provider(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
            include_provider=False,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'provider is required')
        edit_result.assert_not_called()

    def test_edit_requires_explicit_model(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it', 'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'edit model is required')
        edit_result.assert_not_called()

    def test_edit_model_discovery_failure_returns_502(self) -> None:
        # A discovery failure (or an empty listing from a provider that
        # declares supports_edit) is a provider fault: _edit_metadata() now
        # raises rather than returning a degraded dict, and _do_edit_image()
        # turns that into a 502 rather than a 400.
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'openrouter', 'filename': 'a.png', 'prompt': 'change it',
            'model': 'vendor/edit-model',
        }))
        edit_result = mock.Mock(return_value=({'files': ['out.png']}, 200))
        patches = {
            'normalize_provider_api': lambda *a, **k: ('openrouter', 'openrouter'),
            'supports_edit': lambda *a, **k: True,
            '_edit_metadata': mock.Mock(
                side_effect=RuntimeError('could not list edit models: network down'),
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                        view, '_read_image_as_data_url',
                        return_value=('data:image/png;base64,x', None),
                    ), \
                    mock.patch.object(view, '_edit_result', edit_result), \
                    mock.patch.dict(view._do_edit_image.__globals__, patches):
                resp = view._do_edit_image(request, tmp)

        self.assertEqual(resp.status_code, 502)
        self.assertIn('could not list edit models', resp.data['error'])
        edit_result.assert_not_called()

    def test_openrouter_edit_forwards_explicit_size(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '16:9',
             'image_size': '2048x2048'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 200)
        args = edit_result.call_args.args
        self.assertEqual(args[5], '16:9')        # aspect_ratio
        self.assertEqual(args[6], '2048x2048')   # image_size

    def test_openrouter_edit_defaults_size_when_omitted(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(edit_result.call_args.args[6], '1024x1024')

    def test_openrouter_edit_rejects_invalid_size(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1',
             'image_size': '640x480'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('image_size', resp.data['error'])
        edit_result.assert_not_called()

    def test_openrouter_edit_rejects_unknown_model(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/not-an-edit-model', 'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('edit model', resp.data['error'])
        edit_result.assert_not_called()

    def test_edit_rejects_data_url_incompatible_model_before_dispatch(self) -> None:
        edit_meta = dict(_OPENROUTER_EDIT_META)
        edit_meta['edit_model_options'] = [_edit_option('vendor/edit-model')]
        presentation = edit_meta['edit_model_options'][0]['presentation']
        presentation['edit_input']['accepted_source_kinds'] = ['https_url']
        presentation['edit_inputs']['accepted_source_kinds'] = ['https_url']
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            edit_meta,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'data URL unsupported')
        edit_result.assert_not_called()

    def test_venice_edit_defaults_to_auto_source_ratio(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it', 'model': 'qwen-edit'},
            _VENICE_EDIT_META,
            provider='venice',
        )
        self.assertEqual(resp.status_code, 200)
        args = edit_result.call_args.args
        self.assertEqual(args[5], 'auto')   # aspect_ratio
        self.assertIsNone(args[6])          # image_size never sent to Venice

    def test_venice_edit_rejects_explicit_size_with_explanation(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it', 'model': 'qwen-edit',
             'image_size': '1024x1024'},
            _VENICE_EDIT_META,
            provider='venice',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('source image', resp.data['error'])
        edit_result.assert_not_called()

    def test_multi_image_array_reaches_edit_images_in_order(self) -> None:
        # Task 13: the provider-neutral images:[{filename, role?}] shape
        # dispatches through edit_images(), carrying every source through
        # in caller order, once a model's effective_max_count allows it.
        edit_meta = dict(_OPENROUTER_EDIT_META)
        option = _edit_option('vendor/edit-model')
        option['presentation']['edit_inputs']['effective_max_count'] = 2
        edit_meta['edit_model_options'] = [option]
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png'}, {'filename': 'b.png'}],
             'prompt': 'combine them',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            edit_meta,
        )
        self.assertEqual(resp.status_code, 200)
        images_arg, filenames_arg = edit_result.call_args.args[0], edit_result.call_args.args[1]
        self.assertEqual(filenames_arg, ['a.png', 'b.png'])
        self.assertEqual(len(images_arg), 2)
        self.assertNotIn('role', images_arg[0])

    def test_singular_filename_is_input_compatible_with_images_array(self) -> None:
        # The old scalar `filename` field remains accepted as one-release
        # input-only compatibility (specs/mediagen-image-spec.md, "Grove
        # adoption"), equivalent to a single-element `images` array.
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 200)
        images_arg, filenames_arg = edit_result.call_args.args[0], edit_result.call_args.args[1]
        self.assertEqual(filenames_arg, ['a.png'])
        self.assertEqual(images_arg, [{'source': 'data:image/png;base64,x'}])

    _NAMED_ROLES = [
        {'name': 'first', 'required': True, 'position': 0,
         'description': None, 'aliases': [],
         'accepted_source_kinds': ['data_url'],
         'required_backend_transports': {}, 'available_backend_transports': []},
        {'name': 'second', 'required': True, 'position': 1,
         'description': None, 'aliases': [],
         'accepted_source_kinds': ['data_url'],
         'required_backend_transports': {}, 'available_backend_transports': []},
    ]

    def _named_role_edit_meta(self):
        edit_meta = dict(_OPENROUTER_EDIT_META)
        option = _edit_option('vendor/edit-model')
        option['presentation']['edit_inputs'].update({
            'shape': 'named', 'min_count': 2, 'max_count': 2,
            'effective_max_count': 2, 'roles': self._NAMED_ROLES,
        })
        edit_meta['edit_model_options'] = [option]
        return edit_meta

    def test_named_role_model_dispatches_with_supplied_roles(self) -> None:
        # Task 13 Phase 2: the role-assignment UI makes a named schema
        # selectable and dispatchable once every required role is supplied.
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 'first'},
                        {'filename': 'b.png', 'role': 'second'}],
             'prompt': 'combine them', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            self._named_role_edit_meta(),
        )
        self.assertEqual(resp.status_code, 200)
        images_arg = edit_result.call_args.args[0]
        self.assertEqual([i['role'] for i in images_arg], ['first', 'second'])

    def test_named_role_model_rejects_missing_required_role(self) -> None:
        # normalize_edit_inputs() remains the authority on role completeness
        # even though the coarse Grove-level eligibility check now passes.
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 'first'}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            self._named_role_edit_meta(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('missing required role', resp.data['error'])
        edit_result.assert_not_called()

    def test_over_count_request_rejected_before_dispatch(self) -> None:
        # normalize_edit_inputs() enforces effective_max_count before any
        # backend dispatch (specs/mediagen-image-spec.md, "Preflight
        # precedence when checks overlap").
        edit_meta = dict(_OPENROUTER_EDIT_META)
        option = _edit_option('vendor/edit-model')
        option['presentation']['edit_inputs']['effective_max_count'] = 1
        edit_meta['edit_model_options'] = [option]
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png'}, {'filename': 'b.png'}],
             'prompt': 'combine them',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            edit_meta,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('at most 1', resp.data['error'])
        edit_result.assert_not_called()

    def test_over_count_request_never_reads_any_gallery_file(self) -> None:
        # P1 regression: request shape/count is validated against the
        # selected model (normalize_edit_inputs()) before any gallery file
        # is read, so an over-count request never pays to load or
        # base64-encode images it will reject anyway, and a later-listed
        # unreadable file can't produce "file not found" ahead of the
        # count error that should fire first.
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        edit_meta = __import__('copy').deepcopy(_OPENROUTER_EDIT_META)
        option = edit_meta['edit_model_options'][0]
        option['presentation']['edit_inputs']['effective_max_count'] = 1
        row_controls = option['presentation']['controls']['edit']
        row_controls.update({
            'aspect_ratios': edit_meta['edit_aspect_ratios'],
            'image_sizes': edit_meta['edit_image_sizes'],
        })
        request = types.SimpleNamespace(body=json.dumps({
            'provider': 'openrouter',
            'images': [{'filename': 'valid.png'}, {'filename': 'missing.png'}],
            'prompt': 'combine them', 'model': 'vendor/edit-model',
            'aspect_ratio': '1:1',
        }))
        edit_result = mock.Mock(return_value=({'files': ['out.png']}, 200))
        read_image = mock.Mock(return_value=('data:image/png;base64,x', None))
        patches = {
            'normalize_provider_api': lambda *a, **k: ('openrouter', 'openrouter'),
            'supports_edit': lambda *a, **k: True,
            '_edit_metadata': lambda *a, **k: dict(edit_meta),
            'preflight_request': lambda *a, **k: None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(view, '_read_image_as_data_url', read_image), \
                    mock.patch.object(view, '_edit_result', edit_result), \
                    mock.patch.dict(view._do_edit_image.__globals__, patches):
                resp = view._do_edit_image(request, tmp)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('at most 1', resp.data['error'])
        read_image.assert_not_called()
        edit_result.assert_not_called()

    def test_non_string_role_is_rejected_before_normalization(self) -> None:
        # P2 regression: a non-string (or unhashable) role must not be
        # silently dropped into an unroled image, and must not reach
        # normalize_edit_inputs()'s dict-keyed role lookup unexamined.
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 7}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('role must be a string', resp.data['error'])
        edit_result.assert_not_called()

    def test_empty_role_on_ordered_schema_is_rejected_not_dropped(self) -> None:
        # P2 regression: an explicitly-supplied empty role must not be
        # dropped into a valid unroled image for an ordered schema, which
        # forbids any role at all -- normalize_edit_inputs() must see and
        # reject the key.
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': ''}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            _OPENROUTER_EDIT_META,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('does not use named roles', resp.data['error'])
        edit_result.assert_not_called()

    def _warned_single_scope_edit_meta(self):
        # Ordered (no-roles) schema whose only path is a warned transport --
        # exercises _resolved_edit_warning_reason()'s non-named branch.
        edit_meta = dict(_OPENROUTER_EDIT_META)
        option = _edit_option('vendor/edit-model')
        option['presentation']['edit_input'].update({
            'accepted_source_kinds': [],
            'required_backend_transports': {'data_url': 'provider_upload'},
            'available_backend_transports': ['provider_upload'],
            'transport_warnings': {'provider_upload': 'uploads leave LLemon-managed storage'},
        })
        option['presentation']['edit_inputs'].update({
            'accepted_source_kinds': [],
            'required_backend_transports': {'data_url': 'provider_upload'},
            'available_backend_transports': ['provider_upload'],
            'transport_warnings': {'provider_upload': 'uploads leave LLemon-managed storage'},
        })
        edit_meta['edit_model_options'] = [option]
        return edit_meta

    def test_warned_transport_requires_consent_before_dispatch(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1'},
            self._warned_single_scope_edit_meta(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('requires accepting a data-handling warning', resp.data['error'])
        edit_result.assert_not_called()

    def test_warned_transport_dispatches_once_consent_is_given(self) -> None:
        resp, edit_result = self._run_edit(
            {'filename': 'a.png', 'prompt': 'change it',
             'model': 'vendor/edit-model', 'aspect_ratio': '1:1',
             'accept_data_handling_warnings': True},
            self._warned_single_scope_edit_meta(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(edit_result.call_args.args[10], True)

    def test_accept_data_handling_warnings_rejects_non_boolean_values(self) -> None:
        for bad_value in ('true', 'false', 1, 0):
            with self.subTest(bad_value=bad_value):
                resp, edit_result = self._run_edit(
                    {'filename': 'a.png', 'prompt': 'change it',
                     'model': 'vendor/edit-model', 'aspect_ratio': '1:1',
                     'accept_data_handling_warnings': bad_value},
                    self._warned_single_scope_edit_meta(),
                )
                self.assertEqual(resp.status_code, 400)
                self.assertIn('must be a boolean', resp.data['error'])
                edit_result.assert_not_called()

    def _optional_warned_role_edit_meta(self):
        # All-optional named schema mirroring the JS fixture
        # ('mixed-optional-roles'): an optional role is warned, a sibling
        # optional role is clean -- this is the direct regression fixture
        # for "an optional role only needs consent when actually assigned".
        edit_meta = dict(_OPENROUTER_EDIT_META)
        option = _edit_option('vendor/edit-model')
        option['presentation']['edit_inputs'].update({
            'shape': 'named', 'min_count': 0, 'max_count': 2,
            'effective_max_count': 2,
            'transport_warnings': {'provider_upload': 'uploads leave LLemon-managed storage'},
            'roles': [
                {'name': 'warned', 'required': False, 'position': 0,
                 'description': None, 'aliases': [],
                 'accepted_source_kinds': [],
                 'required_backend_transports': {'data_url': 'provider_upload'},
                 'available_backend_transports': ['provider_upload']},
                {'name': 'clean', 'required': False, 'position': 1,
                 'description': None, 'aliases': [],
                 'accepted_source_kinds': ['data_url'],
                 'required_backend_transports': {}, 'available_backend_transports': []},
            ],
        })
        edit_meta['edit_model_options'] = [option]
        return edit_meta

    def test_optional_clean_role_dispatches_without_consent(self) -> None:
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 'clean'}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            self._optional_warned_role_edit_meta(),
        )
        self.assertEqual(resp.status_code, 200)
        edit_result.assert_called_once()

    def test_optional_warned_role_requires_consent_only_when_assigned(self) -> None:
        # This is the direct regression test for the schema-level
        # aggregate's bug: assigning the same image to the *warned*
        # optional role instead of the clean one must require consent,
        # even though the model as a whole was usable without it above.
        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 'warned'}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1'},
            self._optional_warned_role_edit_meta(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('requires accepting a data-handling warning', resp.data['error'])
        edit_result.assert_not_called()

        resp, edit_result = self._run_edit(
            {'images': [{'filename': 'a.png', 'role': 'warned'}],
             'prompt': 'change it', 'model': 'vendor/edit-model',
             'aspect_ratio': '1:1', 'accept_data_handling_warnings': True},
            self._optional_warned_role_edit_meta(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIs(edit_result.call_args.args[10], True)


class EditResultBackendForwardingTests(_DjviewTestCase):
    def _run_edit_result(self, image_size, accept_data_handling_warnings=False):
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        recorded: dict = {}

        class FakeEditBackend:
            def __init__(self, model=None, log_dir=None):
                pass

            def edit_images(self, images, prompt, **kwargs):
                recorded.update(kwargs)
                return {'images': ['fake'], 'usage': None}

            def shutdown(self):
                pass

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        save_result = mock.Mock(return_value=({'files': ['out.png']}, 200))
        patches = {'make_imagegen_backend': mock.Mock(return_value=FakeEditBackend)}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(view, '_log_dir', return_value=''), \
                    mock.patch.object(view, '_save_operation_result', save_result), \
                    mock.patch.dict(view._edit_result.__globals__, patches):
                payload, status = view._edit_result(
                    [{'source': 'data:image/png;base64,x'}], ['a.png'], tmp, 'change it',
                    'vendor/edit-model', '1:1', image_size, None,
                    'openrouter', 'images', accept_data_handling_warnings,
                )
        sidecar = save_result.call_args.args[3]
        return recorded, sidecar

    def test_explicit_size_reaches_backend_and_sidecar(self) -> None:
        recorded, sidecar = self._run_edit_result('2048x2048')
        self.assertEqual(recorded['image_size'], '2048x2048')
        self.assertEqual(sidecar['image_size'], '2048x2048')

    def test_omitted_size_is_not_sent_to_backend(self) -> None:
        recorded, sidecar = self._run_edit_result(None)
        self.assertNotIn('image_size', recorded)
        self.assertNotIn('image_size', sidecar)

    def test_accept_data_handling_warnings_false_reaches_backend(self) -> None:
        recorded, _sidecar = self._run_edit_result(None, accept_data_handling_warnings=False)
        self.assertIs(recorded['accept_data_handling_warnings'], False)

    def test_accept_data_handling_warnings_true_reaches_backend(self) -> None:
        recorded, _sidecar = self._run_edit_result(None, accept_data_handling_warnings=True)
        self.assertIs(recorded['accept_data_handling_warnings'], True)

    def _run_edit_stream(self, accept_data_handling_warnings):
        # The streaming path threads accept_data_handling_warnings through
        # _edit_stream()'s worker thread to the same _edit_result() call the
        # non-streaming path uses -- consumed here directly (not through
        # _do_edit_image()'s StreamingHttpResponse, which this test file's
        # faked Django stubs out as bare `object` and can't construct).
        with mock.patch.dict(sys.modules, _fake_django_modules()):
            from llemon_djview.imagegen import LLemonImageGenViewSet

        view = LLemonImageGenViewSet('llemon_image', 'llemon_image')
        edit_result = mock.Mock(return_value=({'files': ['out.png']}, 200))
        with mock.patch.object(view, '_edit_result', edit_result):
            events = [
                json.loads(line) for line in view._edit_stream(
                    [{'source': 'data:image/png;base64,x'}], ['a.png'], '/tmp', 'change it',
                    'vendor/edit-model', '1:1', None, None,
                    'openrouter', 'images', accept_data_handling_warnings,
                )
            ]
        return edit_result, events

    def test_edit_stream_forwards_accept_data_handling_warnings_true(self) -> None:
        edit_result, events = self._run_edit_stream(True)
        self.assertIs(edit_result.call_args.args[-1], True)
        self.assertTrue(any(e.get('event') == 'done' for e in events))

    def test_edit_stream_forwards_accept_data_handling_warnings_false(self) -> None:
        edit_result, events = self._run_edit_stream(False)
        self.assertIs(edit_result.call_args.args[-1], False)
        self.assertTrue(any(e.get('event') == 'done' for e in events))


if __name__ == '__main__':
    unittest.main()
