from flask import Flask
from .config import Config
from .extensions import db, jwt, migrate, limiter, cors
from .routes import auth, files_bp, book_bp, subscriber_bp, ask_bp, main_bp
from flask_cors import CORS
# import os
# import sys

# def resource_path(relative_path):
#     """Get absolute path to resource, works for dev and for PyInstaller exe"""
#     if hasattr(sys, '_MEIPASS'):
#         return os.path.join(sys._MEIPASS, relative_path)
#     return os.path.join(os.path.abspath("."), relative_path)

def create_app():
    app = Flask(__name__, static_folder='../static', static_url_path='')
    app.config.from_object(Config)

    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    cors.init_app(app)
    CORS(app)
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    app.register_blueprint(auth, url_prefix='/auth')
    app.register_blueprint(book_bp, url_prefix='/book')
    app.register_blueprint(files_bp, url_prefix='/files')
    app.register_blueprint(subscriber_bp, url_prefix="/subscribe")
    app.register_blueprint(ask_bp, url_prefix="/ask")
    app.register_blueprint(main_bp, url_prefix="")

    return app
