import json
import logging
import random
import textwrap
from dataclasses import asdict

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone
from groq import Groq
from pgvector.django import CosineDistance

from search.models import LearningLog

from ..models import BKTObservation, Concept, Exercise, ExerciseAttempt, MateAttempt
from .bkt_math import DEFAULT_PARAMETERS, MODEL_VERSION
from .bkt_service import BKTService
from .bkt_lock import pending_concept_ids
from .exercise_service import ExerciseService


logger = logging.getLogger(__name__)


class LearningMateService:
    """러닝메이트의 path_trace 풀이와 수준 보정.

    LLM은 정답 정보 없이 각 선택지의 가능성을 판단한다. 승인 문항은 사용자
    BKT로 정답 확률을 정하고, 나머지는 기존 추정과 LLM 판단을 혼합한다.
    오답 간 비율은 LLM 판단을 유지해 하나의 선택지를 샘플링한다.
    """

    MODEL = "qwen/qwen3.8-27b"
    PROMPT_VERSION = "v1"
    RECENT_ATTEMPT_LIMIT = 10
    RELATED_LOG_LIMIT = 5
    RELATED_ATTEMPT_LIMIT = 10
    USER_LEVEL_WEIGHT = 0.8
    MIN_STEP_PROBABILITY = 0.15
    MAX_STEP_PROBABILITY = 0.95
    STATE_TIMEOUT_SECONDS = 120
    LLM_TIMEOUT_SECONDS = 30
    REUSE_SECONDS = 3600

    def __init__(self, client=None):
        self.client = client or Groq(
            api_key=settings.GROQ_API_KEY, timeout=self.LLM_TIMEOUT_SECONDS, max_retries=0,
        )

    @classmethod
    @transaction.atomic
    def get_or_create_attempt(cls, exercise):
        """미제출 대결을 1시간 재사용한다. LLM 호출은 이 트랜잭션 밖에서 한다."""
        # 대결이 없는 경우에도 같은 문제의 동시 페이지 진입을 직렬화한다.
        Exercise.objects.select_for_update().get(pk=exercise.pk)
        cutoff = timezone.now() - timezone.timedelta(seconds=cls.REUSE_SECONDS)
        # 제출은 대결 → 문제 순으로 잠근다. 이미 잠긴 대결은 기다리지 않아 역순 교착을 피한다.
        candidates = list(MateAttempt.objects.select_for_update(skip_locked=True).filter(
            exercise=exercise, user_attempt__isnull=True,
            status__in=['pending', 'solving', 'ready'],
        ).order_by('-created_at', '-pk'))
        expired_ids = [
            mate.pk for mate in candidates
            if mate.created_at <= cutoff and mate.status in ['pending', 'ready']
        ]
        if expired_ids:
            MateAttempt.objects.filter(
                pk__in=expired_ids, user_attempt__isnull=True,
                status__in=['pending', 'ready'], created_at__lte=cutoff,
            ).delete()
        for mate in candidates:
            if mate.created_at > cutoff:
                return mate
        return MateAttempt.objects.create(exercise=exercise)

    @classmethod
    def refresh_status(cls, mate_attempt):
        """조회 시 미시작·중단 작업을 정리한다. 엄격한 실행 기한이나 자동 재시도는 아니다."""
        now = timezone.now()
        cutoff = now - timezone.timedelta(seconds=cls.STATE_TIMEOUT_SECONDS)
        MateAttempt.objects.filter(pk=mate_attempt.pk).filter(
            Q(status='pending', created_at__lte=cutoff)
            | Q(status='solving', started_at__lte=cutoff)
            # 배포 중 구버전 워커가 만든 NULL 시작 시각은 생성 시각으로 복구한다.
            | Q(status='solving', started_at__isnull=True, created_at__lte=cutoff)
        ).update(status='failed', error_message='러닝메이트 풀이 대기 또는 처리 시간 초과', answered_at=now)
        mate_attempt.refresh_from_db()
        return mate_attempt

    @staticmethod
    def public_exercise_content(exercise):
        """LLM에게 정답·해설·오답 메타데이터를 노출하지 않는다."""
        return {
            'scenario': exercise.content.get('scenario', ''),
            'steps': [
                {
                    'question': step.get('question', ''),
                    'choices': step.get('choices', []),
                }
                for step in exercise.content.get('steps', [])
            ],
        }

    @staticmethod
    def _attempt_counts(attempts):
        correct = 0
        total = 0
        for attempt in attempts:
            steps = attempt.exercise.content.get('steps', [])
            selected = (attempt.user_answer or {}).get('selected_indices', [])
            for index, step in enumerate(steps):
                if index >= len(selected):
                    continue
                total += 1
                if selected[index] == step.get('correct_index'):
                    correct += 1
        return correct, total

    @staticmethod
    def _beta_probability(correct, total):
        """Beta(2, 2) 사전분포로 적은 표본의 0%·100% 튀어오름을 줄인다."""
        return (correct + 2) / (total + 4)

    def _related_log_ids(self, exercise):
        current = exercise.learning_log
        ids = [current.pk]
        if current.embedding is None:
            return ids

        try:
            similar = (
                LearningLog.objects
                .exclude(pk=current.pk)
                .filter(embedding__isnull=False)
                .annotate(distance=CosineDistance('embedding', current.embedding))
                .order_by('distance')
                .values_list('pk', flat=True)[:self.RELATED_LOG_LIMIT - 1]
            )
            ids.extend(similar)
        except DatabaseError:
            # pgvector가 없는 개발·테스트 DB에서는 현재 학습 로그만 사용한다.
            pass
        return ids

    def estimate_user_probability(self, exercise, before=None):
        attempts = ExerciseAttempt.objects.all()
        if before is not None:
            attempts = attempts.filter(created_at__lt=before)
        recent_attempts = list(
            attempts
            .filter(exercise__exercise_type='path_trace')
            .select_related('exercise')
            .order_by('-created_at')[:self.RECENT_ATTEMPT_LIMIT]
        )
        recent_correct, recent_total = self._attempt_counts(recent_attempts)
        recent_probability = self._beta_probability(recent_correct, recent_total)

        related_log_ids = self._related_log_ids(exercise)
        related_attempts = list(
            attempts
            .filter(
                exercise__exercise_type='path_trace',
                exercise__learning_log_id__in=related_log_ids,
            )
            .select_related('exercise')
            .order_by('-created_at')[:self.RELATED_ATTEMPT_LIMIT]
        )
        related_correct, related_total = self._attempt_counts(related_attempts)
        related_probability = self._beta_probability(related_correct, related_total)

        # 관련 표본 12 step에서 최대 70%까지만 영향을 주어,
        # 유사도 오판이 전체 흐름을 압도하지 않게 한다.
        related_weight = min(related_total / 12, 0.7)
        probability = (
            recent_probability * (1 - related_weight)
            + related_probability * related_weight
        )
        return {
            'probability': probability,
            'recent': {
                'correct_steps': recent_correct,
                'total_steps': recent_total,
                'probability': recent_probability,
            },
            'related': {
                'log_ids': related_log_ids,
                'correct_steps': related_correct,
                'total_steps': related_total,
                'probability': related_probability,
                'weight': related_weight,
            },
        }

    @staticmethod
    def estimate_step_probabilities(exercise, before, fallback):
        """이번 풀이 전 개념 이력으로 예측한다. 현재 숙련도 캐시는 소급 오염을 피하려고 쓰지 않는다."""
        results = [
            {'source': 'heuristic', 'probability': fallback}
            for _ in exercise.content.get('steps', [])
        ]
        active_ids = set(Concept.objects.filter(is_active=True).values_list('pk', flat=True))
        approved = list(BKTService.approved_steps(exercise, active_ids))
        concept_ids = {step['concept_id'] for _, step in approved}
        latest = {}
        for observation in BKTObservation.objects.filter(
            concept_id__in=concept_ids, observed_at__lt=before,
        ).order_by('-observed_at', '-attempt_id', '-step_index'):
            latest.setdefault(observation.concept_id, observation)
        parameters = asdict(DEFAULT_PARAMETERS)
        dirty_ids = pending_concept_ids()
        for index, step in approved:
            if step['concept_id'] in dirty_ids:
                results[index]['reason'] = 'bkt_rebuild_required'
                continue
            observation = latest.get(step['concept_id'])
            if observation and (
                observation.model_version != MODEL_VERSION or observation.parameters != parameters
            ):
                results[index]['reason'] = 'bkt_rebuild_required'
                continue
            mastery = observation.mastery_after if observation else DEFAULT_PARAMETERS.initial_mastery
            results[index] = {
                'source': 'bkt', 'concept_id': step['concept_id'],
                'mastery': mastery,
                'probability': mastery * (1 - DEFAULT_PARAMETERS.slip) + (1 - mastery) * DEFAULT_PARAMETERS.guess,
                'observation_id': observation.pk if observation else None,
                'parameters': parameters, 'model_version': MODEL_VERSION,
            }
        return results

    def _request_choice_judgements(self, exercise):
        public_content = self.public_exercise_content(exercise)
        prompt = textwrap.dedent(f"""
            다음은 학습자가 푸는 경로 추적 문제입니다.
            정답을 맞히려고 하되, 각 선택지가 정답일 가능성을 독립적으로 판단하세요.

            문제:
            {json.dumps(public_content, ensure_ascii=False)}

            JSON으로만 응답하세요 (```없이):
            {{
              "steps": [
                {{
                  "probabilities": [0.1, 0.6, 0.2, 0.1]
                }}
              ]
            }}

            규칙:
            - steps 수는 문제의 steps 수와 같아야 합니다.
            - probabilities의 길이는 해당 choices 수와 같아야 합니다.
            - probabilities는 0 이상이고 합이 1에 가깝게 작성하세요.
            - 다른 문제의 정답이나 사용자의 수준을 추측하지 마세요.
        """).strip()
        response = self.client.chat.completions.create(
            model=self.MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    @staticmethod
    def _normalize_probabilities(values, choice_count):
        if not isinstance(values, list) or len(values) != choice_count:
            return [1 / choice_count] * choice_count
        try:
            cleaned = [max(0.0, float(value)) for value in values]
        except (TypeError, ValueError):
            return [1 / choice_count] * choice_count
        total = sum(cleaned)
        if total <= 0:
            return [1 / choice_count] * choice_count
        return [value / total for value in cleaned]

    @classmethod
    def calibrate_distribution(cls, raw, correct_index, user_probability, *, bkt=False):
        """BKT는 예측 확률 그대로, 그 외에는 사용자 수준 80% + LLM 판단 20%.

        오답으로 갈 확률은 LLM이 판단한 오답별 그럴듯함에 비례해 나눈다.
        """
        raw_correct = raw[correct_index]
        correct_probability = user_probability if bkt else (
            cls.USER_LEVEL_WEIGHT * user_probability
            + (1 - cls.USER_LEVEL_WEIGHT) * raw_correct
        )
        correct_probability = min(
            cls.MAX_STEP_PROBABILITY,
            max(cls.MIN_STEP_PROBABILITY, correct_probability),
        )

        wrong_total = sum(value for i, value in enumerate(raw) if i != correct_index)
        wrong_probability = 1 - correct_probability
        adjusted = []
        for index, value in enumerate(raw):
            if index == correct_index:
                adjusted.append(correct_probability)
            elif wrong_total > 0:
                adjusted.append(wrong_probability * value / wrong_total)
            else:
                adjusted.append(wrong_probability / (len(raw) - 1))
        return adjusted

    def solve_attempt(self, mate_attempt):
        self.refresh_status(mate_attempt)
        started_at = timezone.now()
        claimed = MateAttempt.objects.filter(
            pk=mate_attempt.pk,
            status='pending',
            created_at__gt=started_at - timezone.timedelta(seconds=self.STATE_TIMEOUT_SECONDS),
        ).update(status='solving', started_at=started_at, error_message='')
        if not claimed:
            return self.refresh_status(mate_attempt)

        exercise = mate_attempt.exercise
        try:
            if exercise.exercise_type != 'path_trace':
                raise ValueError("러닝메이트는 path_trace만 풀 수 있습니다.")

            ability = self.estimate_user_probability(exercise, before=mate_attempt.created_at)
            step_abilities = self.estimate_step_probabilities(
                exercise, mate_attempt.created_at, ability['probability'],
            )
            judgement = self._request_choice_judgements(exercise)
            judgement_steps = judgement.get('steps', [])
            exercise_steps = exercise.content.get('steps', [])
            if len(judgement_steps) != len(exercise_steps):
                raise ValueError("러닝메이트 응답의 step 수가 다릅니다.")

            selected_indices = []
            selected_confidence = []
            distributions = []
            for step, judged, step_ability in zip(exercise_steps, judgement_steps, step_abilities):
                choices = step.get('choices', [])
                if len(choices) < 2:
                    raise ValueError("선택지가 2개 미만인 step이 있습니다.")
                raw = self._normalize_probabilities(
                    judged.get('probabilities'), len(choices)
                )
                correct_index = step['correct_index']
                adjusted = self.calibrate_distribution(
                    raw, correct_index, step_ability['probability'],
                    bkt=step_ability['source'] == 'bkt',
                )
                selected = random.choices(
                    range(len(choices)), weights=adjusted, k=1
                )[0]
                selected_indices.append(selected)
                selected_confidence.append(adjusted[selected])
                distributions.append({
                    'raw': raw,
                    'adjusted': adjusted,
                })

            answer = {
                'selected_indices': selected_indices,
                'confidence': selected_confidence,
            }
            evaluation = ExerciseService.__new__(ExerciseService).evaluate_attempt(
                exercise, answer
            )
            mate_attempt.answer = answer
            mate_attempt.score = evaluation['score']
            mate_attempt.is_correct = evaluation['is_correct']
            mate_attempt.context_snapshot = {
                'ability': ability,
                'step_abilities': step_abilities,
                'ability_cutoff': mate_attempt.created_at.isoformat(),
                'calibration_version': 'bkt-v1',
                'distributions': distributions,
            }
            mate_attempt.model_name = self.MODEL
            mate_attempt.prompt_version = self.PROMPT_VERSION
            mate_attempt.status = 'ready'
            mate_attempt.answered_at = timezone.now()
            # 실패로 정리됐거나 다른 실행으로 바뀐 기록을 늦은 응답이 덮어쓰지 않는다.
            MateAttempt.objects.filter(
                pk=mate_attempt.pk, status='solving', started_at=started_at,
            ).update(**{field: getattr(mate_attempt, field) for field in (
                'answer', 'score', 'is_correct', 'context_snapshot',
                'model_name', 'prompt_version', 'status', 'answered_at',
            )})
        except Exception as exc:
            logger.exception(
                "러닝메이트 풀이 실패: mate_attempt_id=%s exercise_id=%s",
                mate_attempt.pk,
                exercise.pk,
            )
            MateAttempt.objects.filter(
                pk=mate_attempt.pk, status='solving', started_at=started_at,
            ).update(status='failed', error_message=str(exc), answered_at=timezone.now())
        mate_attempt.refresh_from_db()
        return mate_attempt

    @staticmethod
    def comparison_rows(mate_attempt):
        if not mate_attempt.user_attempt_id or mate_attempt.status != 'ready':
            return []
        exercise = mate_attempt.exercise
        user_selected = mate_attempt.user_attempt.user_answer.get('selected_indices', [])
        mate_selected = mate_attempt.answer.get('selected_indices', [])
        rows = []
        for index, step in enumerate(exercise.content.get('steps', [])):
            choices = step.get('choices', [])
            user_index = user_selected[index] if index < len(user_selected) else None
            mate_index = mate_selected[index] if index < len(mate_selected) else None
            correct_index = step.get('correct_index')
            rows.append({
                'number': index + 1,
                'user_choice': choices[user_index] if isinstance(user_index, int) and 0 <= user_index < len(choices) else '미응답',
                'mate_choice': choices[mate_index] if isinstance(mate_index, int) and 0 <= mate_index < len(choices) else '미응답',
                'user_correct': user_index == correct_index,
                'mate_correct': mate_index == correct_index,
            })
        return rows
