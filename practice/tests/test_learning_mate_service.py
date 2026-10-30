"""러닝메이트의 수준 추정, 확률 보정, 풀이 저장 테스트."""

import random
from dataclasses import asdict

import pytest

from practice.models import BKTObservation, Concept, ConceptMastery, Exercise, ExerciseAttempt, MateAttempt
from practice.services.bkt_math import DEFAULT_PARAMETERS, MODEL_VERSION
from practice.services.bkt_service import BKTService
from practice.services.learning_mate_service import LearningMateService

from search.tests.factories import LearningLogFactory


pytestmark = pytest.mark.django_db


def path_content():
    return {
        "scenario": "요청이 서비스와 저장소를 차례로 통과한다.",
        "steps": [
            {
                "question": f"Step {index + 1}에서 호출되는 계층은?",
                "choices": ["View", "Service", "Repository", "Database"],
                "correct_index": index,
                "correct_answer": ["View", "Service", "Repository"][index],
                "explanation": f"Step {index + 1} 해설",
                "distractors": [
                    {"index": wrong, "type": "adjacent", "why": "인접 계층과 헷갈렸다."}
                    for wrong in range(4) if wrong != index
                ],
            }
            for index in range(3)
        ],
    }


def make_exercise():
    return Exercise.objects.create(
        learning_log=LearningLogFactory(),
        exercise_type="path_trace",
        content=path_content(),
    )


class TestAbilityEstimation:
    def test_starts_at_neutral_probability_without_history(self):
        exercise = make_exercise()
        service = LearningMateService.__new__(LearningMateService)

        result = service.estimate_user_probability(exercise)

        assert result["probability"] == pytest.approx(0.5)
        assert result["recent"]["total_steps"] == 0
        assert result["related"]["total_steps"] == 0

    def test_uses_step_level_attempt_history(self):
        exercise = make_exercise()
        ExerciseAttempt.objects.create(
            exercise=exercise,
            user_answer={"selected_indices": [0, 0, 2]},
            score=2 / 3,
            is_correct=True,
        )
        service = LearningMateService.__new__(LearningMateService)

        result = service.estimate_user_probability(exercise)

        # Beta(2, 2): (2개 정답 + 2) / (3 step + 4)
        assert result["probability"] == pytest.approx(4 / 7)
        assert result["recent"]["correct_steps"] == 2
        assert result["related"]["correct_steps"] == 2


class TestChoiceCalibration:
    def test_public_content_does_not_expose_answer_metadata(self):
        exercise = make_exercise()

        public = LearningMateService.public_exercise_content(exercise)

        step = public["steps"][0]
        assert set(step) == {"question", "choices"}
        assert "correct_index" not in step
        assert "explanation" not in step
        assert "distractors" not in step

    def test_calibrates_correct_probability_and_preserves_wrong_ratios(self):
        adjusted = LearningMateService.calibrate_distribution(
            raw=[0.1, 0.6, 0.2, 0.1],
            correct_index=1,
            user_probability=0.5,
        )

        assert adjusted == pytest.approx([0.12, 0.52, 0.24, 0.12])
        assert sum(adjusted) == pytest.approx(1)


class TestMateSolving:
    def test_solves_grades_and_saves_attempt(self, monkeypatch):
        exercise = make_exercise()
        mate_attempt = MateAttempt.objects.create(exercise=exercise)
        service = LearningMateService.__new__(LearningMateService)
        monkeypatch.setattr(
            service,
            "_request_choice_judgements",
            lambda current: {
                "steps": [
                    {
                        "probabilities": [0.4, 0.3, 0.2, 0.1],
                    }
                    for _ in range(3)
                ]
            },
        )
        monkeypatch.setattr(
            random,
            "choices",
            lambda population, weights, k: [weights.index(max(weights))],
        )

        result = service.solve_attempt(mate_attempt)

        assert result.status == "ready"
        assert result.answer["selected_indices"] == [0, 1, 2]
        assert result.score == 1
        assert result.is_correct is True
        assert result.context_snapshot["ability"]["probability"] == pytest.approx(0.5)
        assert result.model_name == LearningMateService.MODEL

    def test_comparison_uses_same_choices_as_exercise(self):
        exercise = make_exercise()
        user_attempt = ExerciseAttempt.objects.create(
            exercise=exercise,
            user_answer={"selected_indices": [0, 0, 2]},
            score=2 / 3,
            is_correct=True,
        )
        mate_attempt = MateAttempt.objects.create(
            exercise=exercise,
            user_attempt=user_attempt,
            status="ready",
            answer={"selected_indices": [1, 1, 2]},
            score=2 / 3,
            is_correct=True,
        )

        rows = LearningMateService.comparison_rows(mate_attempt)

        assert rows[0]["user_choice"] == "View"
        assert rows[0]["mate_choice"] == "Service"
        assert rows[0]["user_correct"] is True
        assert rows[0]["mate_correct"] is False


