import os
import re
import subprocess
import threading
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from workbench.models import BuildRecord, GitOperation, Release

from .git_service import (
    GitService,
    WorkflowError,
    execute_git_operation,
    repository_lock,
)


PACKAGE_PATTERN = re.compile(
    r"""(?P<path>(?:[A-Za-z]:)?[^\s"'<>]+\.(?:tar\.gz|tgz|zip))""",
    re.IGNORECASE,
)


def _resolve_build_command(repository):
    repo_path = Path(repository.local_path).expanduser().resolve()
    script_path = (repo_path / "build.sh").resolve()
    if not script_path.exists() or not script_path.is_file():
        raise WorkflowError(
            f"仓库根目录缺少标准打包脚本 build.sh：{script_path}"
        )
    return ["/bin/sh", str(script_path)]


def _find_package(output, repository):
    repo_path = Path(repository.local_path).expanduser().resolve()
    for match in PACKAGE_PATTERN.finditer(output):
        raw = match.group("path").rstrip(".,;:)")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        candidate = candidate.resolve()
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return ""


def _append_log(build_id, line):
    build = BuildRecord.objects.get(pk=build_id)
    combined = f"{build.log}{line}"
    limit = settings.WORKBENCH_BUILD_LOG_LIMIT
    lines = combined.splitlines(keepends=True)
    if len(lines) > limit:
        combined = "".join(lines[-limit:])
    BuildRecord.objects.filter(pk=build_id).update(log=combined)


def _execute_build(build_id):
    close_old_connections()
    process = None
    try:
        build = BuildRecord.objects.select_related("release__repository").get(
            pk=build_id
        )
        repository = build.release.repository
        command = _resolve_build_command(repository)
        output_lines = []
        env = os.environ.copy()
        env["GIT_RELEASE_WORKBENCH"] = "1"
        process = subprocess.Popen(
            command,
            cwd=repository.local_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        def read_output():
            for line in iter(process.stdout.readline, ""):
                output_lines.append(line)
                _append_log(build_id, line)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        timeout = settings.WORKBENCH_BUILD_TIMEOUT_SECONDS
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
            timeout_message = f"\n[构建超时] 超过 {timeout} 秒，进程已终止。\n"
            output_lines.append(timeout_message)
            _append_log(build_id, timeout_message)

        reader.join(timeout=5)
        output = "".join(output_lines)
        package_path = _find_package(output, repository) if returncode == 0 else ""

        build = BuildRecord.objects.select_related("release").get(pk=build_id)
        build.status = (
            BuildRecord.Status.SUCCESS
            if returncode == 0
            else BuildRecord.Status.FAILED
        )
        build.package_path = package_path
        build.finished_at = timezone.now()
        build.save(
            update_fields=["status", "package_path", "finished_at"]
        )

    except Exception as exc:
        close_old_connections()
        message = f"\n[构建启动失败] {exc}\n"
        try:
            _append_log(build_id, message)
            build = BuildRecord.objects.select_related("release").get(pk=build_id)
            build.status = BuildRecord.Status.FAILED
            build.finished_at = timezone.now()
            build.save(update_fields=["status", "finished_at"])
        except Exception:
            pass
    finally:
        if process and process.stdout:
            process.stdout.close()
        lock = repository_lock(
            BuildRecord.objects.select_related("release__repository")
            .get(pk=build_id)
            .release.repository_id
        )
        if lock.locked():
            lock.release()
        close_old_connections()


def start_build(release, operator):
    if not release.repository.saas_build_enabled:
        raise WorkflowError("当前项目未启用 SaaS 打包。")
    existing = getattr(release, "build_record", None)
    if existing and existing.status == BuildRecord.Status.BUILDING:
        raise WorkflowError("该 Release 正在构建，不能重复执行。")
    if existing and existing.status == BuildRecord.Status.SUCCESS:
        raise WorkflowError("该 Release 已构建成功，不能重复打包。")
    if release.status in {
        Release.Status.MERGING,
        Release.Status.MERGE_FAILED,
        Release.Status.BASELINE_MERGED,
        Release.Status.CANCELLED,
    }:
        raise WorkflowError("当前 Release 状态不能执行 SaaS 打包。")

    repository = release.repository
    lock = repository_lock(repository.pk)
    if not lock.acquire(blocking=False):
        raise WorkflowError("该仓库已有发布操作正在执行，请等待当前操作结束。")

    try:
        git = GitService(repository, operator)
        git.ensure_repository()
        allow_untracked_retry = bool(
            existing and existing.status == BuildRecord.Status.FAILED
        )
        git.ensure_clean(allow_untracked=allow_untracked_retry)
        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        release_sha = git.ref_sha(release.name)

        def preflight(operation, created):
            git.ensure_repository()
            git.ensure_clean(allow_untracked=allow_untracked_retry)
            if not git.local_branch_exists(release.name):
                raise WorkflowError(f"本地 Release 分支不存在：{release.name}")

        def execute(operation, created):
            git.checkout(release.name)

        def verify(operation, created):
            if not git.local_branch_exists(release.name):
                return ""
            if git.current_branch() != release.name:
                return ""
            return git.ref_sha(release.name)

        execute_git_operation(
            repository=repository,
            operation_type=GitOperation.OperationType.CHECKOUT_FOR_BUILD,
            target=release.name,
            idempotency_key=(
                f"checkout_for_build:{release.pk}:{release_sha}:{release.name}"
            ),
            preflight=preflight,
            execute=execute,
            verify=verify,
        )
        _resolve_build_command(repository)

        build, _ = BuildRecord.objects.update_or_create(
            release=release,
            defaults={
                "status": BuildRecord.Status.BUILDING,
                "log": "[开始构建]\n",
                "package_path": "",
                "finished_at": None,
            },
        )
    except Exception:
        lock.release()
        raise

    worker = threading.Thread(
        target=_execute_build,
        args=(build.pk,),
        daemon=True,
    )
    worker.start()
    return build
