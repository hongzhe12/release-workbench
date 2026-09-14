from django.contrib import admin

from .models import (
    Branch,
    BuildRecord,
    GitOperationLog,
    Release,
    Repository,
    VerificationRecord,
)


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "local_path",
        "remote_url",
        "baseline_branch",
        "verification_branch",
        "saas_build_enabled",
    )


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "repository", "type", "created_at")
    list_filter = ("repository", "type")


@admin.register(VerificationRecord)
class VerificationRecordAdmin(admin.ModelAdmin):
    list_display = ("branch", "status", "merged_commit", "updated_at")
    list_filter = ("status",)


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    list_display = ("name", "repository", "status", "base_commit", "created_at")
    list_filter = ("repository", "status")


@admin.register(BuildRecord)
class BuildRecordAdmin(admin.ModelAdmin):
    list_display = ("release", "status", "package_path", "created_at", "finished_at")
    list_filter = ("status",)


@admin.register(GitOperationLog)
class GitOperationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "repository", "operator", "action", "result")
    list_filter = ("repository", "result")
    readonly_fields = (
        "repository",
        "operator",
        "action",
        "command",
        "result",
        "stdout",
        "stderr",
        "created_at",
    )
