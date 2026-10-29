from pydantic import BaseModel


class SystemStats(BaseModel):
    version: str
    mem_total: int
    mem_used: int
    cpu_cores: int
    cpu_usage: float
    total_user: int
    online_users: int
    users_active: int
    users_on_hold: int
    users_disabled: int
    users_expired: int
    users_limited: int
    incoming_bandwidth: int
    outgoing_bandwidth: int
    incoming_bandwidth_speed: int
    outgoing_bandwidth_speed: int


class DeviceStats(BaseModel):
    """Aggregated subscription-device counts for the dashboard
    Statistics card. Both numbers exclude blocked rows.

    active_users_devices: devices whose owning user has status=active
    total_devices: devices across all user statuses
    """
    active_users_devices: int
    total_devices: int
