from app import app
from app.extensions import db


if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    app.run(debug=app.config["DEBUG"])
