from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import (
    BranchCreateForm,
    RepositoryForm,
    ReleaseCreateForm,
    VerificationMergeForm,
    VerificationRecordUpdateForm,
)
from .models import (
    Branch,
    BuildRecord,
    Release,
    Repository,
    VerificationRecord,
)
from .services import (
    GitService,
    WorkflowError,
    cancel_release,
    check_release,
    continue_release_merge,
    create_development_branch,
    create_release,
    mark_production_verified,
    merge_branches_to_verification,
    merge_release_to_baseline,
    register_development_branch,
    start_build,
    update_verification_status,
)


def _operator():
    return settings.WORKBENCH_OPERATOR


def _repository_from_request(request):
    repositories = Repository.objects.all()
    repository_id = request.GET.get("repository") or request.POST.get("repository")
    if repository_id:
        return get_object_or_404(repositories, pk=repository_id)
    return repositories.first()


def _redirect_workbench(repository):
    return redirect(f"{repository.get_absolute_url()}")


def _workflow_error(request, exc):
    messages.error(request, str(exc))


def dashboard(request):
    repositories = Repository.objects.all()
    repository = _repository_from_request(request)
    context = {
        "repositories": repositories,
        "repository": repository,
    }
    if repository is None:
        return render(request, "workbench/dashboard.html", context)

    branches = list(
        repository.branches.select_related("verification_record").order_by("name")
    )
    verification_counts = {
        row["verification_record__status"]: row["count"]
        for row in repository.branches.values("verification_record__status").annotate(
            count=Count("id")
        )
    }
    active_release = (
        repository.releases.exclude(
            status__in=[
                Release.Status.BASELINE_MERGED,
                Release.Status.CANCELLED,
            ]
        )
        .prefetch_related("branches")
        .first()
    )
    latest_release = repository.releases.prefetch_related("branches").first()
    passed_branches = [
        branch
        for branch in branches
        if getattr(branch, "verification_record", None)
        and branch.verification_record.status == VerificationRecord.Status.PASSED
        and branch.verification_record.merged_commit
    ]
    context.update(
        {
            "snapshot": GitService(repository, _operator()).snapshot(),
            "branches": branches,
            "verification_counts": verification_counts,
            "passed_branches": passed_branches,
            "active_release": active_release,
            "latest_release": latest_release,
            "recent_logs": repository.git_logs.all()[:8],
        }
    )
    return render(request, "workbench/dashboard.html", context)


def repository_create(request):
    form = RepositoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        repository = form.save()
        messages.success(request, f"已添加项目 {repository.name}。")
        return _redirect_workbench(repository)
    return render(
        request,
        "workbench/repository_form.html",
        {"form": form},
    )


def branch_create(request, repository_id):
    repository = get_object_or_404(Repository, pk=repository_id)
    existing_branches = []
    branch_list_error = ""
    try:
        git = GitService(repository, _operator())
        git.ensure_repository()
        registered_names = set(
            repository.branches.values_list("name", flat=True)
        )
        reserved_names = {
            repository.baseline_branch,
            repository.verification_branch,
        }
        for branch_name in git.remote_branches():
            if (
                branch_name in registered_names
                or branch_name in reserved_names
            ):
                continue
            try:
                branch_type, short_name = Branch.split_name(branch_name)
                Branch.make_name(branch_type, short_name)
            except ValidationError:
                continue
            existing_branches.append(branch_name)
    except WorkflowError as exc:
        branch_list_error = str(exc)

    form = BranchCreateForm(
        request.POST or None,
        existing_branches=existing_branches,
    )
    if request.method == "POST" and form.is_valid():
        try:
            if form.cleaned_data["mode"] == BranchCreateForm.Mode.CREATE:
                branch = create_development_branch(
                    repository=repository,
                    branch_type=form.cleaned_data["branch_type"],
                    short_name=form.cleaned_data["short_name"],
                    operator=_operator(),
                )
                messages.success(request, f"已创建并推送分支 {branch.name}。")
            else:
                branch, created = register_development_branch(
                    repository=repository,
                    branch_name=form.cleaned_data["branch_name"],
                    operator=_operator(),
                )
                if created:
                    messages.success(
                        request,
                        f"已登记已有分支 {branch.name}。",
                    )
                else:
                    messages.info(
                        request,
                        f"分支 {branch.name} 已经登记。",
                    )
        except WorkflowError as exc:
            _workflow_error(request, exc)
        else:
            return _redirect_workbench(repository)
    return render(
        request,
        "workbench/branch_form.html",
        {
            "repository": repository,
            "form": form,
            "branch_list_error": branch_list_error,
        },
    )


@require_POST
def verification_merge(request, repository_id):
    repository = get_object_or_404(Repository, pk=repository_id)
    merge_form = VerificationMergeForm(
        request.POST,
        repository=repository,
    )
    if merge_form.is_valid():
        try:
            selected, merged_any = merge_branches_to_verification(
                repository=repository,
                branch_ids=[
                    branch.pk
                    for branch in merge_form.cleaned_data["branches"]
                ],
                operator=_operator(),
            )
        except WorkflowError as exc:
            _workflow_error(request, exc)
        else:
            if merged_any:
                messages.success(
                    request,
                    f"已将 {len(selected)} 个分支合入验证分支 "
                    f"{repository.verification_branch}，状态已更新为验证中。",
                )
            else:
                messages.info(
                    request,
                    f"所选分支都已包含在 {repository.verification_branch} 中，"
                    "本次没有重复合并。",
                )
    else:
        messages.error(request, "请至少选择一个开发分支。")
    return _redirect_workbench(repository)


