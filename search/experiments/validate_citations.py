"""
인라인 인용 실측: 인용 달림률·무효 인용(지어낸 번호) 비율 측정.

배경:
  생성 프롬프트에 번호 참고([1]~[3]) + 인용 지시(CITATION_RULE)를 넣고,
  sanitize_citations가 범위 밖 번호를 결정적으로 제거한다. 프롬프트 변경이므로
  프로젝트 원칙대로 효과를 실측한다:
    - 인용 포함률: 답변에 [n]이 하나 이상 달리는가 (지시가 실제로 작동하는가)
    - 무효 인용률: 모델이 [4]+처럼 없는 번호를 지어내는 비율 (가드가 걸러낸 양)

실행 (프로젝트 루트에서, MISTRAL_API_KEY 필요 — 검색 API는 안 씀):
  docker compose exec web python search/experiments/validate_citations.py
  docker compose exec web python search/experiments/validate_citations.py --n 5

결과: search/experiments/results/citations_{시각}.jsonl + 콘솔 요약
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django  # noqa: E402

django.setup()

from search.services import LearnlogService  # noqa: E402

RESULTS_DIR = Path(__file__).parent / 'results'
CALL_GAP_SEC = 2

# 고정 픽스처 — 검색 API 없이 인용 메커니즘만 측정 (사실 위주 발췌 3건)
QUERY = 'Django에서 select_related와 prefetch_related의 차이'
SEARCH_RESULTS = {'results': [
    {
        'url': 'https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-related',
        'title': 'QuerySet API reference — select_related',
        'content': (
            'select_related returns a QuerySet that will "follow" foreign-key relationships, '
            'selecting additional related-object data when it executes its query. '
            'This is a performance booster which results in a single more complex query but means '
            'later use of foreign-key relationships won\'t require database queries. '
            'select_related is limited to single-valued relationships - foreign key and one-to-one.'
        ),
    },
    {
        'url': 'https://docs.djangoproject.com/en/5.2/ref/models/querysets/#prefetch-related',
        'title': 'QuerySet API reference — prefetch_related',
        'content': (
            'prefetch_related does a separate lookup for each relationship, and does the "joining" '
            'in Python. This allows it to prefetch many-to-many, many-to-one, and GenericRelation '
            'objects which cannot be done using select_related. prefetch_related executes one query '
            'per relationship by default.'
        ),
    },
    {
        'url': 'https://docs.djangoproject.com/en/5.2/topics/db/optimization/',
        'title': 'Database access optimization',
        'content': (
            'Understand QuerySet evaluation and use select_related and prefetch_related to cut down '
            'the number of database queries. Profile first: use QuerySet.explain() and tools like '
            'django-debug-toolbar to find where queries come from.'
        ),
    },
]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--n', type=int, default=10, help='생성 반복 횟수')
    parser.add_argument('--gap', type=float, default=CALL_GAP_SEC)
    args = parser.parse_args()

    svc = LearnlogService()

    # sanitize 전/후를 모두 보기 위해 래핑 (validate_runtime_guard의 트래킹 패턴)
    original = svc.sanitize_citations
    captured = {}

    def tracking(answer, search_results):
        captured['raw'] = answer
        cleaned = original(answer, search_results)
        captured['clean'] = cleaned
        return cleaned

    svc.sanitize_citations = tracking

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"citations_{datetime.now():%H%M%S}.jsonl"
    rows = []

    print(f'>> {args.n}회 생성 측정 시작...\n', flush=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        for i in range(args.n):
            time.sleep(args.gap)
            captured.clear()
            answer = svc.generate_answer(QUERY, SEARCH_RESULTS)
            if 'raw' not in captured:  # API 오류 등으로 sanitize까지 못 감
                print(f'  [{i + 1}/{args.n}] 생성 실패: {answer[:60]}')
                rows.append({'ok': False})
                continue

            raw_cites = LearnlogService.CITATION_PATTERN.findall(captured['raw'])
            clean_cites = LearnlogService.CITATION_PATTERN.findall(captured['clean'])
            row = {
                'ok': True,
                'citations_raw': len(raw_cites),
                'citations_valid': len(clean_cites),
                'citations_invalid': len(raw_cites) - len(clean_cites),
                'distinct_refs_cited': sorted(set(clean_cites)),
                'answer': captured['clean'],
            }
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(f"  [{i + 1}/{args.n}] 인용 {row['citations_valid']}개 "
                  f"(무효 제거 {row['citations_invalid']}개, 참조 {row['distinct_refs_cited']})",
                  flush=True)

    ok_rows = [r for r in rows if r['ok']]
    with_cite = sum(1 for r in ok_rows if r['citations_valid'] > 0)
    total_raw = sum(r['citations_raw'] for r in ok_rows)
    total_invalid = sum(r['citations_invalid'] for r in ok_rows)

    print(f'\n=== 요약 (N={args.n}, 성공 {len(ok_rows)}) ===')
    if ok_rows:
        print(f'  인용 포함 답변: {with_cite}/{len(ok_rows)} ({with_cite / len(ok_rows) * 100:.0f}%)'
              f'  ← 낮으면 CITATION_RULE 지시 강화 필요')
        print(f'  답변당 평균 인용: {sum(r["citations_valid"] for r in ok_rows) / len(ok_rows):.1f}개')
        if total_raw:
            print(f'  무효 인용(지어낸 번호): {total_invalid}/{total_raw} '
                  f'({total_invalid / total_raw * 100:.1f}%) — 가드가 전부 제거함')
    print(f'\n상세(답변 원문 포함): {out_path}')


if __name__ == '__main__':
    main()
