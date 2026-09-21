#!/usr/bin/env python3
"""한국 공휴일 JSON 생성기.

공공데이터포털의 특일정보(한국천문연구원) 응답을 받아 이 저장소가 제공하는 형식으로 변환한다.
인증키는 코드에 넣지 않고 환경변수 DATA_GO_KR_KEY 또는 --key 인자로 받는다.

사용 예:
    export DATA_GO_KR_KEY=발급받은키
    python3 scripts/update_holidays.py --years 2023-2028              # 미리보기(변경점만 출력)
    python3 scripts/update_holidays.py --years 2023-2028 --write      # holidays/kr.json 갱신
    python3 scripts/update_holidays.py --years 2023-2028 --write --version 2027.01

주의:
    - version 값이 바뀌어야 내려받는 쪽이 변경을 감지한다. 기본값은 실행 시점의 연.월이다.
    - 음력 기반 공휴일은 확정 고시가 된 연도까지만 응답에 존재한다. 응답이 빈 연도는 건너뛴다.
"""

import argparse
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date

API_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
DEFAULT_OUTPUT = "holidays/kr.json"
SCHEMA_VERSION = 1

# 특일정보의 dateName -> (표시 키, 표시 이름)
NAME_MAP = {
    "1월1일": ("holiday.new_year", "신정"),
    "삼일절": ("holiday.independence_movement", "삼일절"),
    "어린이날": ("holiday.childrens_day", "어린이날"),
    "부처님오신날": ("holiday.buddha_birthday", "부처님오신날"),
    "현충일": ("holiday.memorial_day", "현충일"),
    "광복절": ("holiday.liberation", "광복절"),
    "개천절": ("holiday.national_foundation", "개천절"),
    "한글날": ("holiday.hangeul", "한글날"),
    "기독탄신일": ("holiday.christmas", "성탄절"),
    "노동절": ("holiday.labor_day", "근로자의날"),
    "제헌절": ("holiday.constitution", "제헌절"),
    "전국동시지방선거": ("holiday.local_election", "지방선거일"),
    "국회의원선거일": ("holiday.general_election", "국회의원선거일"),
    "국회의원선거": ("holiday.general_election", "국회의원선거일"),
}

# 3일 연휴로 오는 명절. 응답은 세 날 모두 같은 이름이므로 가운데 날을 당일로 본다.
# (공식 데이터의 대체공휴일 배치와 일치한다. 예: 2027년 연휴 2/6~2/8 + 대체 2/9는 설날이 일요일 2/7일 때만 성립)
LUNAR_GROUPS = {
    "설날": ("holiday.seollal_eve", "holiday.seollal", "holiday.seollal_next", "설날 연휴", "설날"),
    "추석": ("holiday.chuseok_eve", "holiday.chuseok", "holiday.chuseok_next", "추석 연휴", "추석"),
}


# 날짜가 법으로 고정된 공휴일(월, 일). 공식 고시가 아직 없는 미래 연도를 --fill-fixed-through로
# 채울 때 쓴다. 음력 기반(설날/추석/부처님오신날), 대체공휴일, 선거일, 임시공휴일은 해마다 달라지므로
# 넣지 않는다. 그 해의 공식 데이터가 나오면 --years 범위를 넓혀 덮어쓰면 된다.
FIXED_HOLIDAYS = [
    (1, 1, "holiday.new_year", "신정"),
    (3, 1, "holiday.independence_movement", "삼일절"),
    (5, 1, "holiday.labor_day", "근로자의날"),
    (5, 5, "holiday.childrens_day", "어린이날"),
    (6, 6, "holiday.memorial_day", "현충일"),
    (7, 17, "holiday.constitution", "제헌절"),
    (8, 15, "holiday.liberation", "광복절"),
    (10, 3, "holiday.national_foundation", "개천절"),
    (10, 9, "holiday.hangeul", "한글날"),
    (12, 25, "holiday.christmas", "성탄절"),
]


def fill_fixed(rows: list, from_year: int, through_year: int) -> list:
    """공식 데이터가 없는 연도를 날짜 고정 공휴일로만 채운다.

    설날/추석/부처님오신날과 대체공휴일은 빠지므로 그 해 달력은 일부만 표시된다.
    """
    added = []
    for year in range(from_year, through_year + 1):
        for month, day, name_key, name in FIXED_HOLIDAYS:
            added.append({"date": f"{year}-{month:02d}-{day:02d}", "nameKey": name_key,
                          "name": name, "isSubstitute": False})
    if added:
        print(f"고정 공휴일 채움: {from_year}~{through_year} {len(added)}건 "
              f"(설날/추석/부처님오신날/대체공휴일 제외)")
    return sorted(rows + added, key=lambda row: (row["date"], row["nameKey"]))


def fetch_year(key: str, year: int) -> list:
    query = urllib.parse.urlencode(
        {"ServiceKey": key, "solYear": year, "numOfRows": 200, "_type": "json"}
    )
    with urllib.request.urlopen(f"{API_URL}?{query}", timeout=30) as response:
        payload = json.loads(response.read().decode())
    body = payload.get("response", {}).get("body", {})
    items = body.get("items")
    items = [] if not items else items.get("item", [])
    return [items] if isinstance(items, dict) else items


