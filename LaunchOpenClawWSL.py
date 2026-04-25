import atexit
import ctypes
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from pathlib import Path

WSL_DISTRO = "Ubuntu"
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
DASHBOARD_URL = "http://127.0.0.1:18789/"
OLLAMA_EXE = (
    Path(os.environ.get("USERPROFILE", str(Path.home())))
    / "AppData"
    / "Local"
    / "Programs"
    / "Ollama"
    / "ollama.exe"
)
WSL_SERVICE_NAME = "openclaw-gateway.service"
WSL_SERVICE_IS_ACTIVE = f"systemctl --user is-active --quiet {WSL_SERVICE_NAME}"
WSL_SERVICE_START = f"systemctl --user start {WSL_SERVICE_NAME} >/dev/null 2>&1 || true"
WSL_SERVICE_STOP = f"systemctl --user stop {WSL_SERVICE_NAME} >/dev/null 2>&1 || true"
BASE_LINUX_PATH = "/usr/local/bin:/usr/bin:/bin"
WSL_FALLBACK_START = (
    "mkdir -p ~/.openclaw/logs; "
    "nohup {openclaw_path} gateway run --force > ~/.openclaw/logs/gateway.out.log 2> ~/.openclaw/logs/gateway.err.log < /dev/null & "
    "printf '%s\\n' \"$!\""
)
WSL_KEEPALIVE_COMMAND = "trap 'exit 0' INT TERM; while true; do sleep 300; done"
WSL_ACTIVE_CONNECTIONS_COMMAND = (
    "ss -Htn state established '( sport = :18789 )' 2>/dev/null | wc -l"
)
MONITOR_POLL_SECONDS = 5
IDLE_EXIT_SECONDS = 300

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

OPENCLAW_STATE_LOCK = threading.Lock()
OPENCLAW_STARTED_SERVICE = False
OPENCLAW_STARTED_FALLBACK_PIDS = set()
WSL_KEEPALIVE_PROCESS = None
OPENCLAW_CLEANUP_DONE = False
CONSOLE_CTRL_HANDLER = None


def status(message):
    print(message, flush=True)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        params = subprocess.list2cmdline([str(Path(__file__).resolve()), *sys.argv[1:]])

    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    if rc <= 32:
        raise RuntimeError(f"Elevation failed with ShellExecuteW rc={rc}")


