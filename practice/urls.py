from django.urls import path

from .api_views import (
    ExerciseAttemptAPIView,
    ExerciseCoachAPIView,
    ExerciseGenerateAPIView,
    MateAttemptResultAPIView,
    MateAttemptSolveAPIView,
)
from .views import ExerciseDetailView, ExerciseListView

app_name = 'practice'

urlpatterns = [
    path('exercises/', ExerciseListView.as_view(), name='exercise_list'),
    path('exercises/<int:pk>/', ExerciseDetailView.as_view(), name='exercise_detail'),
    path('api/exercises/generate/<int:log_pk>/', ExerciseGenerateAPIView.as_view(), name='exercise_generate'),
    path('api/exercises/<int:pk>/attempt/', ExerciseAttemptAPIView.as_view(), name='exercise_attempt'),
    path('api/mate-attempts/<int:pk>/solve/', MateAttemptSolveAPIView.as_view(), name='mate_attempt_solve'),
    path('api/mate-attempts/<int:pk>/result/', MateAttemptResultAPIView.as_view(), name='mate_attempt_result'),
    path('api/attempts/<int:pk>/coach/', ExerciseCoachAPIView.as_view(), name='exercise_coach'),
]
