from flask import Blueprint, request, jsonify, current_app, send_file, Response, abort,  send_from_directory
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, decode_token
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from .models import Publisher, Category, Book, Reader, Highlight, Note, BooksPurchased, Cart, Wishlist, Subscriber, BooksSubscribed
from .extensions import db, limiter
from datetime import datetime
import os
import zipfile
from lxml import etree

from .utils.ai_utils import ask_openrouter
from .utils.encryption import decrypt_file, encrypt_file
from .utils.epub_utils import process_and_store_vectors, process_and_store_vectors2
from .utils.faiss_utils import load_chunks, load_index, search_index, load_chunks2

ph = PasswordHasher()
auth = Blueprint('auth', __name__)
book_bp = Blueprint('book', __name__)
files_bp = Blueprint('files', __name__)
subscriber_bp = Blueprint('subscribe', __name__)

ask_bp = Blueprint('ask', __name__)
main_bp = Blueprint('main', __name__)

# @main_bp.route("/")
# def home():
#     return send_from_directory(current_app.static_folder, 'index.html')
#
# # Catch-all to let React Router handle routing
# @main_bp.route('/<path:path>')
# def catch_all(path):
#     try:
#         return send_from_directory(current_app.static_folder, path)
#     except:
#         return send_from_directory(current_app.static_folder, 'index.html')


@auth.route('/pub/register', methods=['POST'])
def pub_register():
    data = request.json
    required_fields = ["name", "email", "password", "phone", 'geo_location', 'address']

    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    if Publisher.query.filter_by(email=data['email']).first():
        return jsonify({"error": "Email already registered"}), 400

    hashed_password = ph.hash(data['password'])

    new_publisher = Publisher(
        name=data['name'],
        email=data['email'],
        password=hashed_password,
        phone=data.get('phone'),
        geo_location=data.get('geo_location'),
        address=data.get('address'),
        is_institution =data.get('is_institution')
    )
    db.session.add(new_publisher)
    db.session.commit()

    return jsonify({"message": "Registration successful"}), 201


@auth.route('/pub/login', methods=['POST'])
def login():
    data = request.json
    publisher = Publisher.query.filter_by(email=data['email']).first()

    if not publisher:
        return jsonify({"error": "Invalid email or password"}), 401

    try:
        if ph.verify(publisher.password, data['password']):
            access_token = create_access_token(identity=str(publisher.publisher_id))
            return jsonify({"access_token": access_token, "is_institution": publisher.is_institution, "message": "Login successful"}), 200
    except VerifyMismatchError:
        return jsonify({"error": "Invalid email or password"}), 401


@book_bp.route('/pub/add_category', methods=['POST'])
@jwt_required()
def add_category():
    data = request.json
    required_fields = ['category_name', 'description']

    # Check if all required fields are present
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    # Get publisher_id from JWT token
    publisher_id = get_jwt_identity()

    # Check if the publisher exists
    publisher = Publisher.query.get(publisher_id)
    if not publisher:
        return jsonify({"error": "Publisher not found"}), 404

    # Create new category
    new_category = Category(
        publisher_id=publisher_id,
        category_name=data['category_name'],
        description=data.get('description'),
        created_at=datetime.utcnow(),
        updated_time=datetime.utcnow()
    )

    # Add category to database
    db.session.add(new_category)
    db.session.commit()

    return jsonify({"message": "Category added successfully", "category": data['category_name']}), 201

@book_bp.route('/pub/get_categories', methods=['GET'])
@jwt_required()
def get_categories():
    publisher_id = get_jwt_identity()
    publisher = Publisher.query.get(publisher_id)
    if not publisher:
        return jsonify({"error": "Publisher not found"}), 404

    categories = Category.query.filter_by(publisher_id=publisher_id).all()

    return jsonify({
        "categories": [
            {
                "category_id": category.category_id,
                "category_name": category.category_name,
                "description": category.description,
                "created_at": category.created_at,
                "updated_time": category.updated_time
            }
            for category in categories
        ]
    }), 200


