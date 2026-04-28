from django.contrib import admin
from .models import LearningLog, Tag, Reference, Exercise, ExerciseAttempt, Streak

admin.site.register(LearningLog)
admin.site.register(Tag)
admin.site.register(Reference)
admin.site.register(Exercise)
admin.site.register(ExerciseAttempt)
admin.site.register(Streak)