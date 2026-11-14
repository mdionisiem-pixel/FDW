"""Startup checks: stop early, with a plain message, when a required library is missing.

Without these, a missing library only shows up as a crash on the first page
that needs it, often halfway through a paid step.
"""
import importlib.util

from django.core.checks import Error, Warning, register

# (import name, package to install, what needs it)
REQUIRED = (
    ("anthropic", "anthropic", "every model call"),
    ("certifi", "certifi", "secure connections to OpenRouter"),
    ("PIL", "Pillow", "photos in the image test and every image in stage 4"),
)
OPTIONAL = (
    ("pillow_heif", "pillow-heif", "iPhone HEIC photos in the image test"),
)
FIX = "Run: pip install -r requirements.txt  (in the app's virtual environment), then start the app again."


@register()
def libraries_installed(app_configs, **kwargs):
    problems = []
    for module, package, needed_for in REQUIRED:
        if importlib.util.find_spec(module) is None:
            problems.append(Error(
                f"The {package} library is not installed. It is needed for {needed_for}.",
                hint=FIX, id="workflow.E001",
            ))
    for module, package, needed_for in OPTIONAL:
        if importlib.util.find_spec(module) is None:
            problems.append(Warning(
                f"The {package} library is not installed, so {needed_for} will be refused.",
                hint=FIX, id="workflow.W001",
            ))
    return problems
