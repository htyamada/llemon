from django.apps import AppConfig
from django.conf import settings


class DocumentViewConfig(AppConfig):
    name = 'documentview'
    label = 'documentview'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from . import appconfig, config
        appconfig.init_variant(getattr(settings, 'DOCUMENT_VIEWER_VARIANT', 'hty7'))
        config.validate_shape()
