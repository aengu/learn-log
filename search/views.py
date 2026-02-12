from django.shortcuts import render
from django.views import View
from django.core.paginator import Paginator

from .models import LearningLog


class MainPageView(View):
    """메인 페이지 - 질문 입력 폼"""
    def get(self, request):
        return render(request, 'search/main.html')


class LogListView(View):
    """
    학습로그 리스트 페이지
    검색 키워드가 있는 경우 정렬: 연관순
    """
    def get(self, request):
        q = request.GET.get('q', '').strip() # 검색 키워드
        sort = request.GET.get('sort', 'relevance' if q else 'latest')
        logs = LearningLog.get_queryset(q=q, sort=sort)


        paginator = Paginator(logs, 12)
        page = paginator.get_page(1)

        return render(request, 'search/list.html', {
            'logs': page,
            'has_next': page.has_next(),
            'next_page': 2,
            'current_sort': sort,
            'search_query': q,
        })