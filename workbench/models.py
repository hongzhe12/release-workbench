from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse


class Repository(models.Model):
    name = models.CharField("项目名称", max_length=120, unique=True)
    local_path = models.CharField("本地仓库路径", max_length=500)
    remote_url = models.CharField(
        "远程名称或 URL",
        max_length=500,
        default="origin",
        help_text="例如 origin；也可以填写完整 Git URL。",
    )
    baseline_branch = models.CharField(
        "发布基线分支",
        max_length=120,
        default="stable",
        help_text="Release 从这里创建，生产验证后也合入这里；默认 stable。",
    )
    verification_branch = models.CharField(
        "验证分支",
        max_length=120,
        default="uat",
        help_text="用于合入待验证分支；可配置为 uat、sit、qa 等项目实际名称。",
    )
    saas_build_enabled = models.BooleanField(
        "启用 SaaS 打包",
        default=True,
        help_text="启用后在 Release 检查通过时可执行仓库根目录的 build.sh。",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "仓库"
        verbose_name_plural = "仓库"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("workbench:dashboard") + f"?repository={self.pk}"

    def clean(self):
        super().clean()
        path = Path(self.local_path).expanduser()
        if not path.is_absolute():
            raise ValidationError({"local_path": "必须填写绝对路径。"})
        if not path.exists():
            raise ValidationError({"local_path": "路径不存在。"})
        if not path.is_dir():
            raise ValidationError({"local_path": "路径必须是目录。"})


class Branch(models.Model):
    class BranchType(models.TextChoices):
        FEATURE = "feature", "feature"
        BUGFIX = "bugfix", "bugfix"

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="branches",
        verbose_name="仓库",
    )
    name = models.CharField("分支全名", max_length=240)
    type = models.CharField(
        "分支类型",
        max_length=20,
        choices=BranchType.choices,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "name"],
                name="unique_repository_branch",
            )
        ]
        verbose_name = "开发分支"
        verbose_name_plural = "开发分支"

    def __str__(self):
        return self.name

    @staticmethod
    def make_name(branch_type, short_name):
        short_name = (short_name or "").strip()
        if branch_type not in Branch.BranchType.values:
            raise ValidationError("不支持的分支类型。")
        if not short_name or not short_name[0].isalnum():
            raise ValidationError(
                "分支名称必须以中文、字母或数字开头。"
            )
        if any(
            not (character.isalnum() or character in "._-")
            for character in short_name
        ):
            raise ValidationError(
                "分支名称只能包含中文、字母、数字、点、下划线和短横线。"
            )
        if ".." in short_name:
            raise ValidationError("分支名称不能包含连续的 ..。")
        return f"{branch_type}/{short_name}"


class VerificationRecord(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待验证"
        TESTING = "testing", "验证中"
        PASSED = "passed", "验证通过"
        FAILED = "failed", "验证失败"

    branch = models.OneToOneField(
        Branch,
        on_delete=models.CASCADE,
        related_name="verification_record",
        verbose_name="开发分支",
    )
    status = models.CharField(
        "验证状态",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    remark = models.TextField("备注", blank=True)
    merged_commit = models.CharField(
        "已合入验证分支的提交",
        max_length=64,
        blank=True,
    )
    merged_at = models.DateTimeField(
        "最近合入验证分支时间",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "验证记录"
        verbose_name_plural = "验证记录"

    def __str__(self):
        return f"{self.branch}: {self.get_status_display()}"


class Release(models.Model):
    class Status(models.TextChoices):
        MERGING = "merging", "正在合并"
        MERGE_FAILED = "merge_failed", "合并冲突"
        MERGED = "merged", "已合并"
        READY = "ready", "检查通过"
        PRODUCTION_VERIFIED = "production_verified", "生产验证通过"
        BASELINE_MERGED = "baseline_merged", "已合入发布基线"
        CANCELLED = "cancelled", "已放弃"

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="releases",
        verbose_name="仓库",
    )
    name = models.CharField("Release 分支", max_length=160)
    status = models.CharField(
        "状态",
        max_length=40,
        choices=Status.choices,
        default=Status.MERGING,
    )
    base_commit = models.CharField(
        "基于的发布基线提交",
        max_length=64,
        blank=True,
    )
    check_result = models.TextField("检查结果", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    production_verified_at = models.DateTimeField(
        "生产验证时间",
        null=True,
        blank=True,
    )
    baseline_merged_at = models.DateTimeField(
        "合入发布基线时间",
        null=True,
        blank=True,
    )
    cancelled_at = models.DateTimeField(
        "放弃时间",
        null=True,
        blank=True,
    )
    branches = models.ManyToManyField(
        Branch,
        related_name="releases",
        verbose_name="包含的开发分支",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "name"],
                name="unique_repository_release",
            )
        ]
        verbose_name = "Release"
        verbose_name_plural = "Release"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("workbench:release_detail", args=[self.pk])

class BuildRecord(models.Model):
    class Status(models.TextChoices):
        BUILDING = "building", "构建中"
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"

    release = models.OneToOneField(
        Release,
        on_delete=models.CASCADE,
        related_name="build_record",
        verbose_name="Release",
    )
    status = models.CharField(
        "状态",
        max_length=20,
        choices=Status.choices,
        default=Status.BUILDING,
    )
    log = models.TextField("构建日志", blank=True)
    package_path = models.CharField("SaaS 包路径", max_length=1000, blank=True)
    created_at = models.DateTimeField("开始时间", auto_now_add=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "构建记录"
        verbose_name_plural = "构建记录"

    def __str__(self):
        return f"{self.release.name}: {self.get_status_display()}"


class GitOperationLog(models.Model):
    class Result(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="git_logs",
        verbose_name="仓库",
    )
    operator = models.CharField("操作人", max_length=120)
    action = models.CharField("动作", max_length=120)
    command = models.TextField("命令")
    result = models.CharField(
        "结果",
        max_length=20,
        choices=Result.choices,
    )
    stdout = models.TextField("stdout", blank=True,null = True)
    stderr = models.TextField("stderr", blank=True,null = True)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["repository", "-created_at"]),
        ]
        verbose_name = "Git 操作日志"
        verbose_name_plural = "Git 操作日志"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.action}"


class GitOperation(models.Model):
    class OperationType(models.TextChoices):
        CREATE_BRANCH = "create_branch", "创建开发分支"
        MERGE_VERIFICATION = "merge_verification", "合入验证分支"
        CREATE_RELEASE = "create_release", "创建 Release"
        CONTINUE_RELEASE_MERGE = "continue_release_merge", "继续合并 Release"
        MERGE_BASELINE = "merge_baseline", "合入发布基线"
        CHECKOUT_FOR_BUILD = "checkout_for_build", "切换构建分支"

    class Status(models.TextChoices):
        PENDING = "pending", "待执行"
        SUCCEEDED = "succeeded", "已完成"
        FAILED = "failed", "执行前失败"
        UNKNOWN = "unknown", "结果待确认"

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="git_operations",
        verbose_name="仓库",
    )
    operation_type = models.CharField(
        "操作类型",
        max_length=40,
        choices=OperationType.choices,
    )
    target = models.CharField("目标", max_length=300)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    idempotency_key = models.CharField(
        "幂等键",
        max_length=300,
        unique=True,
    )
    log = models.TextField("执行日志", blank=True)
    final_sha = models.CharField("最终 SHA", max_length=64, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["repository", "-created_at"]),
            models.Index(fields=["repository", "status"]),
        ]
        verbose_name = "Git 自动操作"
        verbose_name_plural = "Git 自动操作"

    def __str__(self):
        return f"{self.get_operation_type_display()}: {self.target}"
