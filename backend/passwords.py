"""Password hashing shared by login and account security."""
import hashlib
import hmac
import secrets


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return 'pbkdf2_sha256$600000$' + salt + '$' + digest


def password_matches(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_error(password, confirmation):
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or not password.strip():
        return 'Пароль должен содержать от 12 до 128 символов.'
    if not isinstance(confirmation, str) or not hmac.compare_digest(password.encode(), confirmation.encode()):
        return 'Пароли не совпадают.'
    return None
