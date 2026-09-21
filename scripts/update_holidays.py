#!/usr/bin/env python3
"""공휴일 데이터 갱신 도구.

공공데이터포털의 특일정보(한국천문연구원) 응답을 앱이 쓰는 형식으로 변환해 두 곳을 맞춘다.

  1. 앱 번들 파일  apps/ios/schedulit/Resources/Holidays/holidays-kr.json
     - 앱을 새로 설치한 사용자가 처음 보는 데이터. 앱을 다시 배포해야 반영된다.
  2. 공개 저장소 파일  dev-0ju/public-data 의 holidays/kr.json  (--publish 일 때만)
     - 이미 앱을 쓰고 있는 사용자가 원격 갱신으로 받아가는 데이터.

인증키는 이 파일에 넣지 않는다. 저장소 루트의 .env(추적하지 않음) 또는 환경변수에서 읽는다.
  DATA_GO_KR_KEY  공공데이터포털 인증키 (필수)
  GITHUB_TOKEN    공개 저장소 업로드용 (--publish 일 때만 필요)

사용 예(연도를 지정할 필요가 없다. 아무 때나 같은 명령으로 돌리면 된다):
    python3 scripts/holiday-update.py                      # 미리보기. 변경점만 출력한다
    python3 scripts/holiday-update.py --write              # 앱 번들 파일 갱신
    python3 scripts/holiday-update.py --write --publish    # 공개 저장소까지 업로드
    python3 scripts/holiday-update.py --verify-rules       # 규칙이 공식 데이터를 재현하는지 확인

연도 범위는 자동으로 정한다. 공식 고시가 나와 있는 해까지는 공식 데이터를 쓰고, 그 뒤부터
올해 + 10년까지는 규칙으로 만든다. 새 고시가 나오면 다음 실행에서 저절로 공식 데이터로 바뀐다.

주의:
    version 값이 바뀌어야 앱이 변경을 감지한다. 기본값은 실행 시점의 연.월이다.
    음력 기반 공휴일은 확정 고시가 된 연도까지만 응답에 존재하므로, 빈 연도는 건너뛴다.
"""

import argparse
import base64
import collections
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
LUNAR_API_URL = "https://apis.data.go.kr/B090041/openapi/service/LrsrCldInfoService/getSolCalInfo"
BUNDLE_PATH = "apps/ios/schedulit/Resources/Holidays/holidays-kr.json"
PUBLIC_REPO = "dev-0ju/public-data"
PUBLIC_PATH = "holidays/kr.json"
SCHEMA_VERSION = 1
# 기본 데이터 시작 연도. 지난 공휴일도 달력에서 볼 수 있도록 유지한다.
DEFAULT_START_YEAR = 2023
# 공식 고시가 없는 미래를 규칙으로 몇 년치 채울지. 매번 돌릴 때마다 기준이 올해로 밀린다.
FUTURE_YEARS = 10

# 특일정보의 dateName -> (표시 키, 표시 이름). 앱의 Localizable 키와 일치해야 한다.
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

# 3일 연휴로 오는 명절. 응답은 세 날 모두 같은 이름이라 가운데 날을 당일로 본다
# (공식 데이터의 대체공휴일 배치와 일치한다. 2027년 연휴 2/6~2/8 + 대체 2/9는 설날이 일요일 2/7일 때만 성립).
LUNAR_GROUPS = {
    "설날": ("holiday.seollal_eve", "holiday.seollal", "holiday.seollal_next", "설날 연휴", "설날"),
    "추석": ("holiday.chuseok_eve", "holiday.chuseok", "holiday.chuseok_next", "추석 연휴", "추석"),
}

# 날짜가 법으로 고정된 공휴일. (월, 일, 키, 이름[, 시행 연도]).
# 시행 연도가 있으면 그 해부터 적용한다(근로자의날과 제헌절은 2026년부터 공식 데이터에 등장한다).
FIXED_HOLIDAYS = [
    (1, 1, "holiday.new_year", "신정"),
    (3, 1, "holiday.independence_movement", "삼일절"),
    (5, 1, "holiday.labor_day", "근로자의날", 2026),
    (5, 5, "holiday.childrens_day", "어린이날"),
    (6, 6, "holiday.memorial_day", "현충일"),
    (7, 17, "holiday.constitution", "제헌절", 2026),
    (8, 15, "holiday.liberation", "광복절"),
    (10, 3, "holiday.national_foundation", "개천절"),
    (10, 9, "holiday.hangeul", "한글날"),
    (12, 25, "holiday.christmas", "성탄절"),
]

