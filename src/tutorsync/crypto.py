"""Шифрование секретов, которые вынуждены лежать в базе.

Пока такой секрет один — refresh-токен Google. Он не имеет срока годности и даёт
полный доступ к календарю, поэтому в дампе базы, в бэкапе и в логе репликации
его быть не должно: ключ живёт только в окружении процесса (SECRET_ENC_KEY).

Fernet, а не голый AES: он включает в себя аутентификацию шифртекста и метку
времени, так что подменённое или обрезанное значение не расшифруется молча
в мусор, а честно бросит исключение.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from tutorsync.config import get_settings


class SecretUnreadableError(RuntimeError):
    """Шифртекст не расшифровывается текущим ключом.

    Практически всегда означает одно: SECRET_ENC_KEY поменяли, а база осталась
    старая. Сообщение говорит об этом прямо, потому что иначе диагноз выглядит
    как «Google перестал работать» и ищут его не там.
    """


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().secret_enc_key
    if not key:
        raise RuntimeError(
            "SECRET_ENC_KEY пуст — шифровать нечем. "
            "Сгенерировать: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "SECRET_ENC_KEY не является ключом Fernet: нужны 32 байта в urlsafe-base64. "
            "Строка из secrets.token_urlsafe() не подойдёт — у неё другая длина."
        ) from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnreadableError(
            "Сохранённый секрет не расшифровывается текущим SECRET_ENC_KEY. "
            "Если ключ менялся — придётся заново пройти авторизацию Google: "
            "старый refresh-токен восстановить нельзя."
        ) from exc


def issued_at(ciphertext: str) -> float | None:
    """Когда был создан шифртекст, unix-время.

    Fernet кладёт метку времени в сам токен, и это избавляет от отдельного поля
    со сроком годности и отдельной подписи к нему: значение, которое нельзя
    подделать, не расшифровав токен, уже есть внутри.
    """
    try:
        return float(_fernet().extract_timestamp(ciphertext.encode()))
    except InvalidToken:
        return None


def reset_cache() -> None:
    """Сбрасывает закэшированный Fernet. Нужен тестам, меняющим ключ на лету."""
    _fernet.cache_clear()
