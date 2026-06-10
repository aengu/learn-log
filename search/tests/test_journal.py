"""일일 학습일지 (DailyJournal) 테스트"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from search.models import DailyJournal, Exercise, ExerciseAttempt
from search.services import JournalService
from .factories import LearningLogFactory

pytestmark = pytest.mark.django_db

POPUP_URL = reverse('search:journal_popup')


@pytest.fixture
def no_llm(monkeypatch):
    """요약 LLM 호출 차단 — 테스트에서는 고정 문자열 반환"""
    monkeypatch.setattr(
        JournalService, '_generate_summary',
        lambda self, date, stats: "테스트 요약입니다."
    )


def make_log_at(date):
    """특정 날짜에 생성된 학습 로그 (auto_now_add 우회)"""
    log = LearningLogFactory()
    dt = timezone.make_aware(timezone.datetime(date.year, date.month, date.day, 12))
    type(log).objects.filter(pk=log.pk).update(created_at=dt)
    log.refresh_from_db()
    return log


def make_attempt_at(date, is_correct):
    log = make_log_at(date)
    exercise = Exercise.objects.create(
        learning_log=log, exercise_type='generation_compare',
        content={'question': 'q', 'model_answer': 'a', 'key_points': ['p1']},
    )
    attempt = ExerciseAttempt.objects.create(
        exercise=exercise, user_answer={'text': 'x'}, is_correct=is_correct, score=1.0,
    )
    dt = timezone.make_aware(timezone.datetime(date.year, date.month, date.day, 13))
    ExerciseAttempt.objects.filter(pk=attempt.pk).update(created_at=dt)
    return attempt


class TestJournalService:
    def test_ensure_journal_collects_stats(self, no_llm):
        yesterday = timezone.localdate() - timedelta(days=1)
        make_log_at(yesterday)
        make_attempt_at(yesterday, is_correct=True)
        make_attempt_at(yesterday, is_correct=False)

        journal = JournalService().ensure_journal(yesterday)

        # make_attempt_at이 로그도 만들므로 질문 3개
        assert journal.question_count == 3
        assert journal.attempt_count == 2
        assert journal.pass_count == 1
        assert journal.fail_count == 1
        assert journal.summary == "테스트 요약입니다."

    def test_no_journal_for_inactive_day(self, no_llm):
        inactive = timezone.localdate() - timedelta(days=5)
        assert JournalService().ensure_journal(inactive) is None
        assert DailyJournal.objects.count() == 0

    def test_streak_day_counts_consecutive_days(self, no_llm):
        today = timezone.localdate()
        # 3일 연속 활동 후 하루 공백, 그 전에 하루 더 활동
        for delta in [1, 2, 3, 5]:
            make_log_at(today - timedelta(days=delta))

        journal = JournalService().ensure_journal(today - timedelta(days=1))
        assert journal.streak_day == 3  # 공백 이전 활동은 미포함

    def test_ensure_journal_is_idempotent(self, no_llm):
        yesterday = timezone.localdate() - timedelta(days=1)
        make_log_at(yesterday)
        service = JournalService()
        j1 = service.ensure_journal(yesterday)
        j2 = service.ensure_journal(yesterday)
        assert j1.pk == j2.pk
        assert DailyJournal.objects.count() == 1


class TestJournalPopupAPI:
    def test_popup_returns_modal_for_last_active_day(self, client, no_llm):
        yesterday = timezone.localdate() - timedelta(days=1)
        make_log_at(yesterday)

        resp = client.get(POPUP_URL)
        assert resp.status_code == 200
        assert '학습일지' in resp.content.decode()

    def test_popup_empty_when_no_activity(self, client, no_llm):
        resp = client.get(POPUP_URL)
        assert resp.content == b''

    def test_dismiss_hides_popup(self, client, no_llm):
        yesterday = timezone.localdate() - timedelta(days=1)
        make_log_at(yesterday)
        journal = JournalService().ensure_journal(yesterday)

        resp = client.post(reverse('search:journal_dismiss', args=[journal.pk]))
        assert resp.status_code == 200
        journal.refresh_from_db()
        assert journal.is_dismissed is True

        # 다시보지않기 이후에는 팝업이 비어 있어야 한다
        resp = client.get(POPUP_URL)
        assert resp.content == b''
