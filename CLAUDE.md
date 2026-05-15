# CLAUDE.md

## Project Overview

LearnLog — Django 기반 AI 학습 도구. 질문 → LLM 답변 생성 → 학습 로그 저장 → 간격 반복 연습문제 → 통계 대시보드.

## Tech Stack

- Backend: Django 5, DRF, PostgreSQL 18
- Frontend: HTMX, Tailwind CSS, DaisyUI (라이브러리 추가 없이 순수 CSS/JS 선호)
- LLM: Groq (Llama), Mistral
- Search: Tavily, PostgreSQL Full-Text Search
- Dev: Docker Compose, pytest, factory-boy

## Commands

```bash
# 로컬 실행
docker compose up -d

# 테스트
docker compose exec web pytest

# 마이그레이션
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate

# DB 동기화
docker compose exec web python manage.py dbpull   # Render → 로컬
docker compose exec web python manage.py dbpush   # 로컬 → Render
```

## Project Structure

```
search/
├── models.py           # LearningLog, Exercise, ExerciseAttempt, Streak
├── views.py            # 페이지 뷰
├── api_views.py        # API 뷰 (질문 생성, 채점 등)
├── services/           # LLM 호출, 연습문제 생성/채점
├── signals.py          # Streak 자동 갱신 (post_save)
├── templates/search/   # 템플릿 (DaisyUI 컴포넌트 사용)
└── tests/              # pytest + factory-boy
```

## Principles (based on Karpathy's observations)

### Think Before Coding
- 코드를 작성하기 전에 기존 코드를 먼저 읽고 구조를 파악
- 모호한 요구사항이 있으면 추측하지 말고 질문
- 가정을 명시적으로 표현

### Simplicity First
- 요청된 것 이상의 기능, 리팩토링, 개선 추가 금지
- 불필요한 추상화나 헬퍼 함수 만들지 않기
- 간단한 코드 3줄이 추상화 1개보다 낫다

### Surgical Changes
- 변경이 필요한 부분만 수정
- 주변 코드의 포맷팅이나 스타일을 건드리지 않기
- 기존 코드 컨벤션을 따르기

### Goal-Driven Execution
- 무엇을 달성해야 하는지 먼저 확인
- 검증 가능한 단위로 작업
- 완료 후 동작 확인

## Conventions

- 커밋/푸시는 사용자가 명시적으로 요청할 때만 수행
- 커밋 메시지: `type(scope): 설명` (feat, fix, refactor, docs, test)
- 한국어로 소통, 코드 주석도 한국어 허용
- 템플릿: DaisyUI 컴포넌트 + Tailwind 유틸리티, 반응형은 `sm:` 브레이크포인트 기준
- HTMX: 동적 삽입 후 `htmx.process()` 호출 필수
