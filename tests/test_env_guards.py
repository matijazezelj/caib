"""The two shell guards around `make up`: generated secrets and the refusal to
publish shipped ones."""

import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
SECRETS = (
    "CAIB_PG_SUPERPASS",
    "CAIB_PG_APP_PASSWORD",
    "CAIB_PG_RO_PASSWORD",
    "CAIB_HA_REPLICATION_PASSWORD",
    "CAIB_GRAFANA_ADMIN_PASSWORD",
)


def value_of(text: str, var: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1]
    raise KeyError(var)


def preflight(env_file: pathlib.Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "preflight.sh")],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CAIB_ENV_FILE": str(env_file), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_env_generates_distinct_secrets(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(EXAMPLE, tmp_path / ".env.example")
    shutil.copy(ROOT / "scripts" / "init_env.sh", tmp_path / "scripts" / "init_env.sh")

    subprocess.run(["bash", "scripts/init_env.sh"], cwd=tmp_path, check=True, capture_output=True)
    generated = (tmp_path / ".env").read_text()
    example = EXAMPLE.read_text()

    values = [value_of(generated, var) for var in SECRETS]
    for var, value in zip(SECRETS, values, strict=True):
        assert value != value_of(example, var), f"{var} kept the example value"
        assert len(value) >= 24
    assert len(set(values)) == len(values), "the same secret was reused"

    # Non-secret settings are carried over untouched.
    assert value_of(generated, "CAIB_BIND_ADDR") == value_of(example, "CAIB_BIND_ADDR")

    # Re-running never overwrites an existing .env.
    subprocess.run(["bash", "scripts/init_env.sh"], cwd=tmp_path, check=True, capture_output=True)
    assert (tmp_path / ".env").read_text() == generated


def test_defaults_on_loopback_warn_but_run(tmp_path):
    env_file = tmp_path / ".env"
    shutil.copy(EXAMPLE, env_file)
    result = preflight(env_file)
    assert result.returncode == 0
    assert "warning" in result.stderr


def test_defaults_on_a_routable_address_refuse(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(EXAMPLE.read_text().replace("CAIB_BIND_ADDR=127.0.0.1", "CAIB_BIND_ADDR=0.0.0.0"))
    result = preflight(env_file)
    assert result.returncode == 1
    assert "refusing to start" in result.stderr


def test_the_override_is_honoured(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(EXAMPLE.read_text().replace("CAIB_BIND_ADDR=127.0.0.1", "CAIB_BIND_ADDR=0.0.0.0"))
    assert preflight(env_file, CAIB_ALLOW_DEFAULT_SECRETS="1").returncode == 0


def test_changed_secrets_pass_anywhere(tmp_path):
    text = EXAMPLE.read_text().replace("CAIB_BIND_ADDR=127.0.0.1", "CAIB_BIND_ADDR=0.0.0.0")
    for var in SECRETS:
        text = text.replace(f"{var}={value_of(EXAMPLE.read_text(), var)}", f"{var}=changed-{var}")
    env_file = tmp_path / ".env"
    env_file.write_text(text)
    result = preflight(env_file)
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
