"""可扩展的跨平台业务服务代码目录与映射。

公共 API/UI 支持标准服务名（whatsapp / telegram / google / openai 等），
也支持直接透传任意 SMSBower 原生服务码（如 ot / 自定义 code），便于后续任意项目零改动接入。
"""

from __future__ import annotations

from typing import Any

SERVICE_CATALOG: list[dict[str, Any]] = [
    {"id": "whatsapp", "label": "WhatsApp", "popular": True, "codes": {"smsbower": "wa", "herosms": "wa", "5sim": "whatsapp"}, "aliases": ["wa"]},
    {"id": "telegram", "label": "Telegram", "popular": True, "codes": {"smsbower": "tg", "herosms": "tg", "5sim": "telegram"}, "aliases": ["tg"]},
    {"id": "google", "label": "Google / Gmail", "popular": True, "codes": {"smsbower": "go", "herosms": "go", "5sim": "google"}, "aliases": ["gmail", "go", "谷歌"]},
    {"id": "apple", "label": "Apple / Apple ID", "popular": True, "codes": {"smsbower": "wx", "herosms": "wx", "5sim": "apple"}, "aliases": ["wx", "appleid", "apple_id", "icloud", "苹果"]},
    {"id": "wechat", "label": "WeChat / 微信", "popular": True, "codes": {"smsbower": "wb", "herosms": "wb", "5sim": "wechat"}, "aliases": ["wb", "微信"]},
    {"id": "openai", "label": "OpenAI / ChatGPT", "popular": True, "codes": {"smsbower": "dr", "herosms": "dr", "5sim": "openai"}, "aliases": ["chatgpt", "dr"]},
    {"id": "discord", "label": "Discord", "popular": True, "codes": {"smsbower": "ds", "herosms": "ds", "5sim": "discord"}, "aliases": ["ds"]},
    {"id": "facebook", "label": "Facebook", "popular": False, "codes": {"smsbower": "fb", "herosms": "fb", "5sim": "facebook"}, "aliases": ["fb"]},
    {"id": "instagram", "label": "Instagram", "popular": False, "codes": {"smsbower": "ig", "herosms": "ig", "5sim": "instagram"}, "aliases": ["ig"]},
    {"id": "tiktok", "label": "TikTok", "popular": False, "codes": {"smsbower": "lf", "herosms": "lf", "5sim": "tiktok"}, "aliases": ["lf"]},
    {"id": "twitter", "label": "X / Twitter", "popular": False, "codes": {"smsbower": "tw", "herosms": "tw", "5sim": "twitter"}, "aliases": ["x", "tw", "推特"]},
    {"id": "microsoft", "label": "Microsoft", "popular": False, "codes": {"smsbower": "mm", "herosms": "mm", "5sim": "microsoft"}, "aliases": ["mm", "微软"]},
    {"id": "amazon", "label": "Amazon", "popular": False, "codes": {"smsbower": "am", "herosms": "am", "5sim": "amazon"}, "aliases": ["am", "亚马逊"]},
    {"id": "line", "label": "LINE", "popular": False, "codes": {"smsbower": "me", "herosms": "me", "5sim": "line"}, "aliases": ["me"]},
    {"id": "uber", "label": "Uber", "popular": False, "codes": {"smsbower": "ub", "herosms": "ub", "5sim": "uber"}, "aliases": ["ub"]},
    {"id": "other", "label": "其他 / 通用 (ot)", "popular": False, "codes": {"smsbower": "ot", "herosms": "ot", "5sim": "other"}, "aliases": ["ot", "any"]},
]

SERVICE_ALIASES: dict[str, str] = {}
PROVIDER_SERVICE_CODES: dict[str, dict[str, str]] = {"smsbower": {}, "herosms": {}, "5sim": {}}

for _svc in SERVICE_CATALOG:
    _sid = _svc["id"]
    SERVICE_ALIASES[_sid] = _sid
    for _alias in _svc.get("aliases", ()):
        SERVICE_ALIASES[_alias.lower()] = _sid
    for _prov, _code in _svc["codes"].items():
        PROVIDER_SERVICE_CODES.setdefault(_prov, {})[_sid] = _code


def canonical_service(service: str) -> str:
    key = (service or "whatsapp").strip().lower()
    if key.startswith("custom:"):
        raw = key.split(":", 1)[1].strip()
        return raw or "whatsapp"
    return SERVICE_ALIASES.get(key, key or "whatsapp")


def provider_service_code(provider: str, service: str) -> str:
    raw = (service or "whatsapp").strip().lower()
    provider_key = (provider or "").strip().lower()
    if raw.startswith("custom:"):
        custom_code = raw.split(":", 1)[1].strip()
        if custom_code:
            return custom_code
        raw = "whatsapp"
    canonical = canonical_service(raw)
    return PROVIDER_SERVICE_CODES.get(provider_key, {}).get(canonical, canonical)


def list_services(provider: str = "smsbower") -> list[dict[str, Any]]:
    provider_key = (provider or "smsbower").strip().lower()
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "code": item["codes"].get(provider_key, item["id"]),
            "popular": bool(item.get("popular")),
        }
        for item in SERVICE_CATALOG
    ]
