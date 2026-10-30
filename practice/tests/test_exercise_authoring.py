"""자료 보존, 출제 태그 검증, 저장까지의 출제 경로를 보호한다."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from practice.models import Concept
from practice.services.exercise_context import build_exercise_context
from practice.services.exercise_service import ExerciseService
from search.tests.factories import LearningLogFactory

from .test_exercise_service import make_step


def test_short_markdown_keeps_code_and_conditions():
    markdown = '## 동작\n설명\n```python\n# 주석은 헤딩이 아니다\nx = 1\n```\n단, 오류가 나면 취소된다.'
    context, audit = build_exercise_context(SimpleNamespace(markdown_content=markdown))
    assert context == markdown
    assert audit['omitted_sections'] == 0


def test_long_markdown_skips_entire_oversized_code_section():
    code = '## 코드\n전제 설명\n```python\n' + 'x = 1\n' * 100 + '```\n단, 실패 시 취소한다.'
    warning = '## 주의사항\n예외 처리 위치에 따라 결과가 달라진다.'
    context, audit = build_exercise_context(
        SimpleNamespace(markdown_content=code + '\n\n' + warning), limit=150,
    )
    assert context == warning
    assert '```' not in context
    assert audit['omitted_sections'] == 1


def test_long_markdown_keeps_whole_code_section_with_its_condition():
    section = '## 코드\n전제\n```python\nx = 1\n```\n단, 예외가 발생하면 취소한다.'
    markdown = section + '\n\n## 부가 설명\n' + '긴 부가 설명 ' * 100
    context, _ = build_exercise_context(SimpleNamespace(markdown_content=markdown), limit=150)
    assert context == section


def test_unclosed_code_fails_before_requesting_a_question():
    with pytest.raises(ValueError, match='코드 블록이 닫히지'):
        build_exercise_context(SimpleNamespace(markdown_content='```python\nx = 1'))


def test_reference_section_keeps_only_first_official_url():
    markdown = (
        '## 개념\n학습 설명\n\n## 참고 자료\n'
        '- [블로그](https://example.com/blog)\n'
        '- [AWS](https://docs.aws.amazon.com/first)\n'
        '- [Python](https://docs.python.org/3/tutorial/)\n'
        '위 내용은 노션에 복사하세요.'
    )
    context, audit = build_exercise_context(SimpleNamespace(markdown_content=markdown))
    assert '학습 설명' in context
    assert context.count('https://') == 1
    assert 'https://docs.aws.amazon.com/first' in context
    assert 'example.com' not in context
    assert '복사하세요' not in context
    assert audit['reference_url'] == 'https://docs.aws.amazon.com/first'
    assert audit['selected_chars'] == len(context)


def test_no_official_reference_preserves_later_sections_and_code():
    markdown = (
        '## 예시\n```python\n## 참고 자료\nx = 1\n```\n'
        '## 참고 자료\n- https://example.com/blog\n'
        '## 주의\n예외 조건은 유지한다.'
    )
    context, audit = build_exercise_context(SimpleNamespace(markdown_content=markdown))
    assert '```python\n## 참고 자료\nx = 1\n```' in context
    assert '예외 조건은 유지한다.' in context
    assert 'example.com' not in context
    assert audit['reference_url'] is None


@pytest.mark.django_db
def test_registered_official_reference_can_supply_missing_bibliography():
    from search.tests.factories import ReferenceFactory

    log = LearningLogFactory(markdown_content='## 개념\n내용')
    reference = ReferenceFactory(url='https://official.example.org/docs', source_type='official')
    log.references.add(reference)
    context, audit = build_exercise_context(log)
    assert reference.url in context
    assert audit['reference_url'] == reference.url


def tagged_step():
    step = make_step()
    step.update({
        'concept_id': None,
        'concept_candidate': {'name': '트랜잭션 격리', 'description': '동시 실행 시 데이터 가시성'},
        'cognitive_type': 'application',
        'estimated_difficulty': 'medium',
    })
    return step


@pytest.mark.django_db
def test_generation_saves_tags_and_candidates_without_registering_them(monkeypatch):
    concept = Concept.objects.create(id='db.isolation', name='트랜잭션 격리', description='데이터 가시성')
    log = LearningLogFactory(query='트랜잭션 격리는?', markdown_content='## 격리\n동시 실행 시 가시성을 제어한다.')
    service = ExerciseService.__new__(ExerciseService)
    existing = tagged_step()
    existing.update(concept_id=concept.pk, concept_candidate=None)
    proposed = tagged_step()
    proposed['concept_candidate'] = {'name': '스냅샷', 'description': '특정 시점의 데이터 뷰'}
    # 동일 유형의 오답 3개도 적절할 수 있으므로 유형 다양성은 강제하지 않는다.
    for distractor in existing['distractors']:
        distractor['type'] = 'adjacent'
    response = {'steps': [existing, proposed, deepcopy(existing)]}
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return deepcopy(response)

    monkeypatch.setattr(service, '_call_json', generate)
    exercise = service.generate_exercise(log, 'path_trace')
    exercise.refresh_from_db()

    assert len(prompts) == 1
    assert 'db.isolation' in prompts[0]
    assert log.markdown_content in prompts[0]
    assert exercise.content['steps'][0]['concept_id'] == concept.pk
    assert exercise.content['steps'][1]['concept_candidate']['name'] == '스냅샷'
    assert Concept.objects.count() == 1
    assert exercise.content['_generation']['prompt_version'] == 'choice-v2'


@pytest.mark.django_db
@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('concept_id', 'not.registered', 'invalid-concept-id'),
    ('cognitive_type', ['application'], 'unknown-cognitive-type'),
    ('estimated_difficulty', 'expert', 'unknown-difficulty'),
    ('concept_candidate', None, 'missing-concept-candidate'),
    ('distractors', [], 'distractor-index-mismatch'),
    ('choices', ['중복', '중복', '다른 보기', '또 다른 보기'], 'duplicate-choices'),
])
def test_invalid_generated_fields_are_dropped(field, value, reason):
    step = tagged_step()
    step[field] = value
    result = ExerciseService._filter_valid_steps({'steps': [step]}, require_tags=True)
    assert result['steps'] == []
    assert reason in result['_audit']['dropped'][0]['reason']


@pytest.mark.django_db
def test_malformed_response_retries_and_fails_without_saving(monkeypatch):
    service = ExerciseService.__new__(ExerciseService)
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return ['invalid root']

    monkeypatch.setattr(service, '_call_json', generate)
    log = LearningLogFactory(markdown_content='## 개념\n학습 자료')
    with pytest.raises(ValueError, match='유효한 step이 0개'):
        service.generate_exercise(log, 'path_trace')
    assert len(calls) == 2
    assert not log.exercises.exists()
