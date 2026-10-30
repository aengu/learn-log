from django.urls import path
from .views import MainPageView, LogListView, StatsView
from .api_views import (
    QuerySSEView,
    LogDetailAPIView,
    JournalPopupAPIView,
    JournalDismissAPIView,
)

app_name = 'search'

urlpatterns = [
    # 페이지
    path('', MainPageView.as_view(), name='main'),
    path('logs/', LogListView.as_view(), name='log_list'),
    path('stats/', StatsView.as_view(), name='stats'),

    # Query API
    path('api/query/stream/', QuerySSEView.as_view(), name='query_api_stream'),

    # Log API
    path('api/logs/<int:pk>/', LogDetailAPIView.as_view(), name='log_detail_api'),


    # Journal API
    path('api/journal/popup/', JournalPopupAPIView.as_view(), name='journal_popup'),
    path('api/journal/<int:pk>/dismiss/', JournalDismissAPIView.as_view(), name='journal_dismiss'),
]
