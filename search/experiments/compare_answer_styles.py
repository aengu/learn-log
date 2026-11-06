"""설명 라우팅 전후를 동일한 질문·검색 자료·생성 설정으로 비교한다.

실행: docker compose exec web python -m search.experiments.compare_answer_styles
옵션: --query '2PC가 뭐야?' --repeats 2 --no-web --no-retrieval

DB는 읽기 전용으로 사용하며 답변을 저장하지 않는다. JSONL에는 검색 스냅샷,
라우터 선택·소요시간, 실제 프롬프트·답변·형식 지표가 남는다.
형식 지표는 사실 정확도나 설명 품질의 점수가 아니다. 원문을 함께 검토해야 한다.
"""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time

import django
from groq import RateLimitError

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import connection  # noqa: E402

from search.models import LearningLog  # noqa: E402
from search.services.learnlog_service import (  # noqa: E402
    LearnlogService,
    RETRIEVED_LIMIT_NO_WEB,
    RETRIEVED_LIMIT_WEB,
)


QUERIES = [
    '2PC가 뭐야?',
    'email 컬럼이 중복된 사용자를 조회하는 SQL을 작성해줘',
    'Django QuerySet으로 email이 중복된 사용자를 조회하려면?',
    'Python으로 이진 탐색을 구현해줘',
    'Java로 이진 탐색을 구현해줘',
    'Saga와 2PC는 어떤 차이가 있고 언제 선택해야 해?',
]


def format_metrics(answer):
    """언어별 블록·Django 의존성 등 관찰값이며, 좋고 나쁨을 자동 판정하지 않는다."""
    return {
        'chars': len(answer),
        'code_languages': re.findall(r'^[ \t]*```([^\n`]*)', answer, re.MULTILINE)[::2],
        'django_mentions': len(re.findall(r'django', answer, re.IGNORECASE)),
        'django_import': bool(re.search(r'\b(?:from|import)\s+django\b', answer)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', action='append', help='반복 지정 가능. 미지정 시 대표 질문 6개')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--no-web', action='store_true', help='웹 컨텍스트 없이 양쪽 모두 비교')
    parser.add_argument('--no-retrieval', action='store_true', help='과거 로그 검색 없이 비교')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats는 1 이상이어야 합니다')
    output = args.output
    if output is None:
        directory = Path(tempfile.mkdtemp(prefix='learnlog-answer-styles-'))
        output = directory / 'comparison.jsonl'

    # 실수로 저장 메서드가 추가되어도 실제 학습 기록은 변경하지 못하게 한다.
    with connection.cursor() as cursor:
        cursor.execute('SET default_transaction_read_only = on')
    count_before = LearningLog.objects.count()
    service = LearnlogService()
    print(f'결과 파일: {output}', flush=True)
    with output.open('w', encoding='utf-8') as results:
        for query in args.query or QUERIES:
            logs = [] if args.no_retrieval else service.retrieve_similar_logs(query)
            started = time.perf_counter()
            decision = service.decide_route(query, logs)
            router_seconds = round(time.perf_counter() - started, 3)
            use_web = decision['need_web'] and not args.no_web
            search_results = service.search_official_docs(query) if use_web else {'results': []}
            used_logs = logs if decision['use_logs'] else []
            limit = RETRIEVED_LIMIT_WEB if use_web else RETRIEVED_LIMIT_NO_WEB
            # 두 생성 경로에 동일한 검색 결과와 로그 절삭 예산을 사용한다.
            snapshot = {
                'search_results': search_results,
                'retrieved_logs': [{'id': log.pk, 'query': log.query, 'answer': log.ai_response[:limit]} for log in used_logs],
                'retrieved_limit': limit,
                'no_web': args.no_web,
                'no_retrieval': args.no_retrieval,
            }
            for repeat in range(args.repeats):
                # 순서 효과를 줄이기 위해 반복 시 baseline/routed 순서를 교대로 바꾼다.
                variants = ['baseline', 'routed'] if repeat % 2 == 0 else ['routed', 'baseline']
                for variant in variants:
                    policy = {} if variant == 'baseline' else {
                        'answer_focus': decision['answer_focus'],
                        'example_stack': decision['example_stack'],
                    }
                    meta = {}
                    started = time.perf_counter()
                    for attempt in range(3):
                        try:
                            answer = ''.join(service.generate_answer_stream(
                                query, search_results, retrieved_logs=used_logs,
                                retrieved_limit=limit, meta=meta, **policy,
                            ))
                            break
                        except RateLimitError:
                            if attempt == 2:
                                raise
                            print('분당 토큰 제한: 15초 후 같은 입력으로 재시도합니다.', flush=True)
                            time.sleep(15)
                    record = {
                        'query': query, 'repeat': repeat + 1, 'variant': variant,
                        'decision': decision, 'router_seconds': router_seconds,
                        'generation_seconds': round(time.perf_counter() - started, 3),
                        'rate_limit_retries': attempt,
                        'finish_reason': meta.get('finish_reason'),
                        'model': service.ANSWER_MODEL, 'temperature': 0.7, 'max_tokens': 3000,
                        'context': snapshot,
                        'prompt': service._build_answer_prompt(
                            query, search_results, retrieved_logs=used_logs, retrieved_limit=limit, **policy,
                        ),
                        'answer': answer, 'metrics': format_metrics(answer),
                    }
                    results.write(json.dumps(record, ensure_ascii=False) + '\n')
                    results.flush()
                    print(json.dumps({
                        key: record[key] for key in (
                            'query', 'repeat', 'variant', 'decision', 'router_seconds',
                            'generation_seconds', 'finish_reason', 'metrics',
                        )
                    }, ensure_ascii=False), flush=True)
    assert LearningLog.objects.count() == count_before, '비교 중 학습 로그 개수가 변경됐습니다'
    print(f'학습 로그 {count_before}개 유지. 원문을 보고 설명 방식과 내용 품질을 함께 확인하세요.', flush=True)


if __name__ == '__main__':
    main()
