"""Read the existing G1 localization snapshot. No navigation or motion commands."""
from datetime import datetime, timezone
import math
import time

from brain.adapters.http_client import HttpClient
from brain.storage.tasks import now_iso


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class NavigationPoseClient:
    def __init__(self, url):
        self.http = HttpClient(url, timeout_sec=3, error_body_in_message=False)

    def latest(self):
        started = time.monotonic()
        value = self.http.request_json('GET', '/status.json?motor_details=0',
            headers={'Cache-Control': 'no-cache'}, max_response_bytes=2 * 1024 * 1024)[0]
        elapsed = time.monotonic() - started
        pose = value.get('accepted_pose')
        if not isinstance(pose, list) or len(pose) != 3 or not all(is_number(v) for v in pose):
            raise ValueError('当前定位缺少有效的 x、y、yaw')
        if (value.get('accepted_pose_frame_id') != 'map'
                or value.get('accepted_pose_basis') != 'navigation_localization_body'):
            raise ValueError('当前位姿不是 map 坐标系下的导航定位')
        status = value.get('status') or {}
        if (status.get('initialized') is not True or status.get('failed_gates') != []
                or value.get('source_timing_fresh') is not True):
            raise ValueError('定位未初始化、质量检查未通过或源数据已过期')
        for key in ('accepted_pose_age_sec', 'status_receipt_age_sec', 'source_timing_consumer_age_sec'):
            age = value.get(key)
            if not is_number(age) or not 0 <= age + elapsed <= .5:
                raise ValueError('定位数据已过期或新鲜度未知：' + key)
        stamp = value.get('accepted_pose_source_time_ns')
        timing = value.get('source_timing') or {}
        odom_stamp = (timing.get('source_stamp_ns') or {}).get('odom')
        odom_age = timing.get('odom_source_age_sec')
        if (type(stamp) is not int or stamp <= 0 or type(odom_stamp) is not int or odom_stamp <= 0
                or not is_number(odom_age) or odom_age < 0):
            raise ValueError('定位缺少源采样时间或源时效证据')
        source_age = odom_age + value['source_timing_consumer_age_sec'] + max(0, (odom_stamp - stamp) / 1e9) + elapsed
        if (stamp - odom_stamp > 1_000_000_000 or source_age > 1
                or not -.05 <= time.time() - stamp / 1e9 <= 1):
            raise ValueError('定位源采样已过期或跨端时钟不一致')
        map_id, epoch = value.get('map_sha256'), value.get('localization_epoch')
        if (not isinstance(map_id, str) or len(map_id) != 64
                or any(c not in '0123456789abcdef' for c in map_id)
                or not isinstance(epoch, str) or not epoch):
            raise ValueError('定位缺少地图身份或定位代次')
        return dict(x=pose[0], y=pose[1], yaw=pose[2], frame_id='map',
                    map_sha256=map_id, localization_epoch=epoch,
                    pose_basis=value['accepted_pose_basis'],
                    body_reference=value.get('accepted_pose_body_reference'),
                    source_topic=value.get('accepted_pose_source_topic'), source_time_ns=stamp,
                    captured_at=datetime.fromtimestamp(stamp / 1e9, timezone.utc).isoformat(),
                    received_at=now_iso(), source_age_sec=source_age,
                    pose_receipt_age_sec=value['accepted_pose_age_sec'] + elapsed,
                    status_age_sec=value['status_receipt_age_sec'] + elapsed)
