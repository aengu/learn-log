"""추가 LLM 호출 없이 Markdown의 섹션·코드·조건을 온전히 보존한다."""

import re

from search.domains import is_official_doc


CONTEXT_LIMIT = 6000


def _headings(text):
    # 코드 안의 # 주석을 헤딩으로 오인하지 않는다. 닫히지 않은 fence는 출제하지 않는다.
    lines = text.splitlines(keepends=True)
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip())
        if marker:
            token, suffix = marker.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = None
            continue
        if fence is None:
            heading = re.match(r'^ {0,3}(#{1,6})\s+(.+)', line)
            if heading:
                headings.append((index, len(heading[1]), heading[2].strip()))
    if fence is not None:
        raise ValueError('학습 자료의 코드 블록이 닫히지 않아 출제할 수 없습니다.')
    return lines, headings


def _compact_references(log, text):
    lines, headings = _headings(text)
    removed, bibliography = set(), []
    for position, (start, depth, title) in enumerate(headings):
        if not re.search(r'참고\s*(자료|문헌|링크)|참조\s*문서|참고\s*출처|\breferences\b|\bsources\b', title, re.IGNORECASE):
            continue
        stop = next((index for index, level, _ in headings[position + 1:] if level <= depth), len(lines))
        bibliography.append(''.join(lines[start:stop]))
        removed.update(range(start, stop))

    reference_text = '\n'.join(bibliography)
    urls = list(dict.fromkeys(url.rstrip(').,') for url in re.findall(r'https?://[^\s<>|\]]+', reference_text)))
    official = []
    if hasattr(log, 'references'):
        official = list(log.references.filter(source_type='official').order_by('pk').values_list('url', flat=True))
    # 저장 순서는 검색 순위가 아니므로 Markdown 참고 자료에 등장하는 순서를 우선한다.
    selected = next((url for url in urls if url in official or is_official_doc(url)), None)
    if selected is None and official:
        selected = official[0]  # 본문에 없으면 등록 순서로 선택해 결과를 재현한다.
    body = ''.join(line for index, line in enumerate(lines) if index not in removed).strip()
    reference = f'\n\n참고 공식문서: {selected}' if selected else ''
    return body, reference, selected


def build_exercise_context(log, limit=CONTEXT_LIMIT):
    source = (log.markdown_content or log.ai_response or '').strip()
    if not source:
        raise ValueError('출제할 학습 자료가 없습니다.')
    text, reference, reference_url = _compact_references(log, source)
    if not text:
        raise ValueError('출제할 학습 자료가 없습니다.')
    body_limit = limit - len(reference)
    audit = {'source_chars': len(source), 'reference_url': reference_url}
    if len(text) <= body_limit:
        context = text + reference
        return context, {**audit, 'selected_chars': len(context), 'omitted_sections': 0}
    lines, headings = _headings(text)

    # 최상위 제목 하나 아래에 여러 하위 섹션이 있는 문서는 하위 섹션으로 나눈다.
    levels = sorted({level for _, level, _ in headings})
    level = levels[0] if levels else 1
    if levels and sum(depth == level for _, depth, _ in headings) == 1 and len(levels) > 1:
        level = levels[1]
    starts = sorted({0, *(index for index, depth, _ in headings if depth <= level)})
    sections = [''.join(lines[a:b]).strip() for a, b in zip(starts, starts[1:] + [len(lines)])]
    sections = [section for section in sections if section]

    # 한 섹션의 설명·코드·뒤의 예외를 함께 유지한다. 과대 섹션은 절단하지 않고 제외한다.
    priority = lambda item: (
        0 if any(word in item[1].split('\n', 1)[0].lower() for word in ('조건', '예외', '주의', '전제', '제약', 'caveat', 'limitation')) else 1,
        item[0],
    )
    selected, used = [], 0
    for index, section in sorted(enumerate(sections), key=priority):
        if used + len(section) + 2 <= body_limit:
            selected.append((index, section))
            used += len(section) + 2
    context = '\n\n'.join(section for _, section in sorted(selected))
    if not context:
        raise ValueError('학습 자료가 출제 예산을 초과합니다. 코드·조건을 보존할 수 있도록 자료를 나눠주세요.')
    context += reference
    return context, {
        **audit,
        'selected_chars': len(context),
        'omitted_sections': len(sections) - len(selected),
    }
