from django.db import transaction
from django.utils import timezone

from workbench.models import Branch, GitOperation, Release, VerificationRecord

from .git_service import (
    GitCommandError,
    GitService,
    WorkflowError,
    execute_git_operation,
    repository_operation,
)


def _source_commits(git, branches):
    return {
        branch.pk: git.ensure_branch_synced(branch.name)
        for branch in branches
    }


def _commit_key(commits):
    return ",".join(f"{branch_id}:{sha}" for branch_id, sha in sorted(commits.items()))


def _next_release_name(repository, git):
    base_name = f"release/{timezone.localdate():%Y%m%d}"

    recoverable_operations = GitOperation.objects.filter(
        repository=repository,
        operation_type=GitOperation.OperationType.CREATE_RELEASE,
        target__startswith=base_name,
    ).order_by("-created_at")
    for operation in recoverable_operations:
        if not Release.objects.filter(
            repository=repository,
            name=operation.target,
        ).exists():
            return operation.target

    def available(name):
        if Release.objects.filter(repository=repository, name=name).exists():
            return False
        if git.local_branch_exists(name):
            return False
        if git.remote_branch_exists(name):
            return False
        return True

    if available(base_name):
        return base_name
    sequence = 2
    while True:
        candidate = f"{base_name}.{sequence}"
        if available(candidate):
            return candidate
        sequence += 1


def create_development_branch(repository, branch_type, short_name, operator):
    branch_name = Branch.make_name(branch_type, short_name)
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        if not git.local_branch_exists(repository.baseline_branch):
            raise WorkflowError(f"本地不存在 {repository.baseline_branch} 分支。")
        base_sha = git.ref_sha(repository.baseline_branch)
        idempotency_key = (
            f"create_branch:{repository.pk}:{repository.baseline_branch}:"
            f"{base_sha}:{branch_name}"
        )

        existing_branch = Branch.objects.filter(
            repository=repository,
            name=branch_name,
        ).first()
        if existing_branch:
            succeeded = GitOperation.objects.filter(
                idempotency_key=idempotency_key,
                status=GitOperation.Status.SUCCEEDED,
            ).exists()
            if not succeeded:
                raise WorkflowError(f"分支 {branch_name} 已由工具管理，不能重复创建。")
            VerificationRecord.objects.get_or_create(branch=existing_branch)
            return existing_branch

        def preflight(operation, created):
            git.ensure_clean()
            git.ensure_remote_reachable()
            if not git.local_branch_exists(repository.baseline_branch):
                raise WorkflowError(f"本地不存在 {repository.baseline_branch} 分支。")
            if not git.remote_branch_exists(repository.baseline_branch):
                raise WorkflowError(f"远程不存在 {repository.baseline_branch} 分支。")
            if created:
                if git.local_branch_exists(branch_name):
                    raise WorkflowError(f"本地分支已存在：{branch_name}")
                if git.remote_branch_exists(branch_name):
                    raise WorkflowError(f"远程分支已存在：{branch_name}")

        def execute(operation, created):
            git.fetch()
            git.checkout(repository.baseline_branch)
            git.pull_ff_only(repository.baseline_branch)
            if not git.local_branch_exists(branch_name):
                git.create_branch(branch_name, repository.baseline_branch)
            if not git.remote_branch_exists(branch_name):
                git.push_new_branch(branch_name)

        def verify(operation, created):
            if not git.local_branch_exists(branch_name):
                return ""
            remote_sha = git.remote_branch_sha(branch_name)
            local_sha = git.ref_sha(branch_name)
            return local_sha if remote_sha and local_sha == remote_sha else ""

        execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.CREATE_BRANCH,
            target=branch_name,
            idempotency_key=idempotency_key,
            preflight=preflight,
            execute=execute,
            verify=verify,
        )

        branch, _ = Branch.objects.get_or_create(
            repository=repository,
            name=branch_name,
            defaults={"type": branch_type},
        )
        VerificationRecord.objects.get_or_create(branch=branch)
        return branch


