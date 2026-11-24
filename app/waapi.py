"""Backward-compatible WA adapter imports.

New code should import from app.adapters. This module remains so existing tests,
scripts, and local integrations do not need to change immediately.
"""

from app.adapters.whatsapp_wa import (
    ProbeResult,
    RegisterResult,
    WaApiClient,
    WaApiError,
    WhatsappWaAdapter,
)

__all__ = [
    "ProbeResult",
    "RegisterResult",
    "WaApiClient",
    "WaApiError",
    "WhatsappWaAdapter",
]
