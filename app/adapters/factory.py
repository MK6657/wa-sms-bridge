"""WA adapter factory."""

from app.adapters.whatsapp_wa import WhatsappWaAdapter


def create_wa_adapter(adapter_name: str, base_url: str):
    """Create a WA upstream adapter by name."""
    normalized = (adapter_name or "whatsapp_wa").strip().lower()
    if normalized in {"whatsapp_wa", "whatsapp-wa", "wa-app", "wa_app"}:
        return WhatsappWaAdapter(base_url)
    raise ValueError(f"未知 WA adapter: {adapter_name}")
