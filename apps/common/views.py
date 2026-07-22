from django.shortcuts import render
from django.views.generic import TemplateView

# Create your views here.

class ApiTestView(TemplateView):
    template_name = 'common/test.html'
