from django.contrib import admin
from .models import LearningLog, Tag, Reference
# Register your models here.

admin.site.register(LearningLog)
admin.site.register(Tag)
admin.site.register(Reference)