"""러닝메이트 시작·대기 만료·늦은 결과 저장과 실제 PostgreSQL 경합을 검증한다."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import httpx
import pytest
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone
from groq import APITimeoutError

from practice.models import MateAttempt
from practice.services.learning_mate_service import LearningMateService
from practice.tests.test_learning_mate_service import make_exercise


def judgement(exercise):
    return {'steps': [{'probabilities': [.4, .3, .2, .1]} for _ in exercise.content['steps']]}


@pytest.mark.django_db
class TestMateTimeout:
    def test_sdk_options_are_applied(self):
        service = LearningMateService()
        try:
            assert service.client.timeout == 30
            assert service.client.max_retries == 0
        finally:
            service.client.close()

    @pytest.mark.parametrize(('status', 'age', 'legacy', 'expected'), [
        ('pending', 119, False, 'pending'), ('pending', 120, False, 'failed'),
        ('solving', 119, False, 'solving'), ('solving', 120, False, 'failed'),
        ('solving', 119, True, 'solving'), ('solving', 120, True, 'failed'),
        ('ready', 500, False, 'ready'), ('failed', 500, False, 'failed'),
    ])
    def test_expiry_boundary(self, status, age, legacy, expected):
        now = timezone.now()
        started = now - timezone.timedelta(seconds=age)
        mate = MateAttempt.objects.create(
            exercise=make_exercise(), status=status,
            started_at=started if status == 'solving' and not legacy else None,
        )
        MateAttempt.objects.filter(pk=mate.pk).update(created_at=started)
        with patch('practice.services.learning_mate_service.timezone.now', return_value=now):
            result = LearningMateService.refresh_status(mate)
        assert result.status == expected

    def test_solving_expiry_uses_start_not_creation(self):
        now = timezone.now()
        mate = MateAttempt.objects.create(exercise=make_exercise(), status='solving', started_at=now)
        MateAttempt.objects.filter(pk=mate.pk).update(created_at=now - timezone.timedelta(days=1))
        assert LearningMateService.refresh_status(mate).status == 'solving'

    def test_claim_sets_start_and_preserves_ability_cutoff(self, monkeypatch):
        mate = MateAttempt.objects.create(exercise=make_exercise())
        service = LearningMateService.__new__(LearningMateService)
        monkeypatch.setattr(service, '_request_choice_judgements', judgement)
        result = service.solve_attempt(mate)
        assert result.status == 'ready' and result.started_at is not None
        assert result.context_snapshot['ability_cutoff'] == mate.created_at.isoformat()

    def test_expired_pending_does_not_start_llm(self, monkeypatch):
        mate = MateAttempt.objects.create(exercise=make_exercise())
        MateAttempt.objects.filter(pk=mate.pk).update(created_at=timezone.now() - timezone.timedelta(minutes=3))
        service = LearningMateService.__new__(LearningMateService)
        with patch.object(service, '_request_choice_judgements') as request:
            assert service.solve_attempt(mate).status == 'failed'
            request.assert_not_called()
        assert mate.started_at is None

    @pytest.mark.parametrize('status', ['solving', 'ready', 'failed'])
    def test_duplicate_start_does_not_call_llm(self, status):
        started = timezone.now()
        mate = MateAttempt.objects.create(exercise=make_exercise(), status=status, started_at=started)
        service = LearningMateService.__new__(LearningMateService)
        with patch.object(service, '_request_choice_judgements') as request:
            result = service.solve_attempt(mate)
            request.assert_not_called()
        assert result.status == status and result.started_at == started

    def test_api_timeout_is_saved_as_failure(self):
        mate = MateAttempt.objects.create(exercise=make_exercise())
        service = LearningMateService.__new__(LearningMateService)
        error = APITimeoutError(request=httpx.Request('POST', 'https://example.invalid'))
        with patch.object(service, '_request_choice_judgements', side_effect=error):
            result = service.solve_attempt(mate)
        assert result.status == 'failed' and result.error_message
        assert result.answer == {} and result.answered_at is not None

    @pytest.mark.parametrize('endpoint', ['practice:mate_attempt_solve', 'practice:mate_attempt_result'])
    @pytest.mark.parametrize('status', ['pending', 'solving'])
    def test_status_get_expires_before_and_after_submission_and_stops_polling(self, client, endpoint, status):
        old = timezone.now() - timezone.timedelta(minutes=3)
        mate = MateAttempt.objects.create(exercise=make_exercise(), status=status, started_at=old)
        MateAttempt.objects.filter(pk=mate.pk).update(created_at=old)
        with patch.object(LearningMateService, '_request_choice_judgements') as request:
            response = client.get(reverse(endpoint, args=[mate.pk]))
            request.assert_not_called()
        mate.refresh_from_db()
        assert mate.status == 'failed'
        assert 'hx-get=' not in response.content.decode()
        assert '못했어요' in response.content.decode()

    def test_pre_submission_get_polls_without_exposing_ready_answers(self, client):
        mate = MateAttempt.objects.create(exercise=make_exercise())
        response = client.get(reverse('practice:mate_attempt_solve', args=[mate.pk]))
        assert 'hx-get=' in response.content.decode()
        assert 'load delay:1s' in response.content.decode()
        MateAttempt.objects.filter(pk=mate.pk).update(status='ready', answer={'secret': 'hidden-answer'})
        response = client.get(reverse('practice:mate_attempt_solve', args=[mate.pk]))
        assert 'hx-get=' not in response.content.decode()
        assert 'hidden-answer' not in response.content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('fails', [False, True])
@pytest.mark.parametrize('expire_first', [False, True])
def test_expiry_and_completion_race(monkeypatch, fails, expire_first):
    if connection.vendor != 'postgresql':
        pytest.skip('실제 PostgreSQL 별도 연결 검증')
    mate = MateAttempt.objects.create(exercise=make_exercise())
    entered, release = Event(), Event()
    service = LearningMateService.__new__(LearningMateService)

    def request(exercise):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('테스트 해제 시간 초과')
        if fails:
            raise RuntimeError('LLM 요청 실패')
        return judgement(exercise)

    monkeypatch.setattr(service, '_request_choice_judgements', request)

    def solve():
        close_old_connections()
        try:
            return service.solve_attempt(MateAttempt.objects.select_related('exercise').get(pk=mate.pk)).status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(solve)
        try:
            assert entered.wait(5)
            mate.refresh_from_db()
            future_now = mate.started_at + timezone.timedelta(seconds=120)
            # 첫 요청이 처리 중이면 중복 시작 요청은 LLM을 다시 호출하지 않는다.
            duplicate = LearningMateService.__new__(LearningMateService)
            with patch.object(duplicate, '_request_choice_judgements') as duplicate_request:
                assert duplicate.solve_attempt(mate).status == 'solving'
                duplicate_request.assert_not_called()
            if expire_first:
                with patch('practice.services.learning_mate_service.timezone.now', return_value=future_now):
                    assert LearningMateService.refresh_status(mate).status == 'failed'
        finally:
            release.set()
        outcome = future.result(timeout=10)
    with patch('practice.services.learning_mate_service.timezone.now', return_value=future_now):
        LearningMateService.refresh_status(mate)
    if expire_first:
        assert outcome == mate.status == 'failed'
        assert '시간 초과' in mate.error_message and mate.answer == {}
    else:
        assert outcome == mate.status == ('failed' if fails else 'ready')
        if fails:
            assert mate.error_message == 'LLM 요청 실패'


@pytest.mark.django_db(transaction=True)
def test_migration_grants_grace_only_to_existing_solving():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [('practice', '0004_bkt_failure_recovery')]
    try:
        executor.migrate(old_target)
        apps = executor.loader.project_state(old_target).apps
        log = apps.get_model('search', 'LearningLog').objects.create(query='이전 데이터', ai_response='답변')
        exercise = apps.get_model('practice', 'Exercise').objects.create(
            learning_log=log, exercise_type='path_trace', content={},
        )
        model = apps.get_model('practice', 'MateAttempt')
        ids = {status: model.objects.create(exercise=exercise, status=status).pk
               for status in ['pending', 'solving', 'ready', 'failed']}
        before = timezone.now()
        MigrationExecutor(connection).migrate(latest)
        assert MateAttempt.objects.get(pk=ids['solving']).started_at >= before
        assert all(MateAttempt.objects.get(pk=pk).started_at is None
                   for status, pk in ids.items() if status != 'solving')
        # 새 pending에는 생성 시각 기본값이 자동으로 들어가지 않는다.
        assert MateAttempt.objects.create(exercise_id=exercise.pk).started_at is None
    finally:
        MigrationExecutor(connection).migrate(latest)
