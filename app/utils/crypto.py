import os
from cryptography.fernet import Fernet

SECRET_KEY = os.getenv("FERNET_KEY")

# Generate once and store in .env
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

fernet = Fernet("dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4=")


def encrypt_password(password: str) -> str:
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()