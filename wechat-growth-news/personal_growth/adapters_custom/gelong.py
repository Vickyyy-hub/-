from .wewe_base import WeweRssAdapter


class GelongAdapter(WeweRssAdapter):
    name = "格隆"
    mp_id = "MP_WXS_3258282951"


ADAPTER_KEY = "gelong"
ADAPTER_CLASS = GelongAdapter
