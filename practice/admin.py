from django.contrib import admin

from .models import Exercise, ExerciseAttempt, MateAttempt

admin.site.register(Exercise)
admin.site.register(ExerciseAttempt)
admin.site.register(MateAttempt)
