from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Challenge(models.Model):
    title = models.CharField(max_length=200, verbose_name="문제 제목")
    description = models.TextField(verbose_name="문제 설명")
    category = models.CharField(max_length=50, verbose_name="카테고리") 
    score = models.IntegerField(default=100, verbose_name="배점")
    flag = models.CharField(max_length=255, verbose_name="정답 플래그")
    has_instance = models.BooleanField(default=False, verbose_name="동적 인스턴스 필요 여부")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정일시")

    class Meta:
        db_table = 'challenges'
        verbose_name = 'Challenge'
        verbose_name_plural = 'Challenges'

    def __str__(self):
        return f"[{self.category}] {self.title}"


class ChallengeInstance(models.Model):
    STATUS_CHOICES = (
        ('RUNNING', 'Running'),
        ('STOPPED', 'Stopped'),
        ('EXPIRED', 'Expired'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='instances')
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name='instances')
    connection_url = models.CharField(max_length=255, blank=True, null=True, verbose_name="접속 주소/포트")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='RUNNING', verbose_name="상태")
    expires_at = models.DateTimeField(verbose_name="만료 시각")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성일시")

    class Meta:
        db_table = 'challenge_instances'
        verbose_name = 'Challenge Instance'
        verbose_name_plural = 'Challenge Instances'

    def __str__(self):
        return f"Instance-{self.id} ({self.challenge.title})"