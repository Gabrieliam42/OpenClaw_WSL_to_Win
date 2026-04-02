import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

USER_HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
DEFAULT_WINDOWS_OPENCLAW_CMD = USER_HOME / "AppData" / "Roaming" / "npm" / "openclaw.cmd"
WSL_DISTRO = "Ubuntu"
WSL_SERVICE_NAME = "openclaw-gateway.service"
BASE_LINUX_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass
class InstallInfo:
    path: str
    version: str


@dataclass
class WslResolution:
    npm_path: str
    openclaw_path: str
    version: str


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def print_kv(label: str, value: str) -> None:
    print(f"{label}: {value or 'not found'}")


def run_command(
    args: list[str],
    *,
    check: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def run_powershell(
    command: str,
    *,
    check: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=check,
        timeout=timeout,
    )


def run_wsl_bash(
    command: str,
    *,
    check: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["wsl", "-d", WSL_DISTRO, "-e", "bash", "-lc", command],
        check=check,
        timeout=timeout,
    )


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def parse_key_value_output(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def is_windows_backed_wsl_path(path: str) -> bool:
    normalized = path.strip()
    return normalized.startswith("/mnt/c/") or normalized.lower().startswith("c:\\")


def is_native_wsl_path(path: str) -> bool:
    return bool(path) and path.startswith("/") and not is_windows_backed_wsl_path(path)


def ensure_success(
    result: subprocess.CompletedProcess[str],
    failure_message: str,
) -> subprocess.CompletedProcess[str]:
    if result.returncode == 0:
        return result

    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    details = stderr or stdout
    if details:
        raise RuntimeError(f"{failure_message}\n{details}")
    raise RuntimeError(failure_message)


def find_windows_openclaw_cmd() -> Path:
    if DEFAULT_WINDOWS_OPENCLAW_CMD.exists():
        return DEFAULT_WINDOWS_OPENCLAW_CMD

    result = run_command(["where.exe", "openclaw.cmd"])
    for line in result.stdout.splitlines():
        candidate = Path(line.strip())
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "Windows OpenClaw command not found. "
        f"Expected {DEFAULT_WINDOWS_OPENCLAW_CMD} or openclaw.cmd on PATH."
    )


def inspect_windows_install() -> InstallInfo:
    source_result = run_powershell("(Get-Command openclaw -ErrorAction Stop).Source")
    source = first_nonempty_line(source_result.stdout)
    if not source:
        source = str(find_windows_openclaw_cmd())

    version_result = ensure_success(
        run_powershell("openclaw --version"),
        "Failed to read the Windows PowerShell OpenClaw version.",
    )
    version = first_nonempty_line(version_result.stdout)
    if not version:
        raise RuntimeError("Windows PowerShell OpenClaw version output was empty.")

    return InstallInfo(path=source, version=version)


def inspect_wsl_resolution(path_override: str | None = None) -> WslResolution:
    export_path = ""
    if path_override:
        export_path = f"export PATH={shlex.quote(path_override)}\n"

    command = f"""{export_path}set +e
npm_path="$(command -v npm 2>/dev/null || true)"
openclaw_path="$(command -v openclaw 2>/dev/null || true)"
version=""
if [ -n "$openclaw_path" ]; then
  version="$(openclaw --version 2>/dev/null | head -n 1 || true)"
fi
printf 'npm_path=%s\n' "$npm_path"
printf 'openclaw_path=%s\n' "$openclaw_path"
printf 'version=%s\n' "$version"
"""
    result = run_wsl_bash(command)
    parsed = parse_key_value_output(result.stdout)
    return WslResolution(
        npm_path=parsed.get("npm_path", ""),
        openclaw_path=parsed.get("openclaw_path", ""),
        version=parsed.get("version", ""),
    )


def get_wsl_service_unit_text() -> str:
    result = run_wsl_bash(f"systemctl --user cat {shlex.quote(WSL_SERVICE_NAME)} 2>/dev/null || true")
    return result.stdout


def parse_service_exec_path(service_text: str) -> str:
    for line in service_text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        command = line.removeprefix("ExecStart=").strip()
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if parts:
            return parts[0]
    return ""


def parse_service_native_bin_dir(service_text: str) -> str:
    exec_path = parse_service_exec_path(service_text)
    if is_native_wsl_path(exec_path):
        return str(PurePosixPath(exec_path).parent)

    for line in service_text.splitlines():
        if not line.startswith("Environment=PATH="):
            continue
        raw_value = line.removeprefix("Environment=PATH=").strip().strip('"')
        for entry in raw_value.split(":"):
            candidate = entry.strip()
            if candidate.endswith("/bin") and is_native_wsl_path(candidate):
                return candidate
    return ""


def find_wsl_native_openclaw_path() -> str:
    command = r"""shopt -s nullglob
for candidate in \
  "$HOME"/.nvm/versions/node/*/bin/openclaw \
  "$HOME/.npm-global/bin/openclaw" \
  "$HOME/.local/bin/openclaw" \
  "/usr/local/bin/openclaw" \
  "/usr/bin/openclaw"
do
  [ -x "$candidate" ] || continue
  case "$candidate" in
    /mnt/c/*) continue ;;
  esac
  readlink -f "$candidate" || printf '%s\n' "$candidate"
  exit 0
done
exit 1
"""
    result = run_wsl_bash(command)
    if result.returncode != 0:
        raise RuntimeError("Failed to find a native Linux OpenClaw install inside WSL.")

    path = first_nonempty_line(result.stdout)
    if not is_native_wsl_path(path):
        raise RuntimeError(f"WSL OpenClaw path is not native Linux: {path or 'not found'}")
    return path


def build_wsl_update_path(service_text: str) -> str:
    native_bin_dir = parse_service_native_bin_dir(service_text)
    if not native_bin_dir:
        native_openclaw_path = find_wsl_native_openclaw_path()
        native_bin_dir = str(PurePosixPath(native_openclaw_path).parent)

    if not is_native_wsl_path(native_bin_dir):
        raise RuntimeError(f"WSL native bin directory is invalid: {native_bin_dir or 'not found'}")
    return f"{native_bin_dir}:{BASE_LINUX_PATH}"


def ensure_native_wsl_resolution(resolution: WslResolution, context: str) -> None:
    if not is_native_wsl_path(resolution.npm_path):
        raise RuntimeError(f"{context}: npm does not resolve to a native Linux path: {resolution.npm_path or 'not found'}")
    if not is_native_wsl_path(resolution.openclaw_path):
        raise RuntimeError(
            f"{context}: openclaw does not resolve to a native Linux path: {resolution.openclaw_path or 'not found'}"
        )
    if not resolution.version:
        raise RuntimeError(f"{context}: openclaw --version produced no output.")


def update_windows_openclaw() -> None:
    ensure_success(
        run_command(["npm", "install", "-g", "openclaw@latest"], timeout=180),
        "Failed to update the Windows global OpenClaw npm package.",
    )


def update_wsl_openclaw(wsl_update_path: str) -> None:
    command = f"""export PATH={shlex.quote(wsl_update_path)}
set -e
npm install -g openclaw@latest
"""
    ensure_success(
        run_wsl_bash(command, timeout=180),
        "Failed to update the native WSL OpenClaw npm package.",
    )


def wsl_service_exists() -> bool:
    result = run_wsl_bash(f"systemctl --user cat {shlex.quote(WSL_SERVICE_NAME)} >/dev/null 2>&1")
    return result.returncode == 0


def wsl_service_is_active() -> bool:
    result = run_wsl_bash(f"systemctl --user is-active --quiet {shlex.quote(WSL_SERVICE_NAME)}")
    return result.returncode == 0


def restart_wsl_service_if_needed(wsl_update_path: str) -> str:
    if not wsl_service_exists():
        return "missing"
    if not wsl_service_is_active():
        return "inactive"

    command = f"""export PATH={shlex.quote(wsl_update_path)}
set -e
systemctl --user restart {shlex.quote(WSL_SERVICE_NAME)}
systemctl --user is-active --quiet {shlex.quote(WSL_SERVICE_NAME)}
"""
    ensure_success(
        run_wsl_bash(command, timeout=60),
        f"Failed to restart the WSL bridge service {WSL_SERVICE_NAME}.",
    )
    return "restarted"


def describe_wsl_default_resolution(default_resolution: WslResolution) -> None:
    print_section("WSL Default Resolution")
    print_kv("Default npm path", default_resolution.npm_path)
    print_kv("Default openclaw path", default_resolution.openclaw_path)
    contaminated = is_windows_backed_wsl_path(default_resolution.npm_path) or is_windows_backed_wsl_path(
        default_resolution.openclaw_path
    )
    print_kv("Windows PATH contamination detected", "yes" if contaminated else "no")


def describe_install(label: str, install: InstallInfo) -> None:
    print_section(label)
    print_kv("Path", install.path)
    print_kv("Version", install.version)


def describe_wsl_native_install(label: str, resolution: WslResolution, wsl_update_path: str) -> None:
    print_section(label)
    print_kv("Update PATH", wsl_update_path)
    print_kv("npm path", resolution.npm_path)
    print_kv("OpenClaw path", resolution.openclaw_path)
    print_kv("Version", resolution.version)


def main() -> int:
    try:
        print("Inspecting current OpenClaw installs...")

        windows_before = inspect_windows_install()
        default_wsl_before = inspect_wsl_resolution()
        service_text_before = get_wsl_service_unit_text()
        wsl_update_path = build_wsl_update_path(service_text_before)
        native_wsl_before = inspect_wsl_resolution(wsl_update_path)
        ensure_native_wsl_resolution(native_wsl_before, "Before WSL update")

        describe_install("Windows PowerShell OpenClaw Before", windows_before)
        describe_wsl_default_resolution(default_wsl_before)
        describe_wsl_native_install("WSL Native OpenClaw Before", native_wsl_before, wsl_update_path)

        print_section("Updating Windows PowerShell OpenClaw")
        update_windows_openclaw()
        print("Windows update complete.")

        print_section("Updating Native WSL OpenClaw")
        update_wsl_openclaw(wsl_update_path)
        print("WSL update complete.")

        print_section("WSL Bridge Service")
        service_restart_state = restart_wsl_service_if_needed(wsl_update_path)
        print_kv("Service action", service_restart_state)

        windows_after = inspect_windows_install()
        native_wsl_after = inspect_wsl_resolution(wsl_update_path)
        ensure_native_wsl_resolution(native_wsl_after, "After WSL update")

        service_text_after = get_wsl_service_unit_text()
        service_exec_path = parse_service_exec_path(service_text_after)
        if service_exec_path and not is_native_wsl_path(service_exec_path):
            raise RuntimeError(
                "WSL bridge verification failed: "
                f"{WSL_SERVICE_NAME} ExecStart points to a non-native path: {service_exec_path}"
            )

        describe_install("Windows PowerShell OpenClaw After", windows_after)
        describe_wsl_native_install("WSL Native OpenClaw After", native_wsl_after, wsl_update_path)

        print_section("Bridge Verification")
        print_kv("Service name", WSL_SERVICE_NAME)
        print_kv("Service ExecStart path", service_exec_path or "not available")
        print_kv("Service uses native Linux path", "yes" if is_native_wsl_path(service_exec_path) else "unknown")
        print_kv(
            "WSL native OpenClaw avoids /mnt/c",
            "yes" if not is_windows_backed_wsl_path(native_wsl_after.openclaw_path) else "no",
        )
        print_kv(
            "WSL native npm avoids /mnt/c",
            "yes" if not is_windows_backed_wsl_path(native_wsl_after.npm_path) else "no",
        )

        print_section("Done")
        print("OpenClaw updates completed without changing launcher scripts, bridge logic, ports, tokens, or dashboard settings.")
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
