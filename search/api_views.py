import json
import threading
from concurrent.futures import ThreadPoolExecutor
from django.shortcuts import render
from django.views import View
from django.http import StreamingHttpResponse, HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from practice.models import Exercise

from .models import LearningLog, DailyJournal
from .services import (
    JournalService,
    LearnlogService,
    build_search_agent,
)
from .services.learnlog_service import RETRIEVED_LIMIT_NO_WEB, RETRIEVED_LIMIT_WEB
from .serializers import LearningLogUpdateSerializer

EXERCISE_TYPES = Exercise.EXERCISE_TYPE_CHOICES


# ============================================
# 학습기록 서치 API
# ============================================

@method_decorator(csrf_exempt, name='dispatch')
class QuerySSEView(View):
    """SSE용 질문 처리 - 실시간 진행 스트리밍"""
    def post(self, request):
        query = request.POST.get('query', '').strip()

        if len(query) < 5:
            return StreamingHttpResponse(
                self._error_stream("질문은 최소 5자 이상이어야 합니다."),
                content_type='text/event-stream'
            )

        custom_instructions = request.POST.get('custom_instructions', '').strip() or None

        # 꼬리질문: 부모 로그가 있으면 검색어 변환·답변 생성에 컨텍스트로 사용
        parent = None
        parent_pk = request.POST.get('parent_pk', '').strip()
        if parent_pk:
            parent = LearningLog.objects.filter(pk=parent_pk).first()

        return StreamingHttpResponse(
            self._process_stream(query, custom_instructions, parent),
            content_type='text/event-stream'
        )

    TOTAL_STEPS = 6  # 에이전트 노드 4 + 태그/마크다운 + 저장

    def _process_stream(self, query, custom_instructions=None, parent=None):
        try:
            service = LearnlogService()
            agent = build_search_agent(service)
            total = self.TOTAL_STEPS

            yield self._sse_event('progress', {'step': 1, 'total': total, 'message': '내 학습 기록 검색 중...'})

            # updates: 노드 완료 시점의 상태 변화 / custom: generate 노드의 토큰
            state = {}
            stream = agent.stream(
                {'query': query, 'custom_instructions': custom_instructions, 'parent': parent},
                stream_mode=['updates', 'custom'],
            )
            for mode, chunk in stream:
                if mode == 'custom':
                    if 'token' in chunk:
                        yield self._sse_event('stream_token', {'token': chunk['token']})
                    continue

                for node, delta in chunk.items():
                    state.update(delta or {})
                    if node == 'retrieve_logs':
                        yield self._sse_event('progress', {'step': 2, 'total': total, 'message': '검색 경로 판단 중...'})
                    elif node == 'router':
                        if state.get('need_web', True):
                            yield self._sse_event('progress', {'step': 3, 'total': total, 'message': '공식 문서 검색 중...'})
                        else:
                            yield self._sse_event('progress', {'step': 4, 'total': total, 'message': 'AI 답변 생성 중... (기존 기록으로 충분)'})
                    elif node == 'web_search':
                        yield self._sse_event('progress', {'step': 4, 'total': total, 'message': 'AI 답변 생성 중...'})

            ai_answer = state.get('answer', '')
            search_results = state.get('search_results') or {'results': []}
            answer_source = state.get('answer_source', 'none')  # generate 노드가 라우터 판단으로 산출

            yield self._sse_event('progress', {'step': 5, 'total': total, 'message': '태그 추출 + 마크다운 변환 중...'})
            with ThreadPoolExecutor(max_workers=2) as executor:
                tags_future = executor.submit(service.extract_tags, query, ai_answer)
                md_future = executor.submit(service.convert_to_markdown, query, ai_answer, search_results)
                tag_names = tags_future.result()
                markdown = md_future.result()

            yield self._sse_event('progress', {'step': 6, 'total': total, 'message': '저장 중...'})

            log = service.save_learning_log(
                query, ai_answer, markdown, search_results, tag_names, parent=parent,
                answer_source=answer_source,
                is_truncated=state.get('truncated', False),
            )

            # 모순 검증은 비동기 — 환각의 피해는 읽는 순간이 아니라 저장된 기록이 복습으로
            # 암기되는 것이라, 응답을 막지 않고 저장 후 검사해서 배지로만 표시한다
            if answer_source != 'none':
                used_web = answer_source in ('both', 'web')
                threading.Thread(
                    target=self._verify_in_background,
                    args=(
                        log.pk,
                        state.get('retrieved_logs') if answer_source in ('both', 'logs') else None,
                        # 생성에 쓴 절삭 길이 그대로
                        RETRIEVED_LIMIT_WEB if used_web else RETRIEVED_LIMIT_NO_WEB,
                        search_results if used_web else None,
                    ),
                    daemon=True,
                ).start()

            result_html = render_to_string('search/partials/result.html', {
                'log': log,
                'exercise_types': EXERCISE_TYPES,
            })
            yield self._sse_event('complete', {'html': result_html})

        except Exception as e:
            error_html = render_to_string('search/partials/error.html', {'error_message': str(e)})
            yield self._sse_event('error', {'html': error_html})

    @staticmethod
    def _verify_in_background(log_pk, retrieved_logs, retrieved_limit, search_results):
        """저장 후 비동기 모순 검증 — 스레드별 DB 커넥션을 마지막에 정리한다"""
        from django.db import connection
        try:
            log = LearningLog.objects.filter(pk=log_pk).first()
            if log is None:
                return
            LearnlogService().verify_log(log, retrieved_logs, retrieved_limit, search_results)
        except Exception as e:
            print(f"비동기 검증 스레드 오류: {e}")
        finally:
            connection.close()

    def _error_stream(self, message):
        error_html = render_to_string('search/partials/error.html', {'error_message': message})
        yield self._sse_event('error', {'html': error_html})

    def _sse_event(self, event_type, data):
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================
# 학습기록 리스트 API
# ============================================

