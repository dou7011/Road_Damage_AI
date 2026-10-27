import logging

from road_damage import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=app.config["SERVER_PORT"], debug=False, use_reloader=False)
