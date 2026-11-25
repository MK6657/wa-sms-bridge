"""国家数据工具 — ISO 码 ↔ 区号 ↔ 国家名 互查。

各 provider 的 get_countries() 通过此模块补充 iso/cc 字段。
不引入 pycountry 依赖，用 phonenumbers 库 + 小手工映射表。
"""

import re
from functools import lru_cache
from typing import Optional

import phonenumbers
from phonenumbers import geocoder

# ── 手工映射表（英文名小写 → ISO 3166-1 alpha-2） ─────────
# phonenumbers 没有 name→ISO 的索引，所以需要这个表。
# 覆盖了接码平台常见国家。可随时扩充。

NAME_TO_ISO: dict[str, str] = {
    "russia": "ru", "russian federation": "ru",
    "ukraine": "ua", "kazakhstan": "kz",
    "china": "cn", "philippines": "ph",
    "myanmar": "mm", "indonesia": "id",
    "malaysia": "my", "kenya": "ke",
    "tanzania": "tz", "vietnam": "vn",
    "viet nam": "vn",
    "israel": "il", "united kingdom": "gb",
    "england": "gb", "india": "in",
    "thailand": "th", "united states": "us",
    "usa": "us",
    "south africa": "za", "southafrica": "za", "afghanistan": "af",
    "albania": "al", "algeria": "dz",
    "angola": "ao", "argentina": "ar",
    "australia": "au", "austria": "at",
    "azerbaijan": "az", "bahamas": "bs",
    "bahrain": "bh", "bangladesh": "bd",
    "belarus": "by", "belgium": "be",
    "brazil": "br", "bulgaria": "bg",
    "cambodia": "kh", "cameroon": "cm",
    "canada": "ca", "chile": "cl",
    "colombia": "co", "croatia": "hr",
    "cyprus": "cy", "czech": "cz",
    "denmark": "dk", "egypt": "eg",
    "estonia": "ee", "ethiopia": "et",
    "finland": "fi", "france": "fr",
    "georgia": "ge", "germany": "de",
    "ghana": "gh", "greece": "gr",
    "hongkong": "hk", "hungary": "hu",
    "ireland": "ie", "japan": "jp",
    "jordan": "jo", "latvia": "lv",
    "lithuania": "lt", "mexico": "mx",
    "moldova": "md", "morocco": "ma",
    "nepal": "np", "netherlands": "nl",
    "newzealand": "nz", "new zealand": "nz",
    "nigeria": "ng", "norway": "no",
    "pakistan": "pk", "paraguay": "py",
    "peru": "pe", "poland": "pl",
    "portugal": "pt", "romania": "ro",
    "saudiarabia": "sa", "serbia": "rs",
    "singapore": "sg", "slovakia": "sk",
    "slovenia": "si", "spain": "es",
    "srilanka": "lk", "sri lanka": "lk",
    "sweden": "se", "switzerland": "ch",
    "taiwan": "tw", "turkey": "tr",
    "uganda": "ug", "uruguay": "uy",
    "uzbekistan": "uz", "venezuela": "ve",
    "yemen": "ye", "zimbabwe": "zw",
    "south korea": "kr", "korea": "kr",
    "saudi arabia": "sa",
    "timor-leste": "tl", "timor leste": "tl", "east timor": "tl",
    "united arab emirates": "ae",
    "south sudan": "ss",
    "united states virtual": "us",
    "macau": "mo", "macao": "mo",
    "lao people`s": "la", "lao peoples": "la", "laos": "la",
    "cote d`ivoire ivory coast": "ci", "ivory coast": "ci",
    "moldova, republic of": "md", "moldova republic of": "md",
    "brunei darussalam": "bn",
    "saint vincent": "vc", "saint vincent and the grenadines": "vc",
    "kosovo": "xk",
    "sint maarten": "sx",
    "syrian arab republic": "sy",
    "uk": "gb",
    "great britain": "gb",
    "uae": "ae",
    "dr congo": "cd",
    "democratic republic of the congo": "cd",
    "north macedonia": "mk",
    "czechia": "cz",
    "czech republic": "cz",
    "eswatini": "sz",
    "cabo verde": "cv",
    "cape verde": "cv",
    "salvador": "sv",
    "el salvador": "sv",
    "bosnia": "ba",
    "bosnia and herzegovina": "ba",
}

