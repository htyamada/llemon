"""llemon_djview.videogen -- Django view logic for LLemon video generation."""

import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from django.conf import settings  # type: ignore[import-untyped]
from django.http import FileResponse, Http404, JsonResponse  # type: ignore[import-untyped]
from django.shortcuts import render  # type: ignore[import-untyped]
from django.views.decorators.csrf import csrf_exempt  # type: ignore[import-untyped]
from django.views.decorators.http import require_POST  # type: ignore[import-untyped]

from hty7.llemon.mediagen.videogen import (
    PROVIDERS,
    default_duration,
    default_video_model,
    get_model_note,
    get_model_tag_states,
    get_notes_load_errors,
    get_notes_slot,
    get_reverse_tags,
    get_tags,
    set_model_note,
    make_videogen_backend,
    model_display,
    model_presentation,
    normalize_provider_api,
)
from .storage import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    delete_media_file,
    file_as_data_url,
    safe_name,
    sanitize_metadata_data_urls,
    delete_image_asset,
    delete_video_asset,
    move_image_asset,
    move_video_asset,
    read_video_sidecar,
    save_generated_videos,
    save_uploaded_image_files,
    video_thumb_name,
    write_video_sidecar,
)
from .media_utils import ensure_media_thumbnail, is_video
from .base_viewset import MediaGenViewSetBase, _RESERVED_GALLERY_DIRS
from .media_creator import (
    build_creator_presentation,
    build_model_target,
    build_operation_presentation,
)
logger = logging.getLogger(__name__)

_MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

# Grove-only, hardcoded notes about known provider quirks that aren't part of
# hty7's model_presentation() payload (see mediagen-video-user-interface-spec.md,
# "Known caveat: aspect ratio not honored"). Keyed by (provider, model id); omit
# the 'known_caveat' capability
# key entirely when a model has no entry here.
_KNOWN_MODEL_CAVEATS: dict[tuple[str, str], str] = {
    ('segmind', 'wan-2.2-i2v-fast'): (
        'This model has been observed to ignore the requested aspect '
        'ratio in image-to-video mode and return square (1:1) output '
        'instead. This is Segmind provider behavior, not a Grove bug.'
    ),
}

def _sanitize_video_metadata(value: Any) -> Any:
    return sanitize_metadata_data_urls(value)