@require_POST
def verification_record_update(request, record_id):
    record = get_object_or_404(
        VerificationRecord.objects.select_related("branch__repository"),
        pk=record_id,
    )
    form = VerificationRecordUpdateForm(request.POST, instance=record)
    if form.is_valid():
        try:
            update_verification_status(
                record=record,
                status=form.cleaned_data["status"],
                remark=form.cleaned_data["remark"],
                operator=_operator(),
            )
        except WorkflowError as exc:
            _workflow_error(request, exc)
        else:
            messages.success(request, f"{record.branch.name} 的验证状态已更新。")
    else:
        messages.error(request, "验证状态表单无效。")
    return _redirect_workbench(record.branch.repository)


@require_POST
def release_create(request, repository_id):
    repository = get_object_or_404(Repository, pk=repository_id)
    form = ReleaseCreateForm(request.POST, repository=repository)
    if form.is_valid():
        try:
            release = create_release(
                repository=repository,
                branch_ids=[
                    branch.pk for branch in form.cleaned_data["branches"]
                ],
                operator=_operator(),
            )
        except WorkflowError as exc:
            _workflow_error(request, exc)
            return _redirect_workbench(repository)
        messages.success(request, f"已创建 Release {release.name}。")
        return redirect("release_detail", release.pk)
    for error in form.errors.values():
        messages.error(request, error.as_text())
    return _redirect_workbench(repository)


def release_detail(request, release_id):
    release = get_object_or_404(
        Release.objects.select_related("repository").prefetch_related(
            "branches__verification_record"
        ),
        pk=release_id,
    )
    build = getattr(release, "build_record", None)
    build_enabled = release.repository.saas_build_enabled
    context = {
        "repository": release.repository,
        "release": release,
        "build": build,
        "build_enabled": build_enabled,
        "operation_logs": release.repository.git_logs.all()[:10],
        "can_continue_merge": release.status
        in {Release.Status.MERGING, Release.Status.MERGE_FAILED},
        "can_check": release.status
        not in {
            Release.Status.BASELINE_MERGED,
            Release.Status.CANCELLED,
        },
        "can_build": (
            build_enabled
            and release.status
            in {
                Release.Status.MERGED,
                Release.Status.READY,
                Release.Status.PRODUCTION_VERIFIED,
            }
            and not (
                build
                and build.status
                in {
                    BuildRecord.Status.BUILDING,
                    BuildRecord.Status.SUCCESS,
                }
            )
        ),
        "can_verify": release.status == Release.Status.READY,
        "can_merge_baseline": release.status
        == Release.Status.PRODUCTION_VERIFIED,
        "can_cancel": release.status
        not in {
            Release.Status.BASELINE_MERGED,
            Release.Status.CANCELLED,
        },
    }
    return render(request, "workbench/release_detail.html", context)


@require_POST
def release_continue_merge(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    try:
        continue_release_merge(release, _operator())
    except WorkflowError as exc:
        _workflow_error(request, exc)
    else:
        messages.success(request, "Release 分支合并完成。")
    return redirect("release_detail", release.pk)


@require_POST
def release_check(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    try:
        check_release(release, _operator())
    except WorkflowError as exc:
        _workflow_error(request, exc)
    else:
        messages.success(request, "Release 检查通过，可以继续下一步。")
    return redirect("release_detail", release.pk)


@require_POST
def release_build(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    try:
        build = start_build(release, _operator())
    except WorkflowError as exc:
        _workflow_error(request, exc)
        return redirect("release_detail", release.pk)
    messages.info(request, "SaaS 打包已启动。")
    return redirect("build_detail", build.pk)


def build_detail(request, build_id):
    build = get_object_or_404(
        BuildRecord.objects.select_related("release__repository"),
        pk=build_id,
    )
    return render(
        request,
        "workbench/build_detail.html",
        {
            "build": build,
            "release": build.release,
            "repository": build.release.repository,
        },
    )


def build_status(request, build_id):
    build = get_object_or_404(
        BuildRecord.objects.select_related("release__repository"),
        pk=build_id,
    )
    return render(
        request,
        "workbench/_build_status.html",
        {
            "build": build,
            "release": build.release,
        },
    )


@require_POST
def release_verify(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    confirmation = request.POST.get("confirmation", "").strip()
    if confirmation != "生产验证通过":
        messages.error(request, "请输入“生产验证通过”完成二次确认。")
        return redirect("release_detail", release.pk)
    try:
        mark_production_verified(release)
    except WorkflowError as exc:
        _workflow_error(request, exc)
    else:
        messages.success(request, "已记录生产验证通过。")
    return redirect("release_detail", release.pk)


@require_POST
def release_merge_baseline(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    try:
        merge_release_to_baseline(
            release=release,
            confirmation=request.POST.get("confirmation", "").strip(),
            operator=_operator(),
        )
    except WorkflowError as exc:
        _workflow_error(request, exc)
    else:
        messages.success(
            request,
            f"{release.name} 已合入发布基线分支 "
            f"{release.repository.baseline_branch}。",
        )
    return redirect("release_detail", release.pk)


@require_POST
def release_cancel(request, release_id):
    release = get_object_or_404(Release, pk=release_id)
    try:
        cancel_release(
            release=release,
            confirmation=request.POST.get("confirmation", "").strip(),
        )
    except WorkflowError as exc:
        _workflow_error(request, exc)
    else:
        messages.success(
            request,
            f"{release.name} 已放弃，原分支、产物和日志已保留。",
        )
    return redirect("release_detail", release.pk)


def operation_logs(request, repository_id):
    repository = get_object_or_404(Repository, pk=repository_id)
    logs = repository.git_logs.all()[:500]
    return render(
        request,
        "workbench/operation_logs.html",
        {
            "repository": repository,
            "logs": logs,
        },
    )
