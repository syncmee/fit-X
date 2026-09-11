from app import app
from app.extensions import db


if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    # debug=True hardcoded here would expose the Werkzeug debugger on all
    # interfaces — let FLASK_ENV decide instead.
    app.run(host='0.0.0.0', port=5000, debug=app.config["DEBUG"])
