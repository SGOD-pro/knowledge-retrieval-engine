import json
import logging

from config import settings

try:
    import redis
except ImportError:
    redis = None

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self):
        self.client = None
        self._mem_cache: dict[str, dict] = {}
        redis_url = settings.REDIS_URL
        if not redis_url or "localhost" in redis_url:
            try:
                from aws.infra import get_client

                ec_client = get_client("elasticache")
                response = ec_client.describe_cache_clusters(ShowCacheNodeInfo=True)
                if response["CacheClusters"]:
                    cluster = response["CacheClusters"][0]
                    if "ConfigurationEndpoint" in cluster:
                        host = cluster["ConfigurationEndpoint"]["Address"]
                        port = cluster["ConfigurationEndpoint"]["Port"]
                        redis_url = f"redis://{host}:{port}/0"
                    elif cluster.get("CacheNodes"):
                        host = cluster["CacheNodes"][0]["Endpoint"]["Address"]
                        port = cluster["CacheNodes"][0]["Endpoint"]["Port"]
                        redis_url = f"redis://{host}:{port}/0"
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to fetch ElastiCache instance: %s", e
                )

        if redis_url and redis:
            try:
                self.client = redis.Redis.from_url(
                    redis_url,
                    socket_timeout=1.0,
                    socket_connect_timeout=1.0,
                    decode_responses=True,
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Redis client: {e}")
                self.client = None
        else:
            if not redis:
                logger.warning("Redis library not installed, caching using in-memory fallback.")
            else:
                logger.warning("REDIS_URL not set, caching using in-memory fallback.")

    def get_cache(self, key: str) -> dict | None:
        if self.client:
            try:
                cached_data = self.client.get(key)
                if cached_data:
                    return json.loads(cached_data)
            except Exception as e:
                pass
        return self._mem_cache.get(key)

    def set_cache(self, key: str, value: dict, ttl: int):
        self._mem_cache[key] = value
        if self.client:
            try:
                self.client.setex(key, ttl, json.dumps(value))
            except Exception as e:
                pass


# Singleton instance
cache = RedisCache()
