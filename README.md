# public-data

공개 데이터 파일을 정적 JSON으로 제공하는 저장소입니다. 인증 없이 raw 주소로 내려받을 수 있습니다.

## 파일

| 경로 | 내용 |
|---|---|
| `holidays/kr.json` | 대한민국 공휴일 (대체공휴일, 임시공휴일, 선거일 포함) |

raw 주소 형식:

```
https://raw.githubusercontent.com/dev-0ju/public-data/main/holidays/<국가코드>.json
```

## 형식

```json
{
  "schemaVersion": 1,
  "version": "2026.09",
  "validYears": [2023, 2028],
  "holidays": [
    { "date": "2026-01-01", "nameKey": "holiday.new_year", "name": "신정", "isSubstitute": false }
  ]
}
```

- `version`: 내려받는 쪽이 변경을 감지하는 값. 데이터를 바꾸면 반드시 올린다.
- `validYears`: 데이터가 실제로 담고 있는 연도 범위 `[시작, 끝]`. 범위 밖 연도는 공휴일 없음으로 취급한다.
- `nameKey`: 표시 이름을 다국어로 바꾸기 위한 키. 키를 모르면 `name`을 그대로 쓴다.
- `isSubstitute`: 대체공휴일 여부.

## 갱신 방법

`scripts/update_holidays.py`가 공공데이터포털의 특일정보(한국천문연구원) 응답을 위 형식으로 변환한다.
인증키는 파일에 넣지 않고 환경변수로 받는다.

```bash
export DATA_GO_KR_KEY=발급받은키
python3 scripts/update_holidays.py --years 2023-2028            # 미리보기(변경점 출력)
python3 scripts/update_holidays.py --years 2023-2028 --write     # holidays/kr.json 갱신
```

바뀐 파일을 커밋하면 배포가 끝난다. raw 주소는 최대 5분간 이전 내용을 줄 수 있다.

## 출처

공공데이터포털 특일정보(한국천문연구원). 음력 기반 공휴일은 확정 고시가 된 연도까지만 제공된다.
