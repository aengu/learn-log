"""ExerciseService의 문제 검증, 채점, 복습 주기 테스트."""

from copy import deepcopy

import pytest
from django.utils import timezone

from search.models import Exercise
from search.services.exercise_service import ExerciseService

from .factories import LearningLogFactory


pytestmark = pytest.mark.django_db


def make_step(correct_index=0, choices=None):
    """품질 게이트를 통과하는 기본 경로 추적 step."""
    choices = choices or ["정답", "인접 개념", "핵심 누락", "반대 방향"]
    types = ["adjacent", "one-step-short", "inverted"]
    wrong_indices = [i for i in range(len(choices)) if i != correct_index]
    return {
        "question": "실행 결과는 무엇인가?",
        "choices": choices,
        "correct_index": correct_index,
        "correct_answer": choices[correct_index],
        "explanation": "정답 해설",
        "distractors": [
            {"index": index, "type": types[offset], "why": "오해한 이유"}
            for offset, index in enumerate(wrong_indices)
        ],
    }


def make_exercise(exercise_type, content, **kwargs):
    return Exercise.objects.create(
        learning_log=LearningLogFactory(),
        exercise_type=exercise_type,
        content=content,
        **kwargs,
    )


class TestPathTraceValidation:
    def test_drops_step_when_correct_index_does_not_match_answer(self):
        valid = make_step()
        invalid = make_step()
        invalid["correct_answer"] = "선택지에 없는 답"

        result = ExerciseService._filter_valid_steps({"steps": [valid, invalid]})

        assert result["steps"] == [valid]
        assert result["_audit"] == {
            "raw_count": 2,
            "dropped": [{"reason": "correct-index-mismatch"}],
        }

    @pytest.mark.parametrize(
        ("mutate", "expected_reason"),
        [
            (
                lambda step: step["choices"].__setitem__(1, "위의 모든 것"),
                "banned-choice",
            ),
            (
                lambda step: step["distractors"].__setitem__(
                    1, {"index": 1, "type": "inverted", "why": "중복 인덱스"}
                ),
                "distractor-index-mismatch",
            ),
            (
                lambda step: step["distractors"][0].__setitem__("why", ""),
                "missing-distractor-why",
            ),
            (
                lambda step: step["distractors"][0].__setitem__("type", "unknown"),
                "unknown-distractor-type",
            ),
        ],
    )
    def test_drops_step_with_hard_quality_violation(self, mutate, expected_reason):
        step = make_step()
        mutate(step)

        result = ExerciseService._filter_valid_steps({"steps": [step]})

        assert result["steps"] == []
        assert expected_reason in result["_audit"]["dropped"][0]["reason"]

    def test_keeps_soft_violations_as_flags(self):
        choices = [
            "조건을 확인한 뒤 요청을 처리하고 결과를 저장한다",
            "항상 실패",
            "절대 중단",
            "모든 요청 거부",
        ]
        steps = [make_step(choices=deepcopy(choices)) for _ in range(3)]

        result = ExerciseService._filter_valid_steps({"steps": steps})

        assert len(result["steps"]) == 3
        for step in result["steps"]:
            assert set(step["_quality_flags"]) == {
                "length-bias",
                "absolute-word-skew",
                "correct-index-fixed",
            }

    def test_retries_and_uses_attempt_with_more_valid_steps(self, monkeypatch):
        service = ExerciseService.__new__(ExerciseService)
        first = {"scenario": "첫 시도", "steps": [make_step()], "_audit": {"dropped": [1]}}
        retry = {
            "scenario": "재시도",
            "steps": [make_step(i) for i in range(3)],
            "_audit": {"dropped": []},
        }
        attempts = iter([(first, 2), (retry, 3)])
        monkeypatch.setattr(service, "_gen_and_validate", lambda prompt: next(attempts))

        result = service._gen_path_trace(LearningLogFactory())

        assert result["scenario"] == "재시도"
        assert result["_audit"]["attempts"] == [
            {"dropped": [1]},
            {"dropped": []},
        ]

    def test_raises_when_retry_still_has_too_few_valid_steps(self, monkeypatch):
        service = ExerciseService.__new__(ExerciseService)
        attempts = iter([
            ({"steps": [make_step()], "_audit": {"dropped": [1]}}, 2),
            ({"steps": [make_step()], "_audit": {"dropped": [2]}}, 2),
        ])
        monkeypatch.setattr(service, "_gen_and_validate", lambda prompt: next(attempts))

        with pytest.raises(ValueError, match="유효한 step이 1개"):
            service._gen_path_trace(LearningLogFactory())


