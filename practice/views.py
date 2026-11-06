import random

from django.shortcuts import get_object_or_404, render
from django.views import View

from .models import Exercise, MateAttempt
from .services import ExerciseService


class ExerciseListView(View):
    """복습 대기 중인 연습문제 목록"""
    def get(self, request):
        exercises = ExerciseService.get_due_exercises()
        return render(request, 'practice/exercises/list.html', {'exercises': exercises})


class ExerciseDetailView(View):
    """연습문제 풀기 페이지"""
    def get(self, request, pk):
        exercise = get_object_or_404(
            Exercise.objects.select_related('learning_log'), pk=pk
        )
        mate_attempt = None
        if exercise.exercise_type == 'path_trace':
            mate_attempt = MateAttempt.objects.create(exercise=exercise)
        _shuffle_choices_for_display(exercise)
        return render(request, 'practice/exercises/detail.html', {
            'exercise': exercise,
            'mate_attempt': mate_attempt,
        })


def _shuffle_choices_for_display(exercise):
    """path_trace 보기를 화면에서만 섞는다. 저장값은 건드리지 않는다.

    출제 모델이 정답을 매번 같은 자리에 넣는 경향이 있다(gpt-oss-120b 실측 15개 중 14개).
    그대로 두면 내용을 몰라도 위치만 보고 맞출 수 있다.
    간격 반복(1→3→7→14→30일)으로 같은 문제를 다시 푸는데, 저장 시점에 한 번만 섞으면
    2회차부터는 배치가 고정이라 위치를 외워서 통과할 수 있다. 그래서 볼 때마다 섞는다.

    각 보기에 원본 인덱스를 함께 실어 보낸다(step['shuffled'] = [(원본index, 텍스트), ...]).
    화면 순서만 바뀌고 제출되는 값은 원본 인덱스라, 서버 채점(_evaluate_path_trace)과
    저장된 correct_index·distractors[].index는 그대로 유효하다.

    exercise를 save하지 않으므로 이 변경은 이 요청 안에서만 산다.
    """
    if exercise.exercise_type != 'path_trace':
        return
    for step in exercise.content.get('steps', []):
        pairs = list(enumerate(step.get('choices', [])))
        random.shuffle(pairs)
        step['shuffled'] = pairs


