import pytest

from dev_tools import verify_editor_option_creation


def test_verification_targets_the_isolated_composed_store():
    command = verify_editor_option_creation.build_command()

    assert command[:3] == (
        verify_editor_option_creation.sys.executable,
        "-m",
        "pytest",
    )
    assert command[-1] == verify_editor_option_creation.TEST_TARGET


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GOOGLE_CLOUD_PROJECT", "project-prod"),
        ("CLOUD_RUN", "true"),
        ("K_SERVICE", "service"),
        ("APP_ENV", "production"),
    ],
)
def test_verification_refuses_non_local_environments(variable, value, monkeypatch):
    for key in ("GOOGLE_CLOUD_PROJECT", "CLOUD_RUN", "K_SERVICE", "APP_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(variable, value)

    with pytest.raises(RuntimeError, match="solo puede ejecutarse en desarrollo local"):
        verify_editor_option_creation.assert_local_execution()
