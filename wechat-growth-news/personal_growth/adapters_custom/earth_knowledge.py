from .wewe_base import WeweRssAdapter


class EarthKnowledgeAdapter(WeweRssAdapter):
    name = "地球知识局"
    mp_id = "MP_WXS_3927208278"


ADAPTER_KEY = "earth_knowledge"
ADAPTER_CLASS = EarthKnowledgeAdapter
