import os
from dotenv import load_dotenv
import base64

load_dotenv()

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')
    JWT_ACCESS_TOKEN_EXPIRES = 7 * 24 * 60 * 60  # 7 days
    FILE_ENCRYPTION_KEY = base64.b64decode(os.environ.get('FILE_ENCRYPTION_KEY'))
    AI_API_KEY = os.environ.get('AI_API_KEY')

    # Determine storage path
    if os.getenv('VERCEL'):
        BASE_TEMP = '/tmp/uploads'
    else:
        BASE_TEMP = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'uploads')

    FILE_UPLOAD_FOLDER = os.path.join(BASE_TEMP, 'files')
    IMAGE_UPLOAD_FOLDER = os.path.join(BASE_TEMP, 'cover_images')
    JSON_UPLOAD_FOLDER = os.path.join(BASE_TEMP, 'vectors_json')
    FAISS_UPLOAD_FOLDER = os.path.join(BASE_TEMP, 'vectors_faiss')
    TEMP_UPLOAD_FOLDER = os.path.join(BASE_TEMP, 'temp')

    ALLOWED_EXTENSIONS = {'epub', 'jpg', 'jpeg', 'png'}

    # Ensure folders exist
    os.makedirs(FILE_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(JSON_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(FAISS_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)
