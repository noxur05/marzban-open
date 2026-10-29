from fastapi import APIRouter
from . import (
    admin,
    announcement,
    core,
    device,
    external_subscription,
    internal,
    manual_host,
    node,
    subscription,
    system,
    user_template,
    user,
    home,
)

api_router = APIRouter()

routers = [
    admin.router,
    announcement.router,
    core.router,
    device.router,
    external_subscription.router,
    internal.router,
    manual_host.router,
    node.router,
    subscription.router,
    system.router,
    user_template.router,
    user.router,
    home.router,
]

for router in routers:
    api_router.include_router(router)

__all__ = ["api_router"]