import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "start-macos.sh"


@pytest.fixture
def deployment(tmp_path):
    project = tmp_path / "机器人 project"
    project.mkdir()
    shutil.copyfile(SCRIPT, project / SCRIPT.name)
    (project / ".env").write_text('LLM_CHAT_WHITELIST=["123"]\n', encoding="utf-8")
    conda_base = tmp_path / "miniconda base"
    conda_init = conda_base / "etc/profile.d/conda.sh"
    conda_init.parent.mkdir(parents=True)
    conda_init.write_text('conda() { export CONDA_PREFIX="$2"; }\n', encoding="utf-8")
    environment = tmp_path / "conda env"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").symlink_to("/usr/bin/true")
    nb = environment / "bin/nb"
    nb.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$PWD" "$CONDA_PREFIX" "$@"\n',
        encoding="utf-8",
    )
    nb.chmod(0o755)
    (project / ".env.macos").write_text(
        f'CONDA_BASE="{conda_base}"\nCONDA_ENV_PATH="{environment}"\n',
        encoding="utf-8",
    )
    return project, conda_base, environment


def run_script(project, cwd):
    return subprocess.run(
        ["/bin/bash", str(project / SCRIPT.name)],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "LANG": os.environ.get("LANG", "en_US.UTF-8")},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_launch_uses_configured_environment_and_project_directory(deployment, tmp_path):
    project, _, environment = deployment

    result = run_script(project, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(project),
        str(environment),
        "--python",
        str(environment / "bin/python"),
        "--no-venv",
        "run",
    ]


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        (".env.macos", ".env.macos.example"),
        (".env", ".env.example"),
    ],
)
def test_missing_configuration_stops_startup(deployment, tmp_path, missing, message):
    project, _, _ = deployment
    (project / missing).unlink()

    result = run_script(project, tmp_path)

    assert result.returncode != 0
    assert message in result.stderr
    assert result.stdout == ""


def test_empty_conda_paths_stop_startup(deployment, tmp_path):
    project, _, _ = deployment
    (project / ".env.macos").write_text('CONDA_BASE=""\nCONDA_ENV_PATH=""\n')

    result = run_script(project, tmp_path)

    assert result.returncode != 0
    assert "CONDA_BASE" in result.stderr
    assert result.stdout == ""


def test_activation_must_select_configured_environment(deployment, tmp_path):
    project, conda_base, _ = deployment
    (conda_base / "etc/profile.d/conda.sh").write_text(
        'conda() { export CONDA_PREFIX="/tmp"; }\n'
    )

    result = run_script(project, tmp_path)

    assert result.returncode != 0
    assert "Conda 激活后的环境与配置不一致" in result.stderr
    assert result.stdout == ""
