from base.lib.tools import nav as base_nav, nav_rel as base_nav_rel, specs_nav_item

from . import ImageHandlerViewSet


_image_handler_nav = []
_image_handler_nav_suffix = [specs_nav_item('imhandler')]
_image_handler_specs_url = _image_handler_nav_suffix[0]['url']

_vs = ImageHandlerViewSet(
    base_nav=base_nav,
    base_nav_rel=base_nav_rel,
    nav=_image_handler_nav,
    nav_suffix=_image_handler_nav_suffix,
    index_specs_url=_image_handler_specs_url,
)

index                  = _vs.index
browse                 = _vs.browse
similarity_browse      = _vs.similarity_browse
compare                = _vs.compare
cluster_detail         = _vs.cluster_detail
hide_image             = _vs.hide_image
hidden_images          = _vs.hidden_images
restore_image          = _vs.restore_image
similar                = _vs.similar
semantic_search        = _vs.semantic_search
thumb                  = _vs.thumb
image                  = _vs.image
embed_stream           = _vs.embed_stream
embed_cancel           = _vs.embed_cancel
