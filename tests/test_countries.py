import pytest

from app.countries import iso_to_name, name_to_iso, normalize_name


@pytest.mark.parametrize("value, expected", [
    ("Tunisia", "tn"),
    ("突尼斯", "tn"),
    ("Timor-Leste", "tl"),
    ("Timor Leste", "tl"),
    ("East Timor", "tl"),
    ("东帝汶", "tl"),
    ("Cote d`Ivoire Ivory Coast", "ci"),
    ("科特迪瓦", "ci"),
    ("Moldova, Republic of", "md"),
    ("摩尔多瓦共和国", "md"),
    ("Lao People`s", "la"),
    ("老挝人民", "la"),
])
def test_country_names_resolve_from_bundled_metadata(value, expected):
    assert name_to_iso(value) == expected


def test_country_name_normalization_keeps_unicode_and_ignores_punctuation():
    assert normalize_name("Timor-Leste") == normalize_name("Timor Leste")
    assert normalize_name("东 帝汶") == normalize_name("东帝汶")


def test_iso_to_chinese_name():
    assert iso_to_name("tn", "zh") == "突尼斯"
    assert iso_to_name("tl", "zh") == "东帝汶"