# 음력 공휴일. (음력 월, 음력 일, 넣을 항목들). 항목이 셋이면 앞뒤 하루를 포함한 3일 연휴다.
LUNAR_HOLIDAYS = [
    (1, 1, (("holiday.seollal_eve", "설날 연휴"), ("holiday.seollal", "설날"), ("holiday.seollal_next", "설날 연휴"))),
    (8, 15, (("holiday.chuseok_eve", "추석 연휴"), ("holiday.chuseok", "추석"), ("holiday.chuseok_next", "추석 연휴"))),
    (4, 8, (("holiday.buddha_birthday", "부처님오신날"),)),
]

# 대체공휴일 규칙(관공서의 공휴일에 관한 규정).
# 설날/추석 연휴는 연휴 중 일요일이 있을 때만 하루를 준다(토요일은 해당 없음).
SUBSTITUTE_LUNAR_GROUPS = [
    {"holiday.seollal_eve", "holiday.seollal", "holiday.seollal_next"},
    {"holiday.chuseok_eve", "holiday.chuseok", "holiday.chuseok_next"},
]
# 토요일이나 일요일과 겹치면 대체공휴일을 주는 공휴일. 신정, 현충일, 근로자의날은 대상이 아니다.
SUBSTITUTE_WEEKEND = {
    "holiday.labor_day",
    "holiday.independence_movement",
    "holiday.childrens_day",
    "holiday.buddha_birthday",
    "holiday.constitution",
    "holiday.liberation",
    "holiday.national_foundation",
    "holiday.hangeul",
    "holiday.christmas",
}


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_file_candidates() -> list:
    """.env를 찾을 위치들. 워크트리에서 실행하면 .env가 원본 체크아웃에만 있으므로 그쪽도 본다
    (.env는 추적하지 않는 파일이라 워크트리로 복제되지 않는다)."""
    candidates = [os.path.join(repo_root(), ".env")]
    try:
        import subprocess
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo_root(), capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if common_dir:
            candidates.append(os.path.join(os.path.dirname(common_dir), ".env"))
    except Exception:
        pass
    return candidates


