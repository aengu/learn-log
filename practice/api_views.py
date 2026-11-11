import json

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from search.models import LearningLog

from .models import Exercise, ExerciseAttempt, MateAttempt
from .services import ExerciseService, LearningMateService
from .services.bkt_lock import acquire_bkt_lock

EXERCISE_TYPES = Exercise.EXERCISE_TYPE_CHOICES


class ExerciseGenerateAPIView(View):
    """연습문제 생성 - HTMX POST"""
    def post(self, request, log_pk):
        exercise_type = request.POST.get('exercise_type', '')
        valid_types = [t[0] for t in EXERCISE_TYPES]
        if exercise_type not in valid_types:
            return HttpResponse(
                '<div class="alert alert-error">유효하지 않은 유형입니다.</div>'
            )
        try:
            log = LearningLog.objects.get(pk=log_pk)
        except LearningLog.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">학습 로그를 찾을 수 없습니다.</div>')

        try:
            service = ExerciseService()
            exercise = service.generate_exercise(log, exercise_type)
            url = f'/exercises/{exercise.pk}/'
            return HttpResponse(
                f'<a href="{url}" class="btn btn-sm btn-primary">문제 풀러 가기 →</a>'
            )
        except Exception as e:
            return HttpResponse(
                f'<div class="alert alert-error">생성 오류: {e}</div>'
            )


class ExerciseAttemptAPIView(View):
    """
    연습문제 풀이 제출 - HTMX POST.
    `stage` 폼 파라미터로 다단계 분기:
      - reveal: 1단계 답 받고 → 2단계 partial 반환 (저장 X)
      - reflect: 2단계 체크 받고 → 3단계 partial 반환 (generation_compare 전용, 저장 X)
      - final (또는 미지정): 자가 채점 + 저장 + 결과 partial 반환
    path_trace는 stage 없이 final 동작 (selected_indices만).
    """
    def post(self, request, pk):
        try:
            exercise = Exercise.objects.select_related('learning_log').get(pk=pk)
        except Exercise.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">연습문제를 찾을 수 없습니다.</div>')

        exercise_type = exercise.exercise_type

        # path_trace: 기존 흐름 그대로
        if exercise_type == 'path_trace':
            raw = request.POST.get('selected_indices', '[]')
            try:
                user_answer = {'selected_indices': json.loads(raw)}
            except (json.JSONDecodeError, ValueError):
                return HttpResponse('<div class="alert alert-error">잘못된 답변 형식입니다.</div>')
            return self._finalize(request, exercise, user_answer)

        # generation_compare / retrieval_checkin: stage 분기
        stage = request.POST.get('stage', 'final')

        if stage == 'reveal':
            text = request.POST.get('answer', '').strip()
            return render(request, 'practice/partials/exercise_stage_reveal.html', {
                'exercise': exercise,
                'user_text': text,
            })

        if stage == 'reflect':
            # generation_compare 전용 — 2단계 체크 결과를 3단계로 carry-over
            text = request.POST.get('text', '').strip()
            covered = self._parse_covered(request, exercise)
            return render(request, 'practice/partials/exercise_stage_reflect.html', {
                'exercise': exercise,
                'user_text': text,
                'covered_indices': covered,
                'covered_set': set(covered),
            })

        # final
        text = request.POST.get('text', '').strip()
        covered = self._parse_covered(request, exercise)
        user_answer = {
            'text': text,
            'covered_indices': covered,
        }
        if exercise_type == 'generation_compare':
            user_answer['reflection'] = request.POST.get('reflection', '').strip()
        return self._finalize(request, exercise, user_answer)

    @staticmethod
    def _parse_covered(request, exercise):
        raw = request.POST.getlist('covered')
        total = len(exercise.content.get('key_points', []))
        out = []
        for v in raw:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < total:
                out.append(i)
        return out

    @staticmethod
    def _finalize(request, exercise, user_answer):
        try:
            mate_attempt = None
            mate_attempt_pk = request.POST.get('mate_attempt_id', '').strip()
            with transaction.atomic():
                # 모든 경로에서 공통 잠금 → 대결 행 순서로 획득한다.
                # 저장뿐 아니라 중복 확인과 대결 연결까지 같은 트랜잭션으로 보호한다.
                acquire_bkt_lock()
                if mate_attempt_pk and exercise.exercise_type == 'path_trace':
                    mate_attempt = MateAttempt.objects.select_for_update().filter(
                        pk=mate_attempt_pk,
                        exercise=exercise,
                    ).first()

                # 잠금을 기다린 요청은 첫 요청이 커밋한 연결을 다시 확인한다.
                if mate_attempt and mate_attempt.user_attempt_id:
                    attempt = mate_attempt.user_attempt
                    exercise = attempt.exercise
                    evaluation = {
                        'is_correct': attempt.is_correct,
                        'score': attempt.score,
                        'ai_feedback': attempt.ai_feedback,
                    }
                else:
                    service = ExerciseService()
                    # 채점은 외부 API 호출 없이 수행한다.
                    evaluation = service.evaluate_attempt(exercise, user_answer)
                    # 예상된 BKT 오류만 격리하며, 원본 풀이와 대결 연결은 함께 확정한다.
                    attempt = service.save_attempt(exercise, user_answer, evaluation)
                    exercise = attempt.exercise
                    if mate_attempt:
                        mate_attempt.user_attempt = attempt
                        mate_attempt.save(update_fields=['user_attempt'])
                        mate_attempt.refresh_from_db()
            # 렌더링은 커밋·잠금 해제 후 수행한다.
            return render(request, 'practice/partials/exercise_result.html', {
                'exercise': exercise,
                'attempt': attempt,
                'evaluation': evaluation,
                'mate_attempt': mate_attempt,
                'mate_comparison_rows': (
                    LearningMateService.comparison_rows(mate_attempt)
                    if mate_attempt else []
                ),
            })
        except Exception as e:
            # 위 atomic에서 전파된 예외라면 풀이·대결 연결이 함께 롤백된 상태다.
            return HttpResponse(f'<div class="alert alert-error">채점 오류: {e}</div>')