class LLemonVideoGenViewSet(MediaGenViewSetBase):
    """Django views for LLemon video generation, bound to an app namespace."""

    shared_gallery_template_prefix = 'llemon_image'

    route_gallery = 'video_gallery'
    route_archive = 'video_archive'
    route_video_file = 'video_file'
    route_video_thumbnail = 'video_thumbnail'
    route_video_large_thumbnail = 'video_large_thumbnail'
    route_archive_file = 'video_archive_file'
    route_archive_thumbnail = 'video_archive_thumbnail'
    route_archive_large_thumbnail = 'video_archive_large_thumbnail'
    route_delete = 'video_delete'
    route_archive_delete = 'video_archive_delete'
    route_upload = 'video_upload'
    route_move_to_archive = 'video_move_to_archive'
    route_move_to_gallery = 'video_move_to_gallery'

    def __init__(self, template_prefix: str, url_namespace: str, *, base_nav=None,
                 nav=None, nav_suffix=None):
        super().__init__(
            template_prefix, url_namespace,
            base_nav=base_nav, nav=nav, nav_suffix=nav_suffix,
        )
        self.gallery             = csrf_exempt(self.gallery)
        self.generate            = csrf_exempt(require_POST(self._generate))
        self.model_note          = csrf_exempt(self._model_note)
        self.models_json         = self._models_json
        self.video_file          = self._video_file
        self.archive_video_file  = self._archive_video_file
        self.video_thumbnail         = self._video_thumbnail
        self.video_large_thumbnail   = self._video_large_thumbnail
        self.archive_video_thumbnail = self._archive_video_thumbnail
        self.archive_video_large_thumbnail = self._archive_video_large_thumbnail
        self.upload              = csrf_exempt(require_POST(self._upload))
        self.delete_video        = csrf_exempt(require_POST(self._delete_video))
        self.delete_archive_video = csrf_exempt(require_POST(self._delete_archive_video))
        self.move_to_archive     = csrf_exempt(require_POST(self._move_to_archive))
        self.move_to_gallery     = csrf_exempt(require_POST(self._move_to_gallery))

    def _ctx(self, title, nav, extra):
        ctx = {'title': title, 'nav': nav}
        if self._base_nav is not None:
            ctx['base_nav'] = self._base_nav
        ctx.update(extra)
        return ctx

    def _shared_gallery_template(self, name: str) -> str:
        return f'{self.shared_gallery_template_prefix}/{name}'

    def _media_dir(self):
        return getattr(settings, 'LLEMON_VIDEOGEN_MEDIA_DIR', '')

    def _log_dir(self):
        return getattr(settings, 'LLEMON_VIDEOGEN_LOG_DIR', None)

    def _delete_category_file(self, fname: str):
        try:
            conn = self._gallery_category_db()
            conn.remove_file(fname)
        except Exception as e:
            logger.warning('could not remove video category assignment for %s: %s', fname, e)

    def _nav(self, active: str | None = None):
        items = [
            ('video_creator', 'Video creator'),
            (self.route_gallery, 'Gallery'),
            (self.route_archive, 'Archive'),
        ]
        return self._nav_prefix + [{'name': label, 'url': self._u(name)} for name, label in items]

    def video(self, request):
        pages = [
            {'name': 'Video creator', 'url': self._u('video_creator')},
            {'name': 'Gallery', 'url': self._u(self.route_gallery)},
            {'name': 'Archive', 'url': self._u(self.route_archive)},
        ]
        return render(request, self._t('index.html'), self._ctx(
            'LLemon Video', self._nav_prefix + pages, {'pages': pages},
        ))

    def video_creator(self, request):
        provider_param = request.GET.get('provider', '').strip() or None
        api_param = request.GET.get('api', '').strip() or None
        try:
            provider, api = normalize_provider_api(provider_param, api_param)
            model_options = self._model_options(provider, api)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except Exception as e:
            logger.exception('could not list video generation models')
            return JsonResponse({'error': f'could not list video generation models: {e}'},
                                status=502)
        requested_model = (
            request.GET.get('model') if 'model' in request.GET else None
        )
        creator_data = self._creator_data(
            provider, api, model_options, requested_model=requested_model,
        )
        notes_load_errors = get_notes_load_errors()

        output_subdir_raw = request.GET.get('output_subdir', '').strip()
        try:
            output_subdir = self._safe_subdir(output_subdir_raw)
        except ValueError:
            output_subdir = ''
        if output_subdir:
            gallery_dir_c = self._gallery_dir()
            if not gallery_dir_c or not self._validated_project_dir(gallery_dir_c, output_subdir):
                output_subdir = ''
        def _safe_url(name: str) -> str | None:
            try:
                return self._u(name)
            except Exception:
                return None
        if output_subdir:
            video_file_url = self._u('gallery_project_file', f'{output_subdir}/PLACEHOLDER')
            video_large_thumbnail_url = self._u('gallery_project_large_thumb', f'{output_subdir}/PLACEHOLDER')
            creator_self_url = self._u('video_creator') + '?' + urlencode({'output_subdir': output_subdir})
            gallery_back_url = self._u(self.route_gallery) + '?' + urlencode({'subdir': output_subdir})
            nav = self._nav_prefix + [
                {'name': 'Video creator', 'url': creator_self_url},
                {'name': 'Gallery', 'url': gallery_back_url},
            ]
            image_creator_url = _safe_url('image_creator')
            if image_creator_url:
                nav.append({
                    'name': 'Image Creator',
                    'url': image_creator_url + '?' + urlencode({'output_subdir': output_subdir}),
                })
            archive_url = _safe_url(self.route_archive)
            if archive_url:
                nav.append({'name': 'Archive', 'url': archive_url})
            source_dirs_url = _safe_url('source_dirs')
            if source_dirs_url:
                nav.append({
                    'name': 'Input files',
                    'url': source_dirs_url + '?' + urlencode({'dest_subdir': output_subdir}),
                })
        else:
            video_file_url = self._u(self.route_video_file, 'PLACEHOLDER')
            video_large_thumbnail_url = self._u(self.route_video_large_thumbnail, 'PLACEHOLDER')
            nav = self._nav('creator')

        return render(request, self._t('video.html'), self._ctx(
            'LLemon Video Creator', nav, {
                'providers':        PROVIDERS,
                **creator_data,
                'available_tags':   [] if notes_load_errors else get_tags(),
                'reverse_tags':     [] if notes_load_errors else get_reverse_tags(),
                'notes_load_errors': notes_load_errors,
                'active_notes_slot': get_notes_slot(),
                'output_subdir':    output_subdir,
                'generate_url':     self._u('video_generate'),
                'model_note_url':   self._u('video_model_note'),
                'models_json_url':  self._u('video_models_json'),
                'video_file_url':              video_file_url,
                'video_large_thumbnail_url':   video_large_thumbnail_url,
                'gallery_images':   self._gallery_picker_items(output_subdir),
                'source_dirs_json_url': self._source_dirs_json_url(),
            },
        ))

    def _model_options(self, provider: str, api: str) -> list[dict[str, Any]]:
        backend_cls = make_videogen_backend(provider, api)
        if hasattr(backend_cls, 'list_video_models_with_metadata'):
            rows = backend_cls.list_video_models_with_metadata()
            options = []
            for row in rows:
                capabilities = dict(row.get('capabilities') or {})
                capabilities['presentation'] = model_presentation(
                    row['id'], provider, api, capabilities=capabilities,
                )
                caveat = _KNOWN_MODEL_CAVEATS.get((provider, row['id']))
                if caveat:
                    capabilities['known_caveat'] = caveat
                options.append({
                    'id': row['id'],
                    'display': (
                        f"{row.get('name') or ''} ({row['id']})"
                        if row.get('name') else row['id']
                    ),
                    'description': row.get('description') or '',
                    'capabilities': capabilities,
                })
            return options
        options = []
        for model_id in backend_cls.list_video_models():
            capabilities = {'presentation': model_presentation(model_id, provider, api)}
            caveat = _KNOWN_MODEL_CAVEATS.get((provider, model_id))
            if caveat:
                capabilities['known_caveat'] = caveat
            options.append({
                'id': model_id,
                'display': model_id,
                'description': '',
                'capabilities': capabilities,
            })
        return options

    def _creator_data(
        self,
        provider: str,
        api: str,
        model_options: list[dict[str, Any]],
        *,
        requested_model: str | None = None,
    ) -> dict[str, Any]:
        """Return the provider data shared by initial render and JSON refresh."""
        default_model = default_video_model(provider, api)
        duration = default_duration(provider, api)
        model_tag_states = self._model_tag_states(
            provider, [option['id'] for option in model_options],
        )
        model_ids = {option['id'] for option in model_options}
        selected_model = (
            requested_model
            if requested_model in model_ids
            else default_model
        )
        selected_target = (
            build_model_target(provider, api, 'generate', selected_model)
            if selected_model else None
        )
        data: dict[str, Any] = {
            'provider': provider,
            'api': api,
            'default_model': default_model,
            'selected_model': selected_model,
            'default_duration': duration,
            'model_options': model_options,
            'model_tag_states': model_tag_states,
        }
        data['presentation'] = build_creator_presentation(provider, api, {
            'generate': build_operation_presentation(
                'generate',
                model_options=model_options,
                selected_model=selected_model,
                default_model=default_model,
                defaults={'duration': duration},
                availability={'enabled': True},
                selected_target=selected_target,
                notes={
                    'provider': provider,
                    'model_tag_states': model_tag_states,
                },
            ),
        })
        return data

    def gallery(self, request):
        return self._media_page(
            request,
            title='LLemon Video Gallery',
            media_dir=self._gallery_dir(),
            file_url_name=self.route_video_file,
            thumbnail_url_name=self.route_video_thumbnail,
            large_thumbnail_url_name=self.route_video_large_thumbnail,
            delete_url=self._u(self.route_delete),
            move_url=self._u(self.route_move_to_archive),
            move_label='Archive',
            empty='No videos yet.',
            generate_url=self._u('video_creator'),
            category_enabled=True,
        )

    def archive(self, request):
        return self._media_page(
            request,
            title='LLemon Video Archive',
            template_name='archive.html',
            media_dir=self._archive_dir(),
            file_url_name=self.route_archive_file,
            thumbnail_url_name=self.route_archive_thumbnail,
            large_thumbnail_url_name=self.route_archive_large_thumbnail,
            delete_url=self._u(self.route_archive_delete),
            move_url=self._u(self.route_move_to_gallery),
            move_label='Unarchive',
            empty='No archived videos.',
            generate_url=None,
            category_enabled=False,
        )

    def _media_page(
        self,
        request,
        *,
        title: str,
        media_dir: str,
        file_url_name: str,
        thumbnail_url_name: str,
        delete_url: str,
        move_url: str,
        move_label: str,
        empty: str,
        generate_url: str | None,
        large_thumbnail_url_name: str = '',
        template_name: str = 'gallery.html',
        category_enabled: bool = False,
    ):
        categories, category_ids_by_file, active_category, category_filter = self._process_categories(request, category_enabled)
        videos = self._list_videos(
            media_dir,
            file_url_name,
            thumbnail_url_name,
            large_thumbnail_url_name=large_thumbnail_url_name,
            active_category=active_category if category_enabled else '',
            category_filter=category_filter,
            category_ids_by_file=category_ids_by_file,
        )
        ctx = {
            'images': videos,
            'delete_image_url': delete_url,
            'empty': empty,
            'generate_url': generate_url,
            'video_generate_url': self._u('video_creator'),
            'upload_url': self._u(self.route_upload),
            'category_enabled': category_enabled,
            'categories': categories,
            'active_category': active_category,
        }
        if move_label == 'Archive':
            ctx['move_to_archive_url'] = move_url
        elif move_label == 'Unarchive':
            ctx['move_to_gallery_url'] = move_url
        return render(request, self._shared_gallery_template(template_name), self._ctx(
            title, self._nav(), {
                **ctx,
            },
        ))

    def _list_videos(
        self,
        media_dir: str,
        file_url_name: str,
        thumbnail_url_name: str,
        large_thumbnail_url_name: str = '',
        active_category: str = '',
        category_filter: set[str] | None = None,
        category_ids_by_file: dict[str, set[int]] | None = None,
    ) -> list[dict[str, Any]]:
        if not media_dir or not os.path.isdir(media_dir):
            return []
        if category_ids_by_file is None:
            category_ids_by_file = {}
        result = []
        for fname in sorted(os.listdir(media_dir), reverse=True):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _MEDIA_EXTS:
                continue
            if active_category == 'none' and category_ids_by_file.get(fname):
                continue
            if active_category != 'none' and category_filter is not None and fname not in category_filter:
                continue
            path = os.path.join(media_dir, fname)
            if not os.path.isfile(path):
                continue
            sidecar = self._read_sidecar(media_dir, fname)
            result.append({
                'fname': fname,
                'type': 'video' if is_video(fname) else 'image',
                'url': self._u(file_url_name, fname),
                'thumb_url': self._u(thumbnail_url_name, fname),
                'large_thumb_url': (
                    self._u(large_thumbnail_url_name, fname)
                    if large_thumbnail_url_name else ''
                ),
                'size_mb': os.path.getsize(path) / (1024 * 1024),
                'mtime': datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds'),
                'meta': sidecar,
                'sidecar': sidecar,
                'category_ids': sorted(category_ids_by_file.get(fname, [])),
            })
        return result

    def _video_thumb_dir(self, media_dir: str) -> str:
        return os.path.join(media_dir, 'thumbnails') if media_dir else ''

    def _video_large_thumb_dir(self, media_dir: str) -> str:
        return os.path.join(media_dir, 'thumbnails_large') if media_dir else ''

    def _video_thumb_name(self, fname: str) -> str:
        return video_thumb_name(fname)

    def _ensure_video_thumbnail(self, media_dir: str, fname: str) -> bool:
        return ensure_media_thumbnail(
            media_dir, self._video_thumb_dir(media_dir), fname, size=160, quality='3',
        )

    def _source_dirs_json_url(self) -> str:
        return ''

    def _gallery_picker_items(self, output_subdir: str = '') -> list[dict[str, str]]:
        gallery_dir = self._gallery_dir()
        if not gallery_dir or not os.path.isdir(gallery_dir):
            return []
        result = []
        if output_subdir:
            project_dir = self._validated_project_dir(gallery_dir, output_subdir)
            if project_dir and os.path.isdir(project_dir):
                for fname in sorted(os.listdir(project_dir), reverse=True):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in IMAGE_EXTS or not os.path.isfile(os.path.join(project_dir, fname)):
                        continue
                    rp = f'{output_subdir}/{fname}'
                    try:
                        url = self._u('gallery_project_file', rp)
                        thumb_url = self._u('gallery_project_thumb', rp)
                        large_thumb_url = self._u('gallery_project_large_thumb', rp)
                    except Exception:
                        continue
                    result.append({
                        'fname': fname,
                        'url': url,
                        'thumb_url': thumb_url,
                        'large_thumb_url': large_thumb_url,
                    })
        for fname in sorted(os.listdir(gallery_dir), reverse=True):
            ext = os.path.splitext(fname)[1].lower()
            path = os.path.join(gallery_dir, fname)
            if ext in IMAGE_EXTS and os.path.isfile(path):
                url = self._u(self.route_video_file, fname)
                has_thumb = self._ensure_thumbnail(fname)
                has_large_thumb = self._ensure_large_thumbnail(fname)
                try:
                    thumb_url = self._u(self.route_video_thumbnail, fname) if has_thumb else url
                except Exception:
                    thumb_url = url
                try:
                    large_thumb_url = self._u(self.route_video_large_thumbnail, fname) if has_large_thumb else url
                except Exception:
                    large_thumb_url = url
                result.append({
                    'fname': fname,
                    'url': url,
                    'thumb_url': thumb_url,
                    'large_thumb_url': large_thumb_url,
                })
        return result

    def _read_sidecar(self, media_dir: str, fname: str) -> dict[str, Any]:
        return read_video_sidecar(media_dir, fname, _sanitize_video_metadata)

    @staticmethod
    def _summary_from_video_metadata(meta: dict[str, Any], fname: str = '') -> list[list[str]]:
        rows: list[list[str]] = []
        for label, key in (
            ('Provider', 'provider'),
            ('API', 'api'),
            ('Model', 'model_display'),
            ('Duration', 'duration'),
        ):
            value = meta.get(key)
            if value not in (None, ''):
                rows.append([label, str(value)])
        options = meta.get('options') if isinstance(meta.get('options'), dict) else {}
        for label, key in (
            ('Resolution', 'resolution'),
            ('Aspect ratio', 'aspect_ratio'),
            ('Size', 'size'),
            ('Audio', 'audio'),
            ('Generate audio', 'generate_audio'),
            ('Seed', 'seed'),
            ('Upscale factor', 'upscale_factor'),
        ):
            value = options.get(key)
            if value not in (None, ''):
                if isinstance(value, bool):
                    value = 'yes' if value else 'no'
                rows.append([label, str(value)])
        if fname:
            rows.append(['File', fname])
        prompt = meta.get('prompt')
        if prompt:
            rows.append(['Prompt', str(prompt)])
        generated_prompt = meta.get('generated_prompt')
        if generated_prompt:
            rows.append(['Generated prompt', str(generated_prompt)])
        return rows

    def _models_json(self, request):
        provider_param = request.GET.get('provider', '').strip() or None
        try:
            provider, api = normalize_provider_api(provider_param)
            model_options = self._model_options(provider, api)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except Exception as e:
            logger.exception('could not list video generation models')
            return JsonResponse({'error': f'could not list video generation models: {e}'},
                                status=502)
        requested_model = request.GET.get('selected_model') or None
        data = self._creator_data(
            provider, api, model_options, requested_model=requested_model,
        )
        return JsonResponse(data['presentation'])

    def _generate(self, request):
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return JsonResponse({'error': f'Invalid JSON: {e}'}, status=400)
        prompt = (data.get('prompt') or '').strip()
        if not prompt:
            return JsonResponse({'error': 'prompt is required'}, status=400)
        provider_value = data.get('provider')
        if not isinstance(provider_value, str) or not provider_value.strip():
            return JsonResponse({'error': 'provider is required'}, status=400)
        try:
            provider, api = normalize_provider_api(provider_value.strip(), data.get('api'))
            model = (data.get('model') or default_video_model(provider, api)).strip()
            backend_cls = make_videogen_backend(provider, api)
            presentation = model_presentation(model, provider, api)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        accept_raw = data.get('accept_data_handling_warnings')
        if accept_raw is not None and not isinstance(accept_raw, bool):
            return JsonResponse(
                {'error': 'accept_data_handling_warnings must be a boolean'}, status=400,
            )
        accept_data_handling_warnings = accept_raw is True
        gallery_dir = self._gallery_dir()
        if not gallery_dir:
            return JsonResponse({'error': 'video output directory is not configured'}, status=500)
        output_subdir_raw = str(data.get('output_subdir') or '').strip()
        try:
            output_subdir = self._safe_subdir(output_subdir_raw)
        except ValueError:
            return JsonResponse({'error': 'invalid output_subdir'}, status=400)
        if output_subdir:
            save_dir = self._validated_project_dir(gallery_dir, output_subdir)
            if not save_dir:
                return JsonResponse({'error': 'invalid output_subdir'}, status=400)
        else:
            save_dir = gallery_dir
        duration = data.get('duration') or default_duration(provider, api)
        if provider == 'openrouter':
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                return JsonResponse({'error': 'duration must be an integer for openrouter'},
                                    status=400)
        gen_kwargs: dict[str, Any] = {
            'model': model,
            'log_dir': self._log_dir(),
            'duration': duration,
            'debug': bool(data.get('debug')),
        }
        gen_kwargs['base_url'] = (data.get('url') or '').strip() or None
        if provider == 'venice':
            for key in ('resolution', 'aspect_ratio'):
                value = (data.get(key) or '').strip()
                if value:
                    gen_kwargs[key] = value
        generate_kwargs: dict[str, Any] = {}
        metadata_options: dict[str, Any] = {}
        if provider == 'venice':
            for key in ('negative_prompt', 'resolution', 'aspect_ratio'):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    clean_value = value.strip()
                    generate_kwargs[key] = clean_value
                    metadata_options[key] = clean_value
            for key in ('audio_url', 'video_url'):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    clean_value = value.strip()
                    generate_kwargs[key] = self._data_reference_for_api(request, clean_value)
                    metadata_options[key] = clean_value
            if presentation['allows_start_image'] or presentation['allows_end_image']:
                for key in ('image_url', 'end_image_url'):
                    if (
                        key == 'image_url' and not presentation['allows_start_image']
                        or key == 'end_image_url' and not presentation['allows_end_image']
                    ):
                        continue
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        clean_value = value.strip()
                        generate_kwargs[key] = self._data_reference_for_api(request, clean_value)
                        metadata_options[key] = clean_value
            if presentation['allows_reference_images']:
                ref_value = data.get('reference_image_urls')
                if isinstance(ref_value, list):
                    clean_values = [
                        v.strip()
                        for v in ref_value
                        if isinstance(v, str) and v.strip()
                    ]
                    if clean_values:
                        generate_kwargs['reference_image_urls'] = [
                            self._data_reference_for_api(request, v)
                            for v in clean_values
                        ]
                        metadata_options['reference_image_urls'] = clean_values
                if presentation['allows_scene_images']:
                    scene_value = data.get('scene_image_urls')
                    if isinstance(scene_value, list):
                        clean_values = [
                            v.strip()
                            for v in scene_value
                            if isinstance(v, str) and v.strip()
                        ]
                        if clean_values:
                            generate_kwargs['scene_image_urls'] = [
                                self._data_reference_for_api(request, v)
                                for v in clean_values
                            ]
                            metadata_options['scene_image_urls'] = clean_values
            if isinstance(data.get('audio'), bool):
                generate_kwargs['audio'] = data['audio']
                metadata_options['audio'] = data['audio']
            if data.get('upscale_factor') in (1, 2, 4):
                generate_kwargs['upscale_factor'] = data['upscale_factor']
                metadata_options['upscale_factor'] = data['upscale_factor']
            for key in ('reference_video_urls', 'reference_audio_urls'):
                value = data.get(key)
                if isinstance(value, list):
                    clean_values = [
                        v.strip()
                        for v in value
                        if isinstance(v, str) and v.strip()
                    ]
                    if clean_values:
                        generate_kwargs[key] = [
                            self._data_reference_for_api(request, v)
                            for v in clean_values
                        ]
                        metadata_options[key] = clean_values
            consents = data.get('consents')
            if consents is not None:
                if not isinstance(consents, dict):
                    return JsonResponse({'error': 'consents must be an object'}, status=400)
                generate_kwargs['consents'] = consents
                metadata_options['consents'] = consents

        if provider == 'segmind':
            if presentation['allows_start_image']:
                value = data.get('image_url')
                if isinstance(value, str) and value.strip():
                    clean_value = value.strip()
                    resolved_value = self._data_reference_for_api(request, clean_value)
                    if resolved_value.startswith('data:'):
                        required = (presentation.get('required_backend_transports') or {}).get('data_url')
                        if not required or required not in (presentation.get('available_backend_transports') or []):
                            return JsonResponse(
                                {'error': 'start image upload is not supported for this model'},
                                status=400,
                            )
                        warning = (presentation.get('transport_warnings') or {}).get(required)
                        if warning:
                            if not accept_data_handling_warnings:
                                return JsonResponse(
                                    {'error': 'requires accepting a data-handling warning'},
                                    status=400,
                                )
                            generate_kwargs['accept_data_handling_warnings'] = True
                    generate_kwargs['image_url'] = resolved_value
                    metadata_options['image_url'] = clean_value

        if provider == 'openrouter':
            for key in ('resolution', 'aspect_ratio', 'size', 'callback_url'):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    clean_value = value.strip()
                    generate_kwargs[key] = clean_value
                    metadata_options[key] = clean_value
            if isinstance(data.get('generate_audio'), bool):
                generate_kwargs['generate_audio'] = data['generate_audio']
                metadata_options['generate_audio'] = data['generate_audio']
            seed = data.get('seed')
            if seed not in (None, ''):
                try:
                    seed_int = int(seed)
                except (TypeError, ValueError):
                    return JsonResponse({'error': 'invalid seed'}, status=400)
                generate_kwargs['seed'] = seed_int
                metadata_options['seed'] = seed_int
            openrouter_provider = data.get('openrouter_provider')
            if openrouter_provider is not None:
                if not isinstance(openrouter_provider, dict):
                    return JsonResponse({'error': 'openrouter provider must be an object'},
                                        status=400)
                generate_kwargs['provider'] = openrouter_provider
                metadata_options['provider'] = openrouter_provider
            frame_images = data.get('frame_images')
            if isinstance(frame_images, list):
                clean_frame_images = []
                metadata_frame_images = []
                for item in frame_images:
                    if not isinstance(item, dict):
                        continue
                    image_url = item.get('image_url')
                    if not isinstance(image_url, dict):
                        continue
                    url = image_url.get('url')
                    frame_type = item.get('frame_type')
                    if (
                        not isinstance(url, str)
                        or not url.strip()
                        or frame_type not in ('first_frame', 'last_frame')
                    ):
                        continue
                    clean_url = url.strip()
                    clean_frame_images.append({
                        'type': 'image_url',
                        'image_url': {'url': self._data_reference_for_api(request, clean_url)},
                        'frame_type': frame_type,
                    })
                    metadata_frame_images.append({
                        'type': 'image_url',
                        'image_url': {'url': clean_url},
                        'frame_type': frame_type,
                    })
                if clean_frame_images:
                    generate_kwargs['frame_images'] = clean_frame_images
                    metadata_options['frame_images'] = metadata_frame_images
            input_references = data.get('input_references')
            if isinstance(input_references, list):
                clean_input_references = []
                metadata_input_references = []
                for item in input_references:
                    if not isinstance(item, dict):
                        continue
                    ref_type = item.get('type')
                    if ref_type not in ('image_url', 'audio_url', 'video_url'):
                        continue
                    ref_value = item.get(ref_type)
                    if not isinstance(ref_value, dict):
                        continue
                    url = ref_value.get('url')
                    if not isinstance(url, str) or not url.strip():
                        continue
                    clean_url = url.strip()
                    clean_input_references.append({
                        'type': ref_type,
                        ref_type: {'url': self._data_reference_for_api(request, clean_url)},
                    })
                    metadata_input_references.append({
                        'type': ref_type,
                        ref_type: {'url': clean_url},
                    })
                if clean_input_references:
                    generate_kwargs['input_references'] = clean_input_references
                    metadata_options['input_references'] = metadata_input_references

        try:
            gen = backend_cls(**gen_kwargs)
            result = gen.generate(prompt, **generate_kwargs)
            gen.shutdown()
        except Exception as e:
            logger.exception('video generation failed')
            return JsonResponse({'error': str(e)}, status=500)
        if result.get('error'):
            err = result['error']
            return JsonResponse({'error': err.get('message') or str(err), 'error_info': err},
                                status=502)
        videos = result.get('videos') or []
        if not videos:
            return JsonResponse({'error': 'generation returned no videos'}, status=502)
        saved = save_generated_videos(videos, save_dir)
        actual_model = result.get('model') or model
        meta = {
            'provider': provider,
            'api': api,
            'model_id': actual_model,
            'model': actual_model,
            'model_display': model_display(actual_model, provider, api),
            'duration': duration,
            'prompt': prompt,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'options': _sanitize_video_metadata(metadata_options),
            'files': saved,
        }
        if result.get('generated_prompt') is not None:
            meta['generated_prompt'] = result['generated_prompt']
        if result.get('prompt_enhancement') is not None:
            meta['prompt_enhancement'] = _sanitize_video_metadata(
                result['prompt_enhancement'],
            )
        if result.get('id'):
            meta['request_id'] = result['id']
        if result.get('job_id'):
            meta['job_id'] = result['job_id']
        write_video_sidecar(save_dir, saved[0], meta)
        if output_subdir:
            file_url = self._u('gallery_project_file', f'{output_subdir}/{saved[0]}')
            gallery_url_result = self._u(self.route_gallery) + '?' + urlencode({'subdir': output_subdir})
        else:
            file_url = self._u(self.route_video_file, saved[0])
            gallery_url_result = self._u(self.route_gallery)
        return JsonResponse({
            'ok': True,
            'files': saved,
            'file': saved[0],
            'url': file_url,
            'gallery_url': gallery_url_result,
            'meta': meta,
            'summary': self._summary_from_video_metadata(meta, saved[0]),
        })

    def _model_tag_states(self, provider: str, model_ids: list[str]) -> dict[str, dict[str, bool]]:
        try:
            return get_model_tag_states(provider, model_ids)
        except Exception:
            logger.exception('could not load video model tag states')
            return {}

    def _model_note(self, request):
        if request.method == 'GET':
            provider = request.GET.get('provider', '').strip()
            model_id = request.GET.get('model', '').strip()
            if not provider or not model_id:
                return JsonResponse({'error': 'provider and model are required'}, status=400)
            try:
                notes, tags = get_model_note(provider, model_id)
            except Exception as e:
                return JsonResponse({'error': str(e)}, status=500)
            return JsonResponse({'notes': notes, 'tags': tags})

        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return JsonResponse({'error': f'Invalid JSON: {e}'}, status=400)
        provider = (data.get('provider') or '').strip()
        model_id = (data.get('model') or '').strip()
        notes = data.get('notes', '')
        raw_tags = data.get('tags', {})
        submitted = (
            {k: bool(v) for k, v in raw_tags.items() if isinstance(k, str)}
            if isinstance(raw_tags, dict) else {}
        )
        if not provider or not model_id:
            return JsonResponse({'error': 'provider and model are required'}, status=400)
        try:
            set_model_note(provider, model_id, notes, submitted)
            _notes, tags = get_model_note(provider, model_id)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
        return JsonResponse({'ok': True, 'tags': tags})

    def _safe_name(self, value: str) -> str:
        return safe_name(value)

    def _data_reference_for_api(self, request, value: str) -> str:
        """Convert this app's private media URLs to data URLs for provider APIs."""
        text = value.strip()
        if not text or text.startswith('data:'):
            return text

        parts = urlsplit(text)
        path = parts.path if parts.scheme else text
        if not path:
            return text

        local = self._local_media_path_for_url(request, path, parts.netloc)
        if local is None:
            return text

        file_path, fname = local
        mime_type, _encoding = mimetypes.guess_type(fname)
        if not file_path or not os.path.isfile(file_path) or not mime_type:
            return text

        try:
            return file_as_data_url(file_path, fname)
        except ValueError:
            return text

    def _local_media_path_for_url(
        self,
        request,
        path: str,
        netloc: str,
    ) -> tuple[str, str] | None:
        request_host = request.get_host() if request is not None else ''
        if netloc and netloc != request_host:
            return None

        try:
            fname = self._safe_name(unquote(path.rstrip('/').rsplit('/', 1)[-1]))
        except Exception:
            return None

        route_dirs = (
            (self.route_video_file, self._gallery_dir()),
            (self.route_archive_file, self._archive_dir()),
        )
        for route_name, directory in route_dirs:
            if not directory:
                continue
            try:
                expected_path = self._u(route_name, fname)
            except Exception:
                continue
            if path == expected_path:
                return os.path.join(directory, fname), fname

        # Check for project gallery file URL
        try:
            from django.urls import resolve, Resolver404
            match = resolve(path)
            if match.url_name == 'gallery_project_file' and match.namespace == self._ns:
                subpath = match.kwargs.get('subpath', '')
                if '/' in subpath:
                    subdir_part, fname_part = subpath.rsplit('/', 1)
                    try:
                        clean_subdir = self._safe_subdir(subdir_part)
                        clean_fname = self._safe_name(fname_part)
                    except ValueError:
                        return None
                    project_dir = self._validated_project_dir(self._gallery_dir(), clean_subdir)
                    if project_dir:
                        file_path = os.path.join(project_dir, clean_fname)
                        if os.path.isfile(file_path):
                            return file_path, clean_fname
        except Exception:
            pass
        return None

    def _file_response(self, directory: str, filename: str, allowed_exts: set[str]):
        try:
            fname = self._safe_name(filename)
        except ValueError:
            raise Http404('invalid filename')
        ext = os.path.splitext(fname)[1].lower()
        path = os.path.join(directory, fname)
        if ext not in allowed_exts or not os.path.isfile(path):
            raise Http404('file not found')
        ctype, _ = mimetypes.guess_type(path)
        return FileResponse(open(path, 'rb'), content_type=ctype or 'application/octet-stream')

    def _video_file(self, request, filename):
        return self._file_response(self._gallery_dir(), filename, _MEDIA_EXTS)

    def _archive_video_file(self, request, filename):
        return self._file_response(self._archive_dir(), filename, _MEDIA_EXTS)

    def _video_thumbnail_response(self, directory: str, filename: str):
        try:
            fname = self._safe_name(filename)
        except ValueError:
            raise Http404('invalid filename')
        if not self._ensure_video_thumbnail(directory, fname):
            raise Http404('thumbnail not found')
        thumb_fname = self._video_thumb_name(fname) if is_video(fname) else fname
        path = os.path.join(self._video_thumb_dir(directory), thumb_fname)
        ctype, _ = mimetypes.guess_type(path)
        return FileResponse(open(path, 'rb'), content_type=ctype or 'image/jpeg')

    def _video_thumbnail(self, request, filename):
        return self._video_thumbnail_response(self._gallery_dir(), filename)

    def _archive_video_thumbnail(self, request, filename):
        return self._video_thumbnail_response(self._archive_dir(), filename)

    def _ensure_large_video_thumbnail(self, media_dir: str, fname: str) -> bool:
        return ensure_media_thumbnail(
            media_dir, self._video_large_thumb_dir(media_dir), fname, size=300, quality='2',
        )

    def _video_large_thumbnail_response(self, directory: str, filename: str):
        try:
            fname = self._safe_name(filename)
        except ValueError:
            raise Http404('invalid filename')
        if not self._ensure_large_video_thumbnail(directory, fname):
            raise Http404('thumbnail not found')
        thumb_fname = self._video_thumb_name(fname) if is_video(fname) else fname
        path = os.path.join(self._video_large_thumb_dir(directory), thumb_fname)
        ctype, _ = mimetypes.guess_type(path)
        return FileResponse(open(path, 'rb'), content_type=ctype or 'image/jpeg')

    def _video_large_thumbnail(self, request, filename):
        return self._video_large_thumbnail_response(self._gallery_dir(), filename)

    def _archive_video_large_thumbnail(self, request, filename):
        return self._video_large_thumbnail_response(self._archive_dir(), filename)

    def _image_thumbnail_response(self, media_dir: str, thumb_dir: str, filename: str, ensure_thumb):
        try:
            fname = self._safe_name(filename)
        except ValueError:
            raise Http404('invalid filename')
        ext = os.path.splitext(fname)[1].lower()
        if ext not in IMAGE_EXTS or not media_dir:
            raise Http404('file not found')
        path = os.path.join(thumb_dir, fname)
        if not os.path.isfile(path):
            if not ensure_thumb(fname):
                raise Http404('thumbnail not found')
        ctype, _ = mimetypes.guess_type(path)
        return FileResponse(open(path, 'rb'), content_type=ctype or 'application/octet-stream')

    def _delete_from_dir(
        self,
        request,
        directory: str,
        allowed_exts: set[str],
        thumb_dir: str = '',
        large_thumb_dir: str = '',
        cleanup_categories: bool = False,
        allow_subdir: bool = False,
    ):
        try:
            data = json.loads(request.body)
            fname = self._safe_name(data.get('filename') or '')
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            return JsonResponse({'error': str(e)}, status=400)
        if allow_subdir:
            raw_subdir = str(data.get('subdir') or '').strip()
            if raw_subdir:
                try:
                    subdir = self._safe_subdir(raw_subdir)
                except ValueError:
                    return JsonResponse({'error': 'invalid subdir'}, status=400)
                project_dir = self._validated_project_dir(directory, subdir)
                if not project_dir:
                    return JsonResponse({'error': 'invalid subdir'}, status=400)
                directory = project_dir
                thumb_dir = self._video_thumb_dir(project_dir)
                large_thumb_dir = self._video_large_thumb_dir(project_dir)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in allowed_exts:
            return JsonResponse({'error': 'file not found'}, status=404)
        if ext in VIDEO_EXTS:
            try:
                delete_video_asset(directory, fname, thumb_dir, large_thumb_dir)
            except FileNotFoundError:
                return JsonResponse({'error': 'file not found'}, status=404)
        else:
            try:
                if thumb_dir or large_thumb_dir:
                    delete_image_asset(directory, fname, thumb_dir, large_thumb_dir)
                else:
                    delete_media_file(directory, fname, allowed_exts)
            except FileNotFoundError:
                return JsonResponse({'error': 'file not found'}, status=404)
        if cleanup_categories:
            self._delete_category_file(fname)
        return JsonResponse({'ok': True})

    def _delete_video(self, request):
        gallery_dir = self._gallery_dir()
        return self._delete_from_dir(
            request,
            gallery_dir,
            _MEDIA_EXTS,
            self._video_thumb_dir(gallery_dir),
            self._video_large_thumb_dir(gallery_dir),
            cleanup_categories=True,
            allow_subdir=True,
        )

    def _delete_archive_video(self, request):
        archive_dir = self._archive_dir()
        return self._delete_from_dir(
            request, archive_dir, _MEDIA_EXTS,
            self._video_thumb_dir(archive_dir),
            self._video_large_thumb_dir(archive_dir),
        )

    def _write_upload_sidecars(self, media_dir: str, saved: list[str]) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        for fname in saved:
            payload = {
                'source': 'upload',
                'timestamp': timestamp,
                'uploaded_at': timestamp,
                'files': [fname],
            }
            try:
                write_video_sidecar(media_dir, fname, payload)
            except OSError as e:
                logger.warning('could not write upload metadata for %s: %s', fname, e)

    def _upload(self, request):
        gallery_dir = self._gallery_dir()
        if not gallery_dir:
            return JsonResponse({'error': 'media_dir not configured'}, status=500)
        subdir_raw = request.POST.get('subdir', '').strip()
        if subdir_raw:
            try:
                subdir = self._safe_subdir(subdir_raw)
            except ValueError:
                return JsonResponse({'error': 'invalid subdir'}, status=400)
            upload_dir = self._validated_project_dir(gallery_dir, subdir)
            if not upload_dir:
                return JsonResponse({'error': 'invalid subdir'}, status=400)
        else:
            upload_dir = gallery_dir
        files = request.FILES.getlist('images')
        if not files:
            return JsonResponse({'error': 'no files uploaded'}, status=400)
        saved, errors = save_uploaded_image_files(files, upload_dir)
        self._write_upload_sidecars(upload_dir, saved)
        return JsonResponse({'files': saved, 'errors': errors})

    def _move_file(
        self,
        request,
        src_dir: str,
        dst_dir: str,
        allow_from_subdir: bool = False,
        data: dict | None = None,
    ):
        if data is None:
            try:
                data = json.loads(request.body)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return JsonResponse({'error': str(e)}, status=400)
        try:
            fname = self._safe_name(data.get('filename') or '')
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        if allow_from_subdir:
            raw_subdir = str(data.get('subdir') or '').strip()
            if raw_subdir:
                try:
                    subdir = self._safe_subdir(raw_subdir)
                except ValueError:
                    return JsonResponse({'error': 'invalid subdir'}, status=400)
                project_dir = self._validated_project_dir(src_dir, subdir)
                if not project_dir:
                    return JsonResponse({'error': 'invalid subdir'}, status=400)
                src_dir = project_dir
        ext = os.path.splitext(fname)[1].lower()
        try:
            if ext in VIDEO_EXTS:
                move_video_asset(
                    src_dir, dst_dir, fname,
                    self._video_thumb_dir(src_dir), self._video_thumb_dir(dst_dir),
                    self._video_large_thumb_dir(src_dir), self._video_large_thumb_dir(dst_dir),
                )
            else:
                move_image_asset(
                    src_dir, dst_dir, fname,
                    self._video_thumb_dir(src_dir), self._video_thumb_dir(dst_dir),
                    self._video_large_thumb_dir(src_dir), self._video_large_thumb_dir(dst_dir),
                )
        except ValueError:
            return JsonResponse({'error': 'invalid filename'}, status=400)
        except FileNotFoundError:
            return JsonResponse({'error': 'file not found'}, status=404)
        except FileExistsError:
            return JsonResponse({'error': 'destination file already exists'}, status=409)
        except OSError as e:
            return JsonResponse({'error': str(e)}, status=500)
        if os.path.abspath(src_dir) == os.path.abspath(self._gallery_dir()):
            self._delete_category_file(fname)
        return JsonResponse({'ok': True})

    def _move_to_archive(self, request):
        try:
            data = json.loads(request.body)
            fname = self._safe_name(data.get('filename') or '')
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            return JsonResponse({'error': str(e)}, status=400)
        return self._move_file(
            request,
            self._gallery_dir(),
            self._archive_dir_for_filename(fname),
            allow_from_subdir=True,
            data=data,
        )

    def _move_to_gallery(self, request):
        return self._move_file(request, self._archive_dir(), self._gallery_dir())
