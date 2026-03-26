from django.urls import path
from .views import MainPageView, LogListView, ExerciseListView, ExerciseDetailView
from .api_views import (
    QueryHTMXView,
    QuerySSEView,
    QueryAPIView,
    LogDetailAPIView,
    ExerciseGenerateAPIView,
    ExerciseAttemptAPIView,
)

app_name = 'search'

urlpatterns = [
    # 페이지
    path('', MainPageView.as_view(), name='main'),
    path('logs/', LogListView.as_view(), name='log_list'),
    path('exercises/', ExerciseListView.as_view(), name='exercise_list'),
    path('exercises/<int:pk>/', ExerciseDetailView.as_view(), name='exercise_detail'),

    # Query API
    path('api/query/', QueryHTMXView.as_view(), name='query_api_html'),
    path('api/query/json/', QueryAPIView.as_view(), name='query_api_json'),
    path('api/query/stream/', QuerySSEView.as_view(), name='query_api_stream'),

    # Log API
    path('api/logs/<int:pk>/', LogDetailAPIView.as_view(), name='log_detail_api'),

    # Exercise API
    path('api/exercises/generate/<int:log_pk>/', ExerciseGenerateAPIView.as_view(), name='exercise_generate'),
    path('api/exercises/<int:pk>/attempt/', ExerciseAttemptAPIView.as_view(), name='exercise_attempt'),
]