@book_bp.route('/pub/delete_category/<int:category_id>', methods=['DELETE'])
@jwt_required()
def delete_category(category_id):
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Check if category exists and belongs to the publisher
        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()

        if not category:
            return jsonify({"error": "Category not found or access denied"}), 404

        # Check if any books are associated with the category
        associated_books = Book.query.filter_by(category_id=category_id).first()
        if associated_books:
            return jsonify({"error": "Cannot delete category with associated books"}), 400

        # Delete category
        db.session.delete(category)
        db.session.commit()

        return jsonify({"message": "Category deleted successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# Helper function to check allowed file extensions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']


def extract_cover(epub_path, book_id):
    try:
        with zipfile.ZipFile(epub_path, 'r') as epub:
            # Step 1: Find content.opf using container.xml
            container_path = 'META-INF/container.xml'
            with epub.open(container_path) as container_file:
                container_xml = etree.parse(container_file)
                opf_path = container_xml.xpath("//*[local-name()='rootfile']/@full-path")[0]

            # Step 2: Parse content.opf to get the cover image
            with epub.open(opf_path) as opf_file:
                opf_xml = etree.parse(opf_file)

                # Search for cover image using 'cover' ID or properties="cover-image"
                cover_item = opf_xml.xpath("//*[local-name()='item'][@id='cover' or @properties='cover-image']")

                if not cover_item:
                    raise Exception('Cover image not found.')

                cover_href = cover_item[0].get('href')

                # Ensure correct relative path
                cover_path = os.path.join(os.path.dirname(opf_path), cover_href).replace("\\", "/")

                # Step 3: Extract cover image
                with epub.open(cover_path) as cover_file:
                    cover_ext = os.path.splitext(cover_href)[1]
                    cover_image_filename = f"{book_id}{cover_ext}"
                    full_cover_image_path = os.path.join(current_app.config['IMAGE_UPLOAD_FOLDER'],
                                                         cover_image_filename)

                    with open(full_cover_image_path, 'wb') as out_file:
                        out_file.write(cover_file.read())

                    return cover_image_filename
    except Exception as e:
        print(f"Cover extraction failed: {e}")
        return None



# Helper function to check allowed image file extensions
def allowed_image(filename):
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


@book_bp.route('/pub/get_books_by_cat/<int:category_id>', methods=['GET'])
@jwt_required()
def get_books_by_cat(category_id):
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Verify category belongs to the publisher
        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Invalid category ID"}), 404

        # Get all books for the given category
        books = Book.query.filter_by(category_id=category_id).all()

        # Serialize book data
        books_list = []
        for book in books:
            books_list.append({
                "book_id": book.book_id,
                "title": book.title,
                "author": book.author,
                "isbn": book.isbn,
                "language": book.language,
                "cover_image": book.cover_image,
                "epub_file": book.epub_file,
                "genre": book.genre,
                "e_book_type": book.e_book_type,
                "price": str(book.price),
                "rental_price": str(book.rental_price),
                "offer_price": str(book.offer_price),
                "description": book.description,
                "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "updated_at": book.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
                "status": book.status
            })

        return jsonify({
            "category_id": category_id,
            "category_name": category.category_name,
            "books": books_list
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@book_bp.route('/pub/get_book/<int:book_id>', methods=['GET'])
@jwt_required()
def get_book(book_id):
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Fetch the book details ensuring it belongs to the publisher
        book = Book.query.filter_by(book_id=book_id, publisher_id=publisher_id).first()

        if not book:
            return jsonify({"error": "Book not found"}), 404

        # Serialize book data
        book_details = {
            "book_id": book.book_id,
            "title": book.title,
            "author": book.author,
            "isbn": book.isbn,
            "language": book.language,
            "genre": book.genre,
            "cover_image": book.cover_image,
            "e_book_type": book.e_book_type,
            "price": str(book.price),
            "rental_price": str(book.rental_price),
            "offer_price": str(book.offer_price),
            "description": book.description,
            "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updated_at": book.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
            "epub_file": book.epub_file,
            "category_id": book.category_id,
            "status": book.status
        }
        return jsonify(book_details), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@book_bp.route('/pub/get_all_books', methods=['GET'])
@jwt_required()
def get_books():
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Fetch all books belonging to the publisher
        books = Book.query.filter_by(publisher_id=publisher_id).all()

        if not books:
            return jsonify({"error": "No books found for this publisher"}), 404

        # Serialize book data
        books_details = []
        for book in books:
            book_details = {
            "book_id": book.book_id,
            "title": book.title,
            "author": book.author,
            "isbn": book.isbn,
            "language": book.language,
            "cover_image": book.cover_image,
            "epub_file": book.epub_file,
            "genre": book.genre,
            "e_book_type": book.e_book_type,
            "price": str(book.price),
            "rental_price": str(book.rental_price),
            "offer_price": str(book.offer_price),
            "description": book.description,
            "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updated_at": book.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
            "category_id": book.category_id,
            "status": book.status
            }

            books_details.append(book_details)

        return jsonify(books_details), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@files_bp.route('/pub/details', methods=['GET'])
@jwt_required()
def get_Pub_details():
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        book_count = Book.query.filter_by(publisher_id=publisher_id).count()

        publisher_books = Book.query.with_entities(Book.book_id).filter_by(publisher_id=publisher_id).all()
        publisher_book_ids = [book_id for (book_id,) in publisher_books]

        if not publisher_book_ids:
            return jsonify({"message": "No books published yet", "purchase_count": 0}), 200

        # Count how many times these books were purchased
        purchase_count = BooksPurchased.query.filter(
            BooksPurchased.book_id.in_(publisher_book_ids)
        ).count()

        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        return jsonify({"Book_published": book_count , "purchased_book_count" :purchase_count }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@book_bp.route('/pub/delete_book/<int:book_id>', methods=['DELETE'])
@jwt_required()
def delete_book(book_id):
    try:
        # Get publisher_id from JWT
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Check if book exists and belongs to the publisher
        book = Book.query.filter_by(book_id=book_id, publisher_id=publisher_id).first()

        if not book:
            return jsonify({"error": "Book not found or access denied"}), 404

        # Delete associated files from database and file system
        for file in book.files:
            # Delete the file from the file system
            if os.path.exists(file.file_path):
                os.remove(file.file_path)

            # Delete the file record from the database
            db.session.delete(file)

        # Delete book
        db.session.delete(book)
        db.session.commit()

        return jsonify({"message": "Book and associated files deleted successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@files_bp.route('/pub/update_book/<int:book_id>', methods=['PUT'])
@jwt_required()
def update_book(book_id):
    try:
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        book = Book.query.filter_by(book_id=book_id, publisher_id=publisher_id).first()
        if not book:
            return jsonify({"error": "Book not found"}), 404

        title = request.form.get('title', book.title)
        author = request.form.get('author', book.author)
        isbn = request.form.get('isbn', book.isbn)
        category_id = request.form.get('category_id', book.category_id)
        language = request.form.get('language', book.language)
        genre = request.form.get('genre', book.genre)
        e_book_type = request.form.get('e_book_type', book.e_book_type)
        price = request.form.get('price', book.price)
        rental_price = request.form.get('rental_price', book.rental_price)
        description = request.form.get('description', book.description)
        offer_price = request.form.get('offer_price', book.offer_price)
        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Invalid category ID"}), 400

        file_upload_folder = current_app.config['FILE_UPLOAD_FOLDER']
        image_upload_folder = current_app.config['IMAGE_UPLOAD_FOLDER']

        if 'file' in request.files:
            file = request.files['file']
            if file.filename:
                if allowed_file(file.filename):
                    file_ext = os.path.splitext(file.filename)[1]  # Get file extension
                    epub_filename = f"{book.book_id}{file_ext}"
                    full_file_path = os.path.join(file_upload_folder, epub_filename)
                    file.save(full_file_path)

                    book.epub_file = epub_filename  # Store updated file name

                else:
                    return jsonify({"error": "Invalid file type"}), 400

        if 'cover_image' in request.files:
            cover_image = request.files['cover_image']
            if cover_image.filename:
                if allowed_file(cover_image.filename):
                    cover_ext = os.path.splitext(cover_image.filename)[1]
                    cover_image_filename = f"{book.book_id}{cover_ext}"
                    full_cover_image_path = os.path.join(image_upload_folder, cover_image_filename)

                    cover_image.save(full_cover_image_path)
                    book.cover_image = cover_image_filename  # Store only filename

                else:
                    return jsonify({"error": "Invalid cover image type"}), 400

        # Check if book title already exists
        existing_book = Book.query.filter_by(title=title).first()
        book_status = 'pending' if existing_book else 'live'

        book.title = title
        book.author = author
        book.isbn = isbn
        book.category_id = category_id
        book.language = language
        book.genre = genre
        book.e_book_type = e_book_type
        book.price = price
        book.rental_price = rental_price
        book.description = description
        book.offer_price = offer_price
        book.status = book_status

        db.session.commit()

        return jsonify({
            "message": "Book updated successfully"
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@subscriber_bp.route('/publisher/add_subscriber', methods=['POST'])
@jwt_required()
def add_subscriber():
    try:
        data = request.json
        publisher_id = get_jwt_identity()  # Publisher ID from JWT Token

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404
        category_id = data.get('category_id')
        reader_email = data.get('reader_email')

        if not category_id or not reader_email:
            return jsonify({"error": "Category ID and Reader Email are required"}), 400

        # Check if the category exists and belongs to the publisher
        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Category not found or unauthorized"}), 404

        # Check if subscriber already exists
        existing_subscriber = Subscriber.query.filter_by(category_id=category_id, reader_email=reader_email).first()
        if existing_subscriber:
            return jsonify({"error": "Subscriber already added to this category"}), 400

        # Add subscriber
        new_subscriber = Subscriber(category_id=category_id, reader_email=reader_email, publisher_id=publisher_id)
        db.session.add(new_subscriber)
        db.session.commit()

        return jsonify({"message": "Subscriber added successfully"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/publisher/category/<int:category_id>/readers', methods=['GET'])
@jwt_required()
def get_readers_in_category(category_id):
    try:
        # Get the publisher's ID from JWT Token
        publisher_id = get_jwt_identity()

        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Check if the category exists and belongs to the publisher
        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Category not found or unauthorized"}), 404

        # Get all subscribers in this category
        subscribers = Subscriber.query.filter_by(category_id=category_id).all()

        if not subscribers:
            return jsonify({"message": "No readers found in this category"}), 404

        # Return list of readers' emails
        readers_list = [{"sub_id": sub.sub_id, "reader_email": sub.reader_email} for sub in subscribers]

        return jsonify({"category_id": category_id, "readers": readers_list})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@auth.route('/reader/register', methods=['POST'])
@limiter.limit("5 per minute")  # Rate limit
def reader_register():
    data = request.json
    required_fields = ["name", "email", "password", "phone", 'geo_location', 'address']

    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    if Reader.query.filter_by(email=data['email']).first():
        return jsonify({"error": "Email already registered"}), 400

    hashed_password = ph.hash(data['password'])

    new_reader = Reader(
        name=data['name'],
        email=data['email'],
        password=hashed_password,
        phone=data.get('phone'),
        geo_location=data.get('geo_location'),
        address=data.get('address')
    )
    db.session.add(new_reader)
    db.session.commit()

    return jsonify({"message": "Registration successful"}), 201


@auth.route('/reader/login', methods=['POST'])
@limiter.limit("10 per minute")
def reader_login():
    data = request.json
    reader = Reader.query.filter_by(email=data['email']).first()

    if not reader:
        return jsonify({"error": "Invalid email or password"}), 401

    try:
        if ph.verify(reader.password, data['password']):
            access_token = create_access_token(identity=str(reader.reader_id))

            # Check subscription status
            subscription_exists = db.session.query(
                db.session.query(Subscriber).filter_by(reader_email=reader.email).exists()
            ).scalar()

            return jsonify({
                "access_token": access_token,
                "message": "Login successful",
                "has_subscription": subscription_exists
            }), 200
    except VerifyMismatchError:
        return jsonify({"error": "Invalid email or password"}), 401



@subscriber_bp.route('/publisher/edit_subscriber/<int:sub_id>', methods=['PUT'])
@jwt_required()
def edit_subscriber(sub_id):
    try:
        data = request.json
        publisher_id = get_jwt_identity()
        new_email = data.get('reader_email')

        if not new_email:
            return jsonify({"error": "New email is required"}), 400

        # Check if the subscriber exists and belongs to the publisher
        subscriber = Subscriber.query.join(Category).filter(
            Subscriber.sub_id == sub_id,
            Category.publisher_id == publisher_id
        ).first()

        if not subscriber:
            return jsonify({"error": "Subscriber not found or unauthorized"}), 404

        # Update email
        subscriber.reader_email = new_email
        db.session.commit()

        return jsonify({"message": "Subscriber updated successfully", "reader_email": new_email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@book_bp.route('/reader/get_all_books', methods=['GET'])
@jwt_required()
def get_all_books():
    try:
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        books = Book.query.join(Publisher).filter(
            Book.status == 'live',
            Publisher.is_institution == False
        ).all()
        # Get all wishlist book_ids for the reader
        wishlist_book_ids = {item.book_id for item in Wishlist.query.filter_by(reader_id=reader_id).all()}

        # Get all purchased book_ids for the reader
        purchased_book_ids = {item.book_id for item in BooksPurchased.query.filter_by(reader_id=reader_id).all()}

        # Prepare the response
        books_list = []
        for book in books:
            books_list.append({
                "book_id": book.book_id,
                "title": book.title,
                "author": book.author,
                "isbn": book.isbn,
                "language": book.language,
                "cover_image": book.cover_image,
                "epub_file": book.epub_file,
                "genre": book.genre,
                "e_book_type": book.e_book_type,
                "price": str(book.price),
                "rental_price": str(book.rental_price),
                "offer_price": str(book.offer_price),
                "description": book.description,
                "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "wishlist": book.book_id in wishlist_book_ids,
                "already_purchased": book.book_id in purchased_book_ids
            })

        return jsonify({"books": books_list}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@book_bp.route('/reader/get_book_by_genre/<string:genre>', methods=['GET'])
@jwt_required()
def get_book_by_genre(genre):
    try:
        # Get reader identity from JWT
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Query all 'live' books that match the genre
        books = Book.query.join(Publisher).filter(
            Book.status == 'live',
            Publisher.is_institution == False,
            Book.genre.ilike(f"%{genre}%")  # case-insensitive match
        ).all()

        if not books:
            return jsonify({"message": f"No books found for genre '{genre}'"}), 200

        # Get wishlist and purchased book IDs
        wishlist_book_ids = {item.book_id for item in Wishlist.query.filter_by(reader_id=reader_id).all()}
        purchased_book_ids = {item.book_id for item in BooksPurchased.query.filter_by(reader_id=reader_id).all()}

        # Prepare response list
        books_list = []
        for book in books:
            books_list.append({
                "book_id": book.book_id,
                "title": book.title,
                "author": book.author,
                "isbn": book.isbn,
                "language": book.language,
                "cover_image": book.cover_image,
                "epub_file": book.epub_file,
                "genre": book.genre,
                "e_book_type": book.e_book_type,
                "price": str(book.price),
                "rental_price": str(book.rental_price),
                "offer_price": str(book.offer_price) if book.offer_price else None,
                "description": book.description,
                "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                "wishlist": book.book_id in wishlist_book_ids,
                "already_purchased": book.book_id in purchased_book_ids
            })

        return jsonify({
            "genre": genre,
            "count": len(books_list),
            "books": books_list
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500




@book_bp.route('/reader/get_books_by_publisher/<int:publisher_id>', methods=['GET'])
@jwt_required()
def get_books_by_publisher_id(publisher_id):
    try:
        # Get the logged-in reader
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Fetch the publisher
        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Get wishlist and purchased book IDs
        wishlist_book_ids = {item.book_id for item in Wishlist.query.filter_by(reader_id=reader_id).all()}
        purchased_book_ids = {item.book_id for item in BooksPurchased.query.filter_by(reader_id=reader_id).all()}

        # Filter books for the specific publisher (only live books)
        books_data = []
        for book in publisher.books:
            if book.status == 'live':
                books_data.append({
                    "book_id": book.book_id,
                    "title": book.title,
                    "author": book.author,
                    "isbn": book.isbn,
                    "language": book.language,
                    "cover_image": book.cover_image,
                    "epub_file": book.epub_file,
                    "genre": book.genre,
                    "e_book_type": book.e_book_type,
                    "price": str(book.price),
                    "rental_price": str(book.rental_price),
                    "offer_price": str(book.offer_price),
                    "description": book.description,
                    "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    "wishlist": book.book_id in wishlist_book_ids,
                    "already_purchased": book.book_id in purchased_book_ids
                })

        return jsonify({
            "publisher_id": publisher.publisher_id,
            "publisher_name": publisher.name,
            "books": books_data
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@book_bp.route('/reader/add_highlight', methods=['POST'])
@jwt_required()
def add_highlight():
    data = request.json
    required_fields = ['book_id', 'text', 'highlight_range', 'color']

    # Check if all required fields are present
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    # Get reader_id from JWT token
    reader_id = get_jwt_identity()

    # Check if the reader exists
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    # Check if the book exists
    book = Book.query.get(data['book_id'])
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Create new highlight
    new_highlight = Highlight(
        reader_id=reader_id,
        book_id=data['book_id'],
        text=data['text'],
        highlight_range=data['highlight_range'],
        color=data['color']
    )

    # Add highlight to database
    db.session.add(new_highlight)
    db.session.commit()

    return jsonify({
        "message": "Highlight added successfully"
    }), 201

@book_bp.route('/reader/delete_highlight/<int:highlight_id>', methods=['DELETE'])
@jwt_required()
def delete_highlight(highlight_id):
    # Get reader_id from JWT token
    reader_id = get_jwt_identity()

    # Check if highlight exists
    highlight = Highlight.query.get(highlight_id)
    if not highlight:
        return jsonify({"error": "Highlight not found"}), 404

    # Make sure the highlight belongs to this reader
    if highlight.reader_id != reader_id:
        return jsonify({"error": "Unauthorized to delete this highlight"}), 403

    # Delete highlight
    db.session.delete(highlight)
    db.session.commit()

    return jsonify({"message": "Highlight deleted successfully"}), 200


@book_bp.route('/reader/get_highlights/<int:book_id>', methods=['GET'])
@jwt_required()
def get_highlights(book_id):
    try:
        # Get reader_id from JWT token
        reader_id = get_jwt_identity()

        # Check if the reader exists
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404



        # Check if the book exists
        book = Book.query.get(book_id)
        if not book:
            return jsonify({"error": "Book not found"}), 404

        # Fetch all highlights for the given reader_id and book_id
        highlights = Highlight.query.filter_by(reader_id=reader_id, book_id=book_id).all()

        # Serialize highlights data
        highlights_data = []
        for highlight in highlights:
            highlights_data.append({
                "id": highlight.hl_id,
                "text": highlight.text,
                "highlight_range": highlight.highlight_range,
                "color": highlight.color
            })

        return jsonify({
            "reader_id": reader_id,
            "book_id": book_id,
            "highlights": highlights_data
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@book_bp.route('/reader/add_note', methods=['POST'])
@jwt_required()
def add_note():
    data = request.json
    required_fields = ['book_id', 'text', 'note_range']

    # Check if all required fields are present
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    # Get reader_id from JWT token
    reader_id = get_jwt_identity()

    # Check if the reader exists
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    # Check if the book exists
    book = Book.query.get(data['book_id'])
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Create new note
    new_note = Note(
        reader_id=reader_id,
        book_id=data['book_id'],
        text=data['text'],
        note_range=data['note_range']
    )

    # Add note to database
    db.session.add(new_note)
    db.session.commit()

    return jsonify({
        "message": "Note added successfully"
    }), 201


@book_bp.route('/reader/delete_note/<int:note_id>', methods=['DELETE'])
@jwt_required()
def delete_note(note_id):
    # Get reader_id from JWT token
    reader_id = get_jwt_identity()

    # Check if the note exists
    note = Note.query.get(note_id)
    if not note:
        return jsonify({"error": "Note not found"}), 404

    # Verify the note belongs to the logged-in reader
    if note.reader_id != reader_id:
        return jsonify({"error": "Unauthorized to delete this note"}), 403

    # Delete note
    db.session.delete(note)
    db.session.commit()

    return jsonify({"message": "Note deleted successfully"}), 200



@book_bp.route('/reader/get_notes/<int:book_id>', methods=['GET'])
@jwt_required()
def get_notes(book_id):
    try:
        # Get reader_id from JWT token
        reader_id = get_jwt_identity()

        # Check if the reader exists
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Check if the book exists
        book = Book.query.get(book_id)
        if not book:
            return jsonify({"error": "Book not found"}), 404

        # Fetch all notes for the given reader_id and book_id
        notes = Note.query.filter_by(reader_id=reader_id, book_id=book_id).all()

        # Serialize notes data
        notes_data = []
        for note in notes:
            notes_data.append({
                "text": note.text,
                "note_range": note.note_range
            })

        return jsonify({
            "reader_id": reader_id,
            "book_id": book_id,
            "notes": notes_data
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@book_bp.route('/reader/purchase_book', methods=['POST'])
@jwt_required()
def purchase_book():
    data = request.json
    required_fields = ['book_id']

    # Check if all required fields are present
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required fields"}), 400

    # Get reader_id from JWT token (For now, assuming reader_id = 1)
    reader_id = get_jwt_identity()


    if reader_id != '2' and reader_id != '4' :
        print(reader_id)
        print(type(reader_id))
        return jsonify({
            "message": "Book purchased successfully"
        }), 201

    # Check if the reader exists
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    # Check if the book exists
    book = Book.query.get(data['book_id'])
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Check if the book is already purchased
    existing_purchase = BooksPurchased.query.filter_by(reader_id=reader_id, book_id=data['book_id'], bookmark='0').first()
    if existing_purchase:
        return jsonify({"error": "Book already purchased"}), 400

    # Create a new purchase record
    new_purchase = BooksPurchased(
        reader_id=reader_id,
        book_id=data['book_id'],
        bookmark=0  # Default bookmark at 0
    )

    # Add the purchase to the database
    db.session.add(new_purchase)
    db.session.commit()

    return jsonify({
        "message": "Book purchased successfully"
    }), 201

@book_bp.route('/reader/get_purchased_books', methods=['GET'])
@jwt_required()
def get_purchased_books():
    try:
        # Get reader_id from JWT token
        reader_id = get_jwt_identity()

        # Check if the reader exists
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Fetch all purchased books for the given reader_id
        purchased_books = BooksPurchased.query.filter_by(reader_id=reader_id).all()

        # Serialize purchased books data
        books_data = []
        for purchase in purchased_books:
            book = Book.query.get(purchase.book_id)
            if book:
                books_data.append({
                    "book_id": book.book_id,
                    "title": book.title,
                    "author": book.author,
                    "isbn": book.isbn,
                    "cover_image": book.cover_image,
                    "file_path": book.epub_file,
                    "purchase_date": purchase.purchase_date,
                    "isAiAdded" : book.has_ai_module,
                    "bookmark": purchase.bookmark,
                    "percentage": purchase.percentage
                })

        return jsonify({
            "reader_id": reader_id,
            "purchased_books": books_data
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@book_bp.route('/reader/get_book/<int:book_id>', methods=['GET'])
@jwt_required()
def get_reader_book(book_id):
    try:
        # Get reader_id from JWT
        reader_id = get_jwt_identity()

        # Check if the reader exists
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Fetch the book details
        book = Book.query.filter_by(book_id=book_id).first()

        if not book:
            return jsonify({"error": "Book not found"}), 404

        # Serialize book data with publisher details
        book_details = {
            "book_id": book.book_id,
            "title": book.title,
            "author": book.author,
            "isbn": book.isbn,
            "file_path": book.epub_file,
            "cover_image": book.cover_image,
            "language": book.language,
            "genre": book.genre,
            "e_book_type": book.e_book_type,
            "price": str(book.price),
            "rental_price": str(book.rental_price),
            "offer_price": str(book.offer_price) if book.offer_price else None,
            "description": book.description,
            "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updated_at": book.updated_at.strftime('%Y-%m-%d %H:%M:%S'),
            # Publisher info
            "publisher_id": book.publisher.publisher_id,
            "publisher_name": book.publisher.name
        }

        return jsonify(book_details), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@book_bp.route('/reader/add_cart', methods=['POST'])
@jwt_required()
def add_to_cart():
    data = request.json

    reader_id = get_jwt_identity()
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    book_id = data.get('book_id')

    if not book_id:
        return jsonify({"error": "Book ID is required"}), 400

    # Check if the book exists
    book = Book.query.get(book_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Check if the book is already in the cart
    existing_cart_item = Cart.query.filter_by(reader_id=reader_id, book_id=book_id).first()
    if existing_cart_item:
        return jsonify({"error": "Book is already in the cart"}), 400

    # Add to cart
    new_cart_item = Cart(reader_id=reader_id, book_id=book_id)
    db.session.add(new_cart_item)
    db.session.commit()

    return jsonify({"message": "Book added to cart successfully"}), 201


@book_bp.route('reader/get_cart', methods=['GET'])
@jwt_required()
def get_cart():
    reader_id = get_jwt_identity()
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    cart_items = Cart.query.filter_by(reader_id=reader_id).all()

    # Get total cart items for the reader
    total_items = Cart.query.filter_by(reader_id=reader_id).count()

    return jsonify({
        "cart": [
            {
                "cart_id": item.cart_id,
                "book_id": item.book.book_id,
                "title": item.book.title,
                "author": item.book.author,
                "price": item.book.price,
                "cover_image": item.book.cover_image,
                "rental_price": item.book.rental_price,
                "offer_price": item.book.offer_price,
                "added_at": item.added_at.strftime('%Y-%m-%d %H:%M:%S')
            }
            for item in cart_items
        ],
        "total_items": total_items
    }), 200


@book_bp.route('reader/delete_cart/<int:cart_id>', methods=['DELETE'])
@jwt_required()
def delete_cart(cart_id):
    try:

        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Check if the cart item exists
        cart_item = Cart.query.filter_by(cart_id=cart_id, reader_id=reader_id).first()
        if not cart_item:
            return jsonify({"error": "Cart item not found"}), 404

        # Delete the item
        db.session.delete(cart_item)
        db.session.commit()

        return jsonify({"message": "Cart item deleted successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@book_bp.route('/reader/add_wishlist', methods=['POST'])
@jwt_required()
def add_to_wishlist():
    data = request.json
    reader_id = get_jwt_identity()
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    book_id = data.get('book_id')

    if not book_id:
        return jsonify({"error": "Book ID is required"}), 400

    # Check if the book exists
    book = Book.query.get(book_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Check if the book is already in the wishlist
    existing_wishlist_item = Wishlist.query.filter_by(reader_id=reader_id, book_id=book_id).first()
    if existing_wishlist_item:
        return jsonify({"error": "Book is already in the wishlist"}), 400

    # Add to wishlist
    new_wishlist_item = Wishlist(reader_id=reader_id, book_id=book_id)
    db.session.add(new_wishlist_item)
    db.session.commit()

    return jsonify({"message": "Book added to wishlist successfully"}), 201


@book_bp.route('/reader/get_wishlist', methods=['GET'])
@jwt_required()
def get_wishlist():
    reader_id = get_jwt_identity()
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404

    wishlist_items = Wishlist.query.filter_by(reader_id=reader_id).all()
    # Get total cart items for the reader
    total_items = Wishlist.query.filter_by(reader_id=reader_id).count()

    return jsonify({
        "wishlist": [
            {
                "wishlist_id": item.wishlist_id,
                "book_id": item.book.book_id,
                "title": item.book.title,
                "author": item.book.author,
                "price": item.book.price,
                "cover_image": item.book.cover_image,
                "rental_price": item.book.rental_price,
                "offer_price": item.book.offer_price,
                "added_at": item.added_at.strftime('%Y-%m-%d %H:%M:%S')
            }
            for item in wishlist_items
        ],
        "total_items": total_items
    }), 200


@book_bp.route('/reader/delete_wishlist/<int:wishlist_id>', methods=['DELETE'])
@jwt_required()
def delete_wishlist(wishlist_id):
    try:
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Check if the wishlist item exists
        wishlist_item = Wishlist.query.filter_by(wishlist_id=wishlist_id, reader_id=reader_id).first()
        if not wishlist_item:
            return jsonify({"error": "Wishlist item not found"}), 404

        # Delete the item
        db.session.delete(wishlist_item)
        db.session.commit()

        return jsonify({"message": "Wishlist item deleted successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@book_bp.route('/stream/<filename>')
@jwt_required()
def serve_epub(filename):

    reader_id = get_jwt_identity()
    reader = Reader.query.get(reader_id)
    if not reader:
        return jsonify({"error": "Reader not found"}), 404
    file_path = os.path.join(current_app.config['FILE_UPLOAD_FOLDER'], filename)

    if not os.path.isfile(file_path):
        return {"error": "File not found"}, 404

    try:
        with open(file_path, "rb") as f:
            encrypted_data = f.read()

        decrypted_data = decrypt_file(encrypted_data, current_app.config['FILE_ENCRYPTION_KEY'])

        return Response(decrypted_data, content_type='application/epub+zip')

    except Exception as e:
        return {"error": f"Decryption failed: {str(e)}"}, 500


@book_bp.route('/pub/stream/<filename>')
@jwt_required()
def serve_epub2(filename):

    publisher_id = get_jwt_identity()
    publisher = Publisher.query.get(publisher_id)
    if not publisher:
        return jsonify({"error": "Publisher not found"}), 404
    file_path = os.path.join(current_app.config['FILE_UPLOAD_FOLDER'], filename)

    if not os.path.isfile(file_path):
        return {"error": "File not found"}, 404

    try:
        with open(file_path, "rb") as f:
            encrypted_data = f.read()

        decrypted_data = decrypt_file(encrypted_data, current_app.config['FILE_ENCRYPTION_KEY'])

        return Response(decrypted_data, content_type='application/epub+zip')

    except Exception as e:
        return {"error": f"Decryption failed: {str(e)}"}, 500


@book_bp.route('/reader/update_progress', methods=['PUT'])
@jwt_required()
def update_progress():
    try:
        data = request.json
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        book_id = data.get('book_id')
        bookmark = data.get('bookmark')
        percentage = data.get('percentage')

        if not book_id:
            return jsonify({"error": "Book ID is required"}), 400

        # Find the purchase record
        purchase = BooksPurchased.query.filter_by(reader_id=reader_id, book_id=book_id).first()

        if not purchase:
            purchase = BooksSubscribed.query.filter_by(reader_id=reader_id, book_id=book_id).first()
            if not purchase:
                return jsonify({"error": "Book purchase record not found"}), 404

        # Update fields if provided
        if bookmark is not None:
            purchase.bookmark = bookmark
        if percentage is not None:
            purchase.percentage = percentage

        db.session.commit()
        return jsonify({"message": "Progress updated successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@book_bp.route('/reader/get_progress/<int:book_id>', methods=['GET'])
@jwt_required()
def get_progress(book_id):
    try:
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        if not book_id:
            return jsonify({"error": "Book ID is required"}), 400

        # Find the purchase record
        purchase = BooksPurchased.query.filter_by(reader_id=reader_id, book_id=book_id).first()

        if not purchase:
            purchase = BooksPurchased.query.filter_by(reader_id=reader_id, book_id=book_id).first()
            if not purchase:
                return jsonify({"error": "Book purchase record not found"}), 404

        # Return the progress details
        return jsonify({
            "bookmark": purchase.bookmark,
            "percentage": purchase.percentage
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/reader/subscriptions', methods=['GET'])
@jwt_required()
def get_reader_subscriptions():
    try:
        reader_id = get_jwt_identity()

        # Get the reader's email from the Reader table
        reader = Reader.query.filter_by(reader_id=reader_id).first()
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        reader_email = reader.email

        # Fetch all subscriptions using reader_email and include sub_id
        subscriptions = db.session.query(
            Subscriber.sub_id,
            Category.category_id,
            Category.category_name,
            Publisher.publisher_id,
            Publisher.name
        ).join(Subscriber, Category.category_id == Subscriber.category_id)\
         .join(Publisher, Subscriber.publisher_id == Publisher.publisher_id)\
         .filter(Subscriber.reader_email == reader_email)\
         .all()

        if not subscriptions:
            return jsonify({"message": "No subscriptions found"}), 404

        # Format the response including sub_id
        subscription_list = [
            {
                "sub_id": sub.sub_id,
                "category_id": sub.category_id,
                "category_name": sub.category_name,
                "publisher_id": sub.publisher_id,
                "publisher_name": sub.name
            }
            for sub in subscriptions
        ]


        return jsonify({"subscriptions": subscription_list})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/reader/category/books/<int:category_id>', methods=['GET'])
@jwt_required()  # Optional, based on your app's access policy
def get_books_by_category(category_id):
    books = Book.query.filter_by(category_id=category_id).all()

    if not books:
        return jsonify({"message": "No books found in this category", "books": []}), 200

    result = []
    for book in books:
        result.append({
            "book_id": book.book_id,
            "title": book.title,
            "author": book.author,
            "isbn": book.isbn,
            "language": book.language,
            "genre": book.genre,
            "e_book_type": book.e_book_type,
            "price": float(book.price) if book.price else None,
            "rental_price": float(book.rental_price) if book.rental_price else None,
            "offer_price": float(book.offer_price) if book.offer_price else None,
            "description": book.description,
            "status": book.status,
            "has_ai_module": book.has_ai_module,
            "cover_image": book.cover_image,
            "epub_file": book.epub_file,
            "created_at": book.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updated_at": book.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        })

    return jsonify({"books": result}), 200


@subscriber_bp.route('/reader/add_sub_book', methods=['POST'])
@jwt_required()
def add_sub_book():
    try:
        data = request.json
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        book_id = data.get('book_id')
        sub_id = data.get('sub_id')

        if not book_id:
            return jsonify({"error": "Book ID is required"}), 400

        # Check if the book exists
        book = Book.query.filter_by(book_id=book_id).first()
        if not book:
            return jsonify({"error": "Book not found"}), 404

        # Check if the book is already subscribed
        existing_subscription = BooksSubscribed.query.filter_by(reader_id=reader_id, book_id=book_id).first()
        if existing_subscription:
            return jsonify({"error": "Book already subscribed"}), 400

        # Add subscription
        new_subscription = BooksSubscribed(
            reader_id=reader_id,
            book_id=book_id,
            sub_id=sub_id,
        )
        db.session.add(new_subscription)
        db.session.commit()

        return jsonify({"message": "Book added"}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/reader/get_subscribed_books', methods=['GET'])
@jwt_required()
def get_subscribed_books():
    try:
        data = request.json
        # Get reader_id from JWT token
        reader_id = get_jwt_identity()
        sub_id = data.get("sub_id")


        # Check if the reader exists
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        # Fetch all subscribed books for the given reader_id
        subscribed_books = BooksSubscribed.query.filter_by(reader_id=reader_id, sub_id=sub_id).all()

        # Serialize subscribed books data
        books_data = []
        for sub in subscribed_books:
            book = Book.query.get(sub.book_id)
            if book:
                books_data.append({
                    "book_id": book.book_id,
                    "title": book.title,
                    "author": book.author,
                    "isbn": book.isbn,
                    "cover_image": book.cover_image,
                    "file_path": book.epub_file,
                    "subscription_date": sub.subscription_date,
                    "bookmark": sub.bookmark,
                    "percentage": sub.percentage
                })

        return jsonify({
            "reader_id": reader_id,
            "subscribed_books": books_data
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth.route('/reader/subscribed_categories', methods=['GET'])
@jwt_required()  # Optional, if you want to protect the route
def get_categories_by_email(reader_email):
    reader_email = get_jwt_identity()
    subscriptions = Subscriber.query.filter_by(reader_email=reader_email).all()

    if not subscriptions:
        return jsonify({"message": "No subscriptions found", "categories": []}), 200

    # Collect associated categories
    categories = []
    for sub in subscriptions:
        category = sub.category
        categories.append({
            "category_id": category.category_id,
            "category_name": category.category_name,
            "description": category.description,
            "created_at": category.created_at.strftime('%Y-%m-%d') if category.created_at else None,
            "updated_time": category.updated_time.strftime('%Y-%m-%d') if category.updated_time else None,
            "publisher_id": category.publisher_id
        })

    return jsonify({"categories": categories}), 200




@files_bp.route('/cover_image/<filename>')
def serve_cover_image(filename):
    image_folder = current_app.config['IMAGE_UPLOAD_FOLDER']
    return send_from_directory(image_folder, filename)


@files_bp.route('/ask', methods=['POST'])
@jwt_required()
def ask():
    try:
        reader_id = get_jwt_identity()
        reader = Reader.query.get(reader_id)
        if not reader:
            return jsonify({"error": "Reader not found"}), 404

        data = request.get_json()
        book_id = data.get('book_id')
        question = data.get('question')

        if not book_id or not question:
            return jsonify({'error': 'Missing book_id or question'}), 400

        json_path = os.path.join(current_app.config['JSON_UPLOAD_FOLDER'], f"{book_id}.json.enc")
        faiss_path = os.path.join(current_app.config['FAISS_UPLOAD_FOLDER'], f"{book_id}.faiss")

        if not os.path.exists(json_path) or not os.path.exists(faiss_path):
            return jsonify({'error': 'Book index or chunks not found'}), 404

        chunks, metadata = load_chunks2(json_path, current_app.config['FILE_ENCRYPTION_KEY'])
        index = load_index(faiss_path)
        relevant_chunks = search_index(question, chunks, index)

        # Combine metadata + context for AI
        metadata_str = "\n".join([f"{k}: {v}" for k, v in metadata.items()])
        context_str = "\n\n".join(relevant_chunks)
        full_context = f"{metadata_str}\n\n{context_str}"
        response = ask_openrouter(question, full_context)

        return jsonify({
            "book_id": book_id,
            "question": question,
            "metadata": metadata,
            "context_used": relevant_chunks,
            "response": response
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@files_bp.route('/pub/upload_book', methods=['POST'])
@jwt_required()
def upload_book():
    try:
        publisher_id = get_jwt_identity()
        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        title = request.form.get('title')
        author = request.form.get('author')
        isbn = request.form.get('isbn')
        category_id = request.form.get('category_id')
        language = request.form.get('language', '')
        genre = request.form.get('genre', '')
        e_book_type = request.form.get('e_book_type', 'EPUB')
        price = request.form.get('price', 0)
        rental_price = request.form.get('rental_price', 0)
        offer_price = request.form.get('offer_price')
        description = request.form.get('description')
        has_ai_module = request.form.get('aiChat', 'false').lower() in ['true', '1', 'yes', 'on']

        if not all([title, author, isbn, category_id]):
            return jsonify({"error": "Missing required fields"}), 400

        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Invalid category ID"}), 400

        last_book = Book.query.order_by(Book.book_id.desc()).first()
        new_book_id = last_book.book_id + 1 if last_book else 1

        if 'file' not in request.files or request.files['file'].filename == '':
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        if not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type"}), 400

        file_ext = os.path.splitext(file.filename)[1]
        epub_filename = f"{new_book_id}{file_ext}"
        full_file_path = os.path.join(current_app.config['FILE_UPLOAD_FOLDER'], epub_filename)

        # Save the uploaded file temporarily (unencrypted)
        temp_path = os.path.join(current_app.config['TEMP_UPLOAD_FOLDER'], f"{new_book_id}_temp{file_ext}")
        file_bytes = file.read()
        with open(temp_path, 'wb') as temp_f:
            temp_f.write(file_bytes)

        # Process vectors before encryption
        process_and_store_vectors2(temp_path, new_book_id, current_app.config["FILE_ENCRYPTION_KEY"])

        cover_image_filename = None

        # If cover image is provided, use it
        if 'cover_image' in request.files:
            cover_image = request.files['cover_image']
            if cover_image.filename != '' and allowed_file(cover_image.filename):
                cover_ext = os.path.splitext(cover_image.filename)[1]
                cover_image_filename = f"{new_book_id}{cover_ext}"
                full_cover_image_path = os.path.join(current_app.config['IMAGE_UPLOAD_FOLDER'], cover_image_filename)
                cover_image.save(full_cover_image_path)
            elif cover_image.filename != '':
                return jsonify({"error": "Invalid cover image type"}), 400
        else:
            # Extract cover image from unencrypted EPUB (before encryption)
            cover_image_filename = extract_cover(temp_path, new_book_id)

        # Encrypt the original EPUB file content
        encrypted_data = encrypt_file(file_bytes, current_app.config['FILE_ENCRYPTION_KEY'])
        with open(full_file_path, 'wb') as f:
            f.write(encrypted_data)

        # Delete temporary unencrypted file
        os.remove(temp_path)

        # Check if book title already exists
        existing_book = Book.query.filter_by(title=title).first()
        book_status = 'pending' if existing_book else 'live'

        new_book = Book(
            publisher_id=publisher_id,
            category_id=category_id,
            title=title,
            author=author,
            isbn=isbn,
            epub_file=epub_filename,
            cover_image=cover_image_filename,
            language=language,
            genre=genre,
            e_book_type=e_book_type,
            price=price,
            rental_price=rental_price,
            description=description,
            status=book_status,
            offer_price=offer_price,
            has_ai_module = has_ai_module
        )
        db.session.add(new_book)
        db.session.flush()
        db.session.commit()

        return jsonify({"message": "Book uploaded successfully"}), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@files_bp.route('/pub/upload_book_simple', methods=['POST'])
@jwt_required()
def upload_book_simple():
    try:
        publisher_id = get_jwt_identity()
        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        title = request.form.get('title')
        author = request.form.get('author')
        isbn = request.form.get('isbn')
        category_id = request.form.get('category_id')
        language = request.form.get('language', '')
        genre = request.form.get('genre', '')
        e_book_type = request.form.get('e_book_type', 'EPUB')
        price = request.form.get('price', 0)
        rental_price = request.form.get('rental_price', 0)
        offer_price = request.form.get('offer_price')
        description = request.form.get('description')
        has_ai_module = request.form.get('aiChat', 'false').lower() in ['true', '1', 'yes', 'on']

        if not all([title, author, isbn, category_id]):
            return jsonify({"error": "Missing required fields"}), 400

        category = Category.query.filter_by(category_id=category_id, publisher_id=publisher_id).first()
        if not category:
            return jsonify({"error": "Invalid category ID"}), 400

        last_book = Book.query.order_by(Book.book_id.desc()).first()
        new_book_id = last_book.book_id + 1 if last_book else 1

        if 'file' not in request.files or request.files['file'].filename == '':
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        if not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type"}), 400

        file_ext = os.path.splitext(file.filename)[1]
        epub_filename = f"{new_book_id}{file_ext}"
        full_file_path = os.path.join(current_app.config['FILE_UPLOAD_FOLDER'], epub_filename)

        # Save the file temporarily before encryption
        temp_path = os.path.join(current_app.config['TEMP_UPLOAD_FOLDER'], f"{new_book_id}_temp{file_ext}")
        file_bytes = file.read()
        with open(temp_path, 'wb') as temp_f:
            temp_f.write(file_bytes)

        cover_image_filename = None

        # If cover image is provided, use it
        if 'cover_image' in request.files:
            cover_image = request.files['cover_image']
            if cover_image.filename != '' and allowed_file(cover_image.filename):
                cover_ext = os.path.splitext(cover_image.filename)[1]
                cover_image_filename = f"{new_book_id}{cover_ext}"
                full_cover_image_path = os.path.join(current_app.config['IMAGE_UPLOAD_FOLDER'], cover_image_filename)
                cover_image.save(full_cover_image_path)
            elif cover_image.filename != '':
                return jsonify({"error": "Invalid cover image type"}), 400
        else:
            # Extract cover image from unencrypted file
            cover_image_filename = extract_cover(temp_path, new_book_id)

        # Encrypt and save file
        encrypted_data = encrypt_file(file_bytes, current_app.config['FILE_ENCRYPTION_KEY'])
        with open(full_file_path, 'wb') as f:
            f.write(encrypted_data)

        # Delete temp file
        os.remove(temp_path)

        # Check if book title already exists
        existing_book = Book.query.filter_by(title=title).first()
        book_status = 'pending' if existing_book else 'live'

        new_book = Book(
            publisher_id=publisher_id,
            category_id=category_id,
            title=title,
            author=author,
            isbn=isbn,
            epub_file=epub_filename,
            cover_image=cover_image_filename,
            language=language,
            genre=genre,
            e_book_type=e_book_type,
            price=price,
            rental_price=rental_price,
            description=description,
            status=book_status,
            offer_price=offer_price,
            has_ai_module = has_ai_module
        )
        db.session.add(new_book)
        db.session.flush()
        db.session.commit()

        return jsonify({"message": "Book uploaded successfully"}), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/pub/get_subscribers/<int:category_id>', methods=['GET'])
@jwt_required()
def get_subscribers(category_id):
    try:
        # Get publisher_id from JWT token
        publisher_id = get_jwt_identity()

        # Check if the publisher exists
        publisher = Publisher.query.get(publisher_id)
        if not publisher:
            return jsonify({"error": "Publisher not found"}), 404

        # Query the subscribers table for matching category and publisher
        subscribers = Subscriber.query.filter_by(
            category_id=category_id,
            publisher_id=publisher_id
        ).all()

        # Prepare response with sub_id and reader_email
        subscriber_data = [
            {
                "sub_id": subscriber.sub_id,  # or subscriber.sub_id depending on your column name
                "reader_email": subscriber.reader_email
            }
            for subscriber in subscribers
        ]

        return jsonify({"subscribers": subscriber_data}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@subscriber_bp.route('/pub/delete_subscriber/<int:sub_id>', methods=['DELETE'])
@jwt_required()
def delete_subscriber(sub_id):
    try:
        publisher_id = get_jwt_identity()

        # Check if the subscriber exists and belongs to the publisher
        subscriber = Subscriber.query.join(Category).filter(
            Subscriber.sub_id == sub_id,
            Category.publisher_id == publisher_id
        ).first()

        if not subscriber:
            return jsonify({"error": "Subscriber not found or unauthorized"}), 404

        # Delete subscriber
        db.session.delete(subscriber)
        db.session.commit()

        return jsonify({"message": "Subscriber deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route('/', defaults={'path': ''})
@main_bp.route('/<path:path>')
def serve(path):
    # Serve existing static files directly
    if path and os.path.exists(os.path.join(current_app.static_folder, path)):
        return send_from_directory(current_app.static_folder, path)
    # Otherwise, serve index.html (for React Router)
    return send_from_directory(current_app.static_folder, 'index.html')

