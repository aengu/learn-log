from django.contrib import admin
from .models import (
    DailyJournal,
    Exercise,
    ExerciseAttempt,
    LearningLog,
    MateAttempt,
    Reference,
    Streak,
    Tag,
)

admin.site.register(LearningLog)
admin.site.register(Tag)
admin.site.register(Reference)
admin.site.register(Exercise)
admin.site.register(ExerciseAttempt)
admin.site.register(MateAttempt)
admin.site.register(Streak)
admin.site.register(DailyJournal)
