"""统一业务异常与 HTTP 错误响应契约。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.log_redaction import redact_text

logger = logging.getLogger(__name__)


class AppError(Exception):
    """结构化业务异常，统一映射为标准 JSON 错误体。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "BAD_REQUEST",
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        safe_msg = redact_text(self.message)
        error: dict[str, Any] = {
            "code": self.code,
            "message": safe_msg,
            "retryable": self.retryable,
        }
        if self.details:
            error["details"] = self.details
        # 保留 detail 顶层字段兼容旧客户端/FastAPI 习惯
        return {"error": error, "detail": safe_msg}


class AuthError(AppError):
    def __init__(self, message: str = "未授权或 API Key 无效", *, code: str = "AUTH_INVALID_KEY"):
        super().__init__(message, status_code=401, code=code, retryable=False)


class ForbiddenError(AppError):
    def __init__(self, message: str = "当前密钥缺少所需权限", *, code: str = "AUTH_FORBIDDEN"):
        super().__init__(message, status_code=403, code=code, retryable=False)


class NotFoundError(AppError):
    def __init__(self, message: str, *, code: str = "NOT_FOUND", details: Optional[dict[str, Any]] = None):
        super().__init__(message, status_code=404, code=code, retryable=False, details=details)


class ConflictError(AppError):
    def __init__(self, message: str, *, code: str = "CONFLICT", details: Optional[dict[str, Any]] = None):
        super().__init__(message, status_code=409, code=code, retryable=False, details=details)


class UpstreamError(AppError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "UPSTREAM_ERROR",
        status_code: int = 502,
        retryable: bool = True,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, status_code=status_code, code=code, retryable=retryable, details=details)


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 实例挂载统一错误响应处理器。"""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
            issues.append({"field": loc or "body", "message": err.get("msg", "invalid")})
        summary = "; ".join(f"{item['field']}: {item['message']}" for item in issues) or "请求参数校验失败"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": summary,
                    "retryable": False,
                    "details": {"issues": issues},
                },
                "detail": summary,
            },
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
        raw_message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        message = redact_text(raw_message)
        code_map = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMITED",
            502: "UPSTREAM_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code_map.get(exc.status_code, f"HTTP_{exc.status_code}"),
                    "message": message,
                    "retryable": exc.status_code in {429, 502, 503, 504},
                },
                "detail": message if isinstance(exc.detail, str) else exc.detail,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server exception method=%s path=%s", request.method, request.url.path)
        fallback = "服务器内部错误，请稍后重试"
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": fallback,
                    "retryable": True,
                },
                "detail": fallback,
            },
        )