class TestExerciseEvaluation:
    def test_evaluates_path_trace_and_builds_step_feedback(self):
        exercise = make_exercise(
            "path_trace",
            {"steps": [make_step(0), make_step(1), make_step(2)]},
        )
        service = ExerciseService.__new__(ExerciseService)

        result = service.evaluate_attempt(
            exercise,
            {"selected_indices": [0, 0, 2]},
        )

        assert result["is_correct"] is True
        assert result["score"] == pytest.approx(2 / 3)
        assert result["ai_feedback"].splitlines()[0].startswith("✅ Step 1")
        assert result["ai_feedback"].splitlines()[1].startswith("❌ Step 2")

    def test_self_marked_score_counts_each_valid_index_once(self):
        exercise = make_exercise(
            "generation_compare",
            {"key_points": ["A", "B", "C"]},
        )
        service = ExerciseService.__new__(ExerciseService)

        result = service.evaluate_attempt(
            exercise,
            {
                "covered_indices": [0, 0, 2, -1, 99],
                "reflection": "B를 다시 공부해야 한다.",
            },
        )

        assert result == {
            "is_correct": True,
            "score": pytest.approx(2 / 3),
            "ai_feedback": "B를 다시 공부해야 한다.",
        }

    def test_self_marked_score_is_zero_when_there_are_no_key_points(self):
        exercise = make_exercise("generation_compare", {"key_points": []})
        service = ExerciseService.__new__(ExerciseService)

        result = service.evaluate_attempt(exercise, {"covered_indices": [0]})

        assert result["is_correct"] is False
        assert result["score"] == 0


class TestExercisePersistence:
    def test_generate_exercise_saves_generated_content(self, monkeypatch):
        service = ExerciseService.__new__(ExerciseService)
        content = {"question": "설명해 보세요", "key_points": ["핵심"]}
        monkeypatch.setattr(service, "_generate_content", lambda log, kind: content)
        log = LearningLogFactory()

        exercise = service.generate_exercise(log, "generation_compare")

        assert exercise.learning_log == log
        assert exercise.exercise_type == "generation_compare"
        assert exercise.content == content

    def test_correct_attempt_advances_review_interval(self):
        exercise = make_exercise("generation_compare", {"key_points": ["핵심"]})
        service = ExerciseService.__new__(ExerciseService)
        evaluation = {"is_correct": True, "score": 1.0, "ai_feedback": "잘했어요"}

        attempt = service.save_attempt(exercise, {"covered_indices": [0]}, evaluation)

        exercise.refresh_from_db()
        assert attempt.exercise == exercise
        assert attempt.is_correct is True
        assert exercise.review_interval == 3
        assert exercise.next_review_at > timezone.now()

    def test_incorrect_attempt_resets_review_interval(self):
        exercise = make_exercise(
            "generation_compare",
            {"key_points": ["핵심"]},
            review_interval=7,
        )
        service = ExerciseService.__new__(ExerciseService)
        evaluation = {"is_correct": False, "score": 0.0, "ai_feedback": "다시 보기"}

        service.save_attempt(exercise, {"covered_indices": []}, evaluation)

        exercise.refresh_from_db()
        assert exercise.review_interval == 1
        assert exercise.next_review_at > timezone.now()

    def test_get_due_exercises_excludes_future_reviews(self):
        never_scheduled = make_exercise("generation_compare", {})
        past_due = make_exercise(
            "generation_compare",
            {},
            next_review_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        future = make_exercise(
            "generation_compare",
            {},
            next_review_at=timezone.now() + timezone.timedelta(days=1),
        )

        due_ids = set(ExerciseService.get_due_exercises().values_list("id", flat=True))

        assert never_scheduled.id in due_ids
        assert past_due.id in due_ids
        assert future.id not in due_ids
