from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("repositories/new/", views.repository_create, name="repository_create"),
    path(
        "repositories/<int:repository_id>/branches/new/",
        views.branch_create,
        name="branch_create",
    ),
    path(
        "repositories/<int:repository_id>/verification/merge/",
        views.verification_merge,
        name="verification_merge",
    ),
    path(
        "verification-records/<int:record_id>/",
        views.verification_record_update,
        name="verification_record_update",
    ),
    path(
        "repositories/<int:repository_id>/releases/",
        views.release_create,
        name="release_create",
    ),
    path("releases/<int:release_id>/", views.release_detail, name="release_detail"),
    path(
        "releases/<int:release_id>/continue-merge/",
        views.release_continue_merge,
        name="release_continue_merge",
    ),
    path("releases/<int:release_id>/check/", views.release_check, name="release_check"),
    path("releases/<int:release_id>/build/", views.release_build, name="release_build"),
    path("builds/<int:build_id>/", views.build_detail, name="build_detail"),
    path("builds/<int:build_id>/status/", views.build_status, name="build_status"),
    path("releases/<int:release_id>/verify/", views.release_verify, name="release_verify"),
    path(
        "releases/<int:release_id>/merge-baseline/",
        views.release_merge_baseline,
        name="release_merge_baseline",
    ),
    path("releases/<int:release_id>/cancel/", views.release_cancel, name="release_cancel"),
    path(
        "repositories/<int:repository_id>/logs/",
        views.operation_logs,
        name="operation_logs",
    ),
]
