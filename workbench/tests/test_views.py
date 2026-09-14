from django.test import TestCase
from django.urls import reverse

from workbench.forms import BranchCreateForm
from workbench.models import (
    Branch,
    BuildRecord,
    Release,
    Repository,
    VerificationRecord,
)


class PageSmokeTests(TestCase):
    def test_empty_dashboard_renders(self):
        response = self.client.get(reverse("workbench:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "添加项目")

    def test_repository_form_renders(self):
        response = self.client.get(reverse("workbench:repository_create"))

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
            reverse("workbench:branch_create", args=[repository.pk])
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
            reverse("workbench:dashboard"),
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
            reverse("workbench:dashboard") + f"?repository={repository.pk}",
            reverse("workbench:release_detail", args=[release.pk]),
            reverse("workbench:build_detail", args=[build.pk]),
            reverse("workbench:operation_logs", args=[repository.pk]),
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
            reverse("workbench:release_detail", args=[release.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "执行 SaaS 打包")
        self.assertContains(response, "未启用 SaaS 打包")