def load_env_value(name: str) -> str:
    """환경변수를 우선하고, 없으면 .env에서 읽는다. 값은 출력하지 않는다."""
    value = os.environ.get(name, "")
    if value:
        return value
    for env_path in env_file_candidates():
        if not os.path.exists(env_path):
            continue
        with open(env_path, encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                if key.strip() == name:
                    return raw.strip().strip('"').strip("'")
    return ""


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
        print("처리하지 못한 이름이 있습니다. NAME_MAP과 앱의 Localizable 키 추가가 필요합니다:", file=sys.stderr)
        for year, name in unmapped:
            print(f"  {year}: {name}", file=sys.stderr)
    rows.sort(key=lambda row: (row["date"], row["nameKey"]))
    return rows


def lunar_to_solar(key: str, year: int, month: int, day: int) -> "date | None":
    """한국천문연구원 음양력 API로 음력 날짜를 양력으로 바꾼다(평달 기준).

    파이썬/애플의 음력 계산은 중국 기준(동경 120도)이라 한국 음력과 하루 어긋나는 해가 있다
    (예: 2027년 설날은 한국 2/7, 중국 기준 계산 2/6). 그래서 공식 변환만 쓴다.
    """
    query = urllib.parse.urlencode({
        "ServiceKey": key, "lunYear": year, "lunMonth": f"{month:02d}", "lunDay": f"{day:02d}", "_type": "json"
    })
    with urllib.request.urlopen(f"{LUNAR_API_URL}?{query}", timeout=30) as response:
        body = json.loads(response.read().decode())["response"]["body"]
    items = body.get("items", {}).get("item")
    if isinstance(items, dict):
        items = [items]
    for item in items or []:
        if item.get("lunLeapmonth") == "윤":  # 윤달은 명절 기준이 아니다
            continue
        return date(int(item["solYear"]), int(item["solMonth"]), int(item["solDay"]))
    return None


def generate_year(key: str, year: int) -> list:
    """공식 고시가 없는 연도의 공휴일을 규칙으로 만든다.

    - 날짜 고정 공휴일: FIXED_HOLIDAYS
    - 음력 공휴일: 설날(음 1/1)과 추석(음 8/15)은 앞뒤 하루를 포함한 3일 연휴, 부처님오신날(음 4/8)
    - 대체공휴일: 아래 SUBSTITUTE_* 규칙
    선거일과 임시공휴일은 예측할 수 없어 넣지 않는다. 공식 고시가 나오면 --years로 덮어쓴다.
    """
    rows = []
    for entry in FIXED_HOLIDAYS:
        month, day, name_key, name = entry[:4]
        since = entry[4] if len(entry) > 4 else None
        if since is not None and year < since:
            continue
        rows.append({"date": f"{year}-{month:02d}-{day:02d}", "nameKey": name_key,
                     "name": name, "isSubstitute": False})

    for lunar_month, lunar_day, keys in LUNAR_HOLIDAYS:
        solar = lunar_to_solar(key, year, lunar_month, lunar_day)
        if solar is None:
            print(f"{year}: 음력 {lunar_month}/{lunar_day} 변환 실패", file=sys.stderr)
            continue
        if len(keys) == 1:  # 부처님오신날 등 하루짜리
            name_key, name = keys[0]
            rows.append({"date": solar.isoformat(), "nameKey": name_key, "name": name, "isSubstitute": False})
            continue
        (eve_key, eve_name), (day_key, day_name), (next_key, next_name) = keys
        rows.append({"date": (solar - timedelta(days=1)).isoformat(), "nameKey": eve_key,
                     "name": eve_name, "isSubstitute": False})
        rows.append({"date": solar.isoformat(), "nameKey": day_key, "name": day_name, "isSubstitute": False})
        rows.append({"date": (solar + timedelta(days=1)).isoformat(), "nameKey": next_key,
                     "name": next_name, "isSubstitute": False})

    rows.extend(substitute_days(rows))
    return sorted(rows, key=lambda row: (row["date"], row["nameKey"]))


def substitute_days(rows: list) -> list:
    """대체공휴일을 현행 규칙으로 계산한다.

    - 설날/추석 연휴: 연휴 중 하루라도 일요일이면 대체공휴일 1일(토요일은 해당 없음)
    - 그 밖의 대상 공휴일(SUBSTITUTE_WEEKEND): 토요일이나 일요일과 겹치면 대체공휴일 1일
    - 신정, 현충일, 근로자의날은 대상이 아니다
    대체일은 그 공휴일 다음날부터 세어 공휴일이 아닌 첫날이다.
    """
    taken = {row["date"] for row in rows}
    added = []

    def next_free(from_day: date) -> date:
        candidate = from_day + timedelta(days=1)
        while candidate.isoformat() in taken or candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    for group_keys in (SUBSTITUTE_LUNAR_GROUPS):
        run = sorted(date.fromisoformat(row["date"]) for row in rows if row["nameKey"] in group_keys)
        if run and any(day.weekday() == 6 for day in run):
            replacement = next_free(run[-1])
            added.append({"date": replacement.isoformat(), "nameKey": "holiday.substitute",
                          "name": "대체공휴일", "isSubstitute": True})
            taken.add(replacement.isoformat())

    # 서로 다른 공휴일이 같은 날 겹치면 하루를 더 준다
    # (2025-05-05 어린이날과 부처님오신날, 2028-10-03 추석과 개천절).
    by_date = collections.defaultdict(set)
    for row in rows:
        by_date[row["date"]].add(row["nameKey"])
    for date_text in sorted(by_date):
        if len(by_date[date_text]) < 2:
            continue
        replacement = next_free(date.fromisoformat(date_text))
        added.append({"date": replacement.isoformat(), "nameKey": "holiday.substitute",
                      "name": "대체공휴일", "isSubstitute": True})
        taken.add(replacement.isoformat())

    for row in sorted(rows, key=lambda item: item["date"]):
        if row["nameKey"] not in SUBSTITUTE_WEEKEND:
            continue
        day = date.fromisoformat(row["date"])
        if day.weekday() < 5:
            continue
        replacement = next_free(day)
        added.append({"date": replacement.isoformat(), "nameKey": "holiday.substitute",
                      "name": "대체공휴일", "isSubstitute": True})
        taken.add(replacement.isoformat())

    return added


def verify_rules(key: str, official_rows: list, covered: list) -> int:
    """공식 데이터가 있는 연도를 규칙으로도 만들어 대조한다.

    규칙이 만들 수 없는 항목(선거일, 임시공휴일)은 비교에서 뺀다. 나머지가 전부 일치해야
    미래 연도를 규칙으로 채워도 된다고 볼 수 있다.
    """
    skip_keys = {"holiday.local_election", "holiday.general_election", "holiday.temporary"}
    mismatched = 0
    for year in covered:
        official = sorted(
            (row["date"], row["nameKey"]) for row in official_rows
            if row["date"].startswith(str(year)) and row["nameKey"] not in skip_keys
        )
        generated = sorted((row["date"], row["nameKey"]) for row in generate_year(key, year))
        only_official = [item for item in official if item not in generated]
        only_generated = [item for item in generated if item not in official]
        status = "일치" if not only_official and not only_generated else "불일치"
        print(f"  {year}: 공식 {len(official)}건 / 규칙 {len(generated)}건 -> {status}")
        for date_text, name_key in only_official:
            print(f"    공식에만: {date_text} {name_key}")
        for date_text, name_key in only_generated:
            print(f"    규칙에만: {date_text} {name_key}")
        mismatched += len(only_official) + len(only_generated)
    print("\n규칙이 공식 데이터를 완전히 재현한다." if mismatched == 0
          else f"\n차이 {mismatched}건. 규칙을 손봐야 한다.")
    return 0 if mismatched == 0 else 1


def print_diff(old_rows: list, new_rows: list) -> int:
    old_by_date, new_by_date = collections.defaultdict(list), collections.defaultdict(list)
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
    print("  변경 없음" if changed == 0 else f"  변경된 날짜 {changed}건")
    return changed


def publish(catalog: dict, token: str) -> None:
    """공개 저장소의 파일을 교체한다. 토큰은 Authorization 헤더로만 쓰고 출력하지 않는다."""
    url = f"https://api.github.com/repos/{PUBLIC_REPO}/contents/{PUBLIC_PATH}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            sha = json.loads(response.read().decode())["sha"]
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    content = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    body = {"message": f"공휴일 데이터 갱신 ({catalog['version']}, {len(catalog['holidays'])}건)",
            "content": base64.b64encode(content.encode()).decode()}
    if sha:
        body["sha"] = sha
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())
    print(f"공개 저장소 업로드 완료: {PUBLIC_REPO}/{PUBLIC_PATH} ({result['content']['size']} bytes)")
    print("raw 주소는 최대 5분간 이전 내용을 줄 수 있습니다.")


