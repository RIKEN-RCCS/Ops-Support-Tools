"""Gunicorn configuration for one isolated webhook role per container."""
import fcntl
import os
from pathlib import Path
import socket
import stat
import threading

workers = 1
worker_class = "gthread"
threads = 4
timeout = 40
graceful_timeout = 25
keepalive = 2
umask = 0o117
accesslog = None
errorlog = "-"
preload_app = False
control_socket_disable = True
role = os.environ["SUPPORT_AI_WEBHOOK_ROLE"]
bind = "unix:/ipc/queue.sock" if role == "queue" else "unix:/edge/backend.sock"


def on_starting(server):
    """Keep the owner lock in the master, including during worker replacement."""
    path = Path(bind.removeprefix("unix:"))
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    server.queue_owner_fd = fd
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("invalid socket lock")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if info.st_uid != os.geteuid() or not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("unexpected socket object")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise RuntimeError("socket already serving")


def post_worker_init(worker):
    if role != "queue":
        return
    import common
    from webhook_roles import monitor_snapshot_loop

    common.ensure_spool_dirs()
    # Only this master may own this private snapshot directory.
    for path in Path("/monitor").glob(".monitor-*"):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("unexpected monitor partial object")
        path.unlink()
    worker.snapshot_stop = threading.Event()
    worker.snapshot_thread = threading.Thread(
        target=monitor_snapshot_loop,
        args=(common.QUEUE_DB, "/monitor/queue.sqlite", worker.snapshot_stop),
        name="monitor-snapshot",
    )
    worker.snapshot_thread.start()


def worker_exit(server, worker):
    if hasattr(worker, "snapshot_stop"):
        worker.snapshot_stop.set()
        worker.snapshot_thread.join(timeout=22)
        if worker.snapshot_thread.is_alive():
            server.log.error("monitor_snapshot_shutdown_timeout")


def on_exit(server):
    if hasattr(server, "queue_owner_fd"):
        os.close(server.queue_owner_fd)
