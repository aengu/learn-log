import json
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views import View
from django.core.paginator import Paginator

from .models import LearningLog, Exercise, ExerciseAttempt, Streak
from .services import ExerciseService


class MainPageView(View):
    """메인 페이지 - 질문 입력 폼"""
    def get(self, request):
        streak = Streak.load()
        return render(request, 'search/main.html', {'streak': streak})


class LogListView(View):
    """
    학습로그 리스트 페이지
    무한스크롤용 - HTML 조각 반환
    검색 키워드가 있는 경우 정렬: 연관순
    """
    def get(self, request):
        q = request.GET.get('q', '').strip() # 검색 키워드
        page_num = int(request.GET.get('page', 1))
        sort = request.GET.get('sort', 'relevance' if q else 'latest')
        tag_param = request.GET.get('tag', '')
        tags = [t for t in tag_param.split(',') if t]
        bookmarked = request.GET.get('bookmarked') == 'true'
        logs = LearningLog.get_queryset(q=q, sort=sort, tags=tags, bookmarked=bookmarked)

        paginator = Paginator(logs, 12)
        page = paginator.get_page(page_num)

        context = {
            'logs': page,
            'active_tags': tags,
            'active_tags_str': tag_param,
            'current_sort': sort,
            'search_query': q,
            'has_next': page.has_next(),
            'next_page': page_num + 1,
            'current_sort': sort,
            'search_query': q,
            'active_tags': tags,
            'active_tags_str': tag_param,
            'bookmarked': bookmarked,
        }

        # htmx 요청이 있는 경우(무한 스크롤), 템플릿의 일부분만 반환 
        if request.htmx:        
            return render(request, 'search/partials/log_cards.html', context)
    
        return render(request, 'search/list.html', context)


class ExerciseListView(View):
    """복습 대기 중인 연습문제 목록"""
    def get(self, request):
        exercises = ExerciseService.get_due_exercises()
        return render(request, 'search/exercises/list.html', {'exercises': exercises})


class ExerciseDetailView(View):
    """연습문제 풀기 페이지"""
    def get(self, request, pk):
        exercise = get_object_or_404(
            Exercise.objects.select_related('learning_log'), pk=pk
        )
        return render(request, 'search/exercises/detail.html', {'exercise': exercise})


class StatsView(View):
    """
    통계 대시보드.
    불꽃(streak) 카드, 요약 카드, GitHub 스타일 heatmap을 렌더링한다.
    """
    def get(self, request):
        streak = Streak.load()
        today = timezone.now().date()
        year_ago = today - timedelta(days=364)  # 52주 + 오늘 = 365일

        # ── 요약 통계 ──
        total_logs = LearningLog.objects.count()
        correct_count = ExerciseAttempt.objects.filter(is_correct=True).count()
        # 채점중(is_correct=None)은 제외하고 정답률 계산
        total_attempts = ExerciseAttempt.objects.exclude(is_correct__isnull=True).count()
        accuracy = round(correct_count / total_attempts * 100) if total_attempts else 0

        # ── Heatmap용 일별 활동 집계 ──
        # 학습 로그 생성 건수 (날짜별)
        log_by_date = dict(
            LearningLog.objects.filter(created_at__date__gte=year_ago)
            .annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(cnt=Count('id'))
            .values_list('date', 'cnt')
        )
        # 복습 정답 건수 (날짜별)
        attempt_by_date = dict(
            ExerciseAttempt.objects.filter(
                created_at__date__gte=year_ago, is_correct=True
            )
            .annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(cnt=Count('id'))
            .values_list('date', 'cnt')
        )

        # 두 소스 합산 → 5단계 level로 변환 (GitHub 잔디 색상 매핑)
        heatmap_data = []
        all_dates = set(log_by_date) | set(attempt_by_date)
        counts = {}
        for d in all_dates:
            counts[d] = log_by_date.get(d, 0) + attempt_by_date.get(d, 0)

        # 1년치 날짜를 빠짐없이 순회 (활동 없는 날도 level=0으로 포함)
        day = year_ago
        while day <= today:
            c = counts.get(day, 0)
            # level 기준: 0건=0, 1건=1, 2~3건=2, 4~5건=3, 6건+=4
            if c == 0:
                level = 0
            elif c <= 1:
                level = 1
            elif c <= 3:
                level = 2
            elif c <= 5:
                level = 3
            else:
                level = 4
            heatmap_data.append({
                'date': day.isoformat(),
                'count': c,
                'level': level,
            })
            day += timedelta(days=1)

        context = {
            'streak': streak,
            'total_logs': total_logs,
            'accuracy': accuracy,
            'total_attempts': total_attempts,
            'heatmap_json': json.dumps(heatmap_data),  # 템플릿에서 JS로 전달
        }
        return render(request, 'search/stats.html', context)