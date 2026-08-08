from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Contest
from .serializers import ContestSerializer


@api_view(["GET"])
def contest_timer(request):
    contest = Contest.objects.filter(is_active=True).first()

    if contest is None:
        return Response({
            "code": "SUCCESS",
            "message": "진행 중인 대회가 없습니다.",
            "data": None,
        })

    serializer = ContestSerializer(contest)
    return Response({
        "code": "SUCCESS",
        "message": "성공",
        "data": serializer.data,
    })
