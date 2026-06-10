"""
Streak 자동 갱신 시그널.
기존 서비스 코드를 수정하지 않고, post_save 시그널로 streak을 업데이트한다.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import LearningLog, ExerciseAttempt, Streak


@receiver(post_save, sender=LearningLog)
def update_streak_on_log(sender, instance, created, **kwargs):
    """새 학습 로그 생성 시 streak 갱신"""
    if created:
        streak = Streak.load()
        streak.record_activity(instance.created_at.date())


@receiver(post_save, sender=ExerciseAttempt)
def update_streak_on_attempt(sender, instance, created, **kwargs):
    """복습 시도 시 streak 갱신 — 오답이어도 복습 행위 자체를 활동으로 인정"""
    if created:
        streak = Streak.load()
        streak.record_activity(instance.created_at.date())
