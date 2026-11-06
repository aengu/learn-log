"""
모든 템플릿에 공통으로 주입되는 컨텍스트.
"""
from .services import ExerciseService


def review_badge(request):
    """네비바 복습 배지용 — 복습 대기 중인 연습문제 개수"""
    return {'review_due_count': ExerciseService.get_due_exercises().count()}