def merge_branches_to_verification(repository, branch_ids, operator):
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

    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        source_commits = _source_commits(git, selected)
        verification_branch = repository.verification_branch
        idempotency_key = (
            f"merge_verification:{repository.pk}:{verification_branch}:"
            f"{_commit_key(source_commits)}"
        )

        def preflight(operation, created):
            git.ensure_clean()
            git.ensure_remote_reachable()
            current_commits = _source_commits(git, selected)
            if current_commits != source_commits:
                raise WorkflowError("开发分支在操作期间发生变化，请刷新后重试。")
            if created:
                git.ensure_branch_synced(verification_branch)
            elif not git.local_branch_exists(verification_branch):
                raise WorkflowError(f"本地不存在 {verification_branch} 分支。")
            elif not git.remote_branch_exists(verification_branch):
                raise WorkflowError(f"远程不存在 {verification_branch} 分支。")

        def execute(operation, created):
            git.fetch()
            if not git.local_branch_exists(verification_branch):
                raise WorkflowError(f"本地不存在 {verification_branch} 分支。")

            local_sha = git.ref_sha(verification_branch)
            remote_sha = git.remote_branch_sha(verification_branch)
            if not remote_sha:
                raise WorkflowError(f"远程不存在 {verification_branch} 分支。")
            if local_sha != remote_sha:
                if git.is_ancestor(remote_sha, local_sha):
                    pass
                elif git.is_ancestor(local_sha, remote_sha):
                    git.pull_ff_only(verification_branch)
                else:
                    raise WorkflowError(
                        f"{verification_branch} 本地与远程已分叉，不能自动恢复。"
                    )
            elif created:
                git.pull_ff_only(verification_branch)

            git.checkout(verification_branch)
            try:
                for branch in selected:
                    if git.is_ancestor(source_commits[branch.pk], verification_branch):
                        continue
                    git.merge(
                        branch.name,
                        f"合入验证分支 {verification_branch}：{branch.name}",
                    )
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

            if git.ref_sha(verification_branch) != git.remote_branch_sha(
                verification_branch
            ):
                git.push_branch(verification_branch)

        def verify(operation, created):
            if not git.local_branch_exists(verification_branch):
                return ""
            local_sha = git.ref_sha(verification_branch)
            remote_sha = git.remote_branch_sha(verification_branch)
            if not remote_sha or local_sha != remote_sha:
                return ""
            for commit_sha in source_commits.values():
                if not git.is_ancestor(commit_sha, verification_branch):
                    return ""
            return local_sha

        _, recovered = execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.MERGE_VERIFICATION,
            target=verification_branch,
            idempotency_key=idempotency_key,
            preflight=preflight,
            execute=execute,
            verify=verify,
        )

        now = timezone.now()
        with transaction.atomic():
            for branch in selected:
                record = getattr(branch, "verification_record", None)
                if record and record.merged_commit == source_commits[branch.pk]:
                    continue
                VerificationRecord.objects.update_or_create(
                    branch=branch,
                    defaults={
                        "status": VerificationRecord.Status.TESTING,
                        "merged_commit": source_commits[branch.pk],
                        "merged_at": now,
                    },
                )
        return selected, not recovered


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
            current_sha = git.ref_sha(record.branch.name)
            if current_sha != record.merged_commit:
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
    if not branch_ids:
        raise WorkflowError("请至少选择一个验证通过的分支。")

    selected = list(
        Branch.objects.filter(
            repository=repository,
            pk__in=branch_ids,
        ).select_related("verification_record")
    )
    if len(selected) != len(set(branch_ids)):
        raise WorkflowError("选择的分支不属于当前仓库或已不存在。")

    for branch in selected:
        record = getattr(branch, "verification_record", None)
        if not record or record.status != VerificationRecord.Status.PASSED:
            raise WorkflowError(f"{branch.name} 尚未验证通过。")
        if not record.merged_commit:
            raise WorkflowError(f"{branch.name} 缺少已合入验证分支的提交记录。")

    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        release_name = _next_release_name(repository, git)
        source_commits = _source_commits(git, selected)
        if not git.local_branch_exists(repository.baseline_branch):
            raise WorkflowError(f"本地不存在 {repository.baseline_branch} 分支。")
        base_sha = git.ref_sha(repository.baseline_branch)
        state = {"base_commit": base_sha}
        idempotency_key = (
            f"create_release:{repository.pk}:{release_name}:{base_sha}:"
            f"{_commit_key(source_commits)}"
        )

        def preflight(operation, created):
            if created:
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
            git.ensure_clean(allow_untracked=True)
            git.ensure_remote_reachable()
            current_commits = _source_commits(git, selected)
            if current_commits != source_commits:
                raise WorkflowError("开发分支在操作期间发生变化，请刷新后重试。")
            if created:
                if git.local_branch_exists(release_name):
                    raise WorkflowError(f"本地已存在预发布分支：{release_name}")
                if git.remote_branch_exists(release_name):
                    raise WorkflowError(f"远程已存在预发布分支：{release_name}")
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

        def execute(operation, created):
            git.fetch()
            git.ensure_branch_synced(repository.baseline_branch)
            git.ensure_branch_synced(repository.verification_branch)
            git.checkout(repository.baseline_branch)
            git.pull_ff_only(repository.baseline_branch)
            state["base_commit"] = git.ref_sha(repository.baseline_branch)
            if not git.local_branch_exists(release_name):
                git.create_branch(release_name, repository.baseline_branch)
            for branch in selected:
                if git.is_ancestor(source_commits[branch.pk], release_name):
                    continue
                git.merge(branch.name, f"合入 Release：{branch.name}")

        def verify(operation, created):
            if not git.local_branch_exists(release_name):
                return ""
            if not git.is_ancestor(state["base_commit"], release_name):
                return ""
            for commit_sha in source_commits.values():
                if not git.is_ancestor(commit_sha, release_name):
                    return ""
            if git.has_conflict_markers(release_name):
                return ""
            return git.ref_sha(release_name)

        execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.CREATE_RELEASE,
            target=release_name,
            idempotency_key=idempotency_key,
            preflight=preflight,
            execute=execute,
            verify=verify,
        )

        release, _ = Release.objects.get_or_create(
            repository=repository,
            name=release_name,
            defaults={
                "status": Release.Status.MERGED,
                "base_commit": state["base_commit"],
            },
        )
        release.base_commit = state["base_commit"]
        release.status = Release.Status.MERGED
        release.save(update_fields=["base_commit", "status"])
        release.branches.set(selected)
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
        source_commits = _source_commits(git, selected)
        idempotency_key = (
            f"continue_release:{release.pk}:{_commit_key(source_commits)}"
        )

        def preflight(operation, created):
            git.ensure_clean()
            git.ensure_remote_reachable()
            if not git.local_branch_exists(release.name):
                raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
            for branch in selected:
                record = branch.verification_record
                if source_commits[branch.pk] != record.merged_commit:
                    raise WorkflowError(
                        f"{branch.name} 与验证提交不一致，不能继续本次 Release。"
                    )

        def execute(operation, created):
            git.fetch()
            for branch in selected:
                if git.is_ancestor(source_commits[branch.pk], release.name):
                    continue
                git.merge(branch.name, f"继续合入 Release：{branch.name}")

        def verify(operation, created):
            if not git.local_branch_exists(release.name):
                return ""
            for commit_sha in source_commits.values():
                if not git.is_ancestor(commit_sha, release.name):
                    return ""
            if git.has_conflict_markers(release.name):
                return ""
            return git.ref_sha(release.name)

        execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.CONTINUE_RELEASE_MERGE,
            target=release.name,
            idempotency_key=idempotency_key,
            preflight=preflight,
            execute=execute,
            verify=verify,
        )

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
        allow_untracked = bool(getattr(release, "build_record", None))
        git.ensure_clean(allow_untracked=allow_untracked)
        git.ensure_remote_reachable()
        git.fetch()

        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        if not release.base_commit:
            raise WorkflowError("Release 缺少基于发布基线的提交记录。")
        if not git.is_ancestor(release.base_commit, release.name):
            raise WorkflowError("Release 不是基于记录的发布基线提交创建。")

        missing = []
        for branch in release.branches.all().select_related("verification_record"):
            if not git.is_ancestor(branch.verification_record.merged_commit, release.name):
                missing.append(branch.name)
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

        git.ensure_branch_synced(repository.baseline_branch)
        git.ensure_branch_synced(repository.verification_branch)

        message = (
            f"检查通过：基于 {release.base_commit[:12]}；"
            f"包含 {release.branches.count()} 个开发分支。"
        )
        release.check_result = message
        if release.status != Release.Status.PRODUCTION_VERIFIED:
            release.status = Release.Status.READY
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
    with repository_operation(repository.pk):
        git = GitService(repository, operator)
        git.ensure_repository()
        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        release_sha = git.ref_sha(release.name)
        baseline_branch = repository.baseline_branch
        idempotency_key = (
            f"merge_baseline:{release.pk}:{release_sha}:{baseline_branch}"
        )

        def preflight(operation, created):
            git.ensure_clean(allow_untracked=True)
            git.ensure_remote_reachable()
            if not git.local_branch_exists(release.name):
                raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
            if created:
                git.ensure_branch_synced(baseline_branch)
            elif not git.local_branch_exists(baseline_branch):
                raise WorkflowError(f"本地不存在 {baseline_branch} 分支。")

        def execute(operation, created):
            git.fetch()
            if not git.local_branch_exists(baseline_branch):
                raise WorkflowError(f"本地不存在 {baseline_branch} 分支。")
            local_sha = git.ref_sha(baseline_branch)
            remote_sha = git.remote_branch_sha(baseline_branch)
            if not remote_sha:
                raise WorkflowError(f"远程不存在 {baseline_branch} 分支。")
            if local_sha != remote_sha:
                if git.is_ancestor(remote_sha, local_sha):
                    pass
                elif git.is_ancestor(local_sha, remote_sha):
                    git.pull_ff_only(baseline_branch)
                else:
                    raise WorkflowError(
                        f"{baseline_branch} 本地与远程已分叉，不能自动恢复。"
                    )
            elif created:
                git.pull_ff_only(baseline_branch)

            git.checkout(baseline_branch)
            if not git.is_ancestor(release_sha, baseline_branch):
                git.merge_no_ff(
                    release.name,
                    f"Merge {release.name} after production verification",
                )
            if git.ref_sha(baseline_branch) != git.remote_branch_sha(baseline_branch):
                git.push_branch(baseline_branch)

        def verify(operation, created):
            if not git.local_branch_exists(baseline_branch):
                return ""
            local_sha = git.ref_sha(baseline_branch)
            remote_sha = git.remote_branch_sha(baseline_branch)
            if not remote_sha or local_sha != remote_sha:
                return ""
            return local_sha if git.is_ancestor(release_sha, baseline_branch) else ""

        execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.MERGE_BASELINE,
            target=baseline_branch,
            idempotency_key=idempotency_key,
            preflight=preflight,
            execute=execute,
            verify=verify,
        )

        release.status = Release.Status.BASELINE_MERGED
        release.baseline_merged_at = timezone.now()
        release.save(update_fields=["status", "baseline_merged_at"])
        return release
