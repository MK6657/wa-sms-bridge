"""控制台管理员会话认证接口（修复 P0-01 & P1-08）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.schemas import AdminLoginRequest
from app.core.auth_service import AuthService, enforce_browser_csrf, is_direct_loopback
from app.dependencies import get_auth_service
from app.errors import AuthError, ForbiddenError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/status")
async def auth_status(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    """查询当前客户端的管理员会话状态（不抛 401，供前端判断是否展示登录框或自动初始化本机会话）。"""
    password_required = auth_service.password_configured()
    api_auth_required = await auth_service.is_auth_required()
    direct_loopback = is_direct_loopback(request)

    is_admin = False
    authenticated = False
    project_id = ""
    try:
        caller = await auth_service.authenticate_request(request)
        is_admin = caller.is_admin
        authenticated = caller.authenticated
        project_id = caller.project_id
    except AuthError:
        pass

    return {
        "password_required": password_required,
        "api_auth_required": api_auth_required,
        "direct_loopback": direct_loopback,
        "authenticated": authenticated,
        "is_admin": is_admin,
        "project_id": project_id,
        "can_bootstrap_local_session": (not password_required) and direct_loopback,
    }


@router.post("/login")
async def admin_login(
    req: AdminLoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
):
    """使用 CONSOLE_ADMIN_PASSWORD 登录并签发 HttpOnly 管理员会话 Cookie。"""
    enforce_browser_csrf(request)
    if not auth_service.password_configured():
        if not is_direct_loopback(request):
            raise ForbiddenError("未设置 CONSOLE_ADMIN_PASSWORD，禁止远程登录管理台")
        expires_at = await auth_service.issue_admin_cookie(response, request=request)
        return {"ok": True, "is_admin": True, "expires_at": expires_at}

    auth_service.check_login_rate_limit(request)
    if not auth_service.verify_admin_password(req.password):
        auth_service.record_login_result(request, success=False)
        raise AuthError("管理员口令不正确", code="AUTH_INVALID_PASSWORD")

    auth_service.record_login_result(request, success=True)
    expires_at = await auth_service.issue_admin_cookie(response, request=request)
    return {"ok": True, "is_admin": True, "expires_at": expires_at}


@router.post("/session")
async def bootstrap_local_session(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
):
    """仅限本机直连回环且未设置管理员口令时签发 HttpOnly 会话 Cookie，保证严格模式下工作台 REST 与 SSE 均可安全认证。"""
    enforce_browser_csrf(request)
    if auth_service.password_configured():
        raise AuthError("已配置管理员口令，请使用 /api/v1/auth/login 登录", code="AUTH_PASSWORD_REQUIRED")
    if not is_direct_loopback(request):
        raise ForbiddenError("仅允许本机直连回环初始化免密会话（检测到非回环来源或反代头）")
    expires_at = await auth_service.issue_admin_cookie(response, request=request)
    return {"ok": True, "is_admin": True, "expires_at": expires_at}


@router.post("/logout")
async def admin_logout(
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
):
    enforce_browser_csrf(request)
    await auth_service.revoke_and_clear_admin_cookie(request, response)
    return {"ok": True}
