from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from workbench.models import Branch, Release, VerificationRecord

from .git_service import (
    GitCommandError,
    GitService,
    WorkflowError,
    repository_operation,
)


def _source_commits(git, branches):
    return {
        branch.pk: git.ensure_branch_synced(branch.name)
        for branch in branches
    }


def _selected_branches(repository, branch_ids):
    if not branch_ids:
        raise WorkflowError("请至少选择一个开发分支。")
    selected = list(
        Branch.objects.filter(
            repository=repository,
            pk__in=branch_ids,
        ).select_related("verification_record")
    )
    if len(selected) != len(set(branch_ids)):
        raise WorkflowError("选择的分支不属于当前仓库或已不存在。")
    return selected


def _next_release_name(repository, git):
    base_name = f"release/{timezone.localdate():%Y%m%d}"
    sequence = 1
    while True:
        name = base_name if sequence == 1 else f"{base_name}.{sequence}"
        if (
            not Release.objects.filter(repository=repository, name=name).exists()
            and not git.local_branch_exists(name)
            and not git.remote_branch_exists(name)
        ):
            return name
        sequence += 1


def _save_development_branch(repository, branch_name, branch_type):
    branch, created = Branch.objects.get_or_create(
        repository=repository,
        name=branch_name,
        defaults={"type": branch_type},
    )
    if not created and branch.type != branch_type:
        branch.type = branch_type
        branch.save(update_fields=["type"])
    VerificationRecord.objects.get_or_create(branch=branch)
    return branch, created


def create_development_branch(repository, branch_type, short_name, operator):
    branch_name = Branch.make_name(branch_type, short_name)
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean()
        git.ensure_branch_synced(repository.baseline_branch)
        if git.local_branch_exists(branch_name):
            raise WorkflowError(f"本地分支已存在：{branch_name}")
        if git.remote_branch_exists(branch_name):
            raise WorkflowError(f"远程分支已存在：{branch_name}")

        git.fetch()
        git.checkout(repository.baseline_branch)
        git.pull_ff_only(repository.baseline_branch)
        git.create_branch(branch_name, repository.baseline_branch)
        git.push_new_branch(branch_name)
        branch, _ = _save_development_branch(
            repository,
            branch_name,
            branch_type,
        )
        return branch


def register_development_branch(repository, branch_name, operator):
    branch_name = (branch_name or "").strip()
    try:
        branch_type, _ = Branch.split_name(branch_name)
        normalized = Branch.make_name(
            branch_type,
            branch_name.split("/", 1)[1],
        )
        if normalized != branch_name:
            raise ValidationError("已有分支名称包含不支持的字符。")
    except ValidationError as exc:
        raise WorkflowError(str(exc)) from exc

    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean()
        remote_sha = git.remote_branch_sha(branch_name)
        if not remote_sha:
            raise WorkflowError(f"远程仓库中不存在开发分支：{branch_name}")

        if git.local_branch_exists(branch_name):
            if git.ref_sha(branch_name) != remote_sha:
                raise WorkflowError(
                    f"分支 {branch_name} 本地与远程不一致，"
                    "请先同步后再登记。"
                )
        else:
            git.create_local_branch(branch_name, remote_sha)

        return _save_development_branch(
            repository,
            branch_name,
            branch_type,
        )


def merge_branches_to_verification(repository, branch_ids, operator):
    selected = _selected_branches(repository, branch_ids)
    verification_branch = repository.verification_branch

    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean()
        git.ensure_branch_synced(verification_branch)
        source_commits = _source_commits(git, selected)

        git.fetch()
        git.checkout(verification_branch)
        merged_any = False
        try:
            for branch in selected:
                commit_sha = source_commits[branch.pk]
                if git.is_ancestor(commit_sha, verification_branch):
                    continue
                git.merge(
                    branch.name,
                    f"合入验证分支 {verification_branch}：{branch.name}",
                )
                merged_any = True
        except GitCommandError as exc:
            conflicts = git.conflict_files()
            detail = "、".join(conflicts) if conflicts else "请查看 Git 操作日志"
            VerificationRecord.objects.filter(branch__in=selected).update(
                status=VerificationRecord.Status.FAILED,
                remark=f"合入验证分支冲突，未继续执行。冲突文件：{detail}",
            )
            raise WorkflowError(
                "合入验证分支时发生冲突，后续分支已停止合并。"
                f"冲突文件：{detail}"
            ) from exc

        if merged_any:
            git.push_branch(verification_branch)

        now = timezone.now()
        with transaction.atomic():
            for branch in selected:
                record = branch.verification_record
                if record.merged_commit == source_commits[branch.pk]:
                    continue
                record.status = VerificationRecord.Status.TESTING
                record.merged_commit = source_commits[branch.pk]
                record.merged_at = now
                record.save(
                    update_fields=[
                        "status",
                        "merged_commit",
                        "merged_at",
                        "updated_at",
                    ]
                )
        return selected, merged_any


