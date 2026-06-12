from decimal import Decimal

from django import template
from django.utils.translation import gettext

register = template.Library()


@register.filter
def translate(value):
    """Tłumaczy dynamiczny ciąg (np. etykietę postępu zapisaną w bazie) na aktywny język."""
    if not value:
        return value
    return gettext(str(value))


def _decimal(value):
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


@register.filter
def seconds_time(value):
    total = _decimal(value)
    whole_seconds = int(total)
    fraction = total - Decimal(whole_seconds)
    minutes, seconds = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if fraction:
        seconds_text = f"{seconds + float(fraction):04.1f}"
    else:
        seconds_text = f"{seconds:02d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_text}"
    return f"{minutes:02d}:{seconds_text}"


@register.filter
def seconds_label(value):
    total = _decimal(value)
    whole_seconds = int(total)
    fraction = total - Decimal(whole_seconds)
    minutes, seconds = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or fraction or not parts:
        if fraction:
            seconds_value = Decimal(seconds) + fraction
            parts.append(f"{seconds_value.normalize()} s")
        else:
            parts.append(f"{seconds} s")
    return " ".join(parts)


@register.filter
def confidence_percent(value):
    return f"{round(float(value or 0) * 100)}%"


@register.filter
def confidence_level(value):
    """Zwraca poziom pewności do kolorowania odznaki: high / medium / low."""
    number = float(value or 0)
    if number >= 0.7:
        return "high"
    if number >= 0.4:
        return "medium"
    return "low"


@register.filter
def pct_width(value):
    try:
        number = max(0, min(100, float(value)))
    except (TypeError, ValueError):
        number = 0
    return f"{number}%"


@register.filter
def js_number(value):
    return f"{float(value or 0):.4f}"
