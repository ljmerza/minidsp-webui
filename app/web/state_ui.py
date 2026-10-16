"""The Phase 2 pages and partials as one router: ``app.include_router(app.web.state_ui.router)``.

- `app.web.params`: /routing, /inputs/{n}, /outputs/{n}, the edit endpoint and the per-page sync bar.
- `app.web.sync_ui`: Device page sync table, per-path push, adopt wizard and event log.
- `app.web.settings_ui`: /settings (channel names/colours/ceilings, runtime sync options, Home Assistant).
"""

from fastapi import APIRouter

from app.web import params, settings_ui, sync_ui

router = APIRouter(include_in_schema=False)
router.include_router(params.router)
router.include_router(sync_ui.router)
router.include_router(settings_ui.router)

__all__ = ["router"]
