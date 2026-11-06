"""BKT 수식·승인 필터·중복 방지·시간순 재구성·원본 보존 검증. 실제 API 호출 없음."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db import close_old_connections, connection

from practice.models import BKTObservation, Concept, ConceptMastery, Exercise, ExerciseAttempt
from practice.services.bkt_math import BKTParameters, update_mastery
from practice.services.bkt_service import BKTService
from practice.services.exercise_service import ExerciseService
from search.tests.factories import LearningLogFactory


def make_concept(key='test.concept', **kwargs):
    return Concept.objects.create(id=key, name=key, description='테스트 개념', **kwargs)


def make_step(concept_id, status='approved'):
    return {
        'question': '문항', 'choices': ['정답', '오답1', '오답2', '오답3'],
        'correct_index': 0, 'correct_answer': '정답', 'explanation': '해설',
        'concept_id': concept_id, 'concept_candidate': None,
        '_concept_review': {'status': status},
    }


def make_attempt(steps, selected, **kwargs):
    exercise = Exercise.objects.create(
        learning_log=LearningLogFactory(), exercise_type='path_trace', content={'steps': steps},
    )
    return ExerciseAttempt.objects.create(
        exercise=exercise, user_answer={'selected_indices': selected}, **kwargs,
    )


class TestBKTMath:
    def test_correct_observation_then_learning_transition(self):
        result = update_mastery(0.3, True)
        assert result['predicted_correct'] == pytest.approx(0.445)
        assert result['mastery_posterior'] == pytest.approx(0.27 / 0.445)
        assert result['mastery_after'] == pytest.approx(0.6460674157303371)

    def test_wrong_observation_uses_slip_and_not_guess(self):
        result = update_mastery(0.3, False)
        assert result['mastery_posterior'] == pytest.approx(0.03 / 0.555)
        assert result['mastery_after'] == pytest.approx(0.14864864864864866)

    def test_no_forgetting_preserves_certain_mastery(self):
        assert update_mastery(1, False)['mastery_after'] == 1
        assert update_mastery(0, True)['mastery_posterior'] == 0
        assert update_mastery(0, True)['mastery_after'] == 0.1

    def test_learning_can_increase_mastery_even_after_wrong_answer(self):
        # 오답의 증거로 내려간 사후 확률과, 피드백 후 학습 전이를 구분한다.
        result = update_mastery(0.01, False)
        assert result['mastery_posterior'] < 0.01
        assert result['mastery_after'] > 0.01

    @pytest.mark.parametrize('value', [-0.1, 1.1, float('nan'), float('inf'), True, '0.3'])
    def test_invalid_probability_rejected(self, value):
        with pytest.raises(ValueError):
            update_mastery(value, True)
        with pytest.raises(ValueError):
            BKTParameters(initial_mastery=value)

    def test_uninformative_or_impossible_parameters_rejected(self):
        with pytest.raises(ValueError):
            BKTParameters(slip=0.8, guess=0.3)
        with pytest.raises(ValueError):
            update_mastery(0, True, BKTParameters(guess=0, slip=0))
        with pytest.raises(ValueError):
            update_mastery(0.3, 1)


@pytest.mark.django_db
class TestBKTService:
    def test_only_approved_active_registered_concepts_are_used(self):
        concept = make_concept()
        inactive = make_concept('test.inactive', is_active=False)
        steps = [
            make_step(concept.pk), make_step(concept.pk, 'on_hold'),
            make_step(concept.pk, 'pending'), make_step('test.missing'),
            make_step(inactive.pk), make_step(concept.pk),
        ]
        steps[2].pop('_concept_review')
        steps[5]['concept_candidate'] = {'name': '미검토 후보'}
        attempt = make_attempt(steps, [0] * len(steps), score=0, is_correct=False)
        before = deepcopy(attempt.exercise.content)

        assert BKTService.record_attempt(attempt) == 1

        observation = BKTObservation.objects.get()
        assert observation.step_index == 0
        assert observation.is_correct is True  # 세트 점수가 아니라 단계별 정오답 사용
        assert observation.concept_id == concept.pk
        state = ConceptMastery.objects.get()
        assert state.observation_count == state.correct_count == 1
        assert state.mastery == pytest.approx(0.6460674157303371)
        attempt.refresh_from_db()
        attempt.exercise.refresh_from_db()
        assert attempt.score == 0
        assert attempt.is_correct is False
        assert attempt.exercise.content == before

    @pytest.mark.parametrize('selected', [[], [True], [None], [4], [-1], ['0'], None])
    def test_unanswered_or_invalid_selection_is_not_counted_as_wrong(self, selected):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk)], selected)
        assert BKTService.record_attempt(attempt) == 0
        assert not ConceptMastery.objects.exists()
        assert not BKTObservation.objects.exists()

    def test_invalid_answer_key_is_excluded(self):
        concept = make_concept()
        step = make_step(concept.pk)
        step['correct_answer'] = '실제 보기와 불일치'
        assert BKTService.record_attempt(make_attempt([step], [0])) == 0

    def test_multiple_steps_update_same_concept_in_order_and_are_idempotent(self):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk), make_step(concept.pk)], [0, 1])
        assert BKTService.record_attempt(attempt) == 2
        first, second = list(BKTObservation.objects.all())
        assert second.mastery_before == first.mastery_after
        assert second.is_correct is False
        assert second.mastery_after == pytest.approx(update_mastery(first.mastery_after, False)['mastery_after'])
        snapshot = list(ConceptMastery.objects.values())
        assert BKTService.record_attempt(attempt) == 0
        assert list(ConceptMastery.objects.values()) == snapshot
        assert BKTObservation.objects.count() == 2
        state = ConceptMastery.objects.get()
        assert (state.observation_count, state.correct_count) == (2, 1)

    def test_database_rejects_duplicate_attempt_step(self):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk)], [0])
        BKTService.record_attempt(attempt)
        duplicate = BKTObservation.objects.get()
        duplicate.pk = None
        with pytest.raises(IntegrityError), transaction.atomic():
            duplicate.save(force_insert=True)
        assert BKTObservation.objects.count() == 1

    def test_old_attempt_requires_rebuild_instead_of_out_of_order_update(self):
        concept = make_concept()
        older = make_attempt([make_step(concept.pk)], [0])
        newer = make_attempt([make_step(concept.pk)], [1])
        BKTService.record_attempt(newer)
        with pytest.raises(ValueError, match='시간순'):
            BKTService.record_attempt(older)
        assert BKTObservation.objects.count() == 1
        result = BKTService.rebuild(apply=True)
        assert result['observations'] == 2
        assert list(BKTObservation.objects.values_list('attempt_id', flat=True)) == [older.pk, newer.pk]

    def test_rebuild_preview_does_not_write_and_apply_preserves_sources(self):
        concept = make_concept()
        approved = make_attempt([make_step(concept.pk)], [0], score=0.25)
        make_attempt([make_step(concept.pk, 'on_hold')], [0], score=1)
        before = list(ExerciseAttempt.objects.values())
        preview = BKTService.rebuild()
        assert preview['observations'] == 1
        assert not ConceptMastery.objects.exists()
        assert not BKTObservation.objects.exists()
        result = BKTService.rebuild(apply=True)
        assert result['states'] == preview['states']
        observation = BKTObservation.objects.get()
        assert observation.attempt_id == approved.pk
        assert observation.parameters == {'initial_mastery': 0.3, 'slip': 0.1, 'guess': 0.25, 'learn': 0.1}
        assert observation.step_snapshot['selected_index'] == 0
        assert observation.step_snapshot['step']['_concept_review']['status'] == 'approved'
        assert list(ExerciseAttempt.objects.values()) == before
        assert BKTService.rebuild(apply=True)['states'] == result['states']
        assert BKTObservation.objects.count() == 1

    def test_rebuild_withdraws_later_held_observation(self):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk)], [0])
        BKTService.record_attempt(attempt)
        attempt.exercise.content['steps'][0]['_concept_review']['status'] = 'on_hold'
        attempt.exercise.save(update_fields=['content'])
        assert BKTService.rebuild(apply=True)['observations'] == 0
        assert not ConceptMastery.objects.exists()
        assert not BKTObservation.objects.exists()

    def test_rebuild_does_not_regrade_old_answer_against_changed_question(self):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk)], [0])
        BKTService.record_attempt(attempt)
        before = list(BKTObservation.objects.values())
        step = attempt.exercise.content['steps'][0]
        step['correct_index'], step['correct_answer'] = 1, step['choices'][1]
        attempt.exercise.save(update_fields=['content'])
        with pytest.raises(ValueError, match='본문·정답'):
            BKTService.rebuild(apply=True)
        assert list(BKTObservation.objects.values()) == before
        assert ConceptMastery.objects.get().correct_count == 1

    def test_deleted_source_requires_rebuild_and_rebuild_removes_stale_state(self):
        concept = make_concept()
        attempt = make_attempt([make_step(concept.pk)], [0])
        BKTService.record_attempt(attempt)
        attempt.delete()
        newer = make_attempt([make_step(concept.pk)], [1])
        with pytest.raises(ValueError, match='이력이 변경'):
            BKTService.record_attempt(newer)
        BKTService.rebuild(apply=True)
        state = ConceptMastery.objects.get()
        assert state.observation_count == 1
        assert state.correct_count == 0
        assert state.mastery == pytest.approx(0.14864864864864866)

    def test_new_attempt_saved_with_bkt_and_all_updates_rollback_on_failure(self):
        concept = make_concept()
        initial = make_attempt([make_step(concept.pk)], [1])
        service = ExerciseService.__new__(ExerciseService)
        evaluation = {'score': 1, 'is_correct': True, 'ai_feedback': '정답'}
        saved = service.save_attempt(initial.exercise, {'selected_indices': [0]}, evaluation)
        assert saved.bkt_observations.count() == 1
        assert BKTObservation.objects.get().is_correct is True
        before_attempts = ExerciseAttempt.objects.count()
        initial.exercise.refresh_from_db()
        before_interval = initial.exercise.review_interval
        with patch.object(BKTService, 'record_attempt', side_effect=RuntimeError('저장 실패')):
            with pytest.raises(RuntimeError):
                service.save_attempt(initial.exercise, {'selected_indices': [0]}, evaluation)
        assert ExerciseAttempt.objects.count() == before_attempts
        initial.exercise.refresh_from_db()
        assert initial.exercise.review_interval == before_interval

    def test_self_marked_attempt_is_not_a_bkt_observation(self):
        attempt = make_attempt([], [])
        attempt.exercise.exercise_type = 'generation_compare'
        attempt.exercise.save(update_fields=['exercise_type'])
        assert BKTService.record_attempt(attempt) == 0

    def test_command_defaults_to_preview_and_apply_is_explicit(self):
        concept = make_concept()
        make_attempt([make_step(concept.pk)], [0])
        output = StringIO()
        call_command('rebuild_bkt', stdout=output)
        assert json.loads(output.getvalue())['applied'] is False
        assert not BKTObservation.objects.exists()
        call_command('rebuild_bkt', '--apply', stdout=StringIO())
        assert BKTObservation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_attempt_is_only_counted_once():
    concept = make_concept()
    attempt = make_attempt([make_step(concept.pk)], [0])

    def record():
        close_old_connections()
        try:
            stored = ExerciseAttempt.objects.select_related('exercise').get(pk=attempt.pk)
            return BKTService.record_attempt(stored)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [0, 1]
    assert BKTObservation.objects.count() == 1
    assert ConceptMastery.objects.get().observation_count == 1