class TestBKTCalibration:
    def approved_exercise(self):
        exercise = make_exercise()
        concept = Concept.objects.create(id='test.flow', name='호출 흐름')
        for step in exercise.content['steps']:
            step.update(concept_id=concept.pk, concept_candidate=None,
                        _concept_review={'status': 'approved'})
        exercise.save(update_fields=['content'])
        return exercise

    def test_approved_concept_without_history_uses_initial_prior(self):
        exercise = self.approved_exercise()
        mate = MateAttempt.objects.create(exercise=exercise)
        result = LearningMateService.estimate_step_probabilities(exercise, mate.created_at, 0.5)

        assert len(result) == 3
        assert all(step['source'] == 'bkt' for step in result)
        assert result[0]['mastery'] == 0.3
        assert result[0]['probability'] == pytest.approx(0.445)
        assert result[0]['observation_id'] is None
        assert result[0]['parameters'] == asdict(DEFAULT_PARAMETERS)

    def test_uses_last_prior_observation_not_current_user_result(self):
        exercise = self.approved_exercise()
        previous = ExerciseAttempt.objects.create(
            exercise=exercise, user_answer={'selected_indices': [0]}, score=1,
        )
        BKTService.record_attempt(previous)
        observation = previous.bkt_observations.get()
        mate = MateAttempt.objects.create(exercise=exercise)
        current = ExerciseAttempt.objects.create(
            exercise=exercise, user_answer={'selected_indices': [1, 0, 0]}, score=0,
        )
        BKTService.record_attempt(current)
        service = LearningMateService.__new__(LearningMateService)

        result = service.estimate_step_probabilities(exercise, mate.created_at, 0.5)
        assert all(step['observation_id'] == observation.pk for step in result)
        assert result[0]['mastery'] == pytest.approx(observation.mastery_after)
        assert result[0]['probability'] == pytest.approx(observation.mastery_after * .9 + (1 - observation.mastery_after) * .25)
        fallback = service.estimate_user_probability(exercise, before=mate.created_at)
        assert fallback['recent']['total_steps'] == 1
        assert fallback['recent']['correct_steps'] == 1

    def test_mixed_review_states_and_inactive_concepts_fall_back(self):
        exercise = self.approved_exercise()
        exercise.content['steps'][1]['_concept_review']['status'] = 'on_hold'
        exercise.content['steps'][2]['concept_candidate'] = {'name': '미등록'}
        mate = MateAttempt.objects.create(exercise=exercise)

        result = LearningMateService.estimate_step_probabilities(exercise, mate.created_at, 0.6)
        assert [step['source'] for step in result] == ['bkt', 'heuristic', 'heuristic']
        assert result[1]['probability'] == 0.6
        Concept.objects.update(is_active=False)
        assert all(step['source'] == 'heuristic' for step in LearningMateService.estimate_step_probabilities(exercise, mate.created_at, 0.6))

    def test_each_concept_uses_its_own_mastery(self):
        exercise = self.approved_exercise()
        other = Concept.objects.create(id='test.other', name='다른 개념')
        exercise.content['steps'][1]['concept_id'] = other.pk
        exercise.save(update_fields=['content'])
        previous = ExerciseAttempt.objects.create(
            exercise=exercise, user_answer={'selected_indices': [0, 0]}, score=.5,
        )
        BKTService.record_attempt(previous)
        mate = MateAttempt.objects.create(exercise=exercise)

        result = LearningMateService.estimate_step_probabilities(exercise, mate.created_at, 0.5)
        assert result[0]['mastery'] == pytest.approx(.6460674157)
        assert result[1]['mastery'] == pytest.approx(.1486486486)
        assert result[2]['mastery'] == result[0]['mastery']
        assert result[0]['probability'] > result[1]['probability']

    def test_stale_parameters_fall_back_instead_of_mixing_versions(self):
        exercise = self.approved_exercise()
        previous = ExerciseAttempt.objects.create(
            exercise=exercise, user_answer={'selected_indices': [0]}, score=1,
        )
        BKTService.record_attempt(previous)
        previous.bkt_observations.update(model_version='old-version')
        mate = MateAttempt.objects.create(exercise=exercise)

        result = LearningMateService.estimate_step_probabilities(exercise, mate.created_at, 0.5)
        assert result[0] == {'source': 'heuristic', 'probability': 0.5, 'reason': 'bkt_rebuild_required'}

    def test_bkt_probability_is_not_blended_with_llm(self):
        for raw in ([0.01, 0.97, 0.01, 0.01], [1, 0, 0, 0]):
            adjusted = LearningMateService.calibrate_distribution(raw, 0, .67, bkt=True)
            assert adjusted[0] == pytest.approx(.67)
            assert sum(adjusted) == pytest.approx(1)

    def test_solve_records_per_step_bkt_audit_without_updating_user_state(self, monkeypatch):
        exercise = self.approved_exercise()
        mate = MateAttempt.objects.create(exercise=exercise)
        service = LearningMateService.__new__(LearningMateService)
        monkeypatch.setattr(service, '_request_choice_judgements', lambda exercise: {
            'steps': [{'probabilities': [.25] * 4} for _ in range(3)],
        })

        result = service.solve_attempt(mate)
        assert result.status == 'ready'
        snapshot = result.context_snapshot
        assert snapshot['ability_cutoff'] == mate.created_at.isoformat()
        assert snapshot['step_abilities'][0]['model_version'] == MODEL_VERSION
        assert snapshot['distributions'][0]['adjusted'][0] == pytest.approx(.445)
        assert not ExerciseAttempt.objects.exists()
        assert not ConceptMastery.objects.exists()
        assert not BKTObservation.objects.exists()
