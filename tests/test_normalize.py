"""Offline unit tests. Run: uv run python -m pytest tests/test_normalize.py  (or plain python)"""
from datetime import datetime

from kaufda_mcp import normalize as N


def test_unit_prices():
    cases = {
        "1 kg = 5,16": ("kg", 5.16), "(1 kg = 8.45/6.76)": ("kg", 6.76), "100 g = 1,20": ("kg", 12.0),
        "1 L = -,81": ("l", 0.81), "1 l = 1.-": ("l", 1.0), "(Liter = 0.99)": ("l", 0.99),
        "(11.98 / kg)": ("kg", 11.98), "1kg = 11,63–6,20": ("kg", 6.2), "1 WL = 0.20/0.24": ("WL", 0.2), "11.36 – 15.14 / kg": ("kg", 11.36),
        "": None, "Aktion": None,
    }
    for text, expected in cases.items():
        assert N.parse_unit_price(text) == expected, (text, N.parse_unit_price(text))


def test_validity_relative_to_today():
    v = N.Validity.from_strings("2026-10-14T22:00:00.000+0000", "2026-10-17T21:59:59.000+0000")  # Do 15.10. - Sa 17.10.
    wed = datetime(2026, 10, 14, 12, 0, tzinfo=N.BERLIN)
    thu = datetime(2026, 10, 15, 8, 0, tzinfo=N.BERLIN)
    sat = datetime(2026, 10, 17, 18, 0, tzinfo=N.BERLIN)
    sun = datetime(2026, 10, 18, 9, 0, tzinfo=N.BERLIN)
    assert v.describe(wed)["status"] == "demnächst, ab morgen Do 15.10."
    assert v.describe(wed)["valid_today"] is False and v.describe(wed)["starts_in_days"] == 1
    assert v.describe(thu)["status"] == "aktiv" and v.describe(thu)["valid_today"] is True
    assert v.describe(sat)["status"] == "aktiv, endet heute"
    assert v.describe(sun)["status"] == "abgelaufen"
    assert v.describe(wed)["validity"] == "Do 15.10. bis Sa 17.10."
    assert not v.matches("now", None, wed) and v.matches("upcoming", None, wed) and v.matches("now", None, thu)


def test_midnight_end_belongs_to_previous_day():
    v = N.Validity.from_strings("2026-10-11T22:00:00.000+0000", "2026-10-17T22:00:00.000+0000")
    assert v.describe(datetime(2026, 10, 13, tzinfo=N.BERLIN))["validity"] == "Mo 12.10. bis Sa 17.10."


def test_store_matching():
    assert N.store_matches("Aldi", "ALDI Nord") and N.store_matches("aldi süd", "ALDI SÜD")
    assert N.store_matches("Netto", "Netto Marken-Discount") and N.store_matches("netto", "Netto mit dem Scottie")
    assert N.store_matches("dm", "dm-drogerie markt") and not N.store_matches("dm", "Dehner Garten-Center")
    assert N.store_matches("DE-1013", "Lidl", "DE-1013") and not N.store_matches("Lidl", "Penny")


def test_deals_app_price_is_conditional():
    deals = [
        {"type": "SPECIAL_PRICE", "conditions": [{"other": "App-Preis"}], "min": 0.99, "max": 0.99, "priceByBaseUnit": " 1kg = 3.96"},
        {"type": "SALES_PRICE", "conditions": [], "min": 1.19, "max": 1.19, "priceByBaseUnit": ""},
        {"type": "OTHER", "conditions": [], "min": 0, "max": 0, "description": "2 für 1"},
    ]
    out = N.deals_summary(deals)
    assert out["price"] == 1.19 and out["conditional_prices"][0] == {"price": 0.99, "conditions": ["App-Preis"], "unit_price": "1kg = 3.96"}
    assert out["price_note"] == "2 für 1"


def test_quantity():
    assert N.parse_quantity("250 g") == ("kg", 0.25)
    assert N.parse_quantity("Versch. Sorten je 1,5 l") == ("l", 1.5)
    assert N.parse_quantity("einzeln verpackt 6x20-g-Packung") == ("kg", 0.12)
    assert N.parse_quantity("12 x 1 l") == ("l", 12.0)
    assert N.parse_quantity("Frische Milch", "3,7 % Fett") is None
    o = {"price": 0.99, "description": "1 l"}
    N.estimate_unit_price(o)
    assert o["unit_price_value"] == {"per": "l", "eur": 0.99, "estimated": True}


def test_relevance_tier():
    assert N.relevance_tier({"product": "Fettarme H-Milch", "category_match": True}, "Milch") == 0
    assert N.relevance_tier({"product": "Kondensmilch", "category_match": True}, "Milch") == 1
    assert N.relevance_tier({"product": "Kakao zum Backen", "category_match": True}, "Milch") == 3
    assert N.relevance_tier({"product": "Butterkekse", "category_match": False}, "Butter") == 4


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
