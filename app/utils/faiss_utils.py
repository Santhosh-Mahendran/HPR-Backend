import json
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from .encryption import decrypt_file

model = SentenceTransformer('all-MiniLM-L6-v2')

def load_chunks(path, enc_key):
    with open(path, 'rb') as f:
        encrypted_data = f.read()
    decrypted_data = decrypt_file(encrypted_data, enc_key)
    return json.loads(decrypted_data.decode('utf-8'))

def load_chunks2(path, enc_key):
    with open(path, 'rb') as f:
        encrypted_data = f.read()
    decrypted_data = decrypt_file(encrypted_data, enc_key)
    data = json.loads(decrypted_data.decode('utf-8'))
    return data['chunks'], data['metadata']


def load_index(index_path):
    return faiss.read_index(index_path)

def embed_query(query):
    return model.encode([query])[0]

def search_index(query, chunks, index, top_k=10):
    query_embedding = embed_query(query).astype('float32').reshape(1, -1)
    D, I = index.search(query_embedding, top_k)
    return [chunks[i] for i in I[0] if i < len(chunks)]
