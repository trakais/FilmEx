"""SQLModel entities. Importing this module registers all tables."""

from filmex.models.camera import Camera
from filmex.models.film_brand import FilmBrand
from filmex.models.film_stock import FilmStock
from filmex.models.lens import Lens
from filmex.models.provider import DevelopmentMethod, Provider, ScanMethod

__all__ = [
    "Camera",
    "DevelopmentMethod",
    "FilmBrand",
    "FilmStock",
    "Lens",
    "Provider",
    "ScanMethod",
]