def update_verification_status(record, status, remark, operator):
    if status not in VerificationRecord.Status.values:
        raise WorkflowError("不支持的验证状态。")

    repository = record.branch.repository
    if status == VerificationRecord.Status.PASSED:
        if not record.merged_commit:
            raise WorkflowError("该分支尚未合入验证分支，不能标记为验证通过。")
        with repository_operation(repository.pk):
            git = GitService(repository, operator)
            git.ensure_repository()
            if not git.local_branch_exists(record.branch.name):
                raise WorkflowError(f"本地分支不存在：{record.branch.name}")
            if not git.local_branch_exists(repository.verification_branch):
                raise WorkflowError(
                    f"本地不存在 {repository.verification_branch} 分支。"
                )
            if git.ref_sha(record.branch.name) != record.merged_commit:
                raise WorkflowError(
                    "开发分支在合入验证分支后又有新提交，"
                    "必须重新合入并验证后才能标记通过。"
                )
            if not git.is_ancestor(
                record.merged_commit,
                repository.verification_branch,
            ):
                raise WorkflowError(
                    "记录中的提交已不在当前验证分支中，必须重新合入后再验证。"
                )

    record.status = status
    record.remark = remark
    record.save(update_fields=["status", "remark", "updated_at"])
    return record


def create_release(repository, branch_ids, operator):
    selected = _selected_branches(repository, branch_ids)
    for branch in selected:
        record = branch.verification_record
        if record.status != VerificationRecord.Status.PASSED:
            raise WorkflowError(f"{branch.name} 尚未验证通过。")
        if not record.merged_commit:
            raise WorkflowError(f"{branch.name} 缺少已合入验证分支的提交记录。")

    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean(allow_untracked=True)
        git.ensure_branch_synced(repository.baseline_branch)
        git.ensure_branch_synced(repository.verification_branch)
        source_commits = _source_commits(git, selected)

        for branch in selected:
            record = branch.verification_record
            if source_commits[branch.pk] != record.merged_commit:
                raise WorkflowError(
                    f"{branch.name} 在验证通过后又有新提交，"
                    f"请重新合入 {repository.verification_branch} 并验证。"
                )
            if not git.is_ancestor(
                record.merged_commit,
                repository.verification_branch,
            ):
                raise WorkflowError(
                    f"{branch.name} 的验证提交不在当前 "
                    f"{repository.verification_branch} 分支中，不能上线。"
                )

        active_release = repository.releases.exclude(
            status__in=[
                Release.Status.BASELINE_MERGED,
                Release.Status.CANCELLED,
            ]
        ).first()
        if active_release:
            raise WorkflowError(
                f"当前已有未结束的 Release：{active_release.name}，"
                "不能重复创建。"
            )

        release_name = _next_release_name(repository, git)
        git.fetch()
        git.checkout(repository.baseline_branch)
        git.pull_ff_only(repository.baseline_branch)
        base_commit = git.ref_sha(repository.baseline_branch)
        git.create_branch(release_name, repository.baseline_branch)

        release = Release.objects.create(
            repository=repository,
            name=release_name,
            status=Release.Status.MERGING,
            base_commit=base_commit,
        )
        release.branches.set(selected)

        try:
            for branch in selected:
                if git.is_ancestor(source_commits[branch.pk], release_name):
                    continue
                git.merge(branch.name, f"合入 Release：{branch.name}")
        except GitCommandError as exc:
            conflicts = git.conflict_files()
            detail = "、".join(conflicts) if conflicts else "请查看 Git 操作日志"
            release.status = Release.Status.MERGE_FAILED
            release.check_result = f"Release 合并冲突：{detail}"
            release.save(update_fields=["status", "check_result"])
            raise WorkflowError(f"Release 合并冲突：{detail}") from exc

        if git.has_conflict_markers(release_name):
            release.status = Release.Status.MERGE_FAILED
            release.check_result = "Release 分支仍包含冲突标记。"
            release.save(update_fields=["status", "check_result"])
            raise WorkflowError("Release 分支仍包含冲突标记。")

        release.status = Release.Status.MERGED
        release.save(update_fields=["status"])
        return release


