"""미제출 대결 재사용·만료 정리와 PostgreSQL 행 잠금 경합."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.urls import reverse
from django.utils import timezone

from practice.models import ExerciseAttempt, MateAttempt
from practice.services import LearningMateService
from practice.tests.test_exercise_api import make_exercise


@pytest.mark.django_db
class TestMateReuse:
    @pytest.mark.parametrize('status', ['pending', 'solving', 'ready'])
    def test_reuses_recent_unused_and_preserves_cutoff(self, client, status):
        exercise = make_exercise('path_trace')
        mate = MateAttempt.objects.create(exercise=exercise, status=status)
        now = timezone.now()
        created = now - timezone.timedelta(seconds=3599)
        MateAttempt.objects.filter(pk=mate.pk).update(created_at=created)
        with patch('practice.services.learning_mate_service.timezone.now', return_value=now):
            response = client.get(reverse('practice:exercise_detail', args=[exercise.pk]))
        assert response.context['mate_attempt'].pk == mate.pk
        assert response.context['mate_attempt'].created_at == created
        assert MateAttempt.objects.count() == 1
        start_post = f'hx-post="{reverse("practice:mate_attempt_solve", args=[mate.pk])}"'
        assert (start_post in response.content.decode()) == (status == 'pending')

    @pytest.mark.parametrize('status', ['pending', 'ready'])
    def test_one_hour_boundary_deletes_unused_and_creates_new(self, status):
        exercise = make_exercise('path_trace')
        old = MateAttempt.objects.create(exercise=exercise, status=status)
        now = timezone.now()
        MateAttempt.objects.filter(pk=old.pk).update(created_at=now - timezone.timedelta(hours=1))
        with patch('practice.services.learning_mate_service.timezone.now', return_value=now):
            new = LearningMateService.get_or_create_attempt(exercise)
        assert new.pk != old.pk and new.status == 'pending'
        assert new.created_at == now
        assert not MateAttempt.objects.filter(pk=old.pk).exists()

    def test_cleanup_is_scoped_and_preserves_history_and_solving(self):
        exercise = make_exercise('path_trace')
        other = make_exercise('path_trace')
        attempt = ExerciseAttempt.objects.create(exercise=exercise, user_answer={})
        history = MateAttempt.objects.create(exercise=exercise, status='ready', user_attempt=attempt)
        solving = MateAttempt.objects.create(exercise=exercise, status='solving')
        failed = MateAttempt.objects.create(exercise=exercise, status='failed')
        unrelated = MateAttempt.objects.create(exercise=other, status='pending')
        protected = [history.pk, solving.pk, failed.pk, unrelated.pk]
        MateAttempt.objects.filter(pk__in=protected).update(
            created_at=timezone.now() - timezone.timedelta(hours=2),
        )
        fresh = MateAttempt.objects.create(exercise=exercise, status='ready')
        older = MateAttempt.objects.create(exercise=exercise, status='pending')
        MateAttempt.objects.filter(pk=older.pk).update(created_at=timezone.now() - timezone.timedelta(hours=2))
        assert LearningMateService.get_or_create_attempt(exercise).pk == fresh.pk
        assert MateAttempt.objects.filter(pk__in=protected).count() == 4
        assert not MateAttempt.objects.filter(pk=older.pk).exists()

    def test_uses_latest_reusable_duel(self):
        exercise = make_exercise('path_trace')
        MateAttempt.objects.create(exercise=exercise)
        latest = MateAttempt.objects.create(exercise=exercise, status='ready')
        assert LearningMateService.get_or_create_attempt(exercise).pk == latest.pk

    def test_submission_from_expired_page_still_saves_user_answer(self, client):
        exercise = make_exercise('path_trace')
        old = MateAttempt.objects.create(exercise=exercise)
        MateAttempt.objects.filter(pk=old.pk).update(created_at=timezone.now() - timezone.timedelta(hours=2))
        new = LearningMateService.get_or_create_attempt(exercise)
        response = client.post(reverse('practice:exercise_attempt', args=[exercise.pk]), {
            'selected_indices': '[0, 1, 2]', 'mate_attempt_id': old.pk,
        })
        assert response.status_code == 200 and '채점 오류' not in response.content.decode()
        assert ExerciseAttempt.objects.get().user_answer == {'selected_indices': [0, 1, 2]}
        new.refresh_from_db()
        assert new.user_attempt_id is None

    @pytest.mark.parametrize(('endpoint', 'method'), [
        ('practice:mate_attempt_solve', 'get'), ('practice:mate_attempt_solve', 'post'),
        ('practice:mate_attempt_result', 'get'),
    ])
    def test_deleted_between_fetch_and_status_refresh_is_handled(self, client, monkeypatch, endpoint, method):
        mate = MateAttempt.objects.create(exercise=make_exercise('path_trace'))

        def delete_then_refresh(record):
            MateAttempt.objects.filter(pk=record.pk).delete()
            record.refresh_from_db()

        monkeypatch.setattr(LearningMateService, 'refresh_status', staticmethod(delete_then_refresh))
        response = getattr(client, method)(reverse(endpoint, args=[mate.pk]))
        assert response.status_code == 200
        assert 'hx-trigger' not in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_concurrent_page_visits_create_only_one_duel():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL 잠금 검증')
    exercise = make_exercise('path_trace')
    barrier = Barrier(2)

    def visit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return LearningMateService.get_or_create_attempt(exercise).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(visit) for _ in range(2)]
        ids = [future.result(timeout=10) for future in futures]
    assert ids[0] == ids[1]
    assert MateAttempt.objects.filter(exercise=exercise).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('action', ['submit', 'claim'])
def test_cleanup_skips_locked_duel_without_deadlocking(action):
    """제출·선점 중인 행을 기다리지 않아 문제와 대결의 역순 잠금 교착을 피한다."""
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL 잠금 검증')
    exercise = make_exercise('path_trace')
    mate = MateAttempt.objects.create(exercise=exercise)
    MateAttempt.objects.filter(pk=mate.pk).update(created_at=timezone.now() - timezone.timedelta(hours=2))

    def visit():
        close_old_connections()
        try:
            return LearningMateService.get_or_create_attempt(exercise).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            locked = MateAttempt.objects.select_for_update().get(pk=mate.pk)
            future = pool.submit(visit)
            # 잠금 해제 전에도 페이지 요청이 끝나야 한다.
            new_id = future.result(timeout=5)
            assert new_id != mate.pk
            if action == 'submit':
                attempt = ExerciseAttempt.objects.create(exercise=exercise, user_answer={})
                locked.user_attempt = attempt
                locked.save(update_fields=['user_attempt'])
            else:
                locked.status = 'solving'
                locked.started_at = timezone.now()
                locked.save(update_fields=['status', 'started_at'])
    mate.refresh_from_db()
    if action == 'submit':
        assert mate.user_attempt_id is not None
    else:
        assert mate.status == 'solving'
