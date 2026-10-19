"""HTML pages and HTMX partials. Mount with ``app.include_router(app.web.router)``."""

from app.web import home
from app.web.routes import router

# Home (/) and the /inputs and /outputs overviews (phase 9 design §4). They build on the routes module's page helpers,
# so they are added here rather than imported by it.
router.include_router(home.router)

__all__ = ["router"]
