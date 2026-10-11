from datetime import datetime

from pydantic import BaseModel


class OnlineStats(BaseModel):
    """Online statistics information"""

    registered_users: int
    online_users: int
    playing_users: int
    timestamp: datetime


class OnlineHistoryPoint(BaseModel):
    """Online history data point"""

    timestamp: datetime
    online_count: int
    playing_count: int


class OnlineHistoryStats(BaseModel):
    """24-hour online history statistics"""

    history: list[OnlineHistoryPoint]
    current_stats: OnlineStats


class ServerStatistics(BaseModel):
    """Server statistics information"""

    total_users: int
    online_users: int
    playing_users: int
    spectating_users: int
    multiplayer_users: int
    last_updated: datetime
