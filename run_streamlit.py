import os
import socket
import subprocess
import sys
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


def main() -> None:
    enable_https = os.getenv("ENABLE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}
    preferred_port = int(os.getenv("STREAMLIT_PORT", "8501"))
    port = preferred_port
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", preferred_port))
    except OSError:
        port = get_free_port(preferred_port)

    cmd = [
        sys.executable,
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

    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
