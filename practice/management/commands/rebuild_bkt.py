"""승인된 문항의 사용자 풀이로 BKT 상태를 재구성한다."""
import json

from django.core.management.base import BaseCommand

from practice.services.bkt_service import BKTService


class Command(BaseCommand):
    help = 'BKT 재구성 미리보기. --apply를 명시해야 기존 BKT 계산 결과를 교체합니다. 원본 풀이·점수는 보존합니다.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='BKT 결과 두 테이블을 시간순 재계산한 결과로 교체')

    def handle(self, *args, **options):
        result = BKTService.rebuild(apply=options['apply'])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