def to_iso(locdate) -> str:
    text = str(locdate)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def convert(items_by_year: dict) -> list:
    rows, unmapped = [], []
    for year in sorted(items_by_year):
        items = items_by_year[year]
        for group, (key_eve, key_day, key_next, name_holiday, name_day) in LUNAR_GROUPS.items():
            run = sorted(item["locdate"] for item in items if item["dateName"] == group)
            if not run:
                continue
            day_index = 1 if len(run) == 3 else 0
            for index, locdate in enumerate(run):
                if index == day_index:
                    name_key, name = key_day, name_day
                elif index < day_index:
                    name_key, name = key_eve, name_holiday
                else:
                    name_key, name = key_next, name_holiday
                rows.append({"date": to_iso(locdate), "nameKey": name_key, "name": name, "isSubstitute": False})
        for item in items:
            raw_name = item["dateName"]
            if raw_name in LUNAR_GROUPS:
                continue
            if raw_name.startswith("대체공휴일"):
                rows.append({"date": to_iso(item["locdate"]), "nameKey": "holiday.substitute",
                             "name": "대체공휴일", "isSubstitute": True})
                continue
            if raw_name.startswith("임시공휴일"):
                rows.append({"date": to_iso(item["locdate"]), "nameKey": "holiday.temporary",
                             "name": "임시공휴일", "isSubstitute": False})
                continue
            if raw_name not in NAME_MAP:
                unmapped.append((year, raw_name))
                continue
            name_key, name = NAME_MAP[raw_name]
            rows.append({"date": to_iso(item["locdate"]), "nameKey": name_key, "name": name, "isSubstitute": False})
    if unmapped:
        print("처리하지 못한 이름이 있습니다. NAME_MAP에 추가가 필요합니다:", file=sys.stderr)
        for year, name in unmapped:
            print(f"  {year}: {name}", file=sys.stderr)
    rows.sort(key=lambda row: (row["date"], row["nameKey"]))
    return rows


def print_diff(old_rows: list, new_rows: list) -> None:
    old_by_date = collections.defaultdict(list)
    new_by_date = collections.defaultdict(list)
    for row in old_rows:
        old_by_date[row["date"]].append(row["name"])
    for row in new_rows:
        new_by_date[row["date"]].append(row["name"])
    changed = 0
    for day in sorted(set(old_by_date) | set(new_by_date)):
        before, after = sorted(old_by_date.get(day, [])), sorted(new_by_date.get(day, []))
        if before != after:
            print(f"  {day}  {before or '없음'} -> {after or '없음'}")
            changed += 1
    print("변경 없음" if changed == 0 else f"변경된 날짜 {changed}건")


def main() -> int:
    parser = argparse.ArgumentParser(description="특일정보 응답을 공휴일 JSON으로 변환한다.")
    parser.add_argument("--years", default="2023-2028", help="연도 범위 (예: 2023-2028)")
    parser.add_argument("--key", default=os.environ.get("DATA_GO_KR_KEY", ""),
                        help="공공데이터포털 인증키. 기본값은 환경변수 DATA_GO_KR_KEY")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"출력 파일 (기본 {DEFAULT_OUTPUT})")
    parser.add_argument("--version", default=None, help="version 값 (기본: 실행 시점 연.월)")
    parser.add_argument("--fill-fixed-through", type=int, default=None, metavar="YYYY",
                        help="공식 데이터가 없는 연도를 이 해까지 날짜 고정 공휴일로 채운다 (예: 2035)")
    parser.add_argument("--write", action="store_true", help="파일에 실제로 쓴다. 없으면 미리보기만 한다.")
    args = parser.parse_args()

    if not args.key:
        print("인증키가 없습니다. DATA_GO_KR_KEY 환경변수를 설정하거나 --key를 넘기세요.", file=sys.stderr)
        return 1

    try:
        start, end = (int(part) for part in args.years.split("-"))
    except ValueError:
        print("연도 범위 형식이 잘못되었습니다. 예: --years 2023-2028", file=sys.stderr)
        return 1

    items_by_year, covered = {}, []
    for year in range(start, end + 1):
        try:
            items = fetch_year(args.key, year)
        except Exception as error:  # 네트워크/인증 오류는 해당 연도만 건너뛴다
            print(f"{year}: 조회 실패 ({type(error).__name__})", file=sys.stderr)
            continue
        if items:
            items_by_year[year] = items
            covered.append(year)
        print(f"{year}: {len(items)}건")

    if not covered:
        print("가져온 데이터가 없습니다.", file=sys.stderr)
        return 1

    rows = convert(items_by_year)
    last_year = max(covered)
    if args.fill_fixed_through:
        if args.fill_fixed_through <= last_year:
            print(f"--fill-fixed-through({args.fill_fixed_through})가 공식 데이터 마지막 연도"
                  f"({last_year}) 이하라 채우지 않는다.", file=sys.stderr)
        else:
            rows = fill_fixed(rows, last_year + 1, args.fill_fixed_through)
            last_year = args.fill_fixed_through

    version = args.version or date.today().strftime("%Y.%m")
    catalog = {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "validYears": [min(covered), last_year],
        "holidays": rows,
    }

    print(f"\n합계 {len(rows)}건, 유효 연도 {catalog['validYears']}, version {version}")
    old_rows = []
    if os.path.exists(args.output):
        with open(args.output, encoding="utf-8") as file:
            old_rows = json.load(file).get("holidays", [])
        print("\n기존 파일과 비교:")
        print_diff(old_rows, rows)

    if not args.write:
        print("\n미리보기만 했습니다. 실제로 쓰려면 --write 를 붙이세요.")
        return 0

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(catalog, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(f"\n{args.output} 갱신 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
