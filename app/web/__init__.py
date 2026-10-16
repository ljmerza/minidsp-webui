"""HTML pages and HTMX partials. Mount with ``app.include_router(app.web.router)``."""

from app.web.routes import router

__all__ = ["router"]
