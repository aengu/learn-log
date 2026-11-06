"""수동 승인된 선택지 문항만 단일 사용자의 BKT 관측으로 변환한다."""
from copy import deepcopy
from dataclasses import asdict

from django.db import transaction
from django.utils import timezone

from ..models import BKTFailureStep, BKTObservation, BKTUpdateFailure, Concept, ConceptMastery, Exercise, ExerciseAttempt
from .bkt_lock import acquire_bkt_lock, pending_concept_ids
from .bkt_math import DEFAULT_PARAMETERS, MODEL_VERSION, update_mastery


class BKTConsistencyError(ValueError):
    """원본 풀이를 보존하고 BKT만 보류할 수 있는, 예상된 정합성 오류."""

    def __init__(self, code, message, step_index=None):
        super().__init__(message)
        self.code = code
        self.step_index = step_index


class BKTService:
    @classmethod
    @transaction.atomic
    def capture_attempt(cls, attempt):
        """내부 savepoint가 롤백된 뒤에도 공통 잠금과 원본 풀이는 유지한다."""
        acquire_bkt_lock()
        attempt.exercise = Exercise.objects.select_for_update().get(pk=attempt.exercise_id)
        active_ids = set(Concept.objects.filter(is_active=True).values_list('pk', flat=True))
        steps = list(cls._eligible_steps(attempt, active_ids))
        try:
            return cls.record_attempt(attempt)
        except BKTConsistencyError as exc:
            failure, created = BKTUpdateFailure.objects.get_or_create(
                attempt=attempt,
                defaults={
                    'error_code': exc.code, 'error_message': str(exc),
                    'failed_step_index': exc.step_index,
                    'parameters': asdict(DEFAULT_PARAMETERS), 'model_version': MODEL_VERSION,
                },
            )
            if created:
                # 실패 지점뿐 아니라 전체 롤백된 step 모두를 기록한다.
                BKTFailureStep.objects.bulk_create([
                    BKTFailureStep(
                        failure=failure, concept_id=step['concept_id'], step_index=index,
                        step_snapshot={'step': deepcopy(step), 'selected_index': selected},
                    ) for index, step, selected in steps
                ])
            return 0

    @staticmethod
    def approved_steps(exercise, active_ids):
        """관측과 풀이 전 예측에서 같은 승인·형식 조건을 사용한다."""
        if exercise.exercise_type != 'path_trace':
            return
        content = exercise.content
        if not isinstance(content, dict):
            return
        steps = content.get('steps')
        if not isinstance(steps, list):
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
            yield index, step

    @classmethod
    def _eligible_steps(cls, attempt, active_ids):
        if not isinstance(attempt.user_answer, dict):
            return
        selected = attempt.user_answer.get('selected_indices')
        if not isinstance(selected, list):
            return
        for index, step in cls.approved_steps(attempt.exercise, active_ids):
            choices = step['choices']
            # 미응답·범위 밖 선택·bool 값은 오답 관측으로 추정하지 않는다.
            if index >= len(selected) or type(selected[index]) is not int or not 0 <= selected[index] < len(choices):
                continue
            yield index, step, selected[index]

    @staticmethod
    def _observation(attempt, index, step, selected, prior):
        # ④ 문항별 계산 결과를 메모리의 관측 객체로 만든다. DB 저장은 호출부에서 한다.
        # 세트 점수가 아닌 실제 선택과 정답을 비교하고, 당시 설정·문항도 함께 남긴다.
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
        # ⑥ 최신 요약을 메모리에서 갱신한다. DB 저장은 record_attempt()의 state.save().
        state.mastery = observation.mastery_after
        state.observation_count += 1
        state.correct_count += int(observation.is_correct)
        state.last_observed_at = observation.observed_at

    @classmethod
    @transaction.atomic
    def record_attempt(cls, attempt):
        """같은 풀이·문항의 중복 반영을 막고, 신규 관측만 시간순으로 추가한다."""
        # capture_attempt()이 이 savepoint 바깥에서 이미 공통 잠금을 보유한다.
        # 직접 호출하는 관리/테스트 경로도 동일한 잠금 규칙을 따른다.
        acquire_bkt_lock()
        attempt.exercise = Exercise.objects.select_for_update().get(pk=attempt.exercise_id)
        if attempt.exercise.exercise_type != 'path_trace':
            return 0
        active_ids = set(Concept.objects.filter(is_active=True).values_list('pk', flat=True))
        # ③ 승인·활성 개념·정상 4지선다·유효한 사용자 선택인 문항만 추린다.
        # 대상이 없으면 BKT는 0건 반영하고, 바깥의 원본 풀이 저장은 계속한다.
        steps = list(cls._eligible_steps(attempt, active_ids))
        existing = set(BKTObservation.objects.filter(attempt=attempt).values_list('step_index', flat=True))
        if {step['concept_id'] for _, step, _ in steps} & pending_concept_ids():
            raise BKTConsistencyError('pending_rebuild', '관련 개념에 미해결 BKT 기록이 있어 이번 풀이의 반영을 보류했습니다.')
        applied = 0
        for index, step, selected in steps:
            if index in existing:
                # 같은 풀이의 같은 문항은 재호출해도 한 번만 반영한다.
                continue
            concept_id = step['concept_id']
            # 이전 숙련도를 읽는다. 첫 관측 개념은 기본 초기값으로 상태를 만든다.
            state, _ = ConceptMastery.objects.get_or_create(
                concept_id=concept_id,
                defaults={
                    'mastery': DEFAULT_PARAMETERS.initial_mastery,
                    'parameters': asdict(DEFAULT_PARAMETERS), 'model_version': MODEL_VERSION,
                },
            )
            # 검사 1: 과거 상태와 현재 계산 버전·파라미터가 다르면 기준을 섞지 않는다.
            if state.model_version != MODEL_VERSION or state.parameters != asdict(DEFAULT_PARAMETERS):
                raise BKTConsistencyError('settings_changed', 'BKT 설정이 변경됐습니다. rebuild_bkt --apply로 재구성하세요.', index)
            history = BKTObservation.objects.filter(concept_id=concept_id)
            last = history.order_by('-observed_at', '-attempt_id', '-step_index').first()
            # 검사 2: 요약의 반영 개수·숙련도가 실제 이력 개수·마지막 계산값과 맞아야 한다.
            # 예: 풀이 삭제로 관측은 사라졌지만 요약이 남았다면 재구성이 필요하다.
            if state.observation_count != history.count() or (last and state.mastery != last.mastery_after):
                raise BKTConsistencyError('history_mismatch', '풀이 이력이 변경됐습니다. rebuild_bkt --apply로 재구성하세요.', index)
            # 검사 3: 과거 풀이를 최신 상태 뒤에 덧붙이지 않는다.
            # 시각이 같으면 풀이 ID, 문항 인덱스 순서까지 비교해 시간순 계산을 지킨다.
            if last and (attempt.created_at, attempt.pk, index) < (last.observed_at, last.attempt_id, last.step_index):
                raise BKTConsistencyError('out_of_order', '과거 풀이의 추가 반영은 rebuild_bkt --apply로 시간순 재구성하세요.', index)
            observation = cls._observation(attempt, index, step, selected, state.mastery)
            # ⑤ 정오답·계산 전후 값·예상 정답 확률·당시 설정을 관측 이력으로 저장한다.
            observation.save()
            cls._advance(state, observation)
            # ⑥ 다음 문항에 사용할 숙련도와 누적 집계를 현재 상태에 저장한다.
            state.save()
            applied += 1
        return applied

    @classmethod
    @transaction.atomic
    def rebuild(cls, apply=False):
        """현재 검토 상태로 전체 사용자 풀이를 시간순 재계산한다. 기본은 저장 없는 미리보기."""
        # 미리보기도 일관된 입력을 읽는다. --apply는 미리보기를 신뢰하지 않고 다시 수집한다.
        acquire_bkt_lock()
        active_ids = set(Concept.objects.filter(is_active=True).values_list('pk', flat=True))
        known_steps = {
            (attempt_id, index): snapshot
            for attempt_id, index, snapshot in BKTObservation.objects.values_list('attempt_id', 'step_index', 'step_snapshot')
        }
        failure_steps = list(BKTFailureStep.objects.select_related('failure').order_by('pk'))
        for item in failure_steps:
            known_steps.setdefault((item.failure.attempt_id, item.step_index), item.step_snapshot)
        states, observations = {}, []
        blockers, included = [], set()
        attempts = ExerciseAttempt.objects.filter(exercise__exercise_type='path_trace').select_related('exercise').order_by('created_at', 'pk')
        for attempt in attempts.iterator():
            for index, step, selected in cls._eligible_steps(attempt, active_ids):
                previous = known_steps.get((attempt.pk, index))
                if previous is not None and (
                    any(previous.get('step', {}).get(key) != step.get(key)
                        for key in ('question', 'choices', 'correct_index', 'correct_answer', 'concept_id'))
                    or previous.get('selected_index') != selected
                ):
                    blockers.append({
                        'attempt_id': attempt.pk, 'step_index': index,
                        'reason': '문항의 본문·정답·개념 또는 사용자 선택이 스냅샷과 다릅니다. 기존 문항을 보류하고 수정본은 새 문제로 만드세요.',
                    })
                    continue
                included.add((attempt.pk, index))
                concept_id = step['concept_id']
                state = states.setdefault(concept_id, cls._new_mastery(concept_id))
                observation = cls._observation(attempt, index, step, selected, state.mastery)
                observations.append(observation)
                cls._advance(state, observation)
        outcomes = []
        blocked_keys = {(row['attempt_id'], row['step_index']) for row in blockers}
        for item in failure_steps:
            key = (item.failure.attempt_id, item.step_index)
            if key in included:
                if item.exclusion_reason:
                    blockers.append({'attempt_id': key[0], 'step_index': key[1],
                                     'reason': '제외를 확정한 문항이 아직 반영 대상입니다. 먼저 승인 상태 등을 변경하세요.'})
                status = 'applied'
            elif key not in blocked_keys and item.exclusion_reason:
                status = 'excluded'
            else:
                status = 'pending'
            outcomes.append({'failure_step_id': item.pk, 'attempt_id': key[0],
                             'step_index': key[1], 'concept_id': item.concept_id, 'status': status})
        result = {
            'applied': False, 'can_apply': not blockers, 'blockers': blockers,
            'observations': len(observations), 'concepts': len(states),
            'failure_steps': outcomes,
            'unresolved_steps': sum(row['status'] == 'pending' for row in outcomes),
            'model_version': MODEL_VERSION, 'parameters': asdict(DEFAULT_PARAMETERS),
            'states': [{
                'concept_id': state.concept_id, 'mastery': state.mastery,
                'observation_count': state.observation_count, 'correct_count': state.correct_count,
            } for state in sorted(states.values(), key=lambda state: state.concept_id)],
        }
        if apply:
            if blockers:
                raise BKTConsistencyError('unsafe_rebuild', blockers[0]['reason'])
            # 원본 풀이·문제는 건드리지 않고 재계산 가능한 BKT 결과 두 테이블만 교체한다.
            BKTObservation.objects.all().delete()
            ConceptMastery.objects.all().delete()
            ConceptMastery.objects.bulk_create(list(states.values()))
            BKTObservation.objects.bulk_create(observations)
            for item, outcome in zip(failure_steps, outcomes):
                item.status = outcome['status']
            BKTFailureStep.objects.bulk_update(failure_steps, ['status'])
            for failure in BKTUpdateFailure.objects.all():
                failure.resolved_at = None if failure.steps.filter(status='pending').exists() else timezone.now()
                failure.save(update_fields=['resolved_at'])
            result['applied'] = True
        return result
