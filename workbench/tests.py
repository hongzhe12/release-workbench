import os
import subprocess
import tempfile
import time
from pathlib import Path

from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .forms import BranchCreateForm
from .models import (
    Branch,
    BuildRecord,
    Release,
    Repository,
    VerificationRecord,
)
from .services import (
    WorkflowError,
    cancel_release,
    check_release,
    create_development_branch,
    create_release,
    mark_production_verified,
    merge_branches_to_verification,
    merge_release_to_baseline,
    register_development_branch,
    start_build,
    update_verification_status,
)


class PageSmokeTests(TestCase):
    def test_empty_dashboard_renders(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "添加项目")

    def test_repository_form_renders(self):
        response = self.client.get(reverse("repository_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "添加 Git 仓库")

    def test_branch_form_offers_create_and_existing_modes(self):
        repository = Repository.objects.create(
            name="project",
            local_path="/tmp",
            remote_url="origin",
            baseline_branch="stable",
            verification_branch="uat",
        )

        response = self.client.get(
            reverse("branch_create", args=[repository.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新建分支")
        self.assertContains(response, "选择已有分支")

    def test_existing_branch_form_infers_branch_type(self):
        form = BranchCreateForm(
            {
                "mode": BranchCreateForm.Mode.EXISTING,
                "existing_branch": "bugfix/existing-fix",
            },
            existing_branches=["bugfix/existing-fix"],
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["branch_type"],
            Branch.BranchType.BUGFIX,
        )
        self.assertEqual(
            form.cleaned_data["branch_name"],
            "bugfix/existing-fix",
        )

    def test_dashboard_with_repository_renders(self):
        repository = Repository.objects.create(
            name="project",
            local_path="/tmp",
            remote_url="origin",
            baseline_branch="stable",
            verification_branch="uat",
        )

        response = self.client.get(
            reverse("dashboard"),
            {"repository": repository.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "project")

    def test_workflow_pages_render(self):
        repository = Repository.objects.create(
            name="project",
            local_path="/tmp",
            remote_url="origin",
            baseline_branch="stable",
            verification_branch="uat",
        )
        branch = Branch.objects.create(
            repository=repository,
            name="feature/orders",
            type=Branch.BranchType.FEATURE,
        )
        VerificationRecord.objects.create(
            branch=branch,
            status=VerificationRecord.Status.PASSED,
            merged_commit="a" * 40,
        )
        release = Release.objects.create(
            repository=repository,
            name="release/20260911",
            status=Release.Status.READY,
            base_commit="b" * 40,
        )
        release.branches.add(branch)
        build = BuildRecord.objects.create(
            release=release,
            status=BuildRecord.Status.SUCCESS,
            log="Build success\n",
            package_path="/tmp/app.tar.gz",
        )

        urls = [
            reverse("dashboard") + f"?repository={repository.pk}",
            reverse("release_detail", args=[release.pk]),
            reverse("build_detail", args=[build.pk]),
            reverse("operation_logs", args=[repository.pk]),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_saas_build_button_is_hidden_when_disabled(self):
        repository = Repository.objects.create(
            name="project",
            local_path="/tmp",
            remote_url="origin",
            baseline_branch="stable",
            verification_branch="uat",
            saas_build_enabled=False,
        )
        branch = Branch.objects.create(
            repository=repository,
            name="feature/orders",
            type=Branch.BranchType.FEATURE,
        )
        VerificationRecord.objects.create(
            branch=branch,
            status=VerificationRecord.Status.PASSED,
            merged_commit="a" * 40,
        )
        release = Release.objects.create(
            repository=repository,
            name="release/20260911",
            status=Release.Status.READY,
            base_commit="b" * 40,
        )
        release.branches.add(branch)

        response = self.client.get(
            reverse("release_detail", args=[release.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "执行 SaaS 打包")
        self.assertContains(response, "未启用 SaaS 打包")


class WorkflowIntegrationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"

        self._git_at(self.root, "init", "--bare", str(self.remote))
        self.work.mkdir()
        self._git("init")
        self._git("config", "user.name", "Workbench Test")
        self._git("config", "user.email", "workbench@example.test")

        (self.work / "README.md").write_text(
            "initial\n",
            encoding="utf-8",
        )
        build_script = self.work / "build.sh"
        build_script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "set -eu",
                    'echo "Build start"',
                    'mkdir -p dist',
                    'printf "package" > dist/app.tar.gz',
                    'echo "SaaS package: dist/app.tar.gz"',
                    'echo "Build success"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        build_script.chmod(0o755)
        self._git("add", "README.md", "build.sh")
        self._git("commit", "-m", "initial")
        self._git("branch", "-M", "stable")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-u", "origin", "stable")
        self._git("checkout", "-b", "uat")
        self._git("push", "-u", "origin", "uat")
        self._git("checkout", "stable")

        self.repository = Repository.objects.create(
            name="test-project",
            local_path=str(self.work),
            remote_url="origin",
            baseline_branch="stable",
            verification_branch="uat",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git_at(self, cwd, *args):
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Workbench Test",
                "GIT_AUTHOR_EMAIL": "workbench@example.test",
                "GIT_COMMITTER_NAME": "Workbench Test",
                "GIT_COMMITTER_EMAIL": "workbench@example.test",
            }
        )
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def _git(self, *args):
        return self._git_at(self.work, *args)

    def _create_branch_with_change(self, short_name):
        branch = create_development_branch(
            self.repository,
            Branch.BranchType.FEATURE,
            short_name,
            "tester",
        )
        (self.work / f"{short_name}.txt").write_text(
            f"{short_name}\n",
            encoding="utf-8",
        )
        self._git("add", f"{short_name}.txt")
        self._git("commit", "-m", f"add {short_name}")
        self._git("push", "origin", branch.name)
        return branch

    def _prepare_passed_branch(self):
        branch = self._create_branch_with_change("orders")
        merge_branches_to_verification(self.repository, [branch.pk], "tester")
        record = VerificationRecord.objects.get(branch=branch)
        update_verification_status(
            record,
            VerificationRecord.Status.PASSED,
            "Verification passed",
            "tester",
        )
        return branch

    def test_complete_release_flow(self):
        branch = self._prepare_passed_branch()

        release = create_release(self.repository, [branch.pk], "tester")
        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.MERGED)
        self.assertEqual(release.branches.get(), branch)

        check_release(release, "tester")
        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.READY)

        build = start_build(release, "tester")
        build = self._wait_for_build(build.pk)
        self.assertEqual(build.status, BuildRecord.Status.SUCCESS)
        self.assertTrue(build.package_path.endswith("dist/app.tar.gz"))

        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.READY)

        mark_production_verified(release)
        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.PRODUCTION_VERIFIED)

        merge_release_to_baseline(release, release.name, "tester")
        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.BASELINE_MERGED)

        stable_content = self._git("show", "stable:orders.txt").stdout
        self.assertEqual(stable_content, "orders\n")

    def test_repeated_verification_merge_does_not_reset_passed_status(self):
        branch = self._prepare_passed_branch()

        selected, merged_any = merge_branches_to_verification(
            self.repository,
            [branch.pk],
            "tester",
        )

        record = VerificationRecord.objects.get(branch=branch)
        self.assertEqual(len(selected), 1)
        self.assertFalse(merged_any)
        self.assertEqual(record.status, VerificationRecord.Status.PASSED)
        self.assertEqual(record.remark, "Verification passed")

    def test_new_branch_commit_invalidates_verification_pass(self):
        branch = self._prepare_passed_branch()
        self._git("checkout", branch.name)
        (self.work / "after-verification.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        self._git("add", "after-verification.txt")
        self._git("commit", "-m", "change after verification")
        self._git("push", "origin", branch.name)

        record = VerificationRecord.objects.get(branch=branch)
        with self.assertRaisesMessage(
            WorkflowError,
            "开发分支在合入验证分支后又有新提交",
        ):
            update_verification_status(
                record,
                VerificationRecord.Status.PASSED,
                "",
                "tester",
            )

    def test_release_rejects_branch_that_did_not_pass_verification(self):
        branch = self._create_branch_with_change("not-passed")
        merge_branches_to_verification(self.repository, [branch.pk], "tester")

        with self.assertRaisesMessage(WorkflowError, "尚未验证通过"):
            create_release(self.repository, [branch.pk], "tester")

    def test_saas_build_can_be_disabled(self):
        branch = self._prepare_passed_branch()
        self.repository.saas_build_enabled = False
        self.repository.save(update_fields=["saas_build_enabled"])
        release = create_release(self.repository, [branch.pk], "tester")
        check_release(release, "tester")
        release.refresh_from_db()

        with self.assertRaisesMessage(WorkflowError, "未启用 SaaS 打包"):
            start_build(release, "tester")

        mark_production_verified(release)
        release.refresh_from_db()
        self.assertEqual(release.status, Release.Status.PRODUCTION_VERIFIED)

    def test_rechecking_keeps_production_verified_status(self):
        branch = self._prepare_passed_branch()
        release = create_release(self.repository, [branch.pk], "tester")
        check_release(release, "tester")
        release.refresh_from_db()
        mark_production_verified(release)
        release.refresh_from_db()

        check_release(release, "tester")
        release.refresh_from_db()

        self.assertEqual(release.status, Release.Status.PRODUCTION_VERIFIED)

    def test_successful_build_cannot_run_twice(self):
        branch = self._prepare_passed_branch()
        release = create_release(self.repository, [branch.pk], "tester")
        check_release(release, "tester")
        release.refresh_from_db()
        build = start_build(release, "tester")
        self._wait_for_build(build.pk)
        release.refresh_from_db()

        with self.assertRaisesMessage(WorkflowError, "已构建成功"):
            start_build(release, "tester")

    def test_register_existing_remote_branch_creates_local_copy(self):
        self._git("checkout", "-b", "feature/existing", "stable")
        (self.work / "existing.txt").write_text(
            "existing branch\n",
            encoding="utf-8",
        )
        self._git("add", "existing.txt")
        self._git("commit", "-m", "add existing branch")
        self._git("push", "-u", "origin", "feature/existing")
        self._git("checkout", "stable")
        self._git("branch", "-D", "feature/existing")

        branch, created = register_development_branch(
            self.repository,
            "feature/existing",
            "tester",
        )

        self.assertTrue(created)
        self.assertEqual(branch.name, "feature/existing")
        self.assertEqual(branch.type, Branch.BranchType.FEATURE)
        self.assertTrue(
            VerificationRecord.objects.filter(
                branch=branch,
                status=VerificationRecord.Status.PENDING,
            ).exists()
        )
        self.assertEqual(
            self._git("rev-parse", branch.name).stdout.strip(),
            self._git("rev-parse", f"origin/{branch.name}").stdout.strip(),
        )
        self.assertTrue(
            self.repository.git_logs.filter(
                action__contains="登记已有分支",
                result="success",
            ).exists()
        )

        same_branch, created_again = register_development_branch(
            self.repository,
            "feature/existing",
            "tester",
        )
        self.assertFalse(created_again)
        self.assertEqual(same_branch.pk, branch.pk)
        self.assertEqual(
            Branch.objects.filter(
                repository=self.repository,
                name=branch.name,
            ).count(),
            1,
        )

    def test_register_rejects_missing_remote_branch(self):
        with self.assertRaisesMessage(
            WorkflowError,
            "远程仓库中不存在开发分支",
        ):
            register_development_branch(
                self.repository,
                "feature/missing",
                "tester",
            )

    def test_build_success_can_be_cancelled_and_recreated_with_sequence(self):
        branch = self._prepare_passed_branch()
        release = create_release(self.repository, [branch.pk], "tester")
        check_release(release, "tester")
        release.refresh_from_db()
        build = start_build(release, "tester")
        self._wait_for_build(build.pk)
        release.refresh_from_db()

        cancel_release(release, release.name)
        release.refresh_from_db()
        replacement = create_release(self.repository, [branch.pk], "tester")

        self.assertEqual(release.status, Release.Status.CANCELLED)
        self.assertEqual(replacement.name, f"{release.name}.2")
        self.assertTrue(
            BuildRecord.objects.filter(pk=build.pk).exists()
        )

    def test_custom_baseline_and_verification_branch_names(self):
        self._git("branch", "main", "stable")
        self._git("push", "origin", "main")
        self._git("branch", "sit", "uat")
        self._git("push", "origin", "sit")
        self.repository.baseline_branch = "main"
        self.repository.verification_branch = "sit"
        self.repository.save(
            update_fields=["baseline_branch", "verification_branch"]
        )

        branch = self._create_branch_with_change("custom-names")
        merge_branches_to_verification(self.repository, [branch.pk], "tester")
        record = VerificationRecord.objects.get(branch=branch)
        update_verification_status(
            record,
            VerificationRecord.Status.PASSED,
            "custom branches passed",
            "tester",
        )

        self._git(
            "merge-base",
            "--is-ancestor",
            branch.name,
            "origin/sit",
        )
        release = create_release(self.repository, [branch.pk], "tester")
        self.assertEqual(release.repository.baseline_branch, "main")

    def test_chinese_branch_name(self):
        branch = self._create_branch_with_change("订单导出")

        self.assertEqual(branch.name, "feature/订单导出")
        self.assertEqual(
            self.repository.git_logs.filter(
                action__contains="订单导出",
                result="success",
            ).count()
            > 0,
            True,
        )

    def _wait_for_build(self, build_id):
        for _ in range(200):
            build = BuildRecord.objects.get(pk=build_id)
            if build.status != BuildRecord.Status.BUILDING:
                return build
            time.sleep(0.05)
        self.fail("build did not finish in time")
