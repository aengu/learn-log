from django.shortcuts import render
from django.views import View
from django.core.paginator import Paginator

from .models import LearningLog


class MainPageView(View):
    """메인 페이지 - 질문 입력 폼"""
    def get(self, request):
        return render(request, 'search/main.html')


class LogListView(View):
    """학습로그 리스트 페이지"""
    def get(self, request):
        logs = LearningLog.objects.prefetch_related('tags').order_by('-created_at')
        paginator = Paginator(logs, 12)
        page = paginator.get_page(1)
        
        return render(request, 'search/list.html', {
            'logs': page,
            'has_next': page.has_next(),
            'next_page': 2,
        })