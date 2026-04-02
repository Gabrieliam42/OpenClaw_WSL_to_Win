# OpenClaw WSL to Windows Launcher

A bridge launcher that connects Windows to [OpenClaw](https://openclaw.ai) running inside WSL2. It handles everything between the two environments — starting Ollama on Windows, verifying WSL2 can reach it, bringing up the OpenClaw gateway service, opening the dashboard in Microsoft Edge pre-authenticated, and keeping the whole stack alive for as long as the launcher window is open.

It works with whatever location you use for your Ollama models, provided the PATH or environment configuration is already set for that.

---

## Requirements

- Windows 11 with WSL2 (Ubuntu distro)
- [OpenClaw](https://openclaw.ai) installed inside WSL2 and configured
- [Ollama](https://ollama.com) installed on Windows (default path: `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`)
- The OpenClaw gateway systemd service set up in WSL2 (`openclaw-gateway.service`)
- Microsoft Edge installed on Windows

---

## Usage

### Compiled executable (recommended)

Download `LaunchOpenClawWSL.exe` from the [Releases](../../releases) page and double-click it. It will:

1. Request UAC elevation (admin required for WSL2 interactions)
2. Start Ollama on Windows if it is not already running
3. Verify WSL2 can reach Ollama
4. Start the OpenClaw gateway in WSL2 (via systemd service, with a fallback to direct launch)
5. Open the OpenClaw dashboard in Microsoft Edge, pre-authenticated with your gateway token
6. Copy the gateway token to your clipboard as a fallback
7. Keep WSL2 and the gateway alive — auto-restarting if the gateway goes down
8. Clean up (stop any services it started) when you close the launcher window

### Python script

```text
python LaunchOpenClawWSL.py
```

Requires Python 3.x on Windows. Same behavior as the exe.

---

## How it works

### Startup sequence

```text
Checking Windows Ollama...
Checking WSL access to Windows Ollama...
Starting OpenClaw...
Opening OpenClaw dashboard...
Gateway token copied to clipboard as a fallback.
OpenClaw is ready at http://127.0.0.1:18789/
Keeping WSL/OpenClaw alive until you close this launcher window.
```

- **Ollama check**: hits `http://127.0.0.1:11434/api/tags`. If Ollama is not running, launches `ollama.exe serve` as a detached process and waits up to 45 seconds.
- **WSL bridge check**: runs `curl` inside WSL2 to confirm it can reach the Windows Ollama instance.
- **Gateway startup**: checks `http://127.0.0.1:18789/`. If not up, tries `systemctl --user start openclaw-gateway.service` first (waits 25 s), then falls back to `nohup openclaw gateway run --force` (waits 30 s).
- **Token**: reads `~/.openclaw/openclaw.json` inside WSL2, extracts `gateway.auth.token`, builds the dashboard URL as `http://127.0.0.1:18789/#token=<token>`, opens it in Microsoft Edge, and also copies the token to the clipboard.

### Monitor loop

While the launcher window is open, it polls every 5 seconds:
- Keeps a background WSL2 bash sleep loop running so WSL2 does not exit.
- Detects if the gateway goes down and automatically restarts it.

### Shutdown / cleanup

The launcher only stops services **it started**. If the gateway was already running before you launched it, it is left running when you close the launcher.

- Closing the console window sends `SIGTERM` to any services the launcher started inside WSL2.
- Ctrl+C inside the console stops the monitor loop and triggers cleanup inline.

---

## Configuration

Edit the constants at the top of `LaunchOpenClawWSL.py`:

| Constant | Default | Description |
|---|---|---|
| `WSL_DISTRO` | `"Ubuntu"` | Name of your WSL2 distro |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/tags` | Ollama health check endpoint |
| `DASHBOARD_URL` | `http://127.0.0.1:18789/` | OpenClaw gateway URL |
| `OLLAMA_EXE` | `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` | Path to Ollama executable |
| `WSL_SERVICE_NAME` | `openclaw-gateway.service` | systemd service name in WSL2 |
| `MONITOR_POLL_SECONDS` | `5` | How often the monitor checks gateway health |

---

## Compiling the exe yourself

Requires [PyInstaller](https://pyinstaller.org):

```text
pip install pyinstaller
pyinstaller --onefile --console --name LaunchOpenClawWSL LaunchOpenClawWSL.py
```

The compiled exe will be in `dist/LaunchOpenClawWSL.exe`.
