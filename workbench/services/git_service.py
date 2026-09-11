import re
import shlex
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from workbench.models import GitOperation, GitOperationLog


REMOTE_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s]+@")


class WorkflowError(Exception):
    pass


class GitCommandError(WorkflowError):
    def __init__(self, command, returncode, stdout="", stderr="", message=None):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.message = message or stderr.strip() or stdout.strip() or "Git 命令执行失败"
        super().__init__(self.message)


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


def _append_operation_log(operation, message):
    timestamp = timezone.localtime().strftime("%H:%M:%S")
    operation.log = f"{operation.log.rstrip()}\n[{timestamp}] {message}".strip()
    operation.save(update_fields=["log", "updated_at"])


def execute_git_operation(
    *,
    repository,
    operation_type,
    target,
    idempotency_key,
    preflight,
    execute,
    verify,
):
    operation, created = GitOperation.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "repository": repository,
            "operation_type": operation_type,
            "target": target,
        },
    )
    if not created:
        try:
            final_sha = verify(operation, False)
        except Exception:
            final_sha = ""
        if final_sha:
            operation.status = GitOperation.Status.SUCCEEDED
            operation.final_sha = final_sha
            operation.save(update_fields=["status", "final_sha", "updated_at"])
            _append_operation_log(operation, f"Git 校验确认已完成，SHA={final_sha}")
            return operation, True
        operation.status = GitOperation.Status.PENDING
        operation.save(update_fields=["status", "updated_at"])

    try:
        preflight(operation, created)
    except Exception as exc:
        operation.status = GitOperation.Status.FAILED
        operation.save(update_fields=["status", "updated_at"])
        _append_operation_log(operation, f"执行前检查失败：{exc}")
        raise

    try:
        execute(operation, created)
        final_sha = verify(operation, created)
        if not final_sha:
            raise RuntimeError("Git 操作已执行，但无法确认最终状态。")
    except Exception as exc:
        operation.status = GitOperation.Status.UNKNOWN
        operation.save(update_fields=["status", "updated_at"])
        _append_operation_log(operation, f"Git 结果待确认：{exc}")
        raise

    operation.status = GitOperation.Status.SUCCEEDED
    operation.final_sha = final_sha
    operation.save(update_fields=["status", "final_sha", "updated_at"])
    _append_operation_log(operation, f"Git 操作完成，SHA={final_sha}")
    return operation, False


class GitService:
    def __init__(self, repository, operator):
        self.repository = repository
        self.operator = operator
        self.repo_path = Path(repository.local_path).expanduser().resolve()
        self.remote = repository.remote_url
        self.timeout = settings.WORKBENCH_GIT_TIMEOUT_SECONDS

    def run(self, args, action, check=True):
        command_parts = ["git", *args]
        command = redact_command(command_parts)
        try:
            completed = subprocess.run(
                command_parts,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
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
                command=command,
                returncode=-1,
                stdout=stdout,
                stderr=stderr,
                message=f"{action}超时。",
            ) from exc

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        completed.stdout = stdout
        completed.stderr = stderr
        result = (
            GitOperationLog.Result.SUCCESS
            if completed.returncode == 0
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
            raise GitCommandError(
                command=command,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                message=f"{action}失败。",
            )
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

    def ensure_remote_reachable(self):
        self.run(["ls-remote", "--heads", self.remote], "检查远程仓库")

    def ensure_clean(self, allow_untracked=False):
        entries = self.status_porcelain().splitlines()
        blocking_entries = [
            entry
            for entry in entries
            if not (allow_untracked and entry.startswith("?? "))
        ]
        if blocking_entries:
            raise WorkflowError(
                "工作区存在未提交修改，禁止继续 Git 操作。"
                "请先提交、暂存或清理现场。"
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
                result["verification_commit"] = self.ref_sha(self.repository.verification_branch)
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
