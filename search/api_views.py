import json
from concurrent.futures import ThreadPoolExecutor
from django.shortcuts import render
from django.views import View
from django.http import StreamingHttpResponse, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.core.paginator import Paginator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import LearningLog, Exercise, ExerciseAttempt, DailyJournal
from .services import LearnlogService, ExerciseService, JournalService
from .serializers import LearningLogDetailSerializer, LearningLogUpdateSerializer, QueryInputSerializer

EXERCISE_TYPES = Exercise.EXERCISE_TYPE_CHOICES


# ============================================
# 학습기록 서치 API
# ============================================

class QueryHTMXView(View):
    """HTMX용 질문 처리 - HTML 조각 반환"""
    def post(self, request):
        query = request.POST.get('query', '').strip()

        if len(query) < 5:
            return render(request, 'search/partials/error.html', {
                'error_message': '질문은 최소 5자 이상이어야 합니다.'
            })

        try:
            service = LearnlogService()
            log = service.process_query(query)
            return render(request, 'search/partials/result.html', {
                'log': log,
                'exercise_types': EXERCISE_TYPES,
            })
        except Exception as e:
            return render(request, 'search/partials/error.html', {
                'error_message': str(e)
            })


@method_decorator(csrf_exempt, name='dispatch')
class QuerySSEView(View):
    """SSE용 질문 처리 - 실시간 진행 스트리밍"""
    def post(self, request):
        query = request.POST.get('query', '').strip()

        if len(query) < 5:
            return StreamingHttpResponse(
                self._error_stream("질문은 최소 5자 이상이어야 합니다."),
                content_type='text/event-stream'
            )

        custom_instructions = request.POST.get('custom_instructions', '').strip() or None
        return StreamingHttpResponse(
            self._process_stream(query, custom_instructions),
            content_type='text/event-stream'
        )

    def _process_stream(self, query, custom_instructions=None):
        try:
            service = LearnlogService()

            yield self._sse_event('progress', {'step': 1, 'total': 4, 'message': '공식 문서 검색 중...'})
            search_results = service.search_official_docs(query)

            yield self._sse_event('progress', {'step': 2, 'total': 4, 'message': 'AI 답변 생성 중...'})
            ai_answer_chunks = []
            for chunk in service.generate_answer_stream(query, search_results, custom_instructions):
                ai_answer_chunks.append(chunk)
                yield self._sse_event('stream_token', {'token': chunk})
            ai_answer = ''.join(ai_answer_chunks).strip()

            yield self._sse_event('progress', {'step': 3, 'total': 4, 'message': '태그 추출 + 마크다운 변환 중...'})
            with ThreadPoolExecutor(max_workers=2) as executor:
                tags_future = executor.submit(service.extract_tags, query, ai_answer)
                md_future = executor.submit(service.convert_to_markdown, query, ai_answer, search_results)
                tag_names = tags_future.result()
                markdown = md_future.result()

            yield self._sse_event('progress', {'step': 4, 'total': 4, 'message': '저장 중...'})

            log = service.save_learning_log(query, ai_answer, markdown, search_results, tag_names)

            result_html = render_to_string('search/partials/result.html', {
                'log': log,
                'exercise_types': EXERCISE_TYPES,
            })
            yield self._sse_event('complete', {'html': result_html})

        except Exception as e:
            error_html = render_to_string('search/partials/error.html', {'error_message': str(e)})
            yield self._sse_event('error', {'html': error_html})

    def _error_stream(self, message):
        error_html = render_to_string('search/partials/error.html', {'error_message': message})
        yield self._sse_event('error', {'html': error_html})

    def _sse_event(self, event_type, data):
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class QueryAPIView(APIView):
    """REST API용 질문 처리 - JSON 반환"""
    def post(self, request):
        serializer = QueryInputSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({'error': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        query = serializer.validated_data['query']

        try:
            service = LearnlogService()
            log = service.process_query(query)
            result_serializer = LearningLogDetailSerializer(log)

            return Response({
                'success': True,
                'data': result_serializer.data
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================
# 학습기록 리스트 API
# ============================================

class LogDetailAPIView(APIView):
    """학습로그 상세 - GET: 모달 HTML, PATCH: 부분 수정"""
    authentication_classes = []
    permission_classes = []

    def get(self, request, pk):
        try:
            log = LearningLog.objects.prefetch_related('tags', 'references').get(pk=pk)
            log.increment_view_count()
            return render(request, 'search/partials/log_detail_modal.html', {
                'log': log,
                'exercise_types': EXERCISE_TYPES,
            })
        except LearningLog.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">로그를 찾을 수 없습니다.</div>')

    def patch(self, request, pk):
        try:
            log = LearningLog.objects.get(pk=pk)
        except LearningLog.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = LearningLogUpdateSerializer(log, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ============================================
# 연습문제 API
# ============================================

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
            return render(request, 'search/partials/exercise_stage_reveal.html', {
                'exercise': exercise,
                'user_text': text,
            })

        if stage == 'reflect':
            # generation_compare 전용 — 2단계 체크 결과를 3단계로 carry-over
            text = request.POST.get('text', '').strip()
            covered = self._parse_covered(request, exercise)
            return render(request, 'search/partials/exercise_stage_reflect.html', {
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
            service = ExerciseService()
            evaluation = service.evaluate_attempt(exercise, user_answer)
            attempt = service.save_attempt(exercise, user_answer, evaluation)
            return render(request, 'search/partials/exercise_result.html', {
                'exercise': exercise,
                'attempt': attempt,
                'evaluation': evaluation,
            })
        except Exception as e:
            return HttpResponse(f'<div class="alert alert-error">채점 오류: {e}</div>')


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
            return render(request, 'search/partials/exercise_coach.html', {
                'comment': comment,
            })
        except Exception as e:
            return HttpResponse(f'<div class="alert alert-warning">코멘트 생성 오류: {e}</div>')

# ============================================
# 일일 학습일지 API
# ============================================

class JournalPopupAPIView(View):
    """
    일일 학습일지 팝업 - HTMX GET (메인 페이지 load 시 호출).
    오늘 이전 가장 최근 활동일의 일지를 lazy 생성해서 모달로 반환.
    다시보지않기 처리된 일지면 빈 응답.
    """
    def get(self, request):
        try:
            journal = JournalService().get_pending_popup()
        except Exception as e:
            print(f"일지 팝업 오류: {e}")
            return HttpResponse('')  # 팝업은 부가 기능 — 실패해도 메인 흐름을 막지 않는다

        if journal is None:
            return HttpResponse('')
        return render(request, 'search/partials/journal_popup.html', {'journal': journal})


class JournalDismissAPIView(View):
    """일지 팝업 '다시 보지 않기' - HTMX POST"""
    def post(self, request, pk):
        try:
            journal = DailyJournal.objects.get(pk=pk)
        except DailyJournal.DoesNotExist:
            return HttpResponse('')
        journal.is_dismissed = True
        journal.save(update_fields=['is_dismissed'])
        return HttpResponse('')  # 모달 영역을 빈 내용으로 교체 → 닫힘
