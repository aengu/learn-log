from django.contrib import admin
from .models import (
    DailyJournal,
    LearningLog,
    Reference,
    Streak,
    Tag,
)

admin.site.register(LearningLog)
admin.site.register(Tag)
admin.site.register(Reference)
admin.site.register(Streak)
admin.site.register(DailyJournal)
