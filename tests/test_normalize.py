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


def _deal(typ, price, *conds, desc=""):
    return {"type": typ, "min": price, "max": price, "priceByBaseUnit": "",
            "conditions": [{"other": c} for c in conds], "description": desc}


def test_condition_classes():
    expected = {
        "Je": "noise", "je Packg.": "noise", "Kiste je": "noise", "Stück/Packung": "noise", "Auch online": "noise",
        "Ohne PENNY App": "no_app", "Preis ohne Treuepunkte": "no_app", "Normalpreis": "no_app",
        "Nur mit der App": "loyalty", "APP-PREIS": "loyalty", "MARKTKAUF-APP-PREIS": "loyalty",
        "Mit Lidl Plus": "loyalty", "Netto plus": "loyalty", "XTRA": "loyalty", "Nur mit der REWE App": "loyalty",
        "mit 55 Treuepunkten": "points", "Nur online": "online_only", "ab 4 Dosen": "multi_buy", "2 für": "multi_buy", "4 KAUFEN": "multi_buy",
        "An unserer Theke": "other",
    }
    for text, kind in expected.items():
        assert N.classify_condition(text) == kind, (text, N.classify_condition(text))


def test_netto_app_price_is_not_the_main_price():
    out = N.deals_summary([_deal("SPECIAL_PRICE", 0.99, "App-Preis"), _deal("SALES_PRICE", 1.19)],
                          "Netto mit dem Scottie")
    assert out["price"] == 1.19 and out["app_price"]["price"] == 0.99 and out["app_price"]["program"] == "Netto App"


def test_penny_marks_both_prices():
    out = N.deals_summary([_deal("SALES_PRICE", 1.99, "Ohne PENNY App"), _deal("SPECIAL_PRICE", 1.49, "Nur mit App")],
                          "Penny")
    assert out["price"] == 1.99 and "conditions" not in out
    assert out["app_price"] == {"price": 1.49, "program": "PENNY App", "conditions": ["Nur mit App"]}


def test_app_only_falls_back_to_normal_price():
    out = N.deals_summary([_deal("SALES_PRICE", 1.99, "Mit Lidl Plus"), _deal("REGULAR_PRICE", 3.49)], "Lidl")
    assert out["app_only"] and out["price"] == 3.49 and out["app_price"]["price"] == 1.99


def test_kaufland_je_is_no_condition():
    out = N.deals_summary([_deal("SALES_PRICE", 2.22, "Je")], "Kaufland")
    assert out == {"price": 2.22, "price_text": "2,22 €"}


def test_points_bonus_online_multibuy():
    assert N.deals_summary([_deal("SALES_PRICE", 2.99, "mit 55 Treuepunkten"), _deal("SALES_PRICE", 4.99)],
                           "Penny")["points_price"]["price"] == 2.99
    assert "Bonus" in N.deals_summary([_deal("SALES_PRICE", 1.29, desc="0,10 € Bonus")], "REWE")["app_bonus"]
    assert N.deals_summary([_deal("SALES_PRICE", 99.0, "Nur online")], "Lidl")["online_only"] is True
    multi = N.deals_summary([_deal("SALES_PRICE", 0.79, "Je", "ab 4 Dosen"), _deal("SALES_PRICE", 0.99)], "Netto")
    assert multi["price"] == 0.99 and multi["multi_buy"][0]["price"] == 0.79


def test_your_price_depends_on_apps():
    offer = {"retailer": "Penny", "price": 1.99, "app_price": {"price": 1.49, "program": "PENNY App"},
             "unit_price_value": {"per": "kg", "eur": 7.96}}
    assert N.personalize(dict(offer), [])["your_price"] == 1.99
    mine = N.personalize(dict(offer), ["Penny"])
    assert mine["your_price"] == 1.49 and mine["your_price_basis"] == "mit PENNY App"
    assert mine["your_unit_price"] == {"per": "kg", "eur": 5.96}
    assert N.has_app(["Lidl Plus"], "Lidl", "Lidl Plus") and N.has_app(["alle"], "REWE", "REWE App")
    assert N.has_app(["PAYBACK"], "Netto Marken-Discount", "PAYBACK")
    assert not N.has_app(["Lidl Plus"], "Penny", "PENNY App") and not N.has_app(["App"], "Penny", "PENNY App")


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
