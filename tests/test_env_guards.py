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
    "CAIB_AGENT_RO_PASSWORD",
    "CAIB_AGENT_API_TOKEN",
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


def test_detect_service_receives_every_documented_knob():
    """CAIB_NOTIFY_FORMAT was documented in .env.example for a whole release
    without ever being passed to the detect container, so setting it did
    nothing. Every detect/notify/retention knob the example file documents —
    commented out or not — has to reach the service."""
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    passed = set(compose["services"]["detect"]["environment"])
    prefixes = ("CAIB_DETECT_", "CAIB_NOTIFY_", "CAIB_RETENTION_")
    documented = {
        line.lstrip("#").split("=", 1)[0]
        for line in EXAMPLE.read_text().splitlines()
        if line.lstrip("#").startswith(prefixes)
    }
    assert documented, "no detect knobs found in .env.example"
    assert documented <= passed, f"not passed to detect: {sorted(documented - passed)}"


def test_migrate_receives_every_role_password_it_syncs():
    """migrate sets role passwords from env (sync_passwords). CAIB_AGENT_RO_PASSWORD
    shipped in .env and init_env.sh but was never passed to the migrate
    container, so caib_agent_ro had no password and agent-api could not log in
    on a stock compose stack — invisible to tests that call migrate.run() with
    the variable already in the process env."""
    import inspect
    import re

    import yaml
    from caib_core import migrate

    synced = set(re.findall(r'"(CAIB_[A-Z_]+_PASSWORD)"', inspect.getsource(migrate.sync_passwords)))
    assert synced >= {"CAIB_PG_APP_PASSWORD", "CAIB_PG_RO_PASSWORD", "CAIB_AGENT_RO_PASSWORD"}
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    passed = set(compose["services"]["migrate"]["environment"])
    assert synced <= passed, f"not passed to migrate: {sorted(synced - passed)}"



def test_migrate_receives_the_retention_knobs():
    """migrate applies the events/cagg retention knobs; documenting them in
    .env.example without passing them through compose would make them inert."""
    import yaml
    from caib_core import events_retention

    knobs = {events_retention.EVENTS_VAR, events_retention.CAGG_VAR}
    documented = {
        line.lstrip("#").split("=", 1)[0] for line in EXAMPLE.read_text().splitlines()
    }
    assert knobs <= documented, f"not in .env.example: {sorted(knobs - documented)}"
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["migrate"]["environment"]
    assert knobs <= set(env), f"not passed to migrate: {sorted(knobs - set(env))}"
    # Never `:?` — an .env without them must keep starting, unchanged.
    assert all(":?" not in env[k] for k in knobs)
