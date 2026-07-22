from django.db import models
from django.utils import timezone

class Contest(models.Model):
    name = models.CharField(max_length=100)
    startTime = models.DateTimeField()
    endTime = models.DateTimeField()
    is_active = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    @property
    def status(self):
        now = timezone.now()
        if now < self.startTime:
            return 'BEFORE'
        if now > self.endTime:
            return 'ENDED'
        return 'RUNNING'
    
    @property
    def time_until_start(self): #대회 시작 전까지 남은 시간
        now = timezone.now()
        if now >= self.startTime:
            return 0
        return int((self.startTime - now).total_seconds())
    
    @property
    def remaining_seconds(self):
        now = timezone.now()
        if now >= self.endTime:
            return 0
        if now < self.startTime:
            return int((self.endTime - self.startTime).total_seconds())
        return int((self.endTime - now).total_seconds())

    @property
    def remaining_display(self):
        total = self.remaining_seconds
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def __str__(self):
        return self.name
