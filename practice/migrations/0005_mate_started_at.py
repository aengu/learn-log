from django.db import migrations, models
from django.utils import timezone


def grant_existing_solving_grace(apps, schema_editor):
    # 과거 시작 시각을 복원하는 것이 아니라 배포 시점부터 2분의 유예 시간을 준다.
    apps.get_model('practice', 'MateAttempt').objects.using(schema_editor.connection.alias).filter(
        status='solving', started_at__isnull=True,
    ).update(started_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [('practice', '0004_bkt_failure_recovery')]

    operations = [
        migrations.AddField(
            model_name='mateattempt', name='started_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='러닝메이트 풀이 시작일'),
        ),
        migrations.RunPython(grant_existing_solving_grace, migrations.RunPython.noop),
    ]
