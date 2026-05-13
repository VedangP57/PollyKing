import socket
import threading
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import notifier


def _serve_one(sock_path: str):
    """Start a Unix server, accept one connection, read token, return (received_list, thread)."""
    received = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(2.0)

    def _accept():
        try:
            conn, _ = srv.accept()
            data = conn.recv(64)
            received.append(data.decode())
            conn.close()
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    return received, t


def test_notify_sends_gap_token():
    # macOS AF_UNIX path limit is 104 chars — use /tmp directly
    sock_path = "/tmp/pk_test_gap.sock"
    Path(sock_path).unlink(missing_ok=True)
    received, t = _serve_one(sock_path)

    original = notifier._SOCK_PATH
    notifier._SOCK_PATH = sock_path
    try:
        notifier._notify("gap")
        t.join(timeout=2.0)
        assert received == ["gap\n"]
    finally:
        notifier._SOCK_PATH = original
        Path(sock_path).unlink(missing_ok=True)


def test_notify_silent_on_missing_socket():
    original = notifier._SOCK_PATH
    notifier._SOCK_PATH = "/tmp/does_not_exist_polyking_test.sock"
    try:
        notifier._notify("gap")  # must not raise
    finally:
        notifier._SOCK_PATH = original


def test_notify_sends_trade_token():
    sock_path = "/tmp/pk_test_trade.sock"
    Path(sock_path).unlink(missing_ok=True)
    received, t = _serve_one(sock_path)

    original = notifier._SOCK_PATH
    notifier._SOCK_PATH = sock_path
    try:
        notifier._notify("trade")
        t.join(timeout=2.0)
        assert received == ["trade\n"]
    finally:
        notifier._SOCK_PATH = original
        Path(sock_path).unlink(missing_ok=True)
