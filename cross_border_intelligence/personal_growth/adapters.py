"""Compatibility registry for the bundled pipeline validator.

Active collection is implemented in :mod:`personal_growth.collectors`.  The
validator reads this mapping to ensure every configured source is registered.
"""


class SourceAdapter:
    """Marker used by the template validator; not instantiated at runtime."""


ADAPTERS = {
    "wto": SourceAdapter,
    "federal_register": SourceAdapter,
    "bis_notices": SourceAdapter,
    "ofac": SourceAdapter,
    "eu_customs": SourceAdapter,
    "eu_trade": SourceAdapter,
    "eur_lex": SourceAdapter,
    "safety_gate": SourceAdapter,
    "mercosur": SourceAdapter,
    "aladi": SourceAdapter,
    "siscomex": SourceAdapter,
    "snice": SourceAdapter,
    "anam": SourceAdapter,
    "chile_customs": SourceAdapter,
    "dian": SourceAdapter,
    "shopify": SourceAdapter,
    "google_trends": SourceAdapter,
    "mercadolibre": SourceAdapter,
    "reddit": SourceAdapter,
    "youtube": SourceAdapter,
    "bis_data": SourceAdapter,
    "ecb": SourceAdapter,
    "ons_profile": SourceAdapter,
    "eurostat_profile": SourceAdapter,
    "insee_profile": SourceAdapter,
    "ibge_profile": SourceAdapter,
    "inegi_profile": SourceAdapter,
    "ine_chile_profile": SourceAdapter,
    "dane_profile": SourceAdapter,
    "uk_tariff_updates": SourceAdapter,
    "uk_tariff_api": SourceAdapter,
    "un_comtrade": SourceAdapter,
    "eurostat_data": SourceAdapter,
    "imo": SourceAdapter,
    "port_la": SourceAdapter,
    "amazon_announcements": SourceAdapter,
    "google_merchant": SourceAdapter,
    "ebay_browse": SourceAdapter,
}
