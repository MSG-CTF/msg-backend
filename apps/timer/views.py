from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status as http_status

from .models import Contest
from .serializers import ContestSerializer


@api_view(['GET'])
def ping(request): #ping API
    return Response( {"message" : "pong"} )

@api_view(['GET'])
def contest_timer(request):
    contest = Contest.objects.filter(is_active=True).first()

    if contest is None:
        return Response(
            {"detail": "활성화된 대회가 없습니다."},
            status=http_status.HTTP_404_NOT_FOUND,
        )
    
    serializer = ContestSerializer(contest)
    return Response(serializer.data) #활성 대회 꺼내서 JSON으로 변환