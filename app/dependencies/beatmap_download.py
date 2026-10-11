from typing import Annotated

from app.service.beatmap_download_service import BeatmapDownloadService, download_service

from fastapi import Depends


def get_beatmap_download_service():
    """Get beatmap download service instance"""
    return download_service


DownloadService = Annotated[BeatmapDownloadService, Depends(get_beatmap_download_service)]
