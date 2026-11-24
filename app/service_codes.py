"""Provider-specific service code mapping.

The public UI/API can speak in stable product names like "whatsapp",
"google", and "telegram"; each SMS provider can keep its own native code here.
"""

SERVICE_ALIASES = {
    "whatsapp": "whatsapp",
    "wa": "whatsapp",
    "google": "google",
    "gmail": "google",
    "go": "google",
    "telegram": "telegram",
    "tg": "telegram",
}

PROVIDER_SERVICE_CODES = {
    "smsbower": {"whatsapp": "wa", "google": "go", "telegram": "tg"},
    "herosms": {"whatsapp": "wa", "google": "go", "telegram": "tg"},
    "5sim": {"whatsapp": "whatsapp", "google": "google", "telegram": "telegram"},
}


def canonical_service(service: str) -> str:
    key = (service or "whatsapp").strip().lower()
    return SERVICE_ALIASES.get(key, key or "whatsapp")


def provider_service_code(provider: str, service: str) -> str:
    provider_key = (provider or "").strip().lower()
    canonical = canonical_service(service)
    return PROVIDER_SERVICE_CODES.get(provider_key, {}).get(canonical, canonical)
