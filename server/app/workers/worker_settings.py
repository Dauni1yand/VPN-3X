from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.tasks import bootstrap_node_job, health_check_nodes


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [bootstrap_node_job]
    cron_jobs = [cron(health_check_nodes, minute=set(range(0, 60, 1)))]
    # A node install is minutes of apt + the 3x-ui installer; the default
    # 300s job timeout would kill it midway.
    job_timeout = 1800
