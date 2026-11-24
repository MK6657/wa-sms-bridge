from app.service_codes import canonical_service, provider_service_code


def test_canonical_service_aliases():
    assert canonical_service("wa") == "whatsapp"
    assert canonical_service("GO") == "google"
    assert canonical_service("tg") == "telegram"
    assert canonical_service("") == "whatsapp"


def test_provider_service_code_maps_native_codes():
    assert provider_service_code("smsbower", "whatsapp") == "wa"
    assert provider_service_code("smsbower", "telegram") == "tg"
    assert provider_service_code("herosms", "google") == "go"
    assert provider_service_code("5sim", "wa") == "whatsapp"
    assert provider_service_code("5sim", "telegram") == "telegram"
