from .wewe_base import WeweRssAdapter


class SimplePsychologyAdapter(WeweRssAdapter):
    name = "简单心理"
    mp_id = "MP_WXS_2393043732"


ADAPTER_KEY = "simple_psychology"
ADAPTER_CLASS = SimplePsychologyAdapter
