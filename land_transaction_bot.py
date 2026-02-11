import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import sys
import time
import os

class LandTransactionNotifier:
    def __init__(self, telegram_token: str, chat_id: str):
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        # 성남시 정보공개 목록 URL (menuIdx=1001802: 국토의 계획 및 이용에 관한 법률 관련 정보로 추정)
        self.base_url = "https://www.seongnam.go.kr/infoList/infoList2.do"
        self.menu_idx = "1001802"

    def send_telegram_message(self, message: str):
        """텔레그램 메시지 전송"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            print(f"[전송 성공] {message[:20]}...")
        except Exception as e:
            print(f"[전송 실패] {e}")

    def get_target_date(self) -> str:
        """어제 날짜를 YYYY-MM-DD 형식으로 반환"""
        yesterday = datetime.now() - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d")

    def fetch_and_parse(self, search_keyword: str):
        """웹사이트에서 데이터 조회 및 파싱"""
        target_date = self.get_target_date()
        print(f"[실행] 검색어: '{search_keyword}', 기준일자(어제): {target_date}")

        # 검색 파라미터 구성
        params = {
            'menuIdx': self.menu_idx,
            'searchCondition': '1',  # 1: 제목, 2: 내용 (일반적인 패턴)
            'searchKeyword': search_keyword
        }

        try:
            # 타임아웃 10초 설정
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[오류] 접속 실패: {e}")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 게시판 테이블 구조 파싱 (일반적인 공공기관 게시판 구조: table > tbody > tr)
        # 실제 사이트 구조에 따라 class나 구조가 다를 수 있으므로 일반적인 table 태그 타겟팅
        table = soup.find('table')
        if not table:
            print("[오류] 게시판 테이블을 찾을 수 없습니다.")
            return

        rows = table.find_all('tr')
        found_count = 0

        for row in rows:
            cols = row.find_all('td')
            # 일반적인 구조: 번호, 제목, 부서, 등록일, 조회수 등
            # 데이터가 없는 헤더 행 등은 스킵
            if len(cols) < 4: 
                continue

            # 제목 추출 (보통 2번째 컬럼에 제목과 링크가 있음)
            title_cell = cols[1]
            title_text = title_cell.get_text(strip=True)
            
            # 날짜 추출 (보통 4번째 혹은 5번째 컬럼에 위치, YYYY-MM-DD 형식)
            # 성남시청 게시판 구조를 확인하여 인덱스 조정 필요. 통상적으로 뒤쪽에 위치.
            date_text = ""
            for col in cols:
                txt = col.get_text(strip=True)
                # 날짜 형식(202X-XX-XX)인지 확인
                if len(txt) == 10 and txt.count('-') == 2 and txt.replace('-', '').isdigit():
                    date_text = txt
                    break
            
            # 데이터 검증: 날짜가 어제 날짜와 일치하는지 확인
            if date_text == target_date:
                # 제목에서 필요한 정보가 포함되어 있는지 확인 (검색어로 필터링되었으나 재확인)
                if search_keyword.replace(" ", "") in title_text.replace(" ", ""):
                    message = (
                        f"🚨 <b>토지거래허가 신규 내역 감지</b>\n\n"
                        f"📅 <b>일자:</b> {date_text}\n"
                        f"📄 <b>내용:</b> {title_text}\n\n"
                        f"🔗 <a href='{self.base_url}?menuIdx={self.menu_idx}'>게시판 바로가기</a>"
                    )
                    self.send_telegram_message(message)
                    found_count += 1

        if found_count == 0:
            print(f"[결과] {target_date} 기준 신규 내역 없음.")
        else:
            print(f"[결과] 총 {found_count}건의 알림을 전송했습니다.")

if __name__ == "__main__":
    # --- 설정 영역 ---
    # 환경변수에서 우선적으로 값을 가져오고, 없으면 하드코딩된 값을 사용
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', "YOUR_BOT_TOKEN_HERE")
    CHAT_ID = os.getenv('CHAT_ID', "YOUR_CHAT_ID_HERE")
    SEARCH_QUERY = "중앙동 3001"
    
    # 설정값 검증
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("오류: 텔레그램 봇 토큰을 설정해주세요.")
        sys.exit(1)

    scraper = LandTransactionNotifier(TELEGRAM_TOKEN, CHAT_ID)
    scraper.fetch_and_parse(SEARCH_QUERY)