from dataclasses import dataclass
from enum import StrEnum, auto


@dataclass(frozen=True, slots=True)
class Offer:
    source: str
    sku: str
    price_kopecks: int
    currency: str
    signature: str

    def __post_init__(self) -> None:
        if self.price_kopecks <= 0:
            raise ValueError("`price_kopecks` must be positive")


class FailReason(StrEnum):
    TIMEOUT = auto()
    HTTP_ERROR = auto()
    TRANSPORT_ERROR = auto()
    INVALID_RESPONSE = auto()
    BAD_SIGNATURE = auto()


@dataclass(frozen=True, slots=True)
class Fetched:
    """
    Успешный ответ площадки.
    Гарантии (их даёт контракт OfferFetcher, а не тип):
    - тело ответа разобрано и прошло схему;
    - offer.source и offer.sku совпадают с запрошенными;
    - подпись проверена.
    """

    offer: Offer
    attempts: int
    elapsed_s: float

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("Fetched requires at least one sent request")


@dataclass(frozen=True, slots=True)
class Failed:
    reason: FailReason
    attempts: int  # 0 — запрос так и не отправлен (например, дедлайн истёк в ожидании слота)
    elapsed_s: float
    status_code: int | None = None

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")
        if self.reason is FailReason.HTTP_ERROR and self.status_code is None:
            raise ValueError("HTTP_ERROR requires status_code")
        if self.reason is not FailReason.HTTP_ERROR and self.status_code is not None:
            raise ValueError(f"{self.reason} must not have status_code")


type SourceResult = Fetched | Failed
