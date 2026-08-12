from .wewe_base import WeweRssAdapter


class DxyAdapter(WeweRssAdapter):
    name = "丁香医生"
    mp_id = "MP_WXS_2058310401"


ADAPTER_KEY = "dxy"
ADAPTER_CLASS = DxyAdapter
