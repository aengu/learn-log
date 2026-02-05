import json
from django.shortcuts import render
from django.views import View
from django.http import StreamingHttpResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .services import LearnlogService
from .serializers import LearningLogDetailSerializer, QueryInputSerializer

class MainPageView(View):
    """
    메인 페이지 뷰 - 질문 입력 폼 표시
    """
    def get(self, request):
        return render(request, 'search/main.html')


class QueryHTMXView(View):
    """
    HTMX용 질문 처리 뷰
    POST /api/query/html
    - 질문을 받아서 AI 답변 생성 후 HTML 조각 반환
    """
    def post(self, request):
        query = request.POST.get('query', '').strip()

        # 유효성 검사
        if len(query) < 5:
            return render(request, 'search/partials/error.html', {
                'error_message': '질문은 최소 5자 이상이어야 합니다.'
            })

        try:
            # 서비스 호출 - 검색, AI 답변, 태그 추출, 마크다운 변환, DB 저장
            service = LearnlogService()
            log = service.process_query(query)

            # HTML 조각 반환
            return render(request, 'search/partials/result.html', {
                'log': log
            })

        except Exception as e:
            return render(request, 'search/partials/error.html', {
                'error_message': str(e)
            })


class QueryAPIView(APIView):
    """
    REST API용 질문 처리 뷰
    POST /api/query/json/
    - 질문을 받아서 AI 답변 생성 후 JSON 반환
    """
    def post(self, request):
        serializer = QueryInputSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {'error': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        query = serializer.validated_data['query']

        try:
            # 서비스 호출 - 검색, AI 답변, 태그 추출, 마크다운 변환, DB 저장
            service = LearnlogService()
            log = service.process_query(query)

            # 결과 직렬화
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


@method_decorator(csrf_exempt, name='dispatch')
class QuerySSEView(View):
    """
    SSE용 질문 처리 뷰
    POST /api/query/stream/
    - 진행 상황을 실시간으로 전송하고 최종 결과 반환
    """
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
        """진행 상황을 SSE로 스트리밍"""
        try:
            service = LearnlogService()

            yield self._sse_event('progress', {'step': 1, 'total': 5, 'message': f'질문 받음: {query}'})

            # 웹 검색
            search_results = service.search_official_docs(query)
            yield self._sse_event('progress', {'step': 2, 'total': 5, 'message': f'검색 완료: {len(search_results.get("results", []))}개 결과'})

            # AI 답변 생성
            ai_answer = service.generate_answer(query, search_results)
            yield self._sse_event('progress', {'step': 3, 'total': 5, 'message': f'AI 답변 생성 완료 ({len(ai_answer)}자)'})

            # 태그 추출
            tag_names = service.extract_tags(query, ai_answer)
            yield self._sse_event('progress', {'step': 4, 'total': 5, 'message': f'태그 추출 완료: {tag_names}'})

            # 마크다운 변환
            markdown = service.convert_to_markdown(query, ai_answer, search_results)
            yield self._sse_event('progress', {'step': 5, 'total': 5, 'message': '마크다운 변환 완료'})

            # DB 저장 (서비스 메서드 호출)
            log = service.save_learning_log(query, ai_answer, markdown, search_results, tag_names)

            # 최종 결과 HTML 전송
            result_html = render_to_string('search/partials/result.html', {'log': log})
            yield self._sse_event('complete', {'html': result_html})

        except Exception as e:
            error_html = render_to_string('search/partials/error.html', {'error_message': str(e)})
            yield self._sse_event('error', {'html': error_html})

    def _error_stream(self, message):
        """에러 메시지를 SSE로 전송"""
        error_html = render_to_string('search/partials/error.html', {'error_message': message})
        yield self._sse_event('error', {'html': error_html})

    def _sse_event(self, event_type, data):
        """SSE 이벤트 포맷으로 변환"""
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"