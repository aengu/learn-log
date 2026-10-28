# Practice 앱

연습문제 출제·채점·간격 반복과 러닝메이트 풀이를 담당한다.
학습 로그는 `search.LearningLog`를 FK로 참조하며, 스트릭·통계·일일 학습일지는
`search`에서 `practice`의 풀이 기록을 조회한다.

- URL namespace: `practice` (기존 `/exercises/`, `/api/exercises/` 주소 유지)
- 템플릿: `practice/templates/practice/`
- 테스트: `docker compose exec web pytest practice/tests`
- 커버리지: `docker compose exec web pytest --cov=search --cov=practice --cov-branch`

## 기존 데이터 이전

`search.0011`과 `practice.0001`은 `SeparateDatabaseAndState`로 Django 모델 상태만
옮긴다. 기존 `search_exercise`, `search_exerciseattempt`, `search_mateattempt`
테이블명·PK·FK·인덱스는 유지한다. `practice.0001`은 기존 content type의 앱 이름만
수정하므로 권한·그룹·관리자 이력에서 참조하는 content type PK도 유지된다.

배포 시 평소처럼 `python manage.py migrate`를 실행한다. 새 DB에서는 기존 search
마이그레이션이 먼저 테이블을 만들고, 이후 practice 소속으로 이동한다.
`--fake`나 데이터 복사·테이블 재생성은 필요하지 않다.

앱 분리 단계에는 태깅·풀이 시간 수집·BKT를 추가하지 않는다.
