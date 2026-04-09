"""Entry point for the service window: python3 -m service_window"""

from service_window.app import create_app

app = create_app()
app.run(host="0.0.0.0", port=app.config.get("SERVICE_PORT", 8080))
