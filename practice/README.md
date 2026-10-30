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

## 출제와 개념 태깅

- 새 `path_trace`는 개념 구분·상황 적용·실행 추적 중 자료에 맞는 사고 유형으로 출제한다.
- step마다 `concept_id` 또는 `concept_candidate`, `cognitive_type`,
  `estimated_difficulty`를 저장한다. 기존 문제 JSON은 변경하지 않는다.
- `Concept`은 관리자가 검토해 등록하는 사전이다. 관련 등록 개념을 최대 10개 전달하며,
  사전에 없는 개념은 문항 JSON의 `concept_candidate`에 이름·설명을 남긴다.
  후보는 자동으로 정식 개념이나 숙련도 계산 대상으로 등록되지 않는다.
- 관리자에서 후보와 기존 사전을 검토해 개념을 등록하고, 필요한 기존 문항의
  `concept_id` 연결도 검토한다. 이름만으로 자동 병합하지 않는다.
- 난이도는 출제 시 추정치이며 아직 경험적 난이도나 BKT 파라미터로 사용하지 않는다.
- 자료는 Markdown을 우선 사용하고, 없으면 원문 답변을 사용한다. 6,000자 이하는
  전체 전달하며 긴 자료는 섹션 단위로 선택한다. 조건·예외 섹션을 우선하고,
  섹션 안의 설명·코드·후속 조건을 함께 보존한다. 과대 섹션은 통째로 제외하고,
  닫히지 않은 코드나 선택 가능한 섹션이 없는 자료는 출제 실패로 처리한다.
- 참고 자료 섹션은 제거하고 공식문서 URL 하나만 출처로 덧붙인다. 등록된 공식 출처나
  기존 공식문서 도메인 목록을 기준으로 판단하며, Markdown 참고 자료의 첫 공식문서를
  우선한다. 본문에 없으면 연결된 공식 출처 중 등록 순서로 선택한다. 공식문서가 없으면
  링크를 생략한다. URL 본문을 가져오는 추가 네트워크 요청은 하지 않는다.
- `_generation`에 프롬프트 버전·입력 길이·생략 섹션 수·제공 개념 ID를 기록한다.
- `correct_answer`는 인덱스 일치 검사를 위해 유지한다. 형식·태그 검증은
  기술적 사실성이나 정답 유일성을 보증하지 않는다. 해당 품질은 실제 문항으로 검토한다.
- 러닝메이트에는 기존과 같이 질문·보기만 전달하며 태그·정답·오답 해설은 제외한다.

풀이 시간 수집·BKT·학습 리포트는 다음 단계에서 추가한다.