def http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        # Any HTTP error response (e.g. 503 when UI assets not built) means
        # the gateway process is up and listening.
        return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def wait_for_url(url, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if http_ok(url):
            return True
        time.sleep(1)
    return False


def wait_for_url_down(url, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not http_ok(url):
            return True
        time.sleep(1)
    return False


def get_user_env_var(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None


def run_wsl_bash(command, interactive=False, capture_output=False, check=False, timeout=None):
    shell_flag = "-ic" if interactive else "-lc"
    result = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "-e", "bash", shell_flag, command],
        capture_output=capture_output,
        text=True,
        check=check,
        timeout=timeout,
    )
    return result


def is_windows_backed_wsl_path(path):
    normalized = path.strip()
    return normalized.startswith("/mnt/c/") or normalized.lower().startswith("c:\\")


def is_native_wsl_path(path):
    return bool(path) and path.startswith("/") and not is_windows_backed_wsl_path(path)


def first_nonempty_line(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def get_wsl_service_unit_text():
    result = run_wsl_bash(f"systemctl --user cat {shlex.quote(WSL_SERVICE_NAME)} 2>/dev/null || true", capture_output=True)
    return result.stdout


def parse_service_exec_path(service_text):
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


def parse_service_native_bin_dir(service_text):
    for line in service_text.splitlines():
        if not line.startswith("Environment=PATH="):
            continue
        raw_value = line.removeprefix("Environment=PATH=").strip().strip('"')
        for entry in raw_value.split(":"):
            candidate = entry.strip()
            if candidate.endswith("/bin") and is_native_wsl_path(candidate):
                return candidate
    return ""


def find_wsl_native_openclaw_path():
    service_text = get_wsl_service_unit_text()
    exec_path = parse_service_exec_path(service_text)
    if is_native_wsl_path(exec_path) and exec_path.rstrip("/").endswith("/openclaw"):
        return exec_path

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
    result = run_wsl_bash(command, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("Failed to find a native Linux OpenClaw install inside WSL.")

    path = first_nonempty_line(result.stdout)
    if not is_native_wsl_path(path):
        raise RuntimeError(f"WSL OpenClaw path is not native Linux: {path or 'not found'}")
    return path


def build_wsl_runtime_path():
    service_text = get_wsl_service_unit_text()
    native_bin_dir = parse_service_native_bin_dir(service_text)
    if not native_bin_dir:
        native_bin_dir = find_wsl_native_openclaw_path().rsplit("/", 1)[0]

    if not is_native_wsl_path(native_bin_dir):
        raise RuntimeError(f"WSL native bin directory is invalid: {native_bin_dir or 'not found'}")
    return f"{native_bin_dir}:{BASE_LINUX_PATH}"


def run_native_openclaw(*args, capture_output=False, timeout=None):
    openclaw_path = find_wsl_native_openclaw_path()
    runtime_path = build_wsl_runtime_path()
    command = f"export PATH={shlex.quote(runtime_path)}\n{shlex.quote(openclaw_path)} {' '.join(shlex.quote(arg) for arg in args)}"
    return run_wsl_bash(command, capture_output=capture_output, timeout=timeout)


def wsl_gateway_is_listening():
    try:
        result = run_wsl_bash(
            "ss -H -ltn '( sport = :18789 )' 2>/dev/null | grep -q .",
            timeout=8,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def stop_windows_openclaw_for_wsl():
    if wsl_gateway_is_listening():
        return

    command = (
        "Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique "
        "| Where-Object { $_ -gt 0 } "
        "| ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    wait_for_url_down(DASHBOARD_URL, 12)


def start_wsl_keepalive():
    return subprocess.Popen(
        ["wsl", "-d", WSL_DISTRO, "-e", "bash", "-lc", WSL_KEEPALIVE_COMMAND],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def stop_process(process):
    if not process or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def set_keepalive_process(process):
    global WSL_KEEPALIVE_PROCESS
    with OPENCLAW_STATE_LOCK:
        WSL_KEEPALIVE_PROCESS = process


def remember_openclaw_service_started():
    global OPENCLAW_STARTED_SERVICE
    with OPENCLAW_STATE_LOCK:
        if not OPENCLAW_CLEANUP_DONE:
            OPENCLAW_STARTED_SERVICE = True


def remember_openclaw_fallback_pid(pid):
    if pid is None:
        return

    with OPENCLAW_STATE_LOCK:
        if not OPENCLAW_CLEANUP_DONE:
            OPENCLAW_STARTED_FALLBACK_PIDS.add(pid)


def claim_cleanup_state():
    global OPENCLAW_CLEANUP_DONE, OPENCLAW_STARTED_SERVICE, WSL_KEEPALIVE_PROCESS
    with OPENCLAW_STATE_LOCK:
        if OPENCLAW_CLEANUP_DONE:
            return None

        OPENCLAW_CLEANUP_DONE = True
        cleanup_state = {
            "service_started": OPENCLAW_STARTED_SERVICE,
            "fallback_pids": sorted(OPENCLAW_STARTED_FALLBACK_PIDS),
            "keepalive_process": WSL_KEEPALIVE_PROCESS,
        }
        OPENCLAW_STARTED_SERVICE = False
        OPENCLAW_STARTED_FALLBACK_PIDS.clear()
        WSL_KEEPALIVE_PROCESS = None
        return cleanup_state


def is_openclaw_service_active():
    result = run_wsl_bash(WSL_SERVICE_IS_ACTIVE)
    return result.returncode == 0


def parse_background_pid(output_text):
    for line in reversed(output_text.splitlines()):
        candidate = line.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


def start_openclaw_fallback():
    openclaw_path = find_wsl_native_openclaw_path()
    runtime_path = build_wsl_runtime_path()
    command = (
        f"export PATH={shlex.quote(runtime_path)}\n"
        f"{WSL_SERVICE_STOP}; "
        f"{WSL_FALLBACK_START.format(openclaw_path=shlex.quote(openclaw_path))}"
    )
    result = run_wsl_bash(command, capture_output=True, check=True)
    pid = parse_background_pid(result.stdout)
    remember_openclaw_fallback_pid(pid)
    return pid


def build_fallback_stop_command(pids):
    joined_pids = " ".join(str(pid) for pid in pids)
    return (
        f"for pid in {joined_pids}; do "
        'children=$(ps -o pid= --ppid "$pid" 2>/dev/null); '
        '[ -n "$children" ] && kill -TERM $children >/dev/null 2>&1 || true; '
        'kill -TERM "$pid" >/dev/null 2>&1 || true; '
        "done; "
        "sleep 2; "
        f"for pid in {joined_pids}; do "
        'children=$(ps -o pid= --ppid "$pid" 2>/dev/null); '
        '[ -n "$children" ] && kill -KILL $children >/dev/null 2>&1 || true; '
        'kill -KILL "$pid" >/dev/null 2>&1 || true; '
        "done"
    )


def build_cleanup_wsl_command(cleanup_state):
    commands = []
    if cleanup_state["service_started"]:
        commands.append(WSL_SERVICE_STOP)
    if cleanup_state["fallback_pids"]:
        commands.append(build_fallback_stop_command(cleanup_state["fallback_pids"]))
    return "; ".join(commands)


def spawn_detached_wsl_cleanup(command):
    if not command:
        return

    creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["wsl", "-d", WSL_DISTRO, "-e", "bash", "-lc", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def cleanup_launcher_owned_openclaw(detached=False):
    cleanup_state = claim_cleanup_state()
    if cleanup_state is None:
        return

    stop_process(cleanup_state["keepalive_process"])
    cleanup_command = build_cleanup_wsl_command(cleanup_state)
    if not cleanup_command:
        return

    try:
        if detached:
            spawn_detached_wsl_cleanup(cleanup_command)
        else:
            run_wsl_bash(cleanup_command, timeout=15)
    except Exception:
        pass


def install_exit_hooks():
    global CONSOLE_CTRL_HANDLER

    atexit.register(cleanup_launcher_owned_openclaw)

    if os.name != "nt":
        return

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

    def console_ctrl_handler(ctrl_type):
        if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            cleanup_launcher_owned_openclaw(detached=True)
            return True
        if ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
            return False
        return False

    CONSOLE_CTRL_HANDLER = handler_type(console_ctrl_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(CONSOLE_CTRL_HANDLER, True)


def ensure_ollama():
    if http_ok(OLLAMA_URL):
        return True

    if not OLLAMA_EXE.exists():
        raise FileNotFoundError(f"Ollama executable not found at {OLLAMA_EXE}")

    env = os.environ.copy()
    models_dir = get_user_env_var("OLLAMA_MODELS")
    if models_dir:
        env["OLLAMA_MODELS"] = models_dir

    creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(OLLAMA_EXE), "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )
    return wait_for_url(OLLAMA_URL, 45)


def ensure_wsl_bridge():
    result = run_wsl_bash("curl -fsS --max-time 8 http://127.0.0.1:11434/api/tags >/dev/null")
    return result.returncode == 0


def ensure_openclaw():
    if http_ok(DASHBOARD_URL) and wsl_gateway_is_listening():
        return True
    if http_ok(DASHBOARD_URL):
        stop_windows_openclaw_for_wsl()

    if http_ok(DASHBOARD_URL) and not wsl_gateway_is_listening():
        raise RuntimeError(
            "Port 18789 is still reachable from Windows, but WSL is not listening on it. "
            "Close the other OpenClaw launcher or wait for the old gateway to exit."
        )

    service_was_active = is_openclaw_service_active()
    run_wsl_bash(WSL_SERVICE_START)
    if not service_was_active:
        remember_openclaw_service_started()
    if wait_for_url(DASHBOARD_URL, 25):
        return True

    start_openclaw_fallback()
    return wait_for_url(DASHBOARD_URL, 30)


def get_gateway_token():
    result = run_wsl_bash("cat ~/.openclaw/openclaw.json", capture_output=True, check=True)
    data = json.loads(result.stdout)
    return data["gateway"]["auth"]["token"]


def copy_to_clipboard(text):
    try:
        subprocess.run(["cmd", "/c", "clip"], input=text, text=True, check=True)
        return True
    except subprocess.SubprocessError:
        return False


def open_browser(url):
    app_path_subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, app_path_subkey) as key:
                edge_path, _ = winreg.QueryValueEx(key, None)
        except FileNotFoundError:
            continue

        edge_exe = Path(edge_path)
        if edge_exe.exists():
            subprocess.Popen(
                [str(edge_exe), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            return

    for base in (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not base:
            continue

        edge_exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if edge_exe.exists():
            subprocess.Popen(
                [str(edge_exe), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            return

    result = subprocess.run(
        ["where.exe", "msedge.exe"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        edge_exe = Path(line.strip())
        if edge_exe.exists():
            subprocess.Popen(
                [str(edge_exe), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            return

    raise FileNotFoundError("Microsoft Edge executable not found.")


def build_dashboard_url(token=None):
    if not token:
        return DASHBOARD_URL

    return f"{DASHBOARD_URL}#token={urllib.parse.quote(token, safe='')}"


def extract_dashboard_url(text):
    for match in re.findall(r"https?://[^\s)]+", text):
        return match.rstrip(".,")
    return None


def resolve_dashboard_url(token=None):
    try:
        result = run_native_openclaw("dashboard", "--no-open", capture_output=True, timeout=25)
        cli_url = extract_dashboard_url(f"{result.stdout}\n{result.stderr}")
        if cli_url and "#token=" in cli_url:
            return cli_url
        if token:
            return build_dashboard_url(token)
        if cli_url:
            return cli_url
    except Exception:
        pass

    return build_dashboard_url(token)


def get_active_dashboard_connections():
    result = run_wsl_bash(WSL_ACTIVE_CONNECTIONS_COMMAND, capture_output=True)
    if result.returncode != 0:
        return 0

    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def monitor_openclaw():
    keepalive_process = start_wsl_keepalive()
    set_keepalive_process(keepalive_process)
    dashboard_was_ready = http_ok(DASHBOARD_URL)

    status("Keeping WSL/OpenClaw alive until you close this launcher window.")

    try:
        while True:
            if keepalive_process.poll() is not None:
                status("WSL keepalive exited. Restarting it.")
                keepalive_process = start_wsl_keepalive()
                set_keepalive_process(keepalive_process)

            dashboard_ready = http_ok(DASHBOARD_URL)
            if not dashboard_ready:
                if dashboard_was_ready:
                    status("OpenClaw became unreachable. Restarting it.")
                if not ensure_openclaw():
                    time.sleep(MONITOR_POLL_SECONDS)
                    continue
                dashboard_ready = True
                status("OpenClaw is reachable again.")

            dashboard_was_ready = dashboard_ready
            time.sleep(MONITOR_POLL_SECONDS)
    except KeyboardInterrupt:
        status("Stopping launcher monitor.")
    finally:
        stop_process(keepalive_process)
        set_keepalive_process(None)


def main():
    if os.name != "nt":
        raise RuntimeError("This launcher must be run on Windows.")

    if not is_admin():
        relaunch_as_admin()
        return

    install_exit_hooks()

    status("Checking Windows Ollama...")
    if not ensure_ollama():
        raise RuntimeError(
            "Ollama did not become ready on http://127.0.0.1:11434 within 45 seconds."
        )

    status("Checking WSL access to Windows Ollama...")
    if not ensure_wsl_bridge():
        raise RuntimeError("WSL could not reach Windows Ollama at http://127.0.0.1:11434.")

    status("Stopping Windows OpenClaw if needed...")
    stop_windows_openclaw_for_wsl()

    status("Starting OpenClaw...")
    if not ensure_openclaw():
        raise RuntimeError(
            "OpenClaw did not become ready on http://127.0.0.1:18789/. "
            f"Check logs with: wsl -d {WSL_DISTRO} -e bash -lc 'tail -n 100 ~/.openclaw/logs/gateway.err.log ~/.openclaw/logs/gateway.out.log'"
        )

    status("Opening OpenClaw dashboard...")
    token = None
    try:
        token = get_gateway_token()
    except Exception:
        token = None

    dashboard_url = resolve_dashboard_url(token)
    if token:
        if copy_to_clipboard(token):
            status("Gateway token copied to clipboard as a fallback.")
        else:
            status(f"Gateway token: {token}")

    open_browser(dashboard_url)
    status(f"OpenClaw is ready at {DASHBOARD_URL}")
    monitor_openclaw()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
