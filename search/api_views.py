import json
from django.shortcuts import render
from django.views import View
from django.http import StreamingHttpResponse, HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.core.paginator import Paginator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import LearningLog
from .services import LearnlogService
from .serializers import LearningLogDetailSerializer, QueryInputSerializer


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
            return render(request, 'search/partials/result.html', {'log': log})
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

        return StreamingHttpResponse(
            self._process_stream(query),
            content_type='text/event-stream'
        )

    def _process_stream(self, query):
        try:
            service = LearnlogService()

            yield self._sse_event('progress', {'step': 1, 'total': 5, 'message': f'질문 받음: {query}'})

            search_results = service.search_official_docs(query)
            yield self._sse_event('progress', {'step': 2, 'total': 5, 'message': f'검색 완료: {len(search_results.get("results", []))}개 결과'})

            ai_answer = service.generate_answer(query, search_results)
            yield self._sse_event('progress', {'step': 3, 'total': 5, 'message': f'AI 답변 생성 완료 ({len(ai_answer)}자)'})

            tag_names = service.extract_tags(query, ai_answer)
            yield self._sse_event('progress', {'step': 4, 'total': 5, 'message': f'태그 추출 완료: {tag_names}'})

            markdown = service.convert_to_markdown(query, ai_answer, search_results)
            yield self._sse_event('progress', {'step': 5, 'total': 5, 'message': '마크다운 변환 완료'})

            log = service.save_learning_log(query, ai_answer, markdown, search_results, tag_names)

            result_html = render_to_string('search/partials/result.html', {'log': log})
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

class LogListAPIView(View):
    """무한스크롤용 - HTML 조각 반환"""
    def get(self, request):
        page_num = int(request.GET.get('page', 1))
        logs = LearningLog.objects.prefetch_related('tags').order_by('-created_at')
        paginator = Paginator(logs, 12)
        page = paginator.get_page(page_num)
        
        return render(request, 'search/partials/log_cards.html', {
            'logs': page,
            'has_next': page.has_next(),
            'next_page': page_num + 1,
        })


class LogDetailAPIView(View):
    """학습로그 상세 - 모달용 HTML 반환"""
    def get(self, request, pk):
        try:
            log = LearningLog.objects.prefetch_related('tags', 'references').get(pk=pk)
            return render(request, 'search/partials/log_detail_modal.html', {'log': log})
        except LearningLog.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">로그를 찾을 수 없습니다.</div>')