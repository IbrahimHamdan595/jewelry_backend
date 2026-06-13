from decimal import Decimal as D
from app.core.pricing import calculate_price
from app.models import Karat


def test_stone_value_adds_after_margin_and_making():
    base = calculate_price(rate_24k=D("60"), karat=Karat.K21, weight_grams=D("10"),
                           margin_percent=D("20"), making_charge=D("15"))
    withstone = calculate_price(rate_24k=D("60"), karat=Karat.K21, weight_grams=D("10"),
                                margin_percent=D("20"), making_charge=D("15"), stone_value=D("300"))
    # gold body: 60*0.875*10=525 metal; +20%=105 margin; +15 making = 645
    assert base["final_price"] == D("645.00")
    assert withstone["final_price"] == D("945.00")  # +300 stone
    assert withstone["margin_amount"] == base["margin_amount"]  # stones don't inflate gold margin
    assert withstone["stone_value"] == D("300.00")


def test_no_stone_value_is_identical_to_today():
    out = calculate_price(rate_24k=D("57.33"), karat=Karat.K18, weight_grams=D("4.8"),
                          margin_percent=D("18"), making_charge=D("25"), karat_markup=D("1.5"))
    assert out["stone_value"] == D("0.00")
    # K18 purity=0.750
    # purity_rate  = 57.33 * 0.750          = 42.9975
    # effective    = 42.9975 + 1.5          = 44.4975
    # metal_value  = 44.4975 * 4.8          = 213.588
    # margin_amt   = 213.588 * 0.18         = 38.44584
    # with_margin  = 213.588 + 38.44584     = 252.03384
    # final_price  = 252.03384 + 25         = 277.03384  → rounds to 277.03
    assert out["final_price"] == D("277.03")
