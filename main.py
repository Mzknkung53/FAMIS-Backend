from services.routes import create_app
import logging


app = create_app()

# Reduce Flask/Werkzeug logging noise from polling requests
# Only show warnings and errors, not every request
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)


if __name__ == "__main__":
    # Enable debug and reloader for hot-reload in development
    # app.run(debug=False, port=5000, use_reloader=False)
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=True)


