import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def get_free_port(start_port: int = 8501, max_attempts: int = 10) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start_port}-{start_port + max_attempts - 1}")


def generate_self_signed_cert() -> tuple[str, str]:
    cert_dir = Path(__file__).resolve().parent / "certs"
    cert_dir.mkdir(exist_ok=True)

    cert_path = cert_dir / "localhost.crt"
    key_path = cert_dir / "localhost.key"

    if not cert_path.exists() or not key_path.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "365",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
        )

    return str(cert_path), str(key_path)


def get_python_executable() -> str:
    project_dir = Path(__file__).resolve().parent
    project_environment_executable = project_dir / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if project_environment_executable.is_file():
        return str(project_environment_executable)
    if sys.prefix == sys.base_prefix:
        return sys.executable
    executable = Path(sys.prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise RuntimeError(f"Python environment executable was not found: {executable}")
    return str(executable)


def main() -> None:
    project_dir = Path(__file__).resolve().parent
    python_executable = get_python_executable()
    print(f"Using Python environment: {python_executable}", flush=True)
    enable_https = os.getenv("ENABLE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}
    preferred_port = int(os.getenv("STREAMLIT_PORT", "8501"))
    port = preferred_port
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", preferred_port))
    except OSError:
        port = get_free_port(preferred_port)

    backup_process = None

    # Start the scheduler daemon in the background
    print("Starting research scheduler daemon...")
    log_scheduler_to_stdout = os.getenv("FINBOT_LOG_TO_STDOUT", "false").lower() in {"1", "true", "yes", "on"}
    scheduler_log = None if log_scheduler_to_stdout else (project_dir / "scheduler.log").open("a", buffering=1)
    try:
        scheduler_process = subprocess.Popen(
            [python_executable, "makemoney.py"],
            cwd=project_dir,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        if scheduler_log is not None:
            scheduler_log.close()
        raise
    log_destination = "container stdout" if log_scheduler_to_stdout else "scheduler.log"
    print(f"Scheduler daemon started (PID: {scheduler_process.pid}); logging to {log_destination}")

    if os.getenv("FINBOT_S3_BACKUP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        try:
            backup_process = subprocess.Popen(
                [python_executable, "s3_backup.py", "daemon"],
                cwd=project_dir,
                stdout=scheduler_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception:
            scheduler_process.terminate()
            scheduler_process.wait(timeout=5)
            if scheduler_log is not None:
                scheduler_log.close()
            raise
        print(f"S3 backup daemon started (PID: {backup_process.pid}); logging to {log_destination}")

    try:
        cmd = [
            python_executable,
            "-m",
            "streamlit",
            "run",
            "streamlit_app.py",
            "--server.address",
            "0.0.0.0",
            "--server.port",
            str(port),
        ]

        if enable_https:
            cert_path, key_path = generate_self_signed_cert()
            cmd.extend(["--server.sslCertFile", cert_path, "--server.sslKeyFile", key_path])

        print(f"Starting Streamlit on port {port}...")
        frontend = subprocess.Popen(cmd)
        try:
            while frontend.poll() is None:
                if scheduler_process.poll() is not None:
                    raise RuntimeError("Scheduler exited; restarting the workload is required.")
                if backup_process is not None and backup_process.poll() is not None:
                    raise RuntimeError("S3 backup daemon exited; restarting the workload is required.")
                time.sleep(2)
            if frontend.returncode:
                raise RuntimeError(f"Streamlit exited with status {frontend.returncode}")
        finally:
            if frontend.poll() is None:
                frontend.terminate()
                try:
                    frontend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    frontend.kill()
                    frontend.wait()
    finally:
        # Cleanup scheduler when Streamlit exits
        if scheduler_process.poll() is None:
            print("Stopping scheduler daemon...")
            scheduler_process.terminate()
            try:
                scheduler_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                scheduler_process.kill()
                scheduler_process.wait()
        # The backup daemon receives SIGTERM after the writer stops and creates
        # one final consistent snapshot before the container exits.
        if backup_process is not None and backup_process.poll() is None:
            print("Stopping S3 backup daemon and creating a final snapshot...")
            backup_process.terminate()
            try:
                backup_process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                backup_process.kill()
                backup_process.wait()
        if scheduler_log is not None:
            scheduler_log.close()


if __name__ == "__main__":
    main()
