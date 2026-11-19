"""SSH connections (Paramiko): key login, password login, jump hosts, key setup.

Host keys are trusted on first use and stored in HOTWire's own known_hosts
file. If a device's key changes (e.g. the SystemCore was reimaged) the
connection is refused until the user clicks "Forget host key".
"""

from __future__ import annotations

import shlex
import socket
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from hotwire.settings import Device, Settings, data_dir

CONNECT_TIMEOUT = 4.0


class SSHError(Exception):
    pass


# --------------------------------------------------------------- host keys
def known_hosts_path() -> Path:
    return data_dir() / "known_hosts"


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        p = known_hosts_path()
        p.touch(exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")


def forget_host_key(host: str) -> int:
    """Remove all stored keys for host. Returns how many lines were removed."""
    p = known_hosts_path()
    if not p.exists():
        return 0
    lines = p.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if not (ln.split(" ", 1)[0] in (host, f"[{host}]:22"))]
    p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return len(lines) - len(keep)


def _new_client() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    p = known_hosts_path()
    if p.exists():
        c.load_host_keys(str(p))
    c.set_missing_host_key_policy(_TrustOnFirstUse())
    return c


# --------------------------------------------------------------- keys
def ensure_key(key_path: str) -> tuple[Path, str]:
    """Make sure an ed25519 key exists at key_path. Returns (path, public key line)."""
    priv = Path(key_path).expanduser()
    pub = priv.with_name(priv.name + ".pub")
    if not priv.exists():
        priv.parent.mkdir(parents=True, exist_ok=True)
        key = ed25519.Ed25519PrivateKey.generate()
        priv.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
        )
        line = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        pub.write_text(line.decode() + f" hotwire@{socket.gethostname()}\n", encoding="utf-8")
    if not pub.exists():
        k = paramiko.Ed25519Key.from_private_key_file(str(priv))
        pub.write_text(f"{k.get_name()} {k.get_base64()} hotwire@{socket.gethostname()}\n", encoding="utf-8")
    return priv, pub.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------- connect
class Connection:
    """An SSH client plus any jump connections it depends on."""

    def __init__(self, client: paramiko.SSHClient, host: str, jumps: list["Connection"]):
        self.client = client
        self.host = host
        self._jumps = jumps
        self._sftp: paramiko.SFTPClient | None = None

    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def run(self, cmd: str, timeout: float = 20.0) -> tuple[int, str, str]:
        _stdin, out, err = self.client.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        return out.channel.recv_exit_status(), o, e

    def open_tcp(self, host: str, port: int):
        """Open a TCP channel *from this device* to host:port (for jumps/streams)."""
        t = self.client.get_transport()
        if t is None:
            raise SSHError("SSH transport closed")
        return t.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=CONNECT_TIMEOUT)

    def close(self) -> None:
        try:
            if self._sftp:
                self._sftp.close()
            self.client.close()
        finally:
            for j in self._jumps:
                j.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(
    device: Device,
    settings: Settings,
    password: str | None = None,
    _depth: int = 0,
) -> Connection:
    """Try each of the device's hosts in order. Raises SSHError with all reasons."""
    if _depth > 2:
        raise SSHError("Too many jump hops (check 'via' settings for a loop)")
    errors = []
    key_file = Path(settings.key_path).expanduser()
    for h in device.hosts:
        jumps: list[Connection] = []
        try:
            sock = None
            if h.via:
                via_dev = settings.device(h.via)
                if via_dev is None:
                    raise SSHError(f"via device '{h.via}' not found")
                jump = connect(via_dev, settings, None, _depth + 1)
                jumps.append(jump)
                sock = jump.open_tcp(h.host, 22)
            c = _new_client()
            c.connect(
                h.host,
                username=device.user,
                password=password,
                key_filename=str(key_file) if key_file.exists() and password is None else None,
                look_for_keys=False,
                allow_agent=False,
                timeout=CONNECT_TIMEOUT,
                banner_timeout=CONNECT_TIMEOUT,
                auth_timeout=CONNECT_TIMEOUT,
                sock=sock,
            )
            return Connection(c, h.host + (f" via {h.via}" if h.via else ""), jumps)
        except paramiko.BadHostKeyException:
            for j in jumps:
                j.close()
            raise SSHError(
                f"{device.name} at {h.host}: the device's host key CHANGED. If it was reimaged, "
                "use Settings > Forget host key, then try again."
            )
        except Exception as e:  # try next host
            for j in jumps:
                j.close()
            errors.append(f"{h.host}{' via ' + h.via if h.via else ''}: {type(e).__name__}: {e}")
    raise SSHError(f"Could not reach {device.name}. " + " | ".join(errors or ["no hosts configured"]))


def tcp_reachable(host: str, port: int = 22, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------- key setup
def install_key(device: Device, settings: Settings, password: str) -> str:
    """Log in once with the password and append our public key. Password is not stored."""
    _priv, pub = ensure_key(settings.key_path)
    q = shlex.quote(pub)
    cmd = (
        "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys) && echo HOTWIRE_OK"
    )
    with connect(device, settings, password=password) as conn:
        rc, out, err = conn.run(cmd)
    if rc != 0 or "HOTWIRE_OK" not in out:
        raise SSHError(f"Key install failed (rc={rc}): {err.strip() or out.strip()}")
    return test_key(device, settings)


def test_key(device: Device, settings: Settings) -> str:
    with connect(device, settings) as conn:
        rc, out, _ = conn.run("echo $(whoami)@$(hostname)")
        return f"Key login OK: {out.strip()} ({conn.host})"
