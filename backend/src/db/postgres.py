"""Database module compatibility shim.
Re-exports CloudRepository and PostgresRepository from database.py.
"""
from db.database import CloudRepository, PostgresRepository

__all__ = ["CloudRepository", "PostgresRepository"]
