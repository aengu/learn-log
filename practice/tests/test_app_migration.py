"""앱 이동 전후에 문제·풀이·권한이 같은 레코드를 참조하는지 확인한다."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_app_move_preserves_records_relations_and_content_type():
    old_targets = [('search', '0010_mateattempt'), ('practice', None)]
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state([('search', '0010_mateattempt')]).apps
        log = old_apps.get_model('search', 'LearningLog').objects.create(
            query='앱 이동 검증', ai_response='답변', markdown_content='답변',
        )
        exercise = old_apps.get_model('search', 'Exercise').objects.create(
            learning_log=log,
            exercise_type='path_trace',
            content={'scenario': '기존 문제', 'steps': []},
            review_interval=7,
        )
        attempt = old_apps.get_model('search', 'ExerciseAttempt').objects.create(
            exercise=exercise, user_answer={'selected_indices': [1]}, score=1,
        )
        mate = old_apps.get_model('search', 'MateAttempt').objects.create(
            exercise=exercise, user_attempt=attempt,
            status='ready', answer={'selected_indices': [0]}, score=0,
        )

        from django.contrib.auth.models import Group, Permission
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get(app_label='search', model='exercise')
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename='view_exercise',
            defaults={'name': 'Can view exercise'},
        )
        group = Group.objects.create(name='연습문제 조회자')
        group.permissions.add(permission)
        content_type_pk, permission_pk = content_type.pk, permission.pk

        executor = MigrationExecutor(connection)
        executor.migrate(latest_targets)
        apps = executor.loader.project_state(latest_targets).apps
        moved_exercise = apps.get_model('practice', 'Exercise').objects.get(pk=exercise.pk)
        moved_attempt = apps.get_model('practice', 'ExerciseAttempt').objects.get(pk=attempt.pk)
        moved_mate = apps.get_model('practice', 'MateAttempt').objects.get(pk=mate.pk)

        assert moved_exercise.learning_log_id == log.pk
        assert moved_exercise.content == exercise.content
        assert moved_exercise.review_interval == 7
        assert moved_attempt.exercise_id == exercise.pk
        assert moved_attempt.user_answer == attempt.user_answer
        assert moved_mate.exercise_id == exercise.pk
        assert moved_mate.user_attempt_id == attempt.pk
        assert moved_mate.answer == mate.answer
        assert ContentType.objects.get(pk=content_type_pk).app_label == 'practice'
        assert group.permissions.filter(pk=permission_pk, content_type_id=content_type_pk).exists()
        tables = connection.introspection.table_names()
        assert 'search_exercise' in tables
        assert 'practice_exercise' not in tables

        # 되돌릴 때도 테이블과 기록은 유지되고 모델 소속·content type만 복원된다.
        executor = MigrationExecutor(connection)
        executor.migrate(old_targets)
        assert ContentType.objects.get(pk=content_type_pk).app_label == 'search'
        assert old_apps.get_model('search', 'MateAttempt').objects.get(pk=mate.pk).user_attempt_id == attempt.pk
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
