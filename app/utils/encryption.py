from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
import os

def encrypt_file(input_data: bytes, key: bytes):
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(input_data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded_data) + encryptor.finalize()
    return iv + encrypted  # Store IV at beginning

def decrypt_file(encrypted_data: bytes, key: bytes):
    iv = encrypted_data[:16]
    encrypted = encrypted_data[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_padded = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(decrypted_padded) + unpadder.finalize()
