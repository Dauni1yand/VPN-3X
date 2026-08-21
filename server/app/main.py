from fastapi import FastAPI

from app.api.routes import clients, cloudflare, health, nodes, settings, subscriptions, webhooks

app = FastAPI(title="VPN-3X main server")

app.include_router(health.router)
app.include_router(nodes.router)
app.include_router(clients.router)
app.include_router(subscriptions.router)
app.include_router(settings.router)
app.include_router(webhooks.router)
app.include_router(cloudflare.router)
