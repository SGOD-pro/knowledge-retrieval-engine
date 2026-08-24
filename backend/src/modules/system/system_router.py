from fastapi import APIRouter
from modules.system.system_service import system_service

router = APIRouter(tags=["system"])


@router.get("/health")
def health_check():
    return system_service.health_check()


@router.get("/system/benchmarks")
def get_benchmarks_endpoint():
    return system_service.get_benchmarks()
