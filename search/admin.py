from django.contrib import admin
from practice.services.bkt_lock import BKTLockedAdminMixin
from .models import (
    DailyJournal,
    LearningLog,
    Reference,
    Streak,
    Tag,
)

@admin.register(LearningLog)
class LearningLogAdmin(BKTLockedAdminMixin, admin.ModelAdmin):
    pass


admin.site.register(Tag)
admin.site.register(Reference)
admin.site.register(Streak)
admin.site.register(DailyJournal)
