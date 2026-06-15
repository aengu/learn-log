"""
기존 LearningLog의 답변을 저장된 reference(수집 당시 Tavily 발췌)와 대조해
모순(환각 의심)을 검사한다. 검증 기능 도입 전 로그에는 생성 시 주입한 컨텍스트가
남아있지 않으므로, 같은 시점에 수집된 reference 발췌를 컨텍스트로 사용한다.

기본은 dry-run(리포트만 출력, 저장 안 함). --apply를 주면 verification 필드에
저장돼 배지·연습문제 경고에 반영된다. 로그당 Groq 호출 1회.

사용법:
  docker compose exec web python manage.py verify_logs --limit 10   # 맛보기
  docker compose exec web python manage.py verify_logs              # 전체 dry-run
  docker compose exec web python manage.py verify_logs --apply      # 결과 저장
"""
import time

from django.core.management.base import BaseCommand

from search.models import LearningLog
from search.services import LearnlogService

CALL_GAP_SEC = 2  # groq 무료 티어 분당 한도 완화


class Command(BaseCommand):
    help = "기존 학습 로그를 reference 발췌와 대조해 모순(환각 의심)을 검사합니다"

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='결과를 verification 필드에 저장')
        parser.add_argument('--limit', type=int, default=None, help='검사할 로그 수 제한')

    def handle(self, *args, **options):
        service = LearnlogService()
        logs = (
            LearningLog.objects
            .filter(verification='')  # 미검증만 — 이미 판정된 로그는 건너뜀
            .prefetch_related('references')
            .order_by('pk')
        )
        if options['limit']:
            logs = logs[:options['limit']]

        passed = suspect = skipped = failed = 0
        suspect_lines = []

        for log in logs:
            refs = list(log.references.all())
            if not refs:
                skipped += 1
                continue
            search_results = {'results': [{'url': r.url, 'content': r.excerpt} for r in refs]}

            time.sleep(CALL_GAP_SEC)
            try:
                verdict = service.check_consistency(log.ai_response, search_results=search_results)
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.WARNING(f"  ? #{log.pk}: 판정 실패 ({e})"))
                continue
            if verdict is None:
                skipped += 1
                continue

            if verdict['consistent']:
                passed += 1
                self.stdout.write(f"  ✅ #{log.pk}: {log.query[:45]}")
            else:
                suspect += 1
                line = f"  ⚠️ #{log.pk}: {log.query[:45]} — {verdict['note']}"
                suspect_lines.append(line)
                self.stdout.write(self.style.WARNING(line))

            if options['apply']:
                log.verification = 'passed' if verdict['consistent'] else 'suspect'
                log.verification_note = verdict['note']
                log.save(update_fields=['verification', 'verification_note'])

        mode = "저장 완료" if options['apply'] else "dry-run (저장 안 함 — --apply로 반영)"
        self.stdout.write(self.style.SUCCESS(
            f"\n완료 [{mode}]: 일치 {passed} / 의심 {suspect} / 컨텍스트 없음 {skipped} / 실패 {failed}"
        ))
        if suspect_lines:
            self.stdout.write("\n의심 목록 (내용 확인 추천):")
            for line in suspect_lines:
                self.stdout.write(line)
