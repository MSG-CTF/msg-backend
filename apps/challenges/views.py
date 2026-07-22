# apps/challenges/views.py
import datetime
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import Challenge, ChallengeInstance
from .serializers import (
    ChallengeDetailSerializer, 
    ChallengeInstanceSerializer, 
    FlagSubmitSerializer
)
from .utils import push_instance_task


class ChallengeDetailView(APIView):
    def get(self, request, challenge_id):
        challenge = get_object_or_404(Challenge, id=challenge_id)
        serializer = ChallengeDetailSerializer(challenge)
        return Response(serializer.data, status=status.HTTP_200_OK)


class FlagSubmitView(APIView):
    def post(self, request, challenge_id):
        challenge = get_object_or_404(Challenge, id=challenge_id)
        serializer = FlagSubmitSerializer(data=request.data)
        
        if serializer.is_valid():
            user_flag = serializer.validated_data['flag'].strip()
            if user_flag == challenge.flag:
                return Response({'status': 'success', 'message': '정답입니다!'}, status=status.HTTP_200_OK)
            else:
                return Response({'status': 'fail', 'message': '틀린 플래그입니다.'}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class InstanceCreateView(APIView):
    def post(self, request):
        if not request.user.is_authenticated:
            return Response(
                {"error": {"code": "UNAUTHORIZED", "message": "로그인이 필요합니다."}}, 
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        team_id = getattr(request.user, 'team_id', None)
        if not team_id:
            return Response(
                {"error": {"code": "TEAM_NOT_FOUND", "message": "소속된 팀이 없습니다."}}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        challenge_id = request.data.get('challenge_id')
        challenge = get_object_or_404(Challenge, id=challenge_id)

        existing_instance = ChallengeInstance.objects.filter(
            team_id=team_id, 
            status__in=['RUNNING', 'PENDING']
        ).first()

        if existing_instance:
            return Response(
                {
                    "error": {
                        "code": "INSTANCE_ALREADY_EXISTS",
                        "message": "이미 팀당 1개의 인스턴스가 실행 중이거나 생성 대기 중입니다.",
                        "existing_instance_id": existing_instance.id
                    }
                }, 
                status=status.HTTP_400_BAD_REQUEST
            )

        expires_at = timezone.now() + datetime.timedelta(hours=1)
        instance = ChallengeInstance.objects.create(
            user=request.user,
            challenge=challenge,
            team_id=team_id, 
            status='PENDING',
            expires_at=expires_at
        )

        try:
            push_instance_task(
                action='CREATE',
                instance_id=instance.id,
                challenge_id=challenge.id,
                team_id=team_id
            )
        except Exception as e:
            instance.status = 'FAILED'
            instance.save()
            return Response({
                "error": {
                    "code": "SCHEDULER_UNAVAILABLE",
                    "message": "인스턴스 요청 대기열 연결에 실패했습니다.",
                    "details": str(e)
                }
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        serializer = ChallengeInstanceSerializer(instance)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)


class TeamInstanceStatusView(APIView):
    def get(self, request):
        if not request.user.is_authenticated:
            return Response(
                {"error": {"code": "UNAUTHORIZED", "message": "로그인이 필요합니다."}}, 
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        team_id = getattr(request.user, 'team_id', None)
        if not team_id:
            return Response(
                {"error": {"code": "TEAM_NOT_FOUND", "message": "소속된 팀이 없습니다."}}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        instance = ChallengeInstance.objects.filter(
            team_id=team_id, 
            status__in=['RUNNING', 'PENDING']
        ).first()

        if not instance:
            return Response({'message': '실행 중인 인스턴스가 없습니다.'}, status=status.HTTP_404_NOT_FOUND)
        
        serializer = ChallengeInstanceSerializer(instance)
        return Response(serializer.data, status=status.HTTP_200_OK)


class InstanceResetView(APIView):
    def post(self, request, instance_id):
        instance = get_object_or_404(ChallengeInstance, id=instance_id)
        instance.status = 'PENDING'
        instance.save()

        push_instance_task(
            action='RESET',
            instance_id=instance.id,
            challenge_id=instance.challenge.id
        )
        return Response({'message': f'인스턴스 {instance_id} 재시작 요청이 전달되었습니다.'}, status=status.HTTP_200_OK)


class InstanceDeleteView(APIView):
    def delete(self, request, instance_id):
        instance = get_object_or_404(ChallengeInstance, id=instance_id)
        instance.status = 'STOPPED'
        instance.save()

        push_instance_task(
            action='DELETE',
            instance_id=instance.id,
            challenge_id=instance.challenge.id
        )
        return Response({'message': f'인스턴스 {instance_id}가 종료되었습니다.'}, status=status.HTTP_200_OK)


class InstanceExtendView(APIView):
    def post(self, request, instance_id):
        instance = get_object_or_404(ChallengeInstance, id=instance_id)
        instance.expires_at += datetime.timedelta(minutes=30)
        instance.save()

        push_instance_task(
            action='EXTEND',
            instance_id=instance.id,
            challenge_id=instance.challenge.id
        )
        serializer = ChallengeInstanceSerializer(instance)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AdminInstanceListView(APIView):
    def get(self, request):
        status_filter = request.query_params.get('status')
        queryset = ChallengeInstance.objects.all().order_by('-created_at')
        
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        serializer = ChallengeInstanceSerializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AdminInstanceResetView(APIView):
    def post(self, request, instance_id):
        instance = get_object_or_404(ChallengeInstance, id=instance_id)
        instance.status = 'PENDING'
        instance.save()

        push_instance_task(
            action='RESET',
            instance_id=instance.id,
            challenge_id=instance.challenge.id
        )
        return Response({'message': f'[Admin] 인스턴스 {instance_id} 재시작 요청 완료'}, status=status.HTTP_200_OK)


class AdminInstanceDeleteView(APIView):
    def delete(self, request, instance_id):
        instance = get_object_or_404(ChallengeInstance, id=instance_id)
        instance.status = 'STOPPED'
        instance.save()

        push_instance_task(
            action='DELETE',
            instance_id=instance.id,
            challenge_id=instance.challenge.id
        )
        return Response({'message': f'[Admin] 인스턴스 {instance_id} 강제 종료 완료'}, status=status.HTTP_200_OK)