import factory
from search.models import Tag, Reference, LearningLog


class TagFactory(factory.django.DjangoModelFactory):
    """태그 팩토리 - slug 기준 get_or_create로 중복 방지"""
    class Meta:
        model = Tag
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"tag-{n}")
    slug = factory.LazyAttribute(lambda o: o.name)


class ReferenceFactory(factory.django.DjangoModelFactory):
    """레퍼런스 팩토리 - url 기준 get_or_create로 중복 방지"""
    class Meta:
        model = Reference
        django_get_or_create = ("url",)

    url = factory.Sequence(lambda n: f"https://example.com/doc-{n}")
    title = factory.Sequence(lambda n: f"Reference {n}")
    excerpt = "핵심 내용 발췌"
    source_type = "official"


class LearningLogFactory(factory.django.DjangoModelFactory):
    """학습로그 팩토리 - tags, references는 명시적으로 전달할 때만 연결"""
    class Meta:
        model = LearningLog

    query = factory.Sequence(lambda n: f"질문 {n} 번째")
    ai_response = "ai 질문"
    markdown_content = "## 마크다운"

    @factory.post_generation
    def tags(self, create, extracted, **kwargs):
        if not create or not extracted:
            return
        self.tags.set(extracted)

    @factory.post_generation
    def references(self, create, extracted, **kwargs):
        if not create or not extracted:
            return
        self.references.set(extracted)