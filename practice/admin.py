from django.contrib import admin

from .models import Concept, Exercise, ExerciseAttempt, MateAttempt


@admin.register(Concept)
class ConceptAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'is_active']
    search_fields = ['id', 'name', 'description']
    list_filter = ['is_active']

admin.site.register(Exercise)
admin.site.register(ExerciseAttempt)
admin.site.register(MateAttempt)
