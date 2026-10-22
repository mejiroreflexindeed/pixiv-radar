"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import (
    auth,
    content,
    dashboard,
    discovery,
    events,
    feedback,
    health,
    scan_runs,
    targets,
)
from .auth.providers.supervised import GpptSupervisedAuthorizationProvider
from .auth.service import AuthCapability, AuthService
from .auth.worker import real_login_gate_enabled
from .core.config import get_settings
from .core.credentials import CredentialStore
from .core.keyring_store import LazyKeyringCredentialStore
from .core.logging import setup_logging
from .discovery.demo_fake import DemoFakeDiscoveryAdapter
from .discovery.runner import DiscoveryRunner, real_queries
from .content.service import ContentAnalysisService
from .pixiv.discovery_pixivpy import CredentialedPixivDiscoveryAdapter
from .pixiv.request_gate import RequestGate
from .pixiv.runtime import CredentialedPixivAdapter
from .storage.connection import connect
from .storage.content_repository import ContentRepository
from .storage.discovery_repository import DiscoveryRepository
from .storage.migrations import apply_migrations
from .storage.scan_repository import ScanRepository
from .scheduler.discovery import DiscoveryScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    conn = connect(settings.db_path)
    try:
        applied = apply_migrations(conn)
        if applied:
            logger.info("applied migrations: %s", applied)
    finally:
        conn.close()

    scheduler = getattr(app.state, "discovery_scheduler", None)
    if scheduler is not None:
        scheduler.start()

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        runner = getattr(app.state, "discovery_runner", None)
        if runner is not None:
            runner.shutdown()


def _auth_capability() -> AuthCapability:
    """离线能力探测：区分演示模式、缺依赖、缺 Chromium；不读凭据。"""
    from .auth.providers.supervised import (
        chromium_available,
        gppt_installed,
        real_login_gate_enabled,
    )

    if not real_login_gate_enabled():
        return AuthCapability(False, False, "demo_mode")
    if not gppt_installed():
        return AuthCapability(False, False, "missing_dependency")
    if not chromium_available():
        return AuthCapability(False, True, "missing_browser")
    return AuthCapability(True, True, None)


def _default_validator(gate: RequestGate):
    """用已保存 refresh token 做一次真实认证验证；失败按受控异常分类。

    门的占用、共享冷却检查与最小间隔由 AuthService 统一负责（先检查后读凭据）；
    本函数只调用上游、分类结果，并用捕获的会话世代提交 pause/resume，旧请求的
    迟到结果不会改写新会话。成功恢复暂停（保留有效 429 冷却），401/403 暂停门，
    429 保留冷却并暂停门。
    """
    from .pixiv.exceptions import (
        AuthFailed,
        PixivAdapterError,
        RateLimited,
    )
    from .pixiv.pixivpy_adapter import classify_pixiv_exception
    from .pixiv.runtime import _default_api_factory

    def _classify(exc: Exception) -> Exception:
        if isinstance(exc, PixivAdapterError):
            return exc
        return classify_pixiv_exception(exc)

    def validate(token: str) -> None:
        epoch = gate.session_epoch()
        try:
            _default_api_factory(token)
        except Exception as exc:
            failure = _classify(exc)
            if isinstance(failure, RateLimited):
                gate.pause(
                    "rate_limited",
                    cooldown_seconds=failure.cooldown_seconds,
                    epoch=epoch,
                )
            elif isinstance(failure, AuthFailed):
                gate.pause("auth_failed", epoch=epoch)
            raise failure
        gate.resume(epoch=epoch)

    return validate


