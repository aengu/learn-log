# verification 3분법 도입 — unsupported(근거 없음) 상태 추가, max_length 10→12

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('search', '0009_learninglog_answer_source_learninglog_is_truncated_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='learninglog',
            name='verification',
            field=models.CharField(blank=True, choices=[('pending', '검증 대기'), ('passed', '컨텍스트 일치'), ('suspect', '컨텍스트 불일치 의심'), ('unsupported', '컨텍스트에 근거 없음')], default='', max_length=12, verbose_name='검증 상태'),
        ),
    ]
