from datetime import timedelta

from smartgpms.time_utils import business_now


def test_business_time_is_always_china_standard_time():
    current = business_now()

    assert current.utcoffset() == timedelta(hours=8)
    assert current.tzname() == "Asia/Shanghai"
