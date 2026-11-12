from django import template

register = template.Library()


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
