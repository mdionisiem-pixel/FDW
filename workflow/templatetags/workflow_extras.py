import hashlib
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

_fingerprints = {}


@register.simple_tag
def asset(path):
    """The static URL for ``path`` with a fingerprint of the file's content.

    Static files are served without cache headers, so after an update a browser
    may keep using its old copy of app.css or app.js for a while: the page then
    shows new markup with old styles and old behaviour. A fingerprint gives every
    version of a file its own URL, so a changed file is always fetched fresh.
    The fingerprint is recomputed only when the file's size or time changes."""
    url = static(path)
    found = finders.find(path)
    if not found:
        return url
    info = os.stat(found)
    key = (found, info.st_mtime_ns, info.st_size)
    digest = _fingerprints.get(key)
    if digest is None:
        with open(found, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        _fingerprints[key] = digest
    return f"{url}?v={digest}"


@register.filter
def label(value):
    """Turn a key such as 'body_length' into 'Body length'."""
    text = str(value).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


@register.filter
def tier(route):
    """The public tier name for a production route."""
    return "Atelier" if route == "atelier" else "Studio"


@register.filter
def route_name(route):
    return {
        "atelier": "Made by hand by Malina",
        "on_demand": "On-demand supplier",
        "specialist": "Specialist manufacturer",
    }.get(route, route)
