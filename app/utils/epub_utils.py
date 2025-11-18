import os
import json

import ebooklib
from flask import current_app
from ebooklib import epub
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer
import faiss
from .encryption import encrypt_file

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']

def extract_text_from_epub(epub_path):
    book = epub.read_epub(epub_path)
    text = ''
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            text += soup.get_text() + '\n\n'
    return text

def split_text(text, max_length=500):
    paragraphs = text.split('\n\n')
    chunks = []
    for para in paragraphs:
        if len(para) > max_length:
            chunks.extend([para[i:i+max_length] for i in range(0, len(para), max_length)])
        else:
            chunks.append(para.strip())
    return [chunk for chunk in chunks if chunk.strip()]


def process_and_store_vectors(epub_path, book_id, enc_key, model_name='all-MiniLM-L6-v2'):
    text = extract_text_from_epub(epub_path)
    chunks = split_text(text)

    model = SentenceTransformer(model_name)
    embeddings = model.encode(chunks, convert_to_numpy=True)

    index = faiss.IndexFlatL2(embeddings.shape[1])
    index.add(embeddings)

    # Encrypt JSON
    json_data = json.dumps(chunks, ensure_ascii=False).encode('utf-8')
    encrypted_json = encrypt_file(json_data, enc_key)

    json_path = os.path.join(current_app.config['JSON_UPLOAD_FOLDER'], f"{book_id}.json.enc")
    with open(json_path, 'wb') as f:
        f.write(encrypted_json)

    # Save FAISS index
    faiss_path = os.path.join(current_app.config['FAISS_UPLOAD_FOLDER'], f"{book_id}.faiss")
    faiss.write_index(index, faiss_path)

def process_and_store_vectors2(epub_path, book_id, enc_key, model_name='all-MiniLM-L6-v2'):
    book = epub.read_epub(epub_path)
    text = extract_text_from_epub(epub_path)
    chunks = split_text(text)
    metadata = get_book_metadata(book)

    model = SentenceTransformer(model_name)
    embeddings = model.encode(chunks, convert_to_numpy=True)

    index = faiss.IndexFlatL2(embeddings.shape[1])
    index.add(embeddings)

    # Store both metadata and chunks
    json_data = {
        "metadata": metadata,
        "chunks": chunks
    }

    encrypted_json = encrypt_file(json.dumps(json_data, ensure_ascii=False).encode('utf-8'), enc_key)

    json_path = os.path.join(current_app.config['JSON_UPLOAD_FOLDER'], f"{book_id}.json.enc")
    with open(json_path, 'wb') as f:
        f.write(encrypted_json)

    faiss_path = os.path.join(current_app.config['FAISS_UPLOAD_FOLDER'], f"{book_id}.faiss")
    faiss.write_index(index, faiss_path)


def get_book_metadata(book):
    return {
        "title": book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else "Unknown",
        "author": book.get_metadata('DC', 'creator')[0][0] if book.get_metadata('DC', 'creator') else "Unknown",
        "language": book.get_metadata('DC', 'language')[0][0] if book.get_metadata('DC', 'language') else "Unknown",
        "description": book.get_metadata('DC', 'description')[0][0] if book.get_metadata('DC', 'description') else "No description"
    }
