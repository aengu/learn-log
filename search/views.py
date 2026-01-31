from django.shortcuts import render
from django.views import View
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .services import LearnlogService
from .serializers import LearningLogDetailSerializer, QueryInputSerializer

print('aa')
class MainPageView(View):
    """
    메인 페이지 뷰 - 질문 입력 폼 표시
    """
    def get(self, request):
        return render(request, 'search/main.html')


class QueryHTMXView(View):
    """
    HTMX용 질문 처리 뷰
    POST /api/query/
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
