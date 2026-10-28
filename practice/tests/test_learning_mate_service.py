"""러닝메이트의 수준 추정, 확률 보정, 풀이 저장 테스트."""

import random

import pytest

from practice.models import Exercise, ExerciseAttempt, MateAttempt
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
