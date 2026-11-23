"""Local civil dates. Solar Hijri leap years 1200–1600 from ICU Persian calendar."""
import re
from bisect import bisect_left
from datetime import date, datetime, time, timedelta

LEAPS = (
    1201, 1205, 1210, 1214, 1218, 1222, 1226, 1230, 1234, 1238, 1243, 1247,
    1251, 1255, 1259, 1263, 1267, 1271, 1276, 1280, 1284, 1288, 1292, 1296,
    1300, 1304, 1309, 1313, 1317, 1321, 1325, 1329, 1333, 1337, 1342, 1346,
    1350, 1354, 1358, 1362, 1366, 1370, 1375, 1379, 1383, 1387, 1391, 1395,
    1399, 1403, 1408, 1412, 1416, 1420, 1424, 1428, 1432, 1436, 1441, 1445,
    1449, 1453, 1457, 1461, 1465, 1469, 1474, 1478, 1482, 1486, 1490, 1494,
    1498, 1503, 1507, 1511, 1515, 1519, 1523, 1527, 1531, 1535, 1540, 1544,
    1548, 1552, 1556, 1560, 1564, 1568, 1573, 1577, 1581, 1585, 1589, 1593,
    1597,
)
EPOCH = date(1821, 3, 21)

MONTHS = 'فروردین اردیبهشت خرداد تیر مرداد شهریور مهر آبان آذر دی بهمن اسفند'.split()
WEEKDAYS = ('دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'شنبه', 'یکشنبه')
DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩يكى', '01234567890123456789یکی')


def norm(value):
    return ' '.join(value.translate(DIGITS).replace('\u200c', ' ').casefold().split())


def year_start(year):
    return EPOCH + timedelta(days=(year - 1200) * 365 + bisect_left(LEAPS, year))


def solar_date(year, month, day):
    if not 1200 <= year <= 1600 or not 1 <= month <= 12:
        raise ValueError('تاریخ شمسی باید بین سال‌های ۱۲۰۰ و ۱۶۰۰ باشد.')
    length = 31 if month <= 6 else 30 if month < 12 or year in LEAPS else 29
    if not 1 <= day <= length:
        raise ValueError('روز این ماه معتبر نیست.')
    return year_start(year) + timedelta(days=31 * min(month - 1, 6) + 30 * max(month - 7, 0) + day - 1)


def solar_parts(d):
    year = d.year - 621
    if d < year_start(year):
        year -= 1
    if not 1200 <= year <= 1600:
        raise ValueError('تاریخ خارج از بازهٔ پشتیبانی است.')
    n = (d - year_start(year)).days
    m, day = divmod(n, 31) if n < 186 else divmod(n - 186, 30)
    return year, m + (1 if n < 186 else 7), day + 1


def fa(value):
    return str(value).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))


def label(d):
    y, m, day = solar_parts(d)
    return fa(f'{day} {MONTHS[m - 1]} {y}')


def clock(value):
    value = norm(value)
    if not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', value):
        raise ValueError('ساعت را مثل ۱۸:۳۰ بنویس.')
    h, m = map(int, value.split(':'))
    return time(h, m).strftime('%H:%M')


def relative_days(value):
    value = norm(value)
    if re.fullmatch(r'[+-]\d{1,4}|0', value):
        return int(value)
    return {'امروز': 0, 'فردا': 1, 'پس فردا': 2, 'پسفردا': 2}.get(value)


def relative_label(days):
    return {0: 'امروز', 1: 'فردا', 2: 'پس‌فردا'}.get(days)


def resolve(spec, today):
    if spec is None:
        return None
    offset = relative_days(spec.day)
    if offset is not None:
        return today + timedelta(days=offset)
    value = norm(spec.day)
    try:
        y, m, d = map(int, re.split('[-/]', value))
        return solar_date(y, m, d) if spec.calendar == 'jalali' else date(y, m, d)
    except ValueError:
        raise ValueError('تاریخ را با روز، ماه و سال معتبر بنویس.') from None


def instant(day, hm, zone, *, exact=False):
    local = datetime.combine(day, time.fromisoformat(clock(hm)), zone)
    stamp = int(local.timestamp())
    if exact and datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError('این ساعت به‌دلیل تغییر ساعت محلی وجود ندارد.')
    return stamp


def occurrence(item, since):
    if not item['due_day']:
        return None
    original = date.fromisoformat(item['due_day'])
    if not item['annual'] or original >= since:
        return original
    jalali = item['calendar'] == 'jalali'
    _, m, d = solar_parts(original) if jalali else (original.year, original.month, original.day)
    year = solar_parts(since)[0] if jalali else since.year
    for y in range(year, year + 9):
        try:
            candidate = solar_date(y, m, d) if jalali else date(y, m, d)
        except ValueError:
            continue
        if candidate >= since:
            return candidate
    return None
