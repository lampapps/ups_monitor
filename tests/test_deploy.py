import getpass
import shutil
import subprocess

import pytest

from app import settings

DEPLOY = settings.BASE_DIR / "deploy"
SCRIPTS = [DEPLOY / "install.sh", DEPLOY / "setup-target.sh", settings.BASE_DIR / "setup.sh"]


def run(args, **kw):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=30, **kw)


@pytest.fixture(scope="module")
def pubkey(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen not installed")
    key = tmp_path_factory.mktemp("k") / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ups_monitor@test host", "-f", str(key)], check=True)
    return key.with_suffix(".pub").read_text().strip()


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_scripts_pass_shellcheck():
    result = run(["shellcheck", *SCRIPTS])
    assert result.returncode == 0, result.stdout


def test_scripts_are_executable():
    assert all(s.stat().st_mode & 0o111 for s in SCRIPTS)


# ---------------------------------------------------------------- install.sh


def test_render_only_fills_in_user_and_paths(tmp_path):
    result = run([DEPLOY / "install.sh", "--render-only", tmp_path, "--user", "svcuser"])
    assert result.returncode == 0, result.stderr
    monitor = (tmp_path / "ups-monitor.service").read_text()
    web = (tmp_path / "ups-web.service").read_text()
    for text in (monitor, web):
        assert "@" not in text and "User=svcuser" in text and f"WorkingDirectory={settings.BASE_DIR}" in text
    assert f"ExecStart={settings.BASE_DIR}/.venv/bin/python -m app monitor" in monitor
    assert f"ExecStart={settings.BASE_DIR}/.venv/bin/python -m app web" in web


def test_monitor_unit_allows_sudo_and_web_unit_does_not():
    assert "NoNewPrivileges=yes" not in (DEPLOY / "ups-monitor.service").read_text()
    assert "NoNewPrivileges=yes" in (DEPLOY / "ups-web.service").read_text()


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not installed")
@pytest.mark.skipif(not (settings.BASE_DIR / ".venv/bin/python").exists(), reason="no venv")
def test_rendered_units_verify(tmp_path):
    run([DEPLOY / "install.sh", "--render-only", tmp_path, "--user", getpass.getuser()])
    result = run(["systemd-analyze", "verify", tmp_path / "ups-monitor.service", tmp_path / "ups-web.service"])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("user", ["root", "a;b", "$(id)", "Bad User", ""])
def test_install_rejects_bad_users(tmp_path, user):
    result = run([DEPLOY / "install.sh", "--render-only", tmp_path, "--user", user])
    assert result.returncode != 0 and not (tmp_path / "ups-monitor.service").exists()


def test_install_refuses_to_run_without_root():
    result = run([DEPLOY / "install.sh", "--user", getpass.getuser()])
    assert result.returncode == 1 and "run with sudo" in result.stderr


def test_install_rejects_unknown_options():
    assert run([DEPLOY / "install.sh", "--bogus"]).returncode == 2


# ---------------------------------------------------------------- setup-target.sh


def test_target_dry_run_shows_the_exact_changes(pubkey):
    result = run([DEPLOY / "setup-target.sh", "--dry-run", "--pubkey", pubkey, "--from", "10.10.1.50,10.10.1.51"])
    assert result.returncode == 0, result.stderr
    assert f'restrict,from="10.10.1.50,10.10.1.51" {pubkey}' in result.stdout
    assert "upsshutdown ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff\n" in result.stdout
    assert "upsshutdown ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --dry-run" in result.stdout


def test_target_key_is_never_forced_to_a_command(pubkey):
    out = run([DEPLOY / "setup-target.sh", "--dry-run", "--pubkey", pubkey, "--from", "10.0.0.1"]).stdout
    assert "command=" not in out  # a forced command would make the preflight check power the machine off


def test_target_custom_user(pubkey):
    out = run([DEPLOY / "setup-target.sh", "--dry-run", "--pubkey", pubkey, "--user", "pwrdown"]).stdout
    assert "user pwrdown" in out and "pwrdown ALL=(root) NOPASSWD" in out


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--pubkey", "ssh-rsa AAAAB3NzaC1yc2E"],
        ["--pubkey", "not a key"],
        ["--pubkey", "ssh-ed25519 AAAA\nssh-ed25519 BBBB"],
        ["--pubkey", "ssh-ed25519 AAAA", "--from", "10.0.0.1; rm -rf /"],
        ["--pubkey", "ssh-ed25519 AAAA", "--from", 'x" command="evil'],
        ["--pubkey", "ssh-ed25519 AAAA", "--user", "root"],
        ["--pubkey", "ssh-ed25519 AAAA", "--user", "a b"],
    ],
)
def test_target_rejects_bad_input(args):
    result = run([DEPLOY / "setup-target.sh", "--dry-run", *args])
    assert result.returncode != 0 and "Would write" not in result.stdout


def test_target_refuses_to_change_anything_without_root(pubkey):
    result = run([DEPLOY / "setup-target.sh", "--pubkey", pubkey])
    assert result.returncode == 1 and "run as root" in result.stderr


@pytest.mark.skipif(not shutil.which("visudo"), reason="visudo not installed")
def test_sudoers_rules_are_valid_and_match_the_commands_the_app_runs(tmp_path):
    rules = tmp_path / "rules"
    rules.write_text(
        f"u ALL=(root) NOPASSWD: {settings.POWEROFF_COMMAND.removeprefix('sudo -n ')}\n"
        f"u ALL=(root) NOPASSWD: {settings.DRYRUN_COMMAND.removeprefix('sudo -n ')}\n"
    )
    assert run(["visudo", "-cf", rules]).returncode == 0
    target = (DEPLOY / "setup-target.sh").read_text()
    assert settings.POWEROFF_COMMAND.removeprefix("sudo -n ") in target
    assert settings.DRYRUN_COMMAND.removeprefix("sudo -n ") in target


def test_install_runs_the_config_check_from_the_project_root(tmp_path):
    script = (DEPLOY / "install.sh").read_text()
    assert script.index('cd "$APP_DIR"') < script.index("-m app config")
    other = tmp_path / "elsewhere"
    other.mkdir()
    # Invoked from another directory the script must still find its units.
    result = run([DEPLOY / "install.sh", "--render-only", tmp_path / "out", "--user", "svcuser"], cwd=other)
    assert result.returncode == 0 and (tmp_path / "out" / "ups-monitor.service").exists()
