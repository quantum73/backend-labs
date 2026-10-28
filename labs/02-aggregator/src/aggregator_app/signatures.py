import hashlib
import hmac

from aggregator_app.models import Offer

SIGNATURE_SALT = b"bazaar-offers"
SIGNATURE_ITERATIONS = 60_000


def verify_signature(offer: Offer) -> bool:
    """
    CPU-bound (PBKDF2, ~4 мс на M5 Pro): вызывать только в пуле потоков, не в event loop.
    Сравнение через hmac.compare_digest, а не ==:
    == выходит на первом несовпавшем символе и время ответа выдаёт длину совпавшего префикса.
    Перебирая подпись посимвольно и замеряя время, атакующий подделает её за ~64·16 попыток вместо 16^64.
    compare_digest работает за время, не зависящее от места расхождения.
    """
    payload = f"{offer.source}:{offer.sku}:{offer.price_kopecks}".encode()
    expected = hashlib.pbkdf2_hmac("sha256", payload, SIGNATURE_SALT, SIGNATURE_ITERATIONS).hex()
    return hmac.compare_digest(expected, offer.signature)
