"""연습문제 페이지와 HTMX API의 통합 테스트."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from practice.models import BKTObservation, Concept, ConceptMastery, Exercise, ExerciseAttempt, MateAttempt
from practice.services import ExerciseService, LearningMateService

from search.tests.factories import LearningLogFactory


pytestmark = pytest.mark.django_db


def generation_content():
    return {
        "question": "트랜잭션 격리 수준을 설명해 보세요.",
        "model_answer": "동시 실행되는 트랜잭션 사이의 가시성을 제어합니다.",
        "key_points": ["가시성", "동시성", "격리 수준"],
    }


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
            }
            for index in range(3)
        ],
    }


def make_exercise(exercise_type="generation_compare", content=None, **kwargs):
    if content is None:
        content = generation_content() if exercise_type == "generation_compare" else path_content()
    return Exercise.objects.create(
        learning_log=LearningLogFactory(),
        exercise_type=exercise_type,
        content=content,
        **kwargs,
    )


class TestExercisePages:
    def test_list_shows_only_due_exercises(self, client):
        due = make_exercise()
        future = make_exercise(
            next_review_at=timezone.now() + timezone.timedelta(days=1),
        )

        response = client.get(reverse("practice:exercise_list"))

        assert response.status_code == 200
        assert due.learning_log.query.encode() in response.content
        assert future.learning_log.query.encode() not in response.content

    def test_path_detail_shuffles_display_only(self, client, monkeypatch):
        exercise = make_exercise("path_trace")
        original_content = json.loads(json.dumps(exercise.content))
        monkeypatch.setattr("practice.views.random.shuffle", lambda pairs: pairs.reverse())

        response = client.get(reverse("practice:exercise_detail", args=[exercise.pk]))

        assert response.status_code == 200
        assert "{# pair" not in response.content.decode()
        rendered_exercise = response.context["exercise"]
        assert rendered_exercise.content["steps"][0]["shuffled"] == [
            (3, "Database"),
            (2, "Repository"),
            (1, "Service"),
            (0, "View"),
        ]
        exercise.refresh_from_db()
        assert exercise.content == original_content
        assert MateAttempt.objects.filter(exercise=exercise, status="pending").count() == 1

    def test_generation_detail_does_not_add_shuffled_choices(self, client):
        exercise = make_exercise()

        response = client.get(reverse("practice:exercise_detail", args=[exercise.pk]))

        assert response.status_code == 200
        assert "트랜잭션 격리 수준" in response.content.decode()
        assert "shuffled" not in response.context["exercise"].content

    def test_detail_returns_404_for_missing_exercise(self, client):
        response = client.get(reverse("practice:exercise_detail", args=[999999]))

        assert response.status_code == 404


class TestExerciseGenerateAPI:
    def test_rejects_unknown_exercise_type(self, client):
        log = LearningLogFactory()

        response = client.post(
            reverse("practice:exercise_generate", args=[log.pk]),
            {"exercise_type": "unknown"},
        )

        assert response.status_code == 200
        assert "유효하지 않은 유형" in response.content.decode()
        assert Exercise.objects.count() == 0

    def test_returns_error_for_missing_learning_log(self, client):
        response = client.post(
            reverse("practice:exercise_generate", args=[999999]),
            {"exercise_type": "generation_compare"},
        )

        assert response.status_code == 200
        assert "학습 로그를 찾을 수 없습니다" in response.content.decode()

    def test_generates_exercise_and_returns_detail_link(self, client, monkeypatch):
        log = LearningLogFactory()

        def fake_generate(service, learning_log, exercise_type):
            return Exercise.objects.create(
                learning_log=learning_log,
                exercise_type=exercise_type,
                content=generation_content(),
            )

        monkeypatch.setattr(ExerciseService, "generate_exercise", fake_generate)

        response = client.post(
            reverse("practice:exercise_generate", args=[log.pk]),
            {"exercise_type": "generation_compare"},
        )

        exercise = Exercise.objects.get()
        assert response.status_code == 200
        assert reverse("practice:exercise_detail", args=[exercise.pk]) in response.content.decode()
        assert exercise.learning_log == log

    def test_returns_generation_error(self, client, monkeypatch):
        log = LearningLogFactory()

        def fail_generate(*args, **kwargs):
            raise ValueError("유효한 step 부족")

        monkeypatch.setattr(ExerciseService, "generate_exercise", fail_generate)

        response = client.post(
            reverse("practice:exercise_generate", args=[log.pk]),
            {"exercise_type": "path_trace"},
        )

        assert response.status_code == 200
        assert "생성 오류: 유효한 step 부족" in response.content.decode()


class TestExerciseAttemptAPI:
    def test_returns_error_for_missing_exercise(self, client):
        response = client.post(reverse("practice:exercise_attempt", args=[999999]))

        assert response.status_code == 200
        assert "연습문제를 찾을 수 없습니다" in response.content.decode()

    def test_rejects_invalid_path_trace_json(self, client):
        exercise = make_exercise("path_trace")

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {"selected_indices": "not-json"},
        )

        assert response.status_code == 200
        assert "잘못된 답변 형식" in response.content.decode()
        assert ExerciseAttempt.objects.count() == 0

    def test_path_trace_finalizes_and_saves_attempt(self, client):
        exercise = make_exercise("path_trace")

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {"selected_indices": json.dumps([0, 0, 2])},
        )

        attempt = ExerciseAttempt.objects.get()
        exercise.refresh_from_db()
        assert response.status_code == 200
        assert "통과!" in response.content.decode()
        assert attempt.user_answer == {"selected_indices": [0, 0, 2]}
        assert attempt.score == pytest.approx(2 / 3)
        assert exercise.review_interval == 3

    def test_path_trace_links_the_mate_attempt(self, client):
        exercise = make_exercise("path_trace")
        mate_attempt = MateAttempt.objects.create(
            exercise=exercise,
            status="ready",
            answer={"selected_indices": [0, 1, 2]},
            score=1,
            is_correct=True,
        )

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {
                "selected_indices": json.dumps([0, 0, 2]),
                "mate_attempt_id": str(mate_attempt.pk),
            },
        )

        mate_attempt.refresh_from_db()
        assert response.status_code == 200
        assert mate_attempt.user_attempt is not None
        assert "러닝메이트" in response.content.decode()
        assert "100점" in response.content.decode()

    def test_repeated_path_submission_returns_the_original_duel(self, client):
        exercise = make_exercise("path_trace")
        mate_attempt = MateAttempt.objects.create(
            exercise=exercise,
            status="ready",
            answer={"selected_indices": [0, 1, 2]},
            score=1,
            is_correct=True,
        )
        url = reverse("practice:exercise_attempt", args=[exercise.pk])
        payload = {
            "selected_indices": json.dumps([0, 0, 2]),
            "mate_attempt_id": str(mate_attempt.pk),
        }

        first = client.post(url, payload)
        with CaptureQueriesContext(connection) as queries:
            second = client.post(url, payload)

        mate_attempt.refresh_from_db()
        assert first.status_code == 200
        assert second.status_code == 200
        assert ExerciseAttempt.objects.filter(exercise=exercise).count() == 1
        assert "러닝메이트" in second.content.decode()
        assert mate_attempt.user_attempt_id is not None
        # 기존 결과를 반환하는 경로는 BKT 공통 잠금까지 획득하지 않는다.
        assert not any('practice_bktlock' in row['sql'].lower() for row in queries)

    def test_new_submission_acquires_the_common_lock_only_once(self, client):
        exercise = make_exercise('path_trace')
        mate = MateAttempt.objects.create(exercise=exercise)
        with CaptureQueriesContext(connection) as queries:
            response = client.post(reverse('practice:exercise_attempt', args=[exercise.pk]), {
                'selected_indices': '[0, 1, 2]', 'mate_attempt_id': str(mate.pk),
            })
        assert '통과!' in response.content.decode()
        locks = [row['sql'] for row in queries
                 if 'practice_bktlock' in row['sql'].lower() and 'FOR UPDATE' in row['sql']]
        assert len(locks) == 1

    def test_link_failure_rolls_back_attempt_review_and_bkt(self, client, monkeypatch):
        concept = Concept.objects.create(id='test.link', name='연결 개념')
        content = path_content()
        for step in content['steps']:
            step.update(concept_id=concept.pk, concept_candidate=None, _concept_review={'status': 'approved'})
        exercise = make_exercise('path_trace', content=content)
        mate = MateAttempt.objects.create(exercise=exercise)
        original_save = MateAttempt.save

        def fail_link(instance, *args, **kwargs):
            if kwargs.get('update_fields') == ['user_attempt']:
                raise RuntimeError('대결 연결 실패')
            return original_save(instance, *args, **kwargs)

        monkeypatch.setattr(MateAttempt, 'save', fail_link)
        response = client.post(reverse('practice:exercise_attempt', args=[exercise.pk]), {
            'selected_indices': '[0, 1, 2]', 'mate_attempt_id': str(mate.pk),
        })
        assert '채점 오류: 대결 연결 실패' in response.content.decode()
        assert not ExerciseAttempt.objects.filter(exercise=exercise).exists()
        assert not BKTObservation.objects.exists()
        assert not ConceptMastery.objects.exists()
        exercise.refresh_from_db()
        mate.refresh_from_db()
        assert exercise.review_interval == 1 and mate.user_attempt_id is None

    def test_reveal_returns_answer_and_model_answer(self, client):
        exercise = make_exercise()

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {"stage": "reveal", "answer": "격리 수준은 동시성을 제어한다."},
        )

        body = response.content.decode()
        assert response.status_code == 200
        assert "내가 쓴 답" in body
        assert "격리 수준은 동시성을 제어한다" in body
        assert exercise.content["model_answer"] in body
        assert ExerciseAttempt.objects.count() == 0

    def test_reflect_filters_invalid_covered_indices(self, client):
        exercise = make_exercise()

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {
                "stage": "reflect",
                "text": "내 답변",
                "covered": ["0", "2", "bad", "99", "-1"],
            },
        )

        assert response.status_code == 200
        assert response.context["covered_indices"] == [0, 2]
        assert response.context["covered_set"] == {0, 2}
        assert "회고 작성" in response.content.decode()
        assert ExerciseAttempt.objects.count() == 0

    def test_generation_compare_finalizes_with_reflection(self, client):
        exercise = make_exercise()
        reflection = "격리 수준별 차이와 동시성 제어 방식을 다시 확인해야겠다."

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {
                "stage": "final",
                "text": "내 답변",
                "covered": ["0", "2"],
                "reflection": reflection,
            },
        )

        attempt = ExerciseAttempt.objects.get()
        assert response.status_code == 200
        assert "통과!" in response.content.decode()
        assert attempt.user_answer == {
            "text": "내 답변",
            "covered_indices": [0, 2],
            "reflection": reflection,
        }
        assert attempt.score == pytest.approx(2 / 3)
        assert attempt.ai_feedback == reflection

    def test_returns_grading_error(self, client, monkeypatch):
        exercise = make_exercise()

        def fail_evaluate(*args, **kwargs):
            raise RuntimeError("채점기 오류")

        monkeypatch.setattr(ExerciseService, "evaluate_attempt", fail_evaluate)

        response = client.post(
            reverse("practice:exercise_attempt", args=[exercise.pk]),
            {"stage": "final"},
        )

        assert response.status_code == 200
        assert "채점 오류: 채점기 오류" in response.content.decode()
        assert ExerciseAttempt.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_duel_submissions_save_only_one_attempt(monkeypatch):
    """저장과 대결 연결 사이에 두 번째 요청을 넣어 실제 행 잠금 대기를 검증한다."""
    if connection.vendor != 'postgresql':
        pytest.skip('실제 PostgreSQL 행 잠금 검증')
    concept = Concept.objects.create(id='test.duel', name='대결 개념')
    content = path_content()
    for step in content['steps']:
        step.update(concept_id=concept.pk, concept_candidate=None, _concept_review={'status': 'approved'})
    exercise = make_exercise('path_trace', content=content)
    mate = MateAttempt.objects.create(exercise=exercise)
    url = reverse('practice:exercise_attempt', args=[exercise.pk])
    saved, release, started = Event(), Event(), Event()
    second_pid, calls = [], []
    original_save = ExerciseService.save_attempt

    def pause_after_save(service, *args, **kwargs):
        attempt = original_save(service, *args, **kwargs)
        calls.append(attempt.pk)
        if not saved.is_set():
            saved.set()
            if not release.wait(10):
                raise RuntimeError('첫 요청 해제 시간 초과')
        return attempt

    monkeypatch.setattr(ExerciseService, 'save_attempt', pause_after_save)

    def post(selected, second=False):
        close_old_connections()
        try:
            if second:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    second_pid.append(cursor.fetchone()[0])
                started.set()
            return Client().post(url, {
                'selected_indices': json.dumps(selected), 'mate_attempt_id': str(mate.pk),
            }).content.decode()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, [0, 1, 2])
        try:
            assert saved.wait(5)
            second = pool.submit(post, [3, 3, 3], True)
            assert started.wait(5)
            deadline, blocked = monotonic() + 5, False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_blocking_pids(%s)', [second_pid[0]])
                    blocked = bool(cursor.fetchone()[0])
                if blocked or second.done():
                    break
                sleep(.01)
            assert blocked, '두 번째 요청은 첫 번째 요청의 대결 행 잠금에서 기다려야 합니다.'
            with connection.cursor() as cursor:
                cursor.execute('SELECT query FROM pg_stat_activity WHERE pid = %s', [second_pid[0]])
                assert 'search_mateattempt' in cursor.fetchone()[0]
        finally:
            release.set()
        assert '통과!' in first.result(timeout=10)
        assert '통과!' in second.result(timeout=10)
    assert len(calls) == 1
    attempt = ExerciseAttempt.objects.get(exercise=exercise)
    mate.refresh_from_db()
    exercise.refresh_from_db()
    assert mate.user_attempt_id == attempt.pk
    assert attempt.user_answer == {'selected_indices': [0, 1, 2]}
    assert exercise.review_interval == 3
    assert BKTObservation.objects.filter(attempt=attempt).count() == 3
    assert ConceptMastery.objects.get(concept=concept).observation_count == 3


class TestErrorResponseEscaping:
    @pytest.mark.parametrize(('method', 'endpoint', 'label'), [
        ('generate_exercise', 'practice:exercise_generate', '생성 오류'),
        ('evaluate_attempt', 'practice:exercise_attempt', '채점 오류'),
        ('generate_coach_comment', 'practice:exercise_coach', '코멘트 생성 오류'),
    ])
    def test_exception_html_is_returned_as_text(self, client, monkeypatch, method, endpoint, label):
        exercise = make_exercise()
        payload = '<img src=x onerror="alert(1)"> & \'오류\''

        def fail(*args, **kwargs):
            raise RuntimeError(payload)

        monkeypatch.setattr(ExerciseService, method, fail)
        if endpoint == 'practice:exercise_generate':
            pk, data = exercise.learning_log_id, {'exercise_type': 'path_trace'}
        elif endpoint == 'practice:exercise_coach':
            attempt = ExerciseAttempt.objects.create(exercise=exercise, user_answer={})
            pk, data = attempt.pk, {}
        else:
            pk, data = exercise.pk, {'stage': 'final'}

        response = client.post(reverse(endpoint, args=[pk]), data)

        html = response.content.decode()
        assert response.status_code == 200
        assert f'{label}:' in html
        assert '<div class="alert alert-' in html  # 오류 박스의 HTML은 유지
        assert payload not in html
        assert '<img' not in html
        assert '&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#x27;오류&#x27;' in html

    def test_invalid_mate_id_in_real_django_exception_is_escaped(self, client):
        exercise = make_exercise('path_trace')
        payload = '<img src=x onerror=alert(1)>'

        response = client.post(reverse('practice:exercise_attempt', args=[exercise.pk]), {
            'selected_indices': '[0, 1, 2]', 'mate_attempt_id': payload,
        })

        html = response.content.decode()
        assert response.status_code == 200
        assert '채점 오류:' in html
        assert 'expected a number' in html
        assert payload not in html
        assert '&lt;img src=x onerror=alert(1)&gt;' in html
        assert not ExerciseAttempt.objects.filter(exercise=exercise).exists()


class TestMateAttemptAPI:
    def test_solve_endpoint_runs_learning_mate_service(self, client, monkeypatch):
        exercise = make_exercise("path_trace")
        mate_attempt = MateAttempt.objects.create(exercise=exercise)

        def fake_solve(service, attempt):
            attempt.status = "ready"
            attempt.answer = {"selected_indices": [0, 1, 2]}
            attempt.score = 1
            attempt.is_correct = True
            attempt.save(update_fields=["status", "answer", "score", "is_correct"])
            return attempt

        monkeypatch.setattr(LearningMateService, "solve_attempt", fake_solve)

        response = client.post(
            reverse("practice:mate_attempt_solve", args=[mate_attempt.pk])
        )

        mate_attempt.refresh_from_db()
        assert response.status_code == 200
        assert mate_attempt.status == "ready"
        assert "답을 제출" in response.content.decode()

    def test_result_endpoint_returns_failure_without_exposing_error(self, client):
        exercise = make_exercise("path_trace")
        mate_attempt = MateAttempt.objects.create(
            exercise=exercise,
            status="failed",
            error_message="secret provider error",
        )

        response = client.get(
            reverse("practice:mate_attempt_result", args=[mate_attempt.pk])
        )

        body = response.content.decode()
        assert response.status_code == 200
        assert "결과를 비교하지 못했어요" in body
        assert "secret provider error" not in body


class TestExerciseCoachAPI:
    def test_returns_error_for_missing_attempt(self, client):
        response = client.post(reverse("practice:exercise_coach", args=[999999]))

        assert response.status_code == 200
        assert "풀이 기록을 찾을 수 없습니다" in response.content.decode()

    def test_renders_generated_comment(self, client, monkeypatch):
        exercise = make_exercise()
        attempt = ExerciseAttempt.objects.create(
            exercise=exercise,
            user_answer={"covered_indices": [0]},
            is_correct=False,
            score=1 / 3,
        )
        monkeypatch.setattr(
            ExerciseService,
            "generate_coach_comment",
            lambda service, saved_attempt: "가시성과 격리 수준의 관계를 다시 살펴보세요.",
        )

        response = client.post(reverse("practice:exercise_coach", args=[attempt.pk]))

        assert response.status_code == 200
        assert "가시성과 격리 수준의 관계" in response.content.decode()
