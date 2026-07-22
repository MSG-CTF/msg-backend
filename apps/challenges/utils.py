# apps/challenges/utils.py
import json
import redis
from django.conf import settings

def get_redis_client():
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True
    )

def push_instance_task(action: str, instance_id: int, challenge_id: int, team_id: int = None):
    client = get_redis_client()
    
    payload = {
        'action': action,
        'instance_id': instance_id,
        'challenge_id': challenge_id,
        'team_id': team_id,
    }
    
    client.rpush(settings.INSTANCE_TASK_QUEUE, json.dumps(payload))