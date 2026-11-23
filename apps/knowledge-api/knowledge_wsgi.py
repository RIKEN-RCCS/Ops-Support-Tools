"""Separate writable API and read-only view entry points."""
import os
import fcntl
from pathlib import Path
import threading
import time
import app

bind = '0.0.0.0:8080'
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 60
graceful_timeout = 30
accesslog = None
errorlog = '-'
control_socket_disable = True
umask = 0o007


def application():
    import field_crypto
    field_crypto.load_key()
    app._init_db()
    destination = os.environ.get('KNOWLEDGE_VIEW_DB')
    if destination and not app.READONLY:
        import field_crypto
        from view_snapshot import publish
        directory = Path(destination).parent
        directory.mkdir(parents=True, exist_ok=True)
        lock = os.fdopen(os.open(directory / ".publisher.lock",
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a+b")
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("view publisher already running") from None
        app.app.extensions["knowledge_view_lock"] = lock
        def update():
            publish(app.DB_PATH, destination, field_crypto.load_key(),
                    field_crypto._decode_key(field_crypto.env_secret('KNOWLEDGE_VIEW_KEY')))
        update()
        def loop():
            while True:
                time.sleep(30)
                try:update()
                except Exception as error:app.app.logger.error('view_refresh_failed type=%s',type(error).__name__)
        threading.Thread(target=loop,daemon=True).start()
    return app.app
