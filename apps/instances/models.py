import uuid

from django.conf import settings
from django.db import models


class InstanceStatus(models.TextChoices):
    REQUESTED = "REQUESTED"
    SCHEDULING = "SCHEDULING"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    RESETTING = "RESETTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANED = "CLEANED"


class DeleteReason(models.TextChoices):
    USER_REQUESTED = "USER_REQUESTED"
    REPLACED_BY_NEW_INSTANCE = "REPLACED_BY_NEW_INSTANCE"
    TTL_EXPIRED = "TTL_EXPIRED"
    IDLE_EXPIRED = "IDLE_EXPIRED"
    HARD_TIMEOUT = "HARD_TIMEOUT"
    ADMIN_FORCED = "ADMIN_FORCED"


class ArchitectureType(models.TextChoices):
    AMD64 = "AMD64"
    ARM64 = "ARM64"


class Instance(models.Model):
    instance_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="instances",
    )
    team = models.ForeignKey(
        "accounts.Team",
        on_delete=models.CASCADE,
        db_column="team_id",
        related_name="instances",
    )
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        related_name="instances",
    )
    status = models.CharField(
        max_length=30,
        choices=InstanceStatus.choices,
        default=InstanceStatus.REQUESTED,
    )
    release = models.ForeignKey(
        "instances.ChallengeRelease",
        on_delete=models.SET_NULL,
        db_column="release_id",
        related_name="instances",
        null=True,
        blank=True,
    )
    host = models.CharField(max_length=255, null=True, blank=True)
    ports = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    hard_expires_at = models.DateTimeField(null=True, blank=True)
    extend_count = models.IntegerField(default=0)
    replaced_instance = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        db_column="replaced_instance_id",
        related_name="replacement_instances",
        null=True,
        blank=True,
    )
    delete_reason = models.CharField(
        max_length=40,
        choices=DeleteReason.choices,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instances"
        indexes = [
            models.Index(fields=["user", "status", "created_at"]),
            models.Index(fields=["team", "status", "created_at"]),
            models.Index(fields=["challenge"]),
            models.Index(fields=["replaced_instance"]),
        ]

    def __str__(self):
        return f"{self.instance_id} {self.status}"


class InstanceLock(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        primary_key=True,
        related_name="instance_lock",
    )
    locked_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instance_locks"

    def __str__(self):
        return str(self.user_id)


class ChallengeRuntimeConfig(models.Model):
    challenge = models.OneToOneField(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        primary_key=True,
        related_name="runtime_config",
    )
    current_release = models.ForeignKey(
        "instances.ChallengeRelease",
        on_delete=models.SET_NULL,
        db_column="current_release_id",
        related_name="active_runtime_configs",
        null=True,
        blank=True,
    )
    ttl_minutes = models.IntegerField(default=120)
    hard_timeout_minutes = models.IntegerField(default=180)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "challenge_runtime_configs"

    def __str__(self):
        return str(self.challenge_id)


class ChallengeRelease(models.Model):
    # 문제 실행 릴리즈 정보를 저장한다
    release_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.ForeignKey(
        "challenge.Challenge",
        on_delete=models.CASCADE,
        db_column="challenge_id",
        related_name="releases",
    )
    revision = models.PositiveIntegerField()
    architecture = models.CharField(
        max_length=20,
        choices=ArchitectureType.choices,
        default=ArchitectureType.AMD64,
    )
    cpu_millicores = models.IntegerField(default=500)
    memory_mib = models.IntegerField(default=512)
    ephemeral_storage_mib = models.IntegerField(default=1024)
    is_active = models.BooleanField(default=False)
    source_path = models.CharField(max_length=255, null=True, blank=True)
    source_commit_sha = models.CharField(max_length=40, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "challenge_releases"
        constraints = [
            models.UniqueConstraint(
                fields=["challenge", "revision"],
                name="unique_challenge_release_revision",
            ),
        ]
        indexes = [
            models.Index(fields=["challenge", "is_active"]),
        ]

    def __str__(self):
        return f"{self.challenge_id} r{self.revision}"


class ReleaseContainer(models.Model):
    # 릴리즈별 컨테이너 목록을 저장한다
    release_container_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(
        "instances.ChallengeRelease",
        on_delete=models.CASCADE,
        db_column="release_id",
        related_name="containers",
    )
    name = models.CharField(max_length=63)
    image = models.CharField(max_length=500)
    ports = models.JSONField(default=list)
    expose = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "release_containers"
        constraints = [
            models.UniqueConstraint(
                fields=["release", "name"],
                name="unique_release_container_name",
            ),
        ]

    def __str__(self):
        return f"{self.release_id} {self.name}"
