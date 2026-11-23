"""Production WSGI entry point; all deployment values remain environment driven."""
import threading
import relay

bind = '0.0.0.0:8080'
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 45
graceful_timeout = 30
keepalive = 5
preload_app = False
accesslog = None  # nginx handles access accounting; avoid duplicating query strings.
errorlog = '-'
capture_output = True


def application():
    config = relay.Config.from_env()
    config.validate()
    app = relay.build_app(config)
    threading.Thread(target=relay.periodic_refresh, args=(config,), daemon=True).start()
    return app