ZH_TO_ISO: dict[str, str] = {
    "中国": "cn",
    "俄罗斯": "ru",
    "俄罗斯联邦": "ru",
    "乌克兰": "ua",
    "哈萨克斯坦": "kz",
    "菲律宾": "ph",
    "缅甸": "mm",
    "印度尼西亚": "id",
    "印尼": "id",
    "马来西亚": "my",
    "肯尼亚": "ke",
    "坦桑尼亚": "tz",
    "越南": "vn",
    "以色列": "il",
    "英国": "gb",
    "印度": "in",
    "泰国": "th",
    "美国": "us",
    "南非": "za",
    "哥伦比亚": "co",
    "阿富汗": "af",
    "沙特阿拉伯": "sa",
    "东帝汶": "tl",
    "也门": "ye",
    "韩国": "kr",
    "荷兰": "nl",
    "美国（虚拟）": "us",
    "美国虚拟": "us",
    "澳门": "mo",
    "老挝人民": "la",
    "老挝": "la",
    "科特迪瓦": "ci",
    "摩尔多瓦共和国": "md",
    "文莱达鲁萨兰国": "bn",
    "圣文森特": "vc",
    "科索沃": "xk",
    "圣马丁岛": "sx",
    "叙利亚阿拉伯共和国": "sy",
    "阿联酋": "ae",
    "阿拉伯联合酋长国": "ae",
    "日本": "jp",
    "德国": "de",
    "法国": "fr",
    "加拿大": "ca",
    "澳大利亚": "au",
    "巴西": "br",
    "墨西哥": "mx",
    "新加坡": "sg",
    "土耳其": "tr",
    "香港": "hk",
    "中国香港": "hk",
    "台湾": "tw",
    "中国台湾": "tw",
}


def normalize_name(name: str) -> str:
    """Create a punctuation-insensitive Unicode lookup key."""
    return re.sub(r"[\W_]+", "", str(name or "").strip().casefold(), flags=re.UNICODE)


@lru_cache(maxsize=1)
def _country_name_aliases() -> dict[str, str]:
    aliases = {
        normalize_name(name): iso
        for mapping in (NAME_TO_ISO, ZH_TO_ISO)
        for name, iso in mapping.items()
        if normalize_name(name)
    }
    for region_code in phonenumbers.SUPPORTED_REGIONS:
        number = phonenumbers.example_number(region_code)
        if number is None:
            continue
        for language in ("en", "zh"):
            localized = geocoder.country_name_for_number(number, language)
            key = normalize_name(localized)
            if key:
                aliases.setdefault(key, region_code.lower())
    return aliases


def name_to_iso(name: str) -> str:
    """国家名 → ISO 3166-1 alpha-2 码。

    先用映射表精确查，找不到则尝试通过 phonenumbers 反向推断。
    """
    raw = name.strip()
    if len(raw) == 2 and raw.upper() in phonenumbers.SUPPORTED_REGIONS:
        return raw.lower()
    key = normalize_name(name)
    return _country_name_aliases().get(key, "")


@lru_cache(maxsize=512)
def iso_to_name(iso: str, language: str = "en") -> str:
    """Return a localized country name using bundled phonenumbers metadata."""
    region = str(iso or "").strip().upper()
    if region not in phonenumbers.SUPPORTED_REGIONS:
        return ""
    number = phonenumbers.example_number(region)
    if number is None:
        return ""
    return geocoder.country_name_for_number(number, language) or ""


def iso_to_cc(iso: str) -> str:
    """ISO 国家码 → 国家区号（如 "RU" → "7"）。"""
    cc = phonenumbers.country_code_for_region(iso.upper())
    return str(cc) if cc else ""


def cc_to_iso(cc: str) -> str:
    """国家区号 → ISO 国家码。一国多区号的返回第一个。"""
    code = int(cc) if cc.isdigit() else 0
    if not code:
        return ""
    regions = phonenumbers.COUNTRY_CODE_TO_REGION_CODE.get(code, [])
    return regions[0].lower() if regions else ""
