"""Locale-safe price parsing. A bug here silently corrupts every floor.

Regression that motivated these tests: the Indian thousands separator in
'Rs 3,656' was read as a decimal point, yielding Rs 3.656 -- understating
the value by roughly 1000x. Every break-even floor computed from it would
have been catastrophically low.
"""
import pytest
from steammkt.client import parse_price_to_paise as p

RUPEE = "₹"


@pytest.mark.parametrize("raw,expect", [
    (RUPEE + " 3,656",         365600),   # thousands separator, no decimals
    (RUPEE + " 3,748.04",      374804),
    (RUPEE + " 1,23,456.78", 12345678),   # Indian lakh grouping
    (RUPEE + " 12,00,000",  120000000),
    ("1.234,56",               123456),   # euro style
    ("$0.03",                       3),
    ("Rs 12",                    1200),
    ("0.42",                       42),
    ("1,234",                  123400),
    (RUPEE + " 45",              4500),
    (None,                       None),
    ("",                         None),
    ("n/a",                      None),
])
def test_parse(raw, expect):
    assert p(raw) == expect


def test_numeric_input():
    assert p(12.34) == 1234
    assert p(7) == 700
