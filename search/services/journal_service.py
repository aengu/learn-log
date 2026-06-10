import textwrap

from groq import Groq
from django.conf import settings
from django.db.models.functions import TruncDate
from django.utils import timezone

from ..models import DailyJournal, ExerciseAttempt, LearningLog


class JournalService:
    """
    일일 학습일지 생성·조회.
    cron 없는 환경(Render Free)이라 자정 이후 첫 방문 시점에 lazy 생성한다.
    """

    LIGHT_MODEL = "llama-3.3-70b-versatile"  # 요약은 경량 작업 → Groq

    def __init__(self):
        self.groq_client = Groq(api_key=settings.GROQ_API_KEY)

    # ── 조회/생성 ─────────────────────────────────────────────────────

    def get_pending_popup(self):
        """
        팝업으로 보여줄 일지 반환 (없으면 None).
        대상: 오늘 이전의 가장 최근 활동일. 일지가 없으면 생성하고,
        다시보지않기(is_dismissed) 처리된 일지는 보여주지 않는다.
        """
        today = timezone.localdate()
        target = self._last_active_date(before=today)
        if target is None:
            return None
        journal = self.ensure_journal(target)
        if journal is None or journal.is_dismissed:
            return None
        return journal

    def ensure_journal(self, date):
        """해당 날짜의 일지를 반환. 없으면 통계 집계 + 요약 생성 후 저장."""
        journal = DailyJournal.objects.filter(date=date).first()
        if journal:
            return journal

        stats = self._collect_stats(date)
        if stats['question_count'] == 0 and stats['attempt_count'] == 0:
            return None  # 활동 없는 날은 일지를 만들지 않는다

        summary = self._generate_summary(date, stats)
        journal, _ = DailyJournal.objects.get_or_create(
            date=date,
            defaults={**stats, 'summary': summary},
        )
        return journal

    # ── 내부 ──────────────────────────────────────────────────────────

    def _collect_stats(self, date):
        """해당 날짜의 질문/연습문제 통계 + 불꽃 n일차 집계"""
        question_count = LearningLog.objects.filter(created_at__date=date).count()
        attempts = ExerciseAttempt.objects.filter(created_at__date=date)
        attempt_count = attempts.count()
        pass_count = attempts.filter(is_correct=True).count()
        fail_count = attempts.filter(is_correct=False).count()
        return {
            'question_count': question_count,
            'attempt_count': attempt_count,
            'pass_count': pass_count,
            'fail_count': fail_count,
            'streak_day': self._streak_day_at(date),
        }

    def _active_dates(self):
        """활동(질문 또는 연습문제 시도)이 있었던 날짜 집합"""
        log_dates = (
            LearningLog.objects.annotate(d=TruncDate('created_at'))
            .values_list('d', flat=True).distinct()
        )
        attempt_dates = (
            ExerciseAttempt.objects.annotate(d=TruncDate('created_at'))
            .values_list('d', flat=True).distinct()
        )
        return set(log_dates) | set(attempt_dates)

    def _last_active_date(self, before):
        """before(미포함) 이전의 가장 최근 활동일"""
        past = [d for d in self._active_dates() if d < before]
        return max(past) if past else None

    def _streak_day_at(self, date):
        """해당 날짜 기준 불꽃 n일차 — date에서 거꾸로 연속 활동일을 센다"""
        active = self._active_dates()
        if date not in active:
            return 0
        day, count = date, 0
        while day in active:
            count += 1
            day -= timezone.timedelta(days=1)
        return count

    def _generate_summary(self, date, stats):
        """그날의 질문 목록으로 1~2문장 요약 생성. 실패 시 빈 문자열."""
        queries = list(
            LearningLog.objects.filter(created_at__date=date)
            .values_list('query', flat=True)[:10]
        )
        if not queries:
            return ""

        query_lines = "\n".join(f"- {q}" for q in queries)
        prompt = textwrap.dedent(f"""
            아래는 한 학습자가 {date.month}월 {date.day}일에 검색한 개발 질문 목록입니다.
            어떤 주제를 공부했는지 1~2문장으로 친근하게 요약해주세요.
            예시: "오늘은 Docker 네트워크와 PostgreSQL 격리 수준에 관해 공부했네요."
            요약 문장만 출력하세요.

            {query_lines}
        """).strip()

        try:
            response = self.groq_client.chat.completions.create(
                model=self.LIGHT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=150,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"일지 요약 생성 오류: {e}")
            return ""
