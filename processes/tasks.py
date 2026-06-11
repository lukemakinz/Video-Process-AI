from .models import Video
from .services import run_video_analysis


def analyze_video_task(video_id):
    video = Video.objects.select_related("operation", "operation__process").get(pk=video_id)
    return run_video_analysis(video)
