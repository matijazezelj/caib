"""Compose must interpolate with an .env that predates opt-in services.

`docker compose` interpolates every service, profiled or not, so a `${VAR:?}`
on a variable only an opt-in service uses breaks `make up` for every existing
.env that lacks it. That shipped once (CAIB_AGENT_API_TOKEN); this pins it.
Required-ness for opt-in services belongs in the service's own startup check.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Variables introduced for opt-in (profiled) services. An .env written before
# they existed has none of them.
OPT_IN_VARS = ("CAIB_AGENT_API_TOKEN", "CAIB_AGENT_RO_PASSWORD", "CAIB_AGENT_API_PORT")


def _compose_config(env_file: pathlib.Path, *files: str) -> subprocess.CompletedProcess:
    args = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(env_file)]
    for f in files:
        args += ["-f", str(ROOT / f)]
    return subprocess.run(
        [*args, "config", "--quiet"], capture_output=True, text=True, check=False
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("files", [("docker-compose.yml",),
                                   ("docker-compose.yml", "compose.ha.yml")])
def test_compose_interpolates_without_opt_in_service_variables(tmp_path, files):
    lines = (ROOT / ".env.example").read_text().splitlines()
    pre_feature = [ln for ln in lines if not ln.startswith(OPT_IN_VARS)]
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(pre_feature) + "\n")

    result = _compose_config(env_file, *files)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_vector_services_enable_env_var_interpolation():
    """vector/*.yaml takes its endpoint and credentials from ${CAIB_*}. Since
    Vector 0.57 that stays a literal string unless interpolation is switched
    on, so a service without the variable ships "${CAIB_PG_URL}" as its
    postgres endpoint (0.58 accepted it at validate time; 0.62 does not)."""
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT),
         "--env-file", str(ROOT / ".env.example"), "-f", str(ROOT / "docker-compose.yml"),
         "--profile", "*", "config", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    vector = {n: s for n, s in services.items() if s["image"].startswith("timberio/vector:")}
    assert vector, "no timberio/vector services found in compose"
    for name, svc in vector.items():
        env = svc.get("environment") or {}
        assert env.get("VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION") == "true", name