def main() -> int:
    parser = argparse.ArgumentParser(description="공휴일 데이터를 받아 앱 번들과 공개 저장소를 맞춘다.")
    parser.add_argument("--start", type=int, default=DEFAULT_START_YEAR,
                        help=f"데이터 시작 연도 (기본 {DEFAULT_START_YEAR})")
    parser.add_argument("--through", type=int, default=None, metavar="YYYY",
                        help="데이터 끝 연도 (기본: 올해 + %d년)" % FUTURE_YEARS)
    parser.add_argument("--years", default=None,
                        help="연도 범위를 직접 지정한다 (예: 2023-2028). 지정하면 --start/--through를 대신한다")
    parser.add_argument("--version", default=None, help="version 값 (기본: 실행 시점 연.월)")
    parser.add_argument("--output", default=BUNDLE_PATH,
                        help=f"출력 파일 경로 (기본 {BUNDLE_PATH}). 공개 저장소에서 직접 쓸 때는 holidays/kr.json")
    parser.add_argument("--verify-rules", action="store_true",
                        help="공식 데이터가 있는 연도를 규칙으로도 만들어 대조한다(규칙 정확도 확인)")
    parser.add_argument("--write", action="store_true", help="앱 번들 파일에 실제로 쓴다")
    parser.add_argument("--publish", action="store_true", help="공개 저장소에도 업로드한다 (--write 필요)")
    args = parser.parse_args()

    if args.publish and not args.write:
        print("--publish는 --write와 함께 써야 합니다.", file=sys.stderr)
        return 1

    key = load_env_value("DATA_GO_KR_KEY")
    if not key:
        print("DATA_GO_KR_KEY가 없습니다. .env에 넣거나 환경변수로 지정하세요.", file=sys.stderr)
        return 1
    token = load_env_value("GITHUB_TOKEN") if args.publish else ""
    if args.publish and not token:
        print("GITHUB_TOKEN이 없습니다. 업로드하려면 .env에 넣으세요.", file=sys.stderr)
        return 1

    start = args.start
    through = args.through or (date.today().year + FUTURE_YEARS)
    if args.years:
        try:
            start, through = (int(part) for part in args.years.split("-"))
        except ValueError:
            print("연도 범위 형식이 잘못되었습니다. 예: --years 2023-2028", file=sys.stderr)
            return 1
    if through < start:
        print(f"끝 연도({through})가 시작 연도({start})보다 앞입니다.", file=sys.stderr)
        return 1

    # 공식 고시가 어디까지 나와 있는지는 조회해 봐야 안다. 빈 응답이 두 해 연속이면 거기서 끊고,
    # 나머지 연도는 규칙으로 만든다(연도를 매번 지정하지 않아도 같은 명령으로 최신 상태가 된다).
    items_by_year, covered, empty_streak = {}, [], 0
    for year in range(start, through + 1):
        try:
            items = fetch_year(key, year)
        except Exception as error:  # 조회 실패한 연도만 건너뛴다
            print(f"{year}: 조회 실패 ({type(error).__name__})", file=sys.stderr)
            continue
        if items:
            items_by_year[year] = items
            covered.append(year)
            empty_streak = 0
            print(f"{year}: 공식 {len(items)}건")
        else:
            empty_streak += 1
            print(f"{year}: 공식 고시 없음")
            if empty_streak >= 2:
                print(f"  (공식 데이터는 {max(covered) if covered else start - 1}년까지. "
                      f"이후는 규칙으로 만든다)")
                break

    if not covered:
        print("가져온 데이터가 없습니다.", file=sys.stderr)
        return 1

    rows = convert(items_by_year)

    if args.verify_rules:
        return verify_rules(key, rows, covered)

    last_year = max(covered)
    if through > last_year:
        generated = []
        for year in range(last_year + 1, through + 1):
            generated.extend(generate_year(key, year))
        print(f"규칙으로 생성: {last_year + 1}~{through} {len(generated)}건 (선거일/임시공휴일 제외)")
        rows = sorted(rows + generated, key=lambda row: (row["date"], row["nameKey"]))
        last_year = through

    catalog = {
        "schemaVersion": SCHEMA_VERSION,
        "version": args.version or date.today().strftime("%Y.%m"),
        "validYears": [min(covered), last_year],
        "holidays": rows,
    }
    print(f"\n합계 {len(rows)}건, 유효 연도 {catalog['validYears']}, version {catalog['version']}")

    bundle_file = os.path.join(repo_root(), args.output)
    if os.path.exists(bundle_file):
        with open(bundle_file, encoding="utf-8") as file:
            current = json.load(file)
        print(f"\n{args.output} 와 비교:")
        print_diff(current.get("holidays", []), rows)

    if not args.write:
        print("\n미리보기만 했습니다. 실제로 반영하려면 --write 를 붙이세요.")
        return 0

    os.makedirs(os.path.dirname(bundle_file), exist_ok=True)
    with open(bundle_file, "w", encoding="utf-8") as file:
        json.dump(catalog, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(f"\n파일 갱신 완료: {args.output}")
    print("앱 번들 변경은 앱을 다시 배포해야 사용자에게 반영됩니다.")

    if args.publish:
        publish(catalog, token)
    else:
        print("공개 저장소는 갱신하지 않았습니다. 기존 사용자에게 반영하려면 --publish 를 붙이세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
