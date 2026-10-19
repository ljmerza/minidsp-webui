"""HTML pages and HTMX partials. Mount with ``app.include_router(app.web.router)``."""

from app.web import home, task_adopt, task_diagnose, task_levels, task_louder, task_profiles, tasks
from app.web.routes import router
from app.web.templating import templates

# Home (/) and the /inputs and /outputs overviews (phase 9 design §4). They build on the routes module's page helpers,
# so they are added here rather than imported by it.
router.include_router(home.router)

# Guided tasks (design §14): the wizard shell and its wizards. Registering a wizard serves its /tasks/<id> page, which
# also turns every link to that task (Help me…, Home, the cards) from today's fallback page into the wizard.
tasks.register(task_diagnose.WIZARD)
tasks.register(task_louder.WIZARD)
tasks.register(task_levels.WIZARD)
tasks.register(task_profiles.WIZARD)
tasks.register(task_adopt.WIZARD)
router.include_router(tasks.router)
templates.env.globals["possessive"] = tasks.possessive  # "Sub's", "Shakers'"
templates.env.globals["task_ids"] = tuple(tasks.WIZARDS)  # the wizards the dialog opens (tasks/_dialog.html)

__all__ = ["router"]