class MateAttemptSolveAPIView(View):
    """러닝메이트 풀이. 문제 화면이 열리면 HTMX가 별도 요청으로 호출한다."""

    def post(self, request, pk):
        try:
            mate_attempt = MateAttempt.objects.select_related(
                'exercise', 'exercise__learning_log'
            ).get(pk=pk)
        except MateAttempt.DoesNotExist:
            return HttpResponse(
                '<div class="text-sm text-warning">러닝메이트 풀이를 시작하지 못했어요.</div>'
            )

        mate_attempt = LearningMateService().solve_attempt(mate_attempt)
        return render(request, 'practice/partials/mate_solve_status.html', {
            'mate_attempt': mate_attempt,
        })


class MateAttemptResultAPIView(View):
    """사용자 제출 후 러닝메이트 풀이 완료를 폴링하고 비교한다."""

    def get(self, request, pk):
        try:
            mate_attempt = MateAttempt.objects.select_related(
                'exercise', 'user_attempt'
            ).get(pk=pk)
        except MateAttempt.DoesNotExist:
            return HttpResponse(
                '<div class="alert alert-warning">러닝메이트 결과를 찾을 수 없어요.</div>'
            )
        return render(request, 'practice/partials/mate_comparison.html', {
            'mate_attempt': mate_attempt,
            'mate_comparison_rows': LearningMateService.comparison_rows(mate_attempt),
        })


class ExerciseCoachAPIView(View):
    """AI 한마디 (on-demand) - HTMX POST"""
    def post(self, request, pk):
        try:
            attempt = ExerciseAttempt.objects.select_related('exercise').get(pk=pk)
        except ExerciseAttempt.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">풀이 기록을 찾을 수 없습니다.</div>')

        try:
            service = ExerciseService()
            comment = service.generate_coach_comment(attempt)
            return render(request, 'practice/partials/exercise_coach.html', {
                'comment': comment,
            })
        except Exception as e:
            return HttpResponse(f'<div class="alert alert-warning">코멘트 생성 오류: {e}</div>')

