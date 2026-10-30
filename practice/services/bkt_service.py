"""수동 승인된 선택지 문항만 단일 사용자의 BKT 관측으로 변환한다."""
from copy import deepcopy
from dataclasses import asdict

from django.db import transaction

from ..models import BKTObservation, Concept, ConceptMastery, ExerciseAttempt
from .bkt_math import DEFAULT_PARAMETERS, MODEL_VERSION, update_mastery


class BKTService:
    @staticmethod
    def _eligible_steps(attempt, active_ids):
        if attempt.exercise.exercise_type != 'path_trace':
            return
        content, answer = attempt.exercise.content, attempt.user_answer
        if not isinstance(content, dict) or not isinstance(answer, dict):
            return
        steps, selected = content.get('steps'), answer.get('selected_indices')
        if not isinstance(steps, list) or not isinstance(selected, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            review, concept_id = step.get('_concept_review'), step.get('concept_id')
            if not isinstance(review, dict) or review.get('status') != 'approved':
                continue
            if not isinstance(concept_id, str) or concept_id not in active_ids or step.get('concept_candidate') is not None:
                continue
            choices, correct_index = step.get('choices'), step.get('correct_index')
            if not isinstance(choices, list) or len(choices) != 4 or any(not isinstance(choice, str) or not choice.strip() for choice in choices):
                continue
            if len({choice.strip().casefold() for choice in choices}) != len(choices):
                continue
            if type(correct_index) is not int or not 0 <= correct_index < len(choices):
                continue
            if choices[correct_index] != step.get('correct_answer'):
                continue
            # 미응답·범위 밖 선택·bool 값은 오답 관측으로 추정하지 않는다.
            if index >= len(selected) or type(selected[index]) is not int or not 0 <= selected[index] < len(choices):
                continue
            yield index, step, selected[index]

    @staticmethod
    def _observation(attempt, index, step, selected, prior):
        return BKTObservation(
            attempt=attempt, concept_id=step['concept_id'], step_index=index,
            is_correct=selected == step['correct_index'],
            **update_mastery(prior, selected == step['correct_index']),
            parameters=asdict(DEFAULT_PARAMETERS), model_version=MODEL_VERSION,
            step_snapshot={'step': deepcopy(step), 'selected_index': selected},
            observed_at=attempt.created_at,
        )

    @staticmethod
    def _new_mastery(concept_id):
        return ConceptMastery(
            concept_id=concept_id, mastery=DEFAULT_PARAMETERS.initial_mastery,
            parameters=asdict(DEFAULT_PARAMETERS), model_version=MODEL_VERSION,
        )

    @staticmethod
    def _advance(state, observation):
        state.mastery = observation.mastery_after
        state.observation_count += 1
        state.correct_count += int(observation.is_correct)
        state.last_observed_at = observation.observed_at

    @classmethod
    @transaction.atomic
    def record_attempt(cls, attempt):
        """같은 풀이·문항의 중복 반영을 막고, 신규 관측만 시간순으로 추가한다."""
        if attempt.exercise.exercise_type != 'path_trace':
            return 0
        # 소규모 단일 사용자 MVP: 활성 개념을 같은 순서로 잠가 중복 요청과 재구성을 직렬화한다.
        active_ids = set(Concept.objects.select_for_update().filter(is_active=True).order_by('pk').values_list('pk', flat=True))
        steps = list(cls._eligible_steps(attempt, active_ids))
        existing = set(BKTObservation.objects.filter(attempt=attempt).values_list('step_index', flat=True))
        applied = 0
        for index, step, selected in steps:
            if index in existing:
                continue
            concept_id = step['concept_id']
            state, _ = ConceptMastery.objects.get_or_create(
                concept_id=concept_id,
                defaults={
                    'mastery': DEFAULT_PARAMETERS.initial_mastery,
                    'parameters': asdict(DEFAULT_PARAMETERS), 'model_version': MODEL_VERSION,
                },
            )
            if state.model_version != MODEL_VERSION or state.parameters != asdict(DEFAULT_PARAMETERS):
                raise ValueError('BKT 설정이 변경됐습니다. rebuild_bkt --apply로 재구성하세요.')
            history = BKTObservation.objects.filter(concept_id=concept_id)
            last = history.order_by('-observed_at', '-attempt_id', '-step_index').first()
            if state.observation_count != history.count() or (last and state.mastery != last.mastery_after):
                raise ValueError('풀이 이력이 변경됐습니다. rebuild_bkt --apply로 재구성하세요.')
            if last and (attempt.created_at, attempt.pk, index) < (last.observed_at, last.attempt_id, last.step_index):
                raise ValueError('과거 풀이의 추가 반영은 rebuild_bkt --apply로 시간순 재구성하세요.')
            observation = cls._observation(attempt, index, step, selected, state.mastery)
            observation.save()
            cls._advance(state, observation)
            state.save()
            applied += 1
        return applied

    @classmethod
    @transaction.atomic
    def rebuild(cls, apply=False):
        """현재 검토 상태로 전체 사용자 풀이를 시간순 재계산한다. 기본은 저장 없는 미리보기."""
        concepts = Concept.objects.order_by('pk')
        if apply:
            concepts = concepts.select_for_update()
        active_ids = {concept.pk for concept in concepts if concept.is_active}
        known_steps = {
            (attempt_id, index): snapshot.get('step', {})
            for attempt_id, index, snapshot in BKTObservation.objects.values_list('attempt_id', 'step_index', 'step_snapshot')
        }
        states, observations = {}, []
        attempts = ExerciseAttempt.objects.filter(exercise__exercise_type='path_trace').select_related('exercise').order_by('created_at', 'pk')
        for attempt in attempts.iterator():
            for index, step, selected in cls._eligible_steps(attempt, active_ids):
                previous = known_steps.get((attempt.pk, index))
                if previous is not None and any(
                    previous.get(key) != step.get(key)
                    for key in ('question', 'choices', 'correct_index', 'correct_answer')
                ):
                    raise ValueError('이미 반영한 문항의 본문·정답이 변경됐습니다. 기존 문항을 보류하고 수정본은 새 문제로 만드세요.')
                concept_id = step['concept_id']
                state = states.setdefault(concept_id, cls._new_mastery(concept_id))
                observation = cls._observation(attempt, index, step, selected, state.mastery)
                observations.append(observation)
                cls._advance(state, observation)
        if apply:
            # 원본 풀이·문제는 건드리지 않고 재계산 가능한 BKT 결과 두 테이블만 교체한다.
            BKTObservation.objects.all().delete()
            ConceptMastery.objects.all().delete()
            ConceptMastery.objects.bulk_create(list(states.values()))
            BKTObservation.objects.bulk_create(observations)
        return {
            'applied': apply,
            'observations': len(observations),
            'concepts': len(states),
            'states': [{
                'concept_id': state.concept_id, 'mastery': state.mastery,
                'observation_count': state.observation_count, 'correct_count': state.correct_count,
            } for state in sorted(states.values(), key=lambda state: state.concept_id)],
        }