def create_app(
    auth_service: AuthService | None = None,
    *,
    static_dir: Path | None = None,
    discovery_adapter=None,
    content_adapter=None,
    credential_store: CredentialStore | None = None,
    request_gate: RequestGate | None = None,
) -> FastAPI:
    settings = get_settings()
    resolved_static_dir: Path | None = None
    if static_dir is not None:
        resolved_static_dir = static_dir.resolve(strict=False)
        if not resolved_static_dir.is_dir():
            raise RuntimeError(
                f"静态前端目录不存在: {resolved_static_dir}；请先运行 npm run build"
            )
        if not (resolved_static_dir / "index.html").is_file():
            raise RuntimeError(
                f"静态前端缺少 index.html: {resolved_static_dir}；请先运行 npm run build"
            )
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 默认服务是惰性的：应用启动、迁移和非认证 API 均不访问 Credential Manager。
    # 登录提供者为受监督浏览器；依赖或浏览器 runtime 缺失时 available=False，
    # UI 诚实禁用，不影响离线功能。
    store = credential_store
    request_gate = request_gate or RequestGate()
    app.state.request_gate = request_gate
    if auth_service is None:
        store = store or LazyKeyringCredentialStore()
        app.state.auth_service = AuthService(
            store,
            GpptSupervisedAuthorizationProvider(),
            allow_real=real_login_gate_enabled,
            validator=_default_validator(request_gate),
            capability=_auth_capability,
            gate=request_gate,
        )
    else:
        app.state.auth_service = auth_service

    # 共享生命周期：出站门以"本代已连接会话"为守卫；断开登录立即取消本代业务。
    request_gate.set_session_guard(app.state.auth_service.outbound_session_valid)

    def _revoke_active_business() -> None:
        runner = getattr(app.state, "discovery_runner", None)
        if runner is not None:
            runner.cancel()

    app.state.auth_service.set_session_revoked_hook(_revoke_active_business)

    if discovery_adapter is not None:
        app.state.discovery_runner = DiscoveryRunner(
            discovery_adapter,
            ScanRepository(),
            DiscoveryRepository(),
            settings.db_path,
        )
    elif settings.discovery_mode == "real":
        # 每次 run 新建一个惰性认证适配器：页面加载/status 不读凭据、不联网；
        # 断开登录后也不会复用上一轮缓存的 API。
        store = store or LazyKeyringCredentialStore()
        app.state.discovery_runner = DiscoveryRunner(
            None,
            ScanRepository(),
            DiscoveryRepository(),
            settings.db_path,
            adapter_factory=lambda: CredentialedPixivDiscoveryAdapter(
                store,
                allow_real=real_login_gate_enabled,
                gate=request_gate,
            ),
            query_factory=real_queries,
            source_name="pixiv",
            gate=request_gate,
        )
    else:
        app.state.discovery_runner = DiscoveryRunner(
            DemoFakeDiscoveryAdapter(),
            ScanRepository(),
            DiscoveryRepository(),
            settings.db_path,
        )

    app.state.discovery_scheduler = None
    if (
        settings.discovery_mode == "real"
        and settings.discovery_interval_minutes > 0
    ):
        from .auth.service import AuthState

        def _scheduler_can_run() -> bool:
            if request_gate.state()["paused_reason"]:
                return False
            return app.state.auth_service.status().state is AuthState.CONNECTED

        app.state.discovery_scheduler = DiscoveryScheduler(
            app.state.discovery_runner,
            interval_seconds=settings.discovery_interval_minutes * 60.0,
            tag=settings.discovery_tag,
            can_run=_scheduler_can_run,
        )

    if content_adapter is not None:
        content_adapter_factory = lambda: content_adapter
    elif settings.discovery_mode == "real":
        store = store or LazyKeyringCredentialStore()
        content_adapter_factory = lambda: CredentialedPixivAdapter(
            store,
            allow_real=real_login_gate_enabled,
            gate=request_gate,
        )
    else:
        content_adapter_factory = None
    app.state.content_analysis_service = ContentAnalysisService(
        ContentRepository(),
        settings.db_path,
        adapter_factory=content_adapter_factory,
        gate=request_gate,
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(targets.router, prefix="/api")
    app.include_router(scan_runs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(feedback.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(discovery.router, prefix="/api")
    app.include_router(content.router, prefix="/api")

    if resolved_static_dir is not None:
        assets_dir = resolved_static_dir / "assets"
        if not assets_dir.is_dir():
            raise RuntimeError(
                f"静态前端缺少 assets 目录: {assets_dir}；请重新运行 npm run build"
            )
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir), html=False),
            name="frontend-assets",
        )

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def frontend_root() -> FileResponse:
            return FileResponse(resolved_static_dir / "index.html")

        return app

    @app.get("/")
    def root() -> dict:
        return {"app": settings.app_name, "docs": "/docs"}

    return app


app = create_app()
