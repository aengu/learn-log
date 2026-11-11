"""예상된 실패만 격리하고, 복구 전후 상태와 실제 PostgreSQL 잠금을 검증한다."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.urls import reverse
from django.utils import timezone

from practice.models import BKTFailureStep, BKTLock, BKTObservation, BKTUpdateFailure, ConceptMastery, Exercise, ExerciseAttempt
from practice.services.bkt_lock import BKTLockedAdminMixin, confirm_exclusion, pending_concept_ids
from practice.services.bkt_report import concept_report
from practice.services.bkt_service import BKTConsistencyError, BKTService
from practice.services.exercise_service import ExerciseService
from practice.services.learning_mate_service import LearningMateService
from practice.tests.test_bkt import make_attempt, make_concept, make_step
from practice.tests.bkt_helpers import capture_with_lock, record_with_lock
from search.models import Streak


def submit(exercise, selected=None):
    return ExerciseService.__new__(ExerciseService).save_attempt(
        exercise, {'selected_indices': selected or [0] * len(exercise.content['steps'])},
        {'score': 1, 'is_correct': True, 'ai_feedback': '정답'},
    )


def failed_pair():
    a, b = make_concept('test.a'), make_concept('test.b')
    initial = make_attempt([make_step(b.pk)], [0])
    record_with_lock(initial)
    ConceptMastery.objects.filter(concept=b).update(model_version='old')
    source = make_attempt([make_step(a.pk), make_step(b.pk)], [0, 0])
    Streak.objects.update(current_streak=2, longest_streak=2,
                          last_active_date=timezone.now().date() - timezone.timedelta(days=1))
    attempt = submit(source.exercise)
    return a, b, attempt


@pytest.mark.django_db
class TestBKTRecovery:
    def test_late_step_failure_preserves_attempt_review_and_streak_but_rolls_back_all_bkt(self):
        a, b, attempt = failed_pair()
        assert attempt.pk and attempt.score == 1
        assert attempt.exercise.review_interval == 3
        assert Streak.objects.get().current_streak == 3
        assert Streak.objects.get().last_active_date == attempt.created_at.date()
        assert not attempt.bkt_observations.exists()
        assert not ConceptMastery.objects.filter(concept=a).exists()
        assert BKTObservation.objects.count() == 1
        failure = attempt.bkt_failure
        assert (failure.error_code, failure.failed_step_index) == ('settings_changed', 1)
        assert set(failure.steps.values_list('concept_id', flat=True)) == {a.pk, b.pk}
        assert pending_concept_ids() == {a.pk, b.pk}
        assert failure.steps.get(step_index=0).step_snapshot['selected_index'] == 0

    def test_next_a_only_submission_is_deferred_before_consistency_checks(self):
        a, b, attempt = failed_pair()
        source = make_attempt([make_step(a.pk)], [0])
        # _observation도 상태 생성도 실행하면 안 된다.
        with patch.object(BKTService, '_observation', side_effect=AssertionError('계산 금지')):
            newer = submit(source.exercise)
        assert newer.bkt_failure.error_code == 'pending_rebuild'
        assert not newer.bkt_observations.exists()
        assert not ConceptMastery.objects.filter(concept=a).exists()
        assert ExerciseAttempt.objects.filter(pk=newer.pk).exists()

    def test_rebuild_recovers_deferred_steps_and_preserves_originals(self):
        a, b, attempt = failed_pair()
        source = make_attempt([make_step(a.pk)], [0])
        newer = submit(source.exercise)
        originals = list(ExerciseAttempt.objects.order_by('pk').values())
        preview = BKTService.rebuild()
        assert preview['can_apply'] and preview['unresolved_steps'] == 0
        assert pending_concept_ids() == {a.pk, b.pk}  # 미리보기는 쓰지 않음
        BKTService.rebuild(apply=True)
        assert not pending_concept_ids()
        assert set(BKTFailureStep.objects.values_list('status', flat=True)) == {'applied'}
        assert BKTUpdateFailure.objects.filter(resolved_at__isnull=True).count() == 0
        assert newer.bkt_observations.count() == 1
        assert list(ExerciseAttempt.objects.order_by('pk').values()) == originals

    def test_changed_failed_step_blocks_preview_and_apply_without_losing_old_results(self):
        a, b, attempt = failed_pair()
        item = attempt.bkt_failure.steps.get(step_index=1)
        before = list(BKTObservation.objects.values())
        source = attempt.exercise
        source.content['steps'][1]['correct_index'] = 1
        source.content['steps'][1]['correct_answer'] = source.content['steps'][1]['choices'][1]
        source.save(update_fields=['content'])
        preview = BKTService.rebuild()
        assert not preview['can_apply']
        assert any(row['attempt_id'] == attempt.pk for row in preview['blockers'])
        with pytest.raises(BKTConsistencyError, match='본문·정답'):
            BKTService.rebuild(apply=True)
        assert list(BKTObservation.objects.values()) == before
        item.refresh_from_db()
        assert item.status == 'pending'

    def test_excluded_step_stays_pending_until_operator_confirms_and_rebuild_succeeds(self):
        a, b, attempt = failed_pair()
        item = attempt.bkt_failure.steps.get(step_index=1)
        source = attempt.exercise
        # 두 풀이가 같은 문항을 사용하므로 동일하게 보류된다.
        source.content['steps'][1]['_concept_review']['status'] = 'on_hold'
        source.save(update_fields=['content'])
        BKTService.rebuild(apply=True)
        assert pending_concept_ids() == {b.pk}
        assert attempt.bkt_failure.steps.get(step_index=0).status == 'applied'
        assert attempt.bkt_failure.steps.get(step_index=1).status == 'pending'
        confirm_exclusion(item.pk, '정답이 모호해 BKT에서 제외')
        assert b.pk in pending_concept_ids()  # 확정만으로 해제하지 않음
        BKTService.rebuild(apply=True)
        assert not pending_concept_ids()
        item.refresh_from_db()
        assert item.status == 'excluded'

    def test_exclusion_cannot_silently_skip_a_still_eligible_step(self):
        a, b, attempt = failed_pair()
        confirm_exclusion(attempt.bkt_failure.steps.first().pk, '제외 요청')
        assert not BKTService.rebuild()['can_apply']
        with pytest.raises(BKTConsistencyError):
            BKTService.rebuild(apply=True)

    def test_other_pending_step_keeps_same_concept_dirty(self):
        a, b, attempt = failed_pair()
        source = make_attempt([make_step(a.pk)], [0])
        newer = submit(source.exercise)
        for exercise in (attempt.exercise, source.exercise):
            exercise.content['steps'][0]['_concept_review']['status'] = 'on_hold'
            exercise.save(update_fields=['content'])
        confirm_exclusion(attempt.bkt_failure.steps.get(step_index=0).pk, '해당 문항만 제외')
        BKTService.rebuild(apply=True)
        assert a.pk in pending_concept_ids()
        assert newer.bkt_failure.steps.get().status == 'pending'
        assert attempt.bkt_failure.steps.get(step_index=0).status == 'excluded'

    def test_statistics_and_mate_use_stale_warning_and_fallback(self, client):
        a, b, attempt = failed_pair()
        report = concept_report(b.pk)
        assert report['bkt_selected']['needs_rebuild']
        assert report['bkt_selected']['predicted_correct'] is None
        result = LearningMateService.estimate_step_probabilities(attempt.exercise, before=attempt.created_at, fallback=.5)
        assert all(row['source'] == 'heuristic' and row['reason'] == 'bkt_rebuild_required' for row in result)
        response = client.get(reverse('search:stats'), {'concept': b.pk})
        assert '재계산 필요' in response.content.decode()

    def test_submit_result_distinguishes_failure_from_no_eligible_steps(self, client):
        a, b, attempt = failed_pair()
        response = client.post(reverse('practice:exercise_attempt', args=[attempt.exercise_id]), {'selected_indices': '[0, 0]'})
        html = response.content.decode()
        assert 'BKT 반영 보류' in html
        assert '승인된 개념 문항의 유효한 풀이 기록이 없습니다' not in html

    def test_stale_exercise_is_rejected_before_creating_a_new_attempt(self):
        concept = make_concept()
        source = make_attempt([make_step(concept.pk)], [0])
        old = Exercise.objects.get(pk=source.exercise_id)
        content = deepcopy(old.content)
        content['steps'][0]['question'] = '수정된 문항'
        Exercise.objects.filter(pk=old.pk).update(content=content)
        count = ExerciseAttempt.objects.count()
        with pytest.raises(ValueError, match='문항이 변경'):
            submit(old)
        assert ExerciseAttempt.objects.count() == count

    def test_missing_fixed_lock_fails_closed(self):
        BKTLock.objects.all().delete()
        with pytest.raises(BKTLock.DoesNotExist):
            BKTService.rebuild(apply=True)

    def test_history_mismatch_is_recorded_without_losing_the_submission(self):
        concept = make_concept()
        source = make_attempt([make_step(concept.pk)], [0])
        record_with_lock(source)
        ConceptMastery.objects.update(observation_count=9)
        attempt = submit(source.exercise)
        assert attempt.bkt_failure.error_code == 'history_mismatch'
        assert not attempt.bkt_observations.exists()
        assert ExerciseAttempt.objects.filter(pk=attempt.pk).exists()

    def test_actual_database_error_is_not_misclassified_as_recoverable(self):
        concept = make_concept()
        source = make_attempt([make_step(concept.pk)], [0])
        record_with_lock(source)
        original_count = ExerciseAttempt.objects.count()

        @transaction.atomic
        def duplicate_observation(attempt):
            duplicate = BKTObservation.objects.get()
            duplicate.pk = None
            duplicate.save(force_insert=True)  # 실제 UNIQUE 제약 위반

        with patch.object(BKTService, 'record_attempt', side_effect=duplicate_observation):
            with pytest.raises(IntegrityError):
                submit(source.exercise)
        assert ExerciseAttempt.objects.count() == original_count
        assert not BKTUpdateFailure.objects.exists()


def assert_waiting_on_lock(pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_blocking_pids(%s)', [pid])
            if cursor.fetchone()[0]:
                return
        sleep(.01)
    raise AssertionError('별도 PostgreSQL 트랜잭션이 공통 잠금에서 대기하지 않았습니다.')


def worker(fn, started, pid):
    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_backend_pid()')
            pid.append(cursor.fetchone()[0])
        started.set()
        return fn()
    finally:
        connection.close()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('first', ['failure', 'rebuild', 'edit'])
def test_shared_lock_serializes_failure_rebuild_submit_and_source_edit(first):
    if connection.vendor != 'postgresql':
        pytest.skip('실제 PostgreSQL 행 잠금 검증')
    concept = make_concept()
    source = make_attempt([make_step(concept.pk)], [0])
    started, pid = Event(), []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if first == 'failure':
                # 내부 savepoint 롤백 후에도 바깥에서 실패 기록과 잠금을 보유한다.
                state = BKTService._new_mastery(concept.pk)
                state.model_version = 'old'
                state.save()
                failed = submit(source.exercise)
                assert failed.bkt_failure.steps.count() == 1
                fn = lambda: BKTService.rebuild(apply=True)
            elif first == 'rebuild':
                BKTService.rebuild(apply=True)
                fn = lambda: submit(Exercise.objects.get(pk=source.exercise_id)).pk
            else:
                # 실제 관리자 mixin의 POST 경로를 통해 잠금 후 문항을 변경한다.
                class EditView:
                    def changeform_view(self, *args):
                        source.exercise.content['steps'][0]['_concept_review']['status'] = 'on_hold'
                        source.exercise.save(update_fields=['content'])

                class LockedEditView(BKTLockedAdminMixin, EditView):
                    pass

                LockedEditView().changeform_view(SimpleNamespace(method='POST'))
                fn = lambda: capture_with_lock(ExerciseAttempt.objects.get(pk=source.pk))
            future = pool.submit(worker, fn, started, pid)
            assert started.wait(5)
            assert_waiting_on_lock(pid[0])
        result = future.result(timeout=10)
    if first == 'failure':
        assert result['applied'] and not pending_concept_ids()
        assert BKTObservation.objects.count() == 2
    elif first == 'rebuild':
        assert ExerciseAttempt.objects.filter(pk=result).exists()
        assert BKTObservation.objects.count() == 2
    else:
        assert result == 0 and not BKTObservation.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_empty_concept_set_still_serializes_rebuild():
    if connection.vendor != 'postgresql':
        pytest.skip('실제 PostgreSQL 행 잠금 검증')
    started, pid = Event(), []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            BKTService.rebuild(apply=True)  # 개념이 0개여도 공통 잠금은 보유한다.
            future = pool.submit(worker, lambda: BKTService.rebuild(apply=True), started, pid)
            assert started.wait(5)
            assert_waiting_on_lock(pid[0])
        assert future.result(timeout=10)['applied']
