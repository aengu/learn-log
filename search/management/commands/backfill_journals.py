"""
과거 활동일의 일일 학습일지를 일괄 생성한다 (heatmap hover용).
요약 생성에 활동일당 Groq 호출 1회가 발생하므로 필요할 때 1회만 실행.

사용법: docker compose exec web python manage.py backfill_journals
"""
from django.core.management.base import BaseCommand

from search.models import DailyJournal
from search.services import JournalService


class Command(BaseCommand):
    help = "과거 활동일의 일일 학습일지를 일괄 생성합니다"

    def handle(self, *args, **options):
        service = JournalService()
        dates = sorted(service._active_dates())
        created = skipped = 0

        for date in dates:
            if DailyJournal.objects.filter(date=date).exists():
                skipped += 1
                continue
            journal = service.ensure_journal(date)
            if journal:
                created += 1
                self.stdout.write(f"  ✓ {date}: 🔥{journal.streak_day}일차, "
                                  f"질문 {journal.question_count} / 시도 {journal.attempt_count}")

        self.stdout.write(self.style.SUCCESS(
            f"완료: 생성 {created}건, 기존 {skipped}건"
        ))