class LogDetailAPIView(APIView):
    """학습로그 상세 - GET: 모달 HTML, PATCH: 부분 수정"""
    authentication_classes = []
    permission_classes = []

    def get(self, request, pk):
        try:
            log = LearningLog.objects.prefetch_related('tags', 'references').get(pk=pk)
            log.increment_view_count()
            return render(request, 'search/partials/log_detail_modal.html', {
                'log': log,
                'exercise_types': EXERCISE_TYPES,
            })
        except LearningLog.DoesNotExist:
            return HttpResponse('<div class="alert alert-error">로그를 찾을 수 없습니다.</div>')

    def patch(self, request, pk):
        try:
            log = LearningLog.objects.get(pk=pk)
        except LearningLog.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = LearningLogUpdateSerializer(log, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ============================================
# 일일 학습일지 API
# ============================================

class JournalPopupAPIView(View):
    """
    일일 학습일지 팝업 - HTMX GET (메인 페이지 load 시 호출).
    오늘 이전 가장 최근 활동일의 일지를 lazy 생성해서 모달로 반환.
    다시보지않기 처리된 일지면 빈 응답.
    """
    def get(self, request):
        try:
            journal = JournalService().get_pending_popup()
        except Exception as e:
            print(f"일지 팝업 오류: {e}")
            return HttpResponse('')  # 팝업은 부가 기능 — 실패해도 메인 흐름을 막지 않는다

        if journal is None:
            return HttpResponse('')
        return render(request, 'search/partials/journal_popup.html', {'journal': journal})


class JournalDismissAPIView(View):
    """일지 팝업 '다시 보지 않기' - HTMX POST"""
    def post(self, request, pk):
        try:
            journal = DailyJournal.objects.get(pk=pk)
        except DailyJournal.DoesNotExist:
            return HttpResponse('')
        journal.is_dismissed = True
        journal.save(update_fields=['is_dismissed'])
        return HttpResponse('')  # 모달 영역을 빈 내용으로 교체 → 닫힘
