from django.db import models
from django.utils import timezone
import uuid

class Contest(models.Model):
    contest_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    @property
    def status(self):
        now = timezone.now()
        if now < self.start_time:
            return "BEFORE"
        if now > self.end_time:
            return "ENDED"
        return "RUNNING"

    @property
    def remaining_time(self):
        now = timezone.now()
        if now >= self.end_time: 
            return 0
        if now < self.start_time: 
            return int( (self.end_time - self.start_time).total_seconds())
        return int( (self.end_time - now).total_seconds()) 

    @property
    def time_until_start(self):
        now = timezone.now()
        if now >= self.start_time:
            return 0
        return int( (self.start_time - now).total_seconds())