from .wewe_base import WeweRssAdapter


class LeviathanAdapter(WeweRssAdapter):
    name = "利维坦"
    mp_id = "MP_WXS_3093191505"


ADAPTER_KEY = "leviathan"
ADAPTER_CLASS = LeviathanAdapter
