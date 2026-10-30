from django import template


register = template.Library()


@register.filter
def percent(value):
    """확률 0도 누락하지 않고 소수점 한 자리 백분율로 표시한다."""
    if value is None:
        return '—'
    return f'{value * 100:.1f}%'
