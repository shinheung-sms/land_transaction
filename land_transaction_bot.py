import requests
import json
import re
import os
import sys
import time
import random
import urllib3
from datetime import datetime, timedelta
from html import escape as html_escape
from urllib.parse import quote

# SSL 인증서 검증 경고 숨기기
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LandTransactionNotifier:
    """성남시 토지거래허가 정보를 open.go.kr 검색 API로 조회하여 텔레그램 알림."""

    # 정보공개포털 전문검색(FTR) API
    SEARCH_API_URL = "https://www.open.go.kr/search/service.do"
    # 성남시 기관코드
    GVRNMAPCD = "3780000"

    def __init__(self, telegram_token: str, chat_id: str):
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
            'Referer': 'https://www.seongnam.go.kr/',
        })

    def send_telegram_message(self, message: str):
        """텔레그램 메시지 전송"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            print(f"[전송 성공] {message[:50]}...")
        except Exception as e:
            print(f"[전송 실패] {e}")

    def get_target_dates(self) -> list:
        """오늘 + 최근 3일치 날짜를 반환 (YYYY-MM-DD 형식)."""
        dates = []
        for i in range(1, 4):
            d = datetime.now() - timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))
        dates.insert(0, datetime.now().strftime("%Y-%m-%d"))
        return dates

    def _build_search_payload(self, keyword: str, from_date: str, to_date: str,
                              offset: int = 1, count: int = 10) -> dict:
        """open.go.kr 검색 API용 JSON 페이로드 생성.

        Args:
            keyword: 검색어 (예: "중앙동 3001")
            from_date: 검색 시작일 (YYYY.MM.DD)
            to_date: 검색 종료일 (YYYY.MM.DD)
            offset: 페이지 오프셋 (1부터)
            count: 페이지당 결과 수
        """
        return {
            "version": "1.1",
            "service": "FTR",
            "module": "GvrnService",
            "func": "getResult",
            "firstCall": False,
            "param": {
                "collection": "PDINFOLIST",
                "index": "PDINFOLIST_@yyyy_Q@",
                "keyword": keyword,
                "date": {
                    "period": "",
                    "field": "REGDATE",
                    "from": from_date,
                    "to": to_date
                },
                "sort": [
                    {"field": "REGDATE", "asc": "false"},
                    {"field": "[SCORE]", "asc": "false"}
                ],
                "page": {
                    "offset": offset,
                    "count": count
                },
                "fields": {
                    "returnFields": "IDX_KEY/CLFPATH/DLSRCD/REGDATE/DEPTCD/"
                                    "DSCLASCD/GVRNCD/GVRNNM/GVRNPATH/DEPTNM/"
                                    "UNTBSNM/URTXT_YN",
                    "mergedFields": "",
                    "mergedReturnCharCount": 60,
                    "returnHighlightFields": "TITLE",
                    "facet": None
                },
                "option": {
                    "operator": "AND",
                    "saveTotalcount": 1
                },
                "custom": {
                    "pubopen": "true",
                    "GVRNMAPCD": self.GVRNMAPCD
                },
                "debug": False
            }
        }

    def fetch_results(self, keyword: str, from_date: str, to_date: str) -> list:
        """open.go.kr 검색 API를 호출하여 결과 목록을 반환.

        Returns:
            결과 항목 리스트. 각 항목은 dict (TITLE, REGDATE, DEPTNM 등).
            실패 시 빈 리스트.
        """
        payload = self._build_search_payload(keyword, from_date, to_date)
        json_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        encoded_payload = quote(json_str, safe='')

        # JSONP callback ID 생성
        cb_id = f"jQuery{random.randint(10**16, 10**17 - 1)}_{int(time.time() * 1000)}"
        ts = str(int(time.time() * 1000))

        # URL 직접 구성 (JSONP 형식: callback=...&{encoded_json}&_=timestamp)
        url = f"{self.SEARCH_API_URL}?callback={cb_id}&{encoded_payload}&_={ts}"

        try:
            resp = self.session.get(url, timeout=15, verify=False)
            resp.raise_for_status()
            text = resp.text

            # JSONP 래퍼 제거: jQuery...({"resultData": ...})
            json_match = re.search(r'\((\{.*\})\)\s*$', text, re.DOTALL)
            if not json_match:
                print(f"[오류] JSONP 파싱 실패. 응답 앞부분: {text[:200]}")
                return []

            data = json.loads(json_match.group(1))
            result_data = data.get('resultData', {})
            items = result_data.get('data', [])
            info = result_data.get('info', {})

            total = info.get('totalcount', 0)
            print(f"[*] 검색 결과: 총 {total}건 (현재 페이지 {len(items)}건)")

            return items

        except requests.exceptions.RequestException as e:
            print(f"[오류] API 호출 실패: {e}")
            return []
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[오류] 응답 파싱 실패: {e}")
            return []

    def _parse_regdate(self, regdate: str) -> str:
        """REGDATE(YYYYMMDDHHmmss) → YYYY-MM-DD 변환."""
        if len(regdate) >= 8:
            return f"{regdate[:4]}-{regdate[4:6]}-{regdate[6:8]}"
        return regdate

    def run(self, search_keyword: str):
        """메인 실행 로직"""
        target_dates = self.get_target_dates()
        print(f"[실행] 검색어: '{search_keyword}'")
        print(f"[실행] 대상 날짜: {target_dates}")

        # 검색 기간: 최근 31일 (여유 있게)
        to_date = datetime.now().strftime("%Y.%m.%d")
        from_date = (datetime.now() - timedelta(days=31)).strftime("%Y.%m.%d")
        print(f"[실행] 검색 기간: {from_date} ~ {to_date}")

        # API 호출
        items = self.fetch_results(search_keyword, from_date, to_date)

        if not items:
            print("[결과] 검색 결과 없음.")
            return

        # 전체 결과 출력 (디버깅용)
        print(f"\n[DEBUG] 전체 {len(items)}건:")
        for item in items:
            date_str = self._parse_regdate(item.get('REGDATE', ''))
            title = re.sub(r'<[^>]+>', '', item.get('TITLE', ''))
            print(f"  - [{date_str}] {title}")

        # 대상 날짜 필터링
        results = []
        for item in items:
            date_str = self._parse_regdate(item.get('REGDATE', ''))
            if date_str in target_dates:
                results.append(item)

        if not results:
            print(f"\n[결과] 대상 날짜 {target_dates} 기준 신규 내역 없음.")
            return

        # 결과 전송
        found_count = 0
        for item in results:
            date_str = self._parse_regdate(item.get('REGDATE', ''))
            # HTML 하이라이트 태그 제거 후 특수문자 이스케이프
            title = re.sub(r'<[^>]+>', '', item.get('TITLE', ''))
            title = html_escape(title)
            org_path = html_escape(item.get('GVRNPATH', ''))

            message = (
                f"🚨 <b>토지거래허가 신규 내역 감지</b>\n\n"
                f"📅 <b>일자:</b> {date_str}\n"
                f"📄 <b>내용:</b> {title}\n"
                f"🏢 <b>부서:</b> {org_path}\n\n"
                f"🔗 <a href='https://www.seongnam.go.kr/infoList/"
                f"infoList2.do?menuIdx=1001802'>게시판 바로가기</a>"
            )
            self.send_telegram_message(message)
            found_count += 1

        print(f"\n[결과] 총 {found_count}건의 알림을 전송했습니다.")


if __name__ == "__main__":
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', "YOUR_BOT_TOKEN_HERE")
    CHAT_ID = os.getenv('CHAT_ID', "YOUR_CHAT_ID_HERE")
    SEARCH_QUERY = "중앙동 3001"

    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("오류: 텔레그램 봇 토큰을 설정해주세요.")
        sys.exit(1)

    scraper = LandTransactionNotifier(TELEGRAM_TOKEN, CHAT_ID)
    scraper.run(SEARCH_QUERY)
