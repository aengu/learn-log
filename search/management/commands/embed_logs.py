"""
임베딩이 없는 기존 LearningLog를 일괄 임베딩한다 (pgvector 백필).
mistral-embed 호출이 발생하므로 필요할 때 1회만 실행.

사용법: docker compose exec web python manage.py embed_logs
"""
import time

from django.core.management.base import BaseCommand

from search.models import LearningLog
from search.services import LearnlogService


class Command(BaseCommand):
    help = "임베딩 없는 학습 로그를 일괄 임베딩합니다"

    def handle(self, *args, **options):
        service = LearnlogService()
        logs = LearningLog.objects.filter(embedding__isnull=True).order_by('pk')
        done = failed = 0

        for log in logs:
            time.sleep(1)  # mistral 무료 티어 레이트리밋(1 req/s) 회피
            embedding = service._embed(
                service._embedding_input(log.query, log.ai_response)
            )
            if embedding is None:
                failed += 1
                self.stdout.write(self.style.WARNING(f"  ✗ #{log.pk}: 임베딩 실패"))
                continue
            log.embedding = embedding
            log.save(update_fields=['embedding'])
            done += 1
            self.stdout.write(f"  ✓ #{log.pk}: {log.query[:40]}")

        self.stdout.write(self.style.SUCCESS(
            f"완료: 임베딩 {done}건, 실패 {failed}건"
        ))
