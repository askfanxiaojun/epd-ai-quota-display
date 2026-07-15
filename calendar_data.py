"""Chinese calendar labels adapted from the EPD-nRF5_DYC firmware calendar."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from Foundation import (
    NSCalendar,
    NSCalendarIdentifierChinese,
    NSCalendarUnitDay,
    NSCalendarUnitMonth,
    NSCalendarUnitYear,
    NSDate,
    NSDateFormatter,
    NSLocale,
)


LUNAR_MONTH_NAMES = (
    "",
    "正月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "冬月",
    "腊月",
)
LUNAR_DAY_NAMES = (
    "",
    "初一",
    "初二",
    "初三",
    "初四",
    "初五",
    "初六",
    "初七",
    "初八",
    "初九",
    "初十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "廿一",
    "廿二",
    "廿三",
    "廿四",
    "廿五",
    "廿六",
    "廿七",
    "廿八",
    "廿九",
    "三十",
)
ZODIAC_NAMES = ("鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪")

SOLAR_FESTIVALS = {
    (1, 1): "元旦节",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (3, 12): "植树节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (11, 1): "万圣节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}
LUNAR_FESTIVALS = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (2, 2): "龙抬头",
    (5, 5): "端午节",
    (7, 7): "七夕节",
    (7, 15): "中元节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (10, 1): "寒衣节",
    (12, 8): "腊八节",
}
SOLAR_TERM_NAMES = (
    "小寒",
    "大寒",
    "立春",
    "雨水",
    "惊蛰",
    "春分",
    "清明节",
    "谷雨",
    "立夏",
    "小满",
    "芒种",
    "夏至",
    "小暑",
    "大暑",
    "立秋",
    "处暑",
    "白露",
    "秋分",
    "寒露",
    "霜降",
    "立冬",
    "小雪",
    "大雪",
    "冬至",
)
SOLAR_TERM_BASE_DAYS = (
    6, 20, 4, 19, 6, 21, 5, 20, 6, 21, 6, 21,
    7, 23, 8, 23, 8, 23, 8, 24, 8, 22, 7, 22,
)
SOLAR_TERM_BITS = (
    0x4E, 0xA6, 0x99, 0x9C, 0xA2, 0x98, 0x80, 0x00, 0x18, 0x00, 0x10, 0x24,
    0x4E, 0xA6, 0x99, 0x9C, 0xA2, 0x98, 0x80, 0x82, 0x18, 0x00, 0x10, 0x24,
    0x4E, 0xA6, 0xD9, 0x9E, 0xA2, 0x98, 0x80, 0x82, 0x18, 0x00, 0x10, 0x04,
    0x4E, 0xE6, 0xD9, 0x9E, 0xA6, 0xA8, 0x80, 0x82, 0x18, 0x00, 0x10, 0x00,
    0x0F, 0xE6, 0xD9, 0xBE, 0xA6, 0x98, 0x88, 0x82, 0x18, 0x80, 0x00, 0x00,
    0x0F, 0xEF, 0xD9, 0xBE, 0xA6, 0x99, 0x8C, 0x82, 0x98, 0x80, 0x00, 0x00,
    0x0F, 0xEF, 0xDB, 0xBE, 0xA6, 0x99, 0x9C, 0xA2, 0x98, 0x80, 0x00, 0x18,
    0x0F, 0xEF, 0xDB, 0xBE, 0xA6, 0x99, 0x9C, 0xA2, 0x98, 0x80, 0x00, 0x18,
    0x0F, 0xEF, 0xDB, 0xBE, 0xA2, 0x99, 0x8C, 0xA0, 0x98, 0x80, 0x82, 0x18,
    0x0B, 0xEF, 0xDB, 0xBE, 0xA6, 0x99, 0x8C, 0xA2, 0x98, 0x80, 0x82, 0x18,
    0x0F, 0xEF, 0xDB, 0xBE, 0xE6, 0xD9, 0x9E, 0xA2, 0x98, 0x80, 0x82, 0x18,
    0x0F, 0xEF, 0xFB, 0xBF, 0xE6, 0xD9, 0x9E, 0xA6, 0x98, 0x80, 0x82, 0x18,
    0x0F, 0xFF, 0xFF, 0xFC, 0xEF, 0xD9, 0xBE, 0xA6, 0x18,
)

HOLIDAY_YEAR = 2026
HOLIDAY_CODES = (
    0x0101, 0x0102, 0x0103, 0x1104, 0x120E, 0x020F, 0x0210, 0x0211, 0x0212, 0x0213,
    0x0214, 0x0215, 0x0216, 0x0217, 0x121C, 0x0404, 0x0405, 0x0406, 0x0501, 0x0502,
    0x0503, 0x0504, 0x0505, 0x1509, 0x0613, 0x0614, 0x0615, 0x0919, 0x091A, 0x091B,
    0x1914, 0x0A01, 0x0A02, 0x0A03, 0x0A04, 0x0A05, 0x0A06, 0x0A07, 0x1A0A,
)


@dataclass(frozen=True)
class LunarDate:
    related_year: int
    month: int
    day: int
    is_leap: bool
    cyclical_year: str

    @property
    def zodiac(self) -> str:
        return ZODIAC_NAMES[(self.related_year - 4) % 12]

    @property
    def month_label(self) -> str:
        prefix = "闰" if self.is_leap else ""
        return prefix + LUNAR_MONTH_NAMES[self.month]

    @property
    def day_label(self) -> str:
        return LUNAR_DAY_NAMES[self.day]


_CHINESE_CALENDAR = NSCalendar.calendarWithIdentifier_(NSCalendarIdentifierChinese)
_CYCLICAL_FORMATTER = NSDateFormatter.alloc().init()
_CYCLICAL_FORMATTER.setLocale_(NSLocale.localeWithLocaleIdentifier_("zh_CN@calendar=chinese"))
_CYCLICAL_FORMATTER.setCalendar_(_CHINESE_CALENDAR)
_CYCLICAL_FORMATTER.setDateFormat_("U")
_RELATED_YEAR_FORMATTER = NSDateFormatter.alloc().init()
_RELATED_YEAR_FORMATTER.setLocale_(NSLocale.localeWithLocaleIdentifier_("zh_CN@calendar=chinese"))
_RELATED_YEAR_FORMATTER.setCalendar_(_CHINESE_CALENDAR)
_RELATED_YEAR_FORMATTER.setDateFormat_("r")


def solar_to_lunar(value: date | datetime) -> LunarDate:
    if isinstance(value, datetime):
        local = value.astimezone()
    else:
        local = datetime(value.year, value.month, value.day, 12).astimezone()
    ns_date = NSDate.dateWithTimeIntervalSince1970_(local.timestamp())
    components = _CHINESE_CALENDAR.components_fromDate_(
        NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay,
        ns_date,
    )
    return LunarDate(
        related_year=int(_RELATED_YEAR_FORMATTER.stringFromDate_(ns_date)),
        month=int(components.month()),
        day=int(components.day()),
        is_leap=bool(components.isLeapMonth()),
        cyclical_year=str(_CYCLICAL_FORMATTER.stringFromDate_(ns_date)),
    )


def solar_term(value: date) -> str | None:
    if not 2000 <= value.year <= 2050:
        return None
    term_index = (value.month - 1) * 2 + (1 if value.day >= 15 else 0)
    bit_byte = SOLAR_TERM_BITS[(value.year - 2000) * 3 + term_index // 8]
    adjusted = bool((bit_byte << (term_index % 8)) & 0x80)
    term_day = SOLAR_TERM_BASE_DAYS[term_index]
    if adjusted:
        if term_index in (1, 11, 18, 21) and value.year < 2044:
            term_day += 1
        else:
            term_day -= 1
    return SOLAR_TERM_NAMES[term_index] if value.day == term_day else None


def holiday_marker(value: date) -> str | None:
    if value.year != HOLIDAY_YEAR:
        return None
    for code in HOLIDAY_CODES:
        if ((code >> 8) & 0xF, code & 0xFF) == (value.month, value.day):
            return "班" if (code >> 12) & 1 else "休"
    return None


def calendar_label(value: date) -> tuple[str, bool]:
    lunar = solar_to_lunar(value)
    next_lunar = solar_to_lunar(value + timedelta(days=1))
    if (next_lunar.month, next_lunar.day) == (1, 1):
        return "除夕", True
    festival = LUNAR_FESTIVALS.get((lunar.month, lunar.day))
    if festival and not lunar.is_leap:
        return festival, True
    weekday = value.weekday()
    if value.month == 5 and weekday == 6 and 8 <= value.day <= 14:
        return "母亲节", True
    if value.month == 6 and weekday == 6 and 15 <= value.day <= 21:
        return "父亲节", True
    if value.month == 11 and weekday == 3 and 22 <= value.day <= 28:
        return "感恩节", True
    festival = SOLAR_FESTIVALS.get((value.month, value.day))
    if festival:
        return festival, True
    term = solar_term(value)
    if term:
        return term, True
    return (lunar.month_label if lunar.day == 1 else lunar.day_label), False
