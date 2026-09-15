import os
import re
import shlex
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import (
    Branch,
    BuildRecord,
    GitOperationLog,
    Release,
    VerificationRecord,
)


REMOTE_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s]+@")


class WorkflowError(Exception):
    pass


class GitCommandError(WorkflowError):
    pass


_locks = {}
_locks_guard = threading.Lock()


def repository_lock(repository_id):
    with _locks_guard:
        return _locks.setdefault(repository_id, threading.Lock())


@contextmanager
def repository_operation(repository_id):
    lock = repository_lock(repository_id)
    if not lock.acquire(blocking=False):
        raise WorkflowError("该仓库已有发布操作正在执行，请等待当前操作结束。")
    try:
        yield
    finally:
        lock.release()


def redact_command(parts):
    return REMOTE_CREDENTIALS.sub(r"\g<scheme>***@", shlex.join(parts))


class GitService:
    def __init__(self, repository, operator):
        self.repository = repository
        self.operator = operator
        self.repo_path = Path(repository.local_path).expanduser().resolve()
        self.remote = repository.remote_url
        self.timeout = settings.WORKBENCH_GIT_TIMEOUT_SECONDS

    def run(self, args, action, check=True, ok_codes=(0,)):
        command_parts = ["git", *args]
        command = redact_command(command_parts)
        try:
            completed = subprocess.run(
                command_parts,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            self._write_log(
                action=action,
                command=command,
                result=GitOperationLog.Result.FAILED,
                stdout=stdout,
                stderr=stderr or f"命令超时（{self.timeout} 秒）",
            )
            raise GitCommandError(
                f"{action}超时。"
            ) from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        completed.stdout = stdout
        completed.stderr = stderr
        result = (
            GitOperationLog.Result.SUCCESS
            if completed.returncode in ok_codes
            else GitOperationLog.Result.FAILED
        )
        self._write_log(
            action=action,
            command=command,
            result=result,
            stdout=stdout,
            stderr=stderr,
        )
        if check and completed.returncode != 0:
            raise GitCommandError(f"{action}失败：{stderr or stdout}")
        return completed

    def _write_log(self, action, command, result, stdout, stderr):
        GitOperationLog.objects.create(
            repository=self.repository,
            operator=self.operator,
            action=action,
            command=command,
            result=result,
            stdout=stdout,
            stderr=stderr,
        )

    def ensure_repository(self):
        if not self.repo_path.exists() or not self.repo_path.is_dir():
            raise WorkflowError(f"仓库目录不存在：{self.repo_path}")
        result = self.run(
            ["rev-parse", "--is-inside-work-tree"],
            "检查仓库",
            check=False,
        )
        if result.returncode != 0 or result.stdout != "true":
            raise WorkflowError(f"目录不是有效的 Git 工作区：{self.repo_path}")

    def remote_branches(self):
        result = self.run(
            ["ls-remote", "--heads", self.remote],
            "读取远程分支列表",
        )
        prefix = "refs/heads/"
        branches = set()
        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2 or not parts[1].startswith(prefix):
                continue
            branches.add(parts[1][len(prefix):])
        return sorted(branches)

    def ensure_clean(self, allow_untracked=False):
        entries = self.status_porcelain().splitlines()
        blocking_entries = [
            entry
            for entry in entries
            if not (allow_untracked and entry.startswith("?? "))
        ]
        if blocking_entries:
            raise WorkflowError(
                "工作区存在未提交修改，禁止继续 Git 操作。" "请先提交、暂存或清理现场。"
            )

    def status_porcelain(self):
        return self.run(
            ["status", "--porcelain"],
            "检查工作区",
        ).stdout

    def current_branch(self):
        return self.run(
            ["branch", "--show-current"],
            "读取当前分支",
        ).stdout

    def ref_sha(self, ref):
        return self.run(
            ["rev-parse", "--verify", f"{ref}^{{commit}}"],
            f"读取 {ref} 提交",
        ).stdout

    def local_branch_exists(self, branch):
        result = self.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            f"检查本地分支 {branch}",
            check=False,
            ok_codes=(0, 1),
        )
        return result.returncode == 0

    def remote_branch_sha(self, branch):
        result = self.run(
            [
                "ls-remote",
                "--heads",
                self.remote,
                f"refs/heads/{branch}",
            ],
            f"读取远程分支 {branch}",
        )
        if not result.stdout:
            return ""
        return result.stdout.split()[0]

    def remote_branch_exists(self, branch):
        return bool(self.remote_branch_sha(branch))

    def ensure_branch_synced(self, branch):
        if not self.local_branch_exists(branch):
            raise WorkflowError(f"本地分支不存在：{branch}")
        remote_sha = self.remote_branch_sha(branch)
        if not remote_sha:
            raise WorkflowError(f"远程分支不存在：{branch}")
        local_sha = self.ref_sha(branch)
        if local_sha != remote_sha:
            raise WorkflowError(
                f"分支 {branch} 本地与远程不一致，可能存在未推送提交或远程更新。"
            )
        return local_sha

    def fetch(self):
        self.run(["fetch", self.remote, "--prune"], "fetch")

    def checkout(self, branch):
        self.run(["checkout", branch], f"切换到 {branch}")

    def pull_ff_only(self, branch):
        self.run(
            ["pull", "--ff-only", self.remote, branch],
            f"更新 {branch}",
        )

    def create_branch(self, branch, base_branch):
        self.run(
            ["checkout", "-b", branch, base_branch],
            f"创建分支 {branch}",
        )

    def create_local_branch(self, branch, start_point):
        self.run(
            ["branch", branch, start_point],
            f"登记已有分支 {branch}",
        )

    def push_new_branch(self, branch):
        self.run(
            ["push", "-u", self.remote, branch],
            f"首次推送 {branch}",
        )

    def push_branch(self, branch):
        self.run(
            ["push", self.remote, branch],
            f"推送 {branch}",
        )

    def merge(self, ref, action_label=None):
        self.run(
            ["merge", "--no-edit", ref],
            action_label or f"合并 {ref}",
        )

    def merge_no_ff(self, ref, message):
        self.run(
            ["merge", "--no-ff", "--no-edit", "-m", message, ref],
            f"合并 {ref} 到发布基线",
        )

    def is_ancestor(self, ancestor, descendant):
        result = self.run(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            f"检查 {ancestor} 是否已包含在 {descendant}",
            check=False,
            ok_codes=(0, 1),
        )
        if result.returncode not in (0, 1):
            raise WorkflowError(f"无法检查提交关系：{ancestor} -> {descendant}")
        return result.returncode == 0

    def conflict_files(self):
        result = self.run(
            ["diff", "--name-only", "--diff-filter=U"],
            "读取冲突文件",
            check=False,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    def has_conflict_markers(self, ref):
        result = self.run(
            [
                "grep",
                "-n",
                "-E",
                r"^(<<<<<<< |=======$|>>>>>>> )",
                ref,
                "--",
            ],
            f"检查 {ref} 冲突标记",
            check=False,
            ok_codes=(0, 1),
        )
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1:
            return ""
        raise WorkflowError(result.stderr or "无法检查冲突标记。")

    def snapshot(self):
        try:
            self.ensure_repository()
            clean = not bool(self.status_porcelain())
            result = {
                "ok": True,
                "clean": clean,
                "current_branch": self.current_branch(),
                "baseline_commit": "",
                "verification_commit": "",
                "error": "",
            }
            if self.local_branch_exists(self.repository.baseline_branch):
                result["baseline_commit"] = self.ref_sha(
                    self.repository.baseline_branch
                )
            if self.local_branch_exists(self.repository.verification_branch):
                result["verification_commit"] = self.ref_sha(
                    self.repository.verification_branch
                )
            return result
        except (WorkflowError, GitCommandError) as exc:
            return {
                "ok": False,
                "clean": False,
                "current_branch": "",
                "baseline_commit": "",
                "verification_commit": "",
                "error": str(exc),
            }


PACKAGE_PATTERN = re.compile(
    r"""(?P<path>(?:[A-Za-z]:)?[^\s"'<>]+\.(?:tar\.gz|tgz|zip))""",
    re.IGNORECASE,
)


def _build_command(repository):
    script = Path(repository.local_path).expanduser().resolve() / "build.sh"
    if not script.is_file():
        raise WorkflowError(f"仓库根目录缺少标准打包脚本 build.sh：{script}")
    return ["/bin/sh", str(script)]


def _find_package(output, repository):
    repo_path = Path(repository.local_path).expanduser().resolve()
    for match in PACKAGE_PATTERN.finditer(output):
        candidate = Path(match.group("path").rstrip(".,;:)"))
        if not candidate.is_absolute():
            candidate = repo_path / candidate
        if candidate.is_file():
            return candidate.resolve().as_posix()
    return ""


def _append_build_log(build_id, line):
    build = BuildRecord.objects.get(pk=build_id)
    lines = f"{build.log}{line}".splitlines(keepends=True)
    BuildRecord.objects.filter(pk=build_id).update(
        log="".join(lines[-settings.WORKBENCH_BUILD_LOG_LIMIT:])
    )


def _execute_build(build_id, lock):
    close_old_connections()
    process = None
    try:
        build = BuildRecord.objects.select_related("release__repository").get(
            pk=build_id
        )
        repository = build.release.repository
        env = os.environ.copy()
        env["GIT_RELEASE_WORKBENCH"] = "1"
        process = subprocess.Popen(
            _build_command(repository),
            cwd=repository.local_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        output = []

        def read_output():
            for line in iter(process.stdout.readline, ""):
                output.append(line)
                _append_build_log(build_id, line)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            returncode = process.wait(
                timeout=settings.WORKBENCH_BUILD_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
            timeout = (
                "\n[构建超时] 超过 "
                f"{settings.WORKBENCH_BUILD_TIMEOUT_SECONDS} 秒，进程已终止。\n"
            )
            output.append(timeout)
            _append_build_log(build_id, timeout)
        reader.join(timeout=5)

        build.status = (
            BuildRecord.Status.SUCCESS
            if returncode == 0
            else BuildRecord.Status.FAILED
        )
        build.package_path = (
            _find_package("".join(output), repository) if returncode == 0 else ""
        )
        build.finished_at = timezone.now()
        build.save(update_fields=["status", "package_path", "finished_at"])
    except Exception as exc:
        build = BuildRecord.objects.get(pk=build_id)
        build.log += f"\n[构建启动失败] {exc}\n"
        build.status = BuildRecord.Status.FAILED
        build.finished_at = timezone.now()
        build.save(update_fields=["log", "status", "finished_at"])
    finally:
        if process and process.stdout:
            process.stdout.close()
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
        git.ensure_clean(
            allow_untracked=bool(
                existing and existing.status == BuildRecord.Status.FAILED
            )
        )
        if not git.local_branch_exists(release.name):
            raise WorkflowError(f"本地 Release 分支不存在：{release.name}")
        git.checkout(release.name)
        _build_command(repository)
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

    threading.Thread(
        target=_execute_build,
        args=(build.pk, lock),
        daemon=True,
    ).start()
    return build


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


def _merge_branches(git, target, branches, source_commits, action):
    merged = False
    for branch in branches:
        commit = source_commits[branch.pk]
        if git.is_ancestor(commit, target):
            continue
        git.merge(branch.name, f"{action}：{branch.name}")
        merged = True
    return merged


def _conflict_detail(git):
    return "、".join(git.conflict_files()) or "请查看 Git 操作日志"


def _next_release_name(repository, git):
    base_name = f"release-{timezone.localdate():%Y%m%d}"
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
        branch_type, short_name = Branch.split_name(branch_name)
        Branch.make_name(branch_type, short_name)
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
        try:
            merged_any = _merge_branches(
                git,
                verification_branch,
                selected,
                source_commits,
                f"合入验证分支 {verification_branch}",
            )
        except GitCommandError as exc:
            detail = _conflict_detail(git)
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
            _merge_branches(
                git,
                release_name,
                selected,
                source_commits,
                "合入 Release",
            )
        except GitCommandError as exc:
            detail = _conflict_detail(git)
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
        _merge_branches(
            git,
            release.name,
            selected,
            source_commits,
            "继续合入 Release",
        )
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
