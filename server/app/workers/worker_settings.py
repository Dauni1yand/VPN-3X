from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.tasks import health_check_nodes


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    cron_jobs = [cron(health_check_nodes, minute=set(range(0, 60, 1)))]
