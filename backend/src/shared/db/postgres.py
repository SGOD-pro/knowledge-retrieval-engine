"""Shared database module compatibility shim.
Re-exports CloudRepository and PostgresRepository from db.database.
"""
from db.database import CloudRepository, PostgresRepository

__all__ = ["CloudRepository", "PostgresRepository"]
