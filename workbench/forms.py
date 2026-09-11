from pathlib import Path

from django import forms

from .models import Branch, Repository, VerificationRecord


class RepositoryForm(forms.ModelForm):
    class Meta:
        model = Repository
        fields = [
            "name",
            "local_path",
            "remote_url",
            "baseline_branch",
            "verification_branch",
            "saas_build_enabled",
        ]

    def clean_local_path(self):
        value = self.cleaned_data["local_path"].strip()
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise forms.ValidationError("必须填写绝对路径。")
        if not path.exists() or not path.is_dir():
            raise forms.ValidationError("目录不存在。")
        return str(path.resolve())


class BranchCreateForm(forms.Form):
    type = forms.ChoiceField(
        label="分支类型",
        choices=Branch.BranchType.choices,
    )
    short_name = forms.CharField(
        label="分支名称",
        max_length=200,
        help_text="只填写后缀，例如 feature/订单导出 或 bugfix/login-fix。",
    )

    def clean(self):
        cleaned = super().clean()
        branch_type = cleaned.get("type")
        short_name = cleaned.get("short_name")
        if branch_type and short_name:
            try:
                cleaned["branch_name"] = Branch.make_name(branch_type, short_name)
            except forms.ValidationError as exc:
                self.add_error("short_name", exc)
        return cleaned


class VerificationMergeForm(forms.Form):
    branches = forms.ModelMultipleChoiceField(
        label="选择开发分支",
        queryset=Branch.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, repository=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branches"].queryset = Branch.objects.filter(
            repository=repository
        ).select_related("verification_record")


class VerificationRecordUpdateForm(forms.ModelForm):
    class Meta:
        model = VerificationRecord
        fields = ["status", "remark"]
        widgets = {
            "remark": forms.Textarea(attrs={"rows": 2, "class": "compact-input"}),
        }


class ReleaseCreateForm(forms.Form):
    branches = forms.ModelMultipleChoiceField(
        label="本次上线内容",
        queryset=Branch.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, repository=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branches"].queryset = (
            Branch.objects.filter(
                repository=repository,
                verification_record__status=VerificationRecord.Status.PASSED,
            )
            .exclude(verification_record__merged_commit="")
            .select_related("verification_record")
        )
