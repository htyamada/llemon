from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from .. import config
from .base import DocumentViewTestCase


class LimitTests(DocumentViewTestCase):
    def test_returns_builtin_default(self):
        self.assertEqual(
            config.limit('DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES'),
            config._LIMIT_DEFAULTS['DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES'],
        )

    def test_host_setting_overrides_default(self):
        with override_settings(DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES=7):
            self.assertEqual(config.limit('DOCUMENT_VIEWER_MAX_PDF_PREVIEW_PAGES'), 7)

    def test_host_setting_without_builtin_default_is_returned_not_raised(self):
        # Regression: the guard used to evaluate _LIMIT_DEFAULTS[name]
        # eagerly as getattr's default, so a name a host had configured but
        # that had no built-in default raised KeyError instead of returning
        # the configured value.
        with override_settings(DOCUMENT_VIEWER_FUTURE_LIMIT=42):
            self.assertEqual(config.limit('DOCUMENT_VIEWER_FUTURE_LIMIT'), 42)

    def test_unknown_and_unconfigured_name_raises(self):
        with self.assertRaises(KeyError):
            config.limit('DOCUMENT_VIEWER_NO_SUCH_LIMIT')


class EmptyRootFallbackTests(DocumentViewTestCase):
    """Regression: an explicitly empty DOCUMENT_VIEWER_ROOT/_EXPORTS_DIR
    setting used to be treated as "set" by root()/exports_dir() (only a
    bare `None` fell back to etc/documentview.conf), while validate_shape()
    already treated it as unset. That mismatch let `Path('')` -- the
    process's working directory -- silently stand in for a real root.
    """

    def test_empty_root_setting_falls_back_to_appconfig(self):
        with override_settings(DOCUMENT_VIEWER_ROOT=''):
            with mock.patch.object(config.appconfig, 'root', str(self.root)):
                self.assertEqual(config.root(), self.root.resolve())

    def test_empty_root_setting_with_empty_appconfig_raises(self):
        with override_settings(DOCUMENT_VIEWER_ROOT=''):
            with mock.patch.object(config.appconfig, 'root', ''):
                with self.assertRaises(ImproperlyConfigured):
                    config.root()

    def test_empty_exports_dir_setting_falls_back_to_appconfig(self):
        with override_settings(DOCUMENT_VIEWER_EXPORTS_DIR=''):
            with mock.patch.object(config.appconfig, 'exports_dir', str(self.active)):
                self.assertEqual(config.exports_dir(), self.active.resolve())

    def test_empty_exports_dir_setting_with_empty_appconfig_raises(self):
        with override_settings(DOCUMENT_VIEWER_EXPORTS_DIR=''):
            with mock.patch.object(config.appconfig, 'exports_dir', ''):
                with self.assertRaises(ImproperlyConfigured):
                    config.exports_dir()

    def test_validate_shape_treats_empty_root_setting_as_unconfigured_when_appconfig_also_empty(self):
        with override_settings(DOCUMENT_VIEWER_ROOT=''):
            with mock.patch.object(config.appconfig, 'root', ''):
                config.validate_shape()  # must not raise -- "app installed but not configured"


class CacheDirInsideExportsDirTests(DocumentViewTestCase):
    """Regression: the exports-directory lock file lives at
    `active_lock_path() == cache_dir() / 'active.lock'`. If
    DOCUMENT_VIEWER_CACHE_DIR is configured to equal, or nest inside,
    DOCUMENT_VIEWER_EXPORTS_DIR, the lock file would land inside the exports
    directory itself -- exactly what moving the lock out of exports_dir was
    meant to prevent. Only ROOT-vs-EXPORTS_DIR/CACHE_DIR containment used to
    be checked; CACHE_DIR-vs-EXPORTS_DIR was not.
    """

    def test_validate_shape_rejects_cache_dir_equal_to_exports_dir(self):
        with override_settings(DOCUMENT_VIEWER_CACHE_DIR=str(self.active)):
            with self.assertRaises(ImproperlyConfigured):
                config.validate_shape()

    def test_validate_shape_rejects_cache_dir_nested_inside_exports_dir(self):
        with override_settings(DOCUMENT_VIEWER_CACHE_DIR=str(self.active / 'nested')):
            with self.assertRaises(ImproperlyConfigured):
                config.validate_shape()

    def test_validate_live_rejects_cache_dir_nested_inside_exports_dir(self):
        with override_settings(DOCUMENT_VIEWER_CACHE_DIR=str(self.active / 'nested')):
            with self.assertRaises(ImproperlyConfigured):
                config.validate_live()
