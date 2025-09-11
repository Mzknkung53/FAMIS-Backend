from services.routes import create_app


app = create_app()


if __name__ == "__main__":
    # Enable debug and reloader for hot-reload in development
    # app.run(debug=False, port=5000, use_reloader=False)
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=True)