def continue_release_merge(release, operator):
    if release.status not in {
        Release.Status.MERGING,
        Release.Status.MERGE_FAILED,
    }:
        raise WorkflowError("当前 Release 不需要继续合并。")

    repository = release.repository
    selected = list(release.branches.all().select_related("verification_record"))
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean()
        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        source_commits = _source_commits(git, selected)

        git.fetch()
        for branch in selected:
            commit_sha = source_commits[branch.pk]
            if git.is_ancestor(commit_sha, release.name):
                continue
            git.merge(branch.name, f"继续合入 Release：{branch.name}")

        if git.has_conflict_markers(release.name):
            raise WorkflowError("Release 分支仍包含冲突标记。")
        release.status = Release.Status.MERGED
        release.check_result = ""
        release.save(update_fields=["status", "check_result"])
        return release


def check_release(release, operator):
    if release.status in {
        Release.Status.BASELINE_MERGED,
        Release.Status.CANCELLED,
    }:
        raise WorkflowError("该 Release 已结束，不能重新检查。")

    repository = release.repository
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean(
            allow_untracked=bool(getattr(release, "build_record", None))
        )
        git.ensure_branch_synced(repository.baseline_branch)
        git.ensure_branch_synced(repository.verification_branch)
        git.fetch()

        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        if not release.base_commit:
            raise WorkflowError("Release 缺少基于发布基线的提交记录。")
        if not git.is_ancestor(release.base_commit, release.name):
            raise WorkflowError("Release 不是基于记录的发布基线提交创建。")

        missing = [
            branch.name
            for branch in release.branches.all()
            if not git.is_ancestor(
                branch.verification_record.merged_commit,
                release.name,
            )
        ]
        if missing:
            raise WorkflowError(
                "以下分支尚未完整合入 Release：" + "、".join(missing)
            )

        conflict_markers = git.has_conflict_markers(release.name)
        if conflict_markers:
            release.status = Release.Status.MERGE_FAILED
            release.check_result = f"发现冲突标记：\n{conflict_markers}"
            release.save(update_fields=["status", "check_result"])
            raise WorkflowError("Release 中仍存在合并冲突标记，禁止构建。")

        if release.status != Release.Status.PRODUCTION_VERIFIED:
            release.status = Release.Status.READY
        release.check_result = (
            f"检查通过：基于 {release.base_commit[:12]}；"
            f"包含 {release.branches.count()} 个开发分支。"
        )
        release.save(update_fields=["status", "check_result"])
        return release


def mark_production_verified(release):
    if release.status != Release.Status.READY:
        raise WorkflowError("Release 检查通过后才能记录生产验证。")
    release.status = Release.Status.PRODUCTION_VERIFIED
    release.production_verified_at = timezone.now()
    release.save(update_fields=["status", "production_verified_at"])
    return release


def cancel_release(release, confirmation):
    if confirmation != release.name:
        raise WorkflowError("二次确认内容与 Release 分支名不一致。")
    if release.status == Release.Status.BASELINE_MERGED:
        raise WorkflowError("已合入发布基线的 Release 不能放弃。")
    if release.status == Release.Status.CANCELLED:
        raise WorkflowError("该 Release 已经放弃。")

    release.status = Release.Status.CANCELLED
    release.cancelled_at = timezone.now()
    release.save(update_fields=["status", "cancelled_at"])
    return release


def merge_release_to_baseline(release, confirmation, operator):
    if confirmation != release.name:
        raise WorkflowError("二次确认内容与 Release 分支名不一致。")
    if release.status != Release.Status.PRODUCTION_VERIFIED:
        raise WorkflowError("必须先完成生产验证，才能合入发布基线。")

    repository = release.repository
    baseline_branch = repository.baseline_branch
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        git.ensure_clean(allow_untracked=True)
        git.ensure_branch_synced(baseline_branch)
        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        release_sha = git.ref_sha(release.name)

        git.fetch()
        git.checkout(baseline_branch)
        if not git.is_ancestor(release_sha, baseline_branch):
            git.merge_no_ff(
                release.name,
                f"Merge {release.name} after production verification",
            )
        git.push_branch(baseline_branch)

        release.status = Release.Status.BASELINE_MERGED
        release.baseline_merged_at = timezone.now()
        release.save(update_fields=["status", "baseline_merged_at"])
        return release
