import json
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

from .models import LearningLog, Exercise
from .services import LearnlogService, ExerciseService
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

            yield self._sse_event('progress', {'step': 1, 'total': 5, 'message': f'질문 받음: {query}'})

            search_results = service.search_official_docs(query)
            yield self._sse_event('progress', {'step': 2, 'total': 5, 'message': f'검색 완료: {len(search_results.get("results", []))}개 결과'})

            ai_answer = service.generate_answer(query, search_results, custom_instructions)
            yield self._sse_event('progress', {'step': 3, 'total': 5, 'message': f'AI 답변 생성 완료 ({len(ai_answer)}자)'})

            tag_names = service.extract_tags(query, ai_answer)
            yield self._sse_event('progress', {'step': 4, 'total': 5, 'message': f'태그 추출 완료: {tag_names}'})

            markdown = service.convert_to_markdown(query, ai_answer, search_results)
            yield self._sse_event('progress', {'step': 5, 'total': 5, 'message': '마크다운 변환 완료'})

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
        valid_types = ['generation_compare', 'path_trace', 'retrieval_checkin']
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
    """연습문제 풀이 제출 - HTMX POST"""
    def post(self, request, pk):
        try:
            exercise = Exercise.objects.select_related('learning_log').get(pk=pk)
        except Exercise.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">연습문제를 찾을 수 없습니다.</div>')

        exercise_type = exercise.exercise_type
        if exercise_type == 'path_trace':
            raw = request.POST.get('selected_indices', '[]')
            try:
                user_answer = {'selected_indices': json.loads(raw)}
            except (json.JSONDecodeError, ValueError):
                return HttpResponse('<div class="alert alert-error">잘못된 답변 형식입니다.</div>')
        else:
            user_answer = {'text': request.POST.get('answer', '').strip()}

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