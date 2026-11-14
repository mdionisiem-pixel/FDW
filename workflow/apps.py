from django.apps import AppConfig


class WorkflowConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "workflow"

    def ready(self):
        from . import checks  # noqa: F401  registers the startup checks
