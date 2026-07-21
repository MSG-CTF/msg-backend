from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .serializers import LoginRequestSerializer


class LoginView(APIView):
    permission_classes = []

    def post(self, request):
        serializer = LoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        login_id = serializer.validated_data['loginId']
        password = serializer.validated_data['password']

        user = User.objects.filter(username=login_id).first()

        if user is None or not user.check_password(password):
            return Response(
                {
                    'code': 'INVALID_CREDENTIALS',
                    'message': '아이디 또는 비밀번호가 올바르지 않습니다',
                    'data': None,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)

        return Response(
            {
                'code': 'SUCCESS',
                'message': '로그인 성공',
                'data': {
                    'accessToken': access_token,
                    'role': user.role,
                    'userId': user.username,
                    'teamId': str(user.team.id) if user.team else None,
                    'teamName': user.team.name if user.team else None,
                },
            },
            status=status.HTTP_200_OK,
        )