import json
import logging
import os

try:
    import redis
except ImportError:
    redis = None

logger = logging.getLogger(__name__)

class RedisCache:
    def __init__(self):
        self.client = None
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url or "localhost" in redis_url:
            try:
                from kre.shared.aws import get_client
                ec_client = get_client("elasticache")
                response = ec_client.describe_cache_clusters(ShowCacheNodeInfo=True)
                if response["CacheClusters"]:
                    cluster = response["CacheClusters"][0]
                    # ElastiCache nodes are returned in CacheNodes or via ConfigurationEndpoint depending on engine/version
                    # Try to fetch from ConfigurationEndpoint first (Redis Cluster mode)
                    if "ConfigurationEndpoint" in cluster:
                        host = cluster["ConfigurationEndpoint"]["Address"]
                        port = cluster["ConfigurationEndpoint"]["Port"]
                        redis_url = f"redis://{host}:{port}/0"
                    elif "CacheNodes" in cluster and cluster["CacheNodes"]:
                        host = cluster["CacheNodes"][0]["Endpoint"]["Address"]
                        port = cluster["CacheNodes"][0]["Endpoint"]["Port"]
                        redis_url = f"redis://{host}:{port}/0"
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Failed to fetch ElastiCache instance: %s", e)
        
        if redis_url and redis:
            try:
                # Set short timeouts to fail-open quickly if Redis is unreachable
                self.client = redis.Redis.from_url(
                    redis_url, 
                    socket_timeout=1.0, 
                    socket_connect_timeout=1.0,
                    decode_responses=True
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Redis client: {e}")
                self.client = None
        else:
            if not redis:
                logger.warning("Redis library not installed, caching disabled.")
            else:
                logger.warning("REDIS_URL not set, caching disabled.")

    def get_cache(self, key: str) -> dict | None:
        if not self.client:
            return None
            
        try:
            cached_data = self.client.get(key)
            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.warning(f"Redis get_cache failed for key {key}: {e}")
            return None

    def set_cache(self, key: str, value: dict, ttl: int):
        if not self.client:
            return
            
        try:
            self.client.setex(key, ttl, json.dumps(value))
        except Exception as e:
            logger.warning(f"Redis set_cache failed for key {key}: {e}")

# Singleton instance
cache = RedisCache()
