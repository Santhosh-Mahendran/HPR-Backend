from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS

db = SQLAlchemy()
jwt = JWTManager()
migrate = Migrate()
limiter = Limiter(key_func=get_remote_address)
cors = CORS()
