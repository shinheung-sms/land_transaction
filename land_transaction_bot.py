import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import sys
import re
import os
import urllib3
from typing import Optional

# SSL 인증서 검증 경고 숨기기
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LandTransactionNotifier:
    def __init__(self, telegram_token: str, chat_id: str):
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        self.base_url = "https://www.seongnam.go.kr/infoList/infoList2.do"
        self.menu_idx = "1001802"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
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
        """
        어제 날짜뿐 아니라 최근 3일치 날짜를 반환.
        
        [근거] GitHub Actions cron은 UTC 기준이라 한국시간(KST=UTC+9)과 
        최대 1일 차이가 발생할 수 있음. 또한 주말/공휴일에는 게시가 
        안 될 수 있어 전날만 체크하면 누락됨.
        """
        dates = []
        for i in range(1, 4):  # 1일전 ~ 3일전
            d = datetime.now() - timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))
        # 오늘도 포함 (UTC/KST 시차 대응)
        dates.insert(0, datetime.now().strftime("%Y-%m-%d"))
        return dates

    def fetch_page(self, search_keyword: str, page_no: int = 1) -> Optional[str]:
        """웹사이트에서 HTML 가져오기"""
        
        # =====================================================================
        # [핵심 수정 1] GET과 POST 모두 시도
        # 
        # 공공기관 게시판은 GET/POST 방식이 혼용됨.
        # 기존 코드는 GET만 사용 → POST로 동작하는 경우 검색 결과가 안 나옴.
        # =====================================================================
        
        # GET 방식 파라미터
        get_params = {
            'menuIdx': self.menu_idx,
            'searchCondition': '1',
            'searchKeyword': search_keyword,
            'pageIndex': str(page_no),
        }
        
        try:
            # 1차: GET 방식 시도
            resp = self.session.get(
                self.base_url, params=get_params, 
                timeout=15, verify=False
            )
            resp.raise_for_status()
            
            # 인코딩 자동 감지 보정
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            
            html = resp.text
            
            # GET 결과에 실제 데이터가 있는지 확인
            if self._has_results(html):
                print(f"[*] GET 방식 성공 (page={page_no})")
                return html
            
            # 2차: POST 방식 시도
            post_data = {
                'menuIdx': self.menu_idx,
                'searchCondition': '1',
                'searchKeyword': search_keyword,
                'pageIndex': str(page_no),
                'returnURL': '/main.do',
            }
            resp = self.session.post(
                self.base_url, data=post_data,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            
            html = resp.text
            if self._has_results(html):
                print(f"[*] POST 방식 성공 (page={page_no})")
                return html
            
            # 3차: 검색 파라미터명 변형 시도 (searchWord 등)
            alt_params = {
                'menuIdx': self.menu_idx,
                'searchSelect': 'title',
                'searchWord': search_keyword,
                'pageIndex': str(page_no),
            }
            resp = self.session.get(
                self.base_url, params=alt_params,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            
            html = resp.text
            if self._has_results(html):
                print(f"[*] 대체 파라미터(searchWord) 방식 성공 (page={page_no})")
                return html
            
            # 결과가 없더라도 GET 응답은 반환 (파싱 시도용)
            print(f"[!] 검색 결과가 비어있을 수 있음. GET 응답 반환.")
            return resp.text
            
        except requests.exceptions.RequestException as e:
            print(f"[오류] 접속 실패: {e}")
            return None

    def _has_results(self, html: str) -> bool:
        """HTML에 실제 게시물 데이터가 있는지 간단 체크"""
        soup = BeautifulSoup(html, 'html.parser')
        # 테이블에 실제 데이터 행이 있는지
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            for row in rows:
                tds = row.find_all('td')
                if len(tds) >= 3:
                    text = row.get_text()
                    # 날짜 패턴이 포함된 행이 있으면 데이터 있음
                    if re.search(r'\d{4}[-./]\d{2}[-./]\d{2}', text):
                        return True
        
        # div/ul 기반 목록도 체크
        for item in soup.select('ul li a, div.list a, div.board a'):
            if re.search(r'\d{4}[-./]\d{2}[-./]\d{2}', item.parent.get_text()):
                return True
        
        return False

    def parse_results(self, html: str, target_dates: list) -> list:
        """
        HTML에서 게시물 제목과 날짜를 추출.
        
        [핵심 수정 2] 다양한 HTML 구조에 대응
        
        공공기관 게시판 구조는 크게 3가지:
        1) table > tbody > tr > td (전통적 게시판)
        2) ul > li (리스트형)
        3) div 기반 (카드형/모던)
        
        기존 코드는 1번만 지원하고 컬럼 인덱스가 고정이라
        실제 사이트 구조와 안 맞으면 데이터를 못 찾음.
        """
        soup = BeautifulSoup(html, 'html.parser')
        found_items = []

        # ===== 방법 1: 테이블 기반 파싱 (다양한 컬럼 구조 대응) =====
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            
            # 헤더 행에서 컬럼 매핑 파악
            header_row = table.find('tr')
            col_map = self._detect_column_mapping(header_row)
            
            for row in rows:
                cols = row.find_all('td')
                if not cols:
                    continue  # 헤더 행(th) 스킵
                
                # 전체 행 텍스트
                row_text = row.get_text(strip=True)
                
                # 제목 추출: <a> 태그 우선, 없으면 td 텍스트
                title = ""
                link_tag = row.find('a')
                if link_tag:
                    title = link_tag.get_text(strip=True)
                elif col_map.get('title_idx') is not None and col_map['title_idx'] < len(cols):
                    title = cols[col_map['title_idx']].get_text(strip=True)
                else:
                    # 가장 긴 텍스트를 가진 td를 제목으로 추정
                    title = max(
                        [td.get_text(strip=True) for td in cols],
                        key=len, default=""
                    )
                
                # 날짜 추출: 모든 td에서 날짜 패턴 검색
                date_text = ""
                for col in cols:
                    txt = col.get_text(strip=True)
                    # 다양한 날짜 형식 대응
                    date_match = re.search(
                        r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', txt
                    )
                    if date_match:
                        date_text = self._normalize_date(date_match.group(1))
                        break
                
                if title and date_text:
                    found_items.append({
                        'title': title,
                        'date': date_text,
                        'raw': row_text[:200]
                    })

        # ===== 방법 2: ul/li 기반 파싱 =====
        list_selectors = [
            'ul.board_list li', 'ul.list_type li', 'ul.info_list li',
            'div.board_list li', 'div.result_list li',
            'ul li',  # fallback
        ]
        for selector in list_selectors:
            items = soup.select(selector)
            if not items:
                continue
            for item in items:
                link = item.find('a')
                if not link:
                    continue
                title = link.get_text(strip=True)
                item_text = item.get_text()
                date_match = re.search(
                    r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', item_text
                )
                if date_match and title:
                    found_items.append({
                        'title': title,
                        'date': self._normalize_date(date_match.group(1)),
                        'raw': item_text.strip()[:200]
                    })
            if found_items:
                break  # 결과 있으면 다음 selector 스킵

        # ===== 방법 3: 전체 HTML에서 정규식 추출 (최종 fallback) =====
        if not found_items:
            print("[!] 테이블/리스트 파싱 실패. 전체 텍스트에서 정규식 추출 시도.")
            # 제목 패턴과 날짜가 같은 블록에 있는 경우
            text_blocks = re.split(r'\n{2,}|<br\s*/?>|</?(?:div|li|tr)[^>]*>', html)
            for block in text_blocks:
                clean = BeautifulSoup(block, 'html.parser').get_text(strip=True)
                date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', clean)
                if date_match and len(clean) > 10:
                    found_items.append({
                        'title': clean[:150],
                        'date': self._normalize_date(date_match.group(1)),
                        'raw': clean[:200]
                    })

        # ===== 필터링: 검색어 + 날짜 매칭 =====
        results = []
        for item in found_items:
            # 날짜 필터: target_dates 중 하나와 매칭
            if item['date'] not in target_dates:
                continue
            results.append(item)

        return results

    def _detect_column_mapping(self, header_row) -> dict:
        """
        헤더 행의 th 텍스트를 분석하여 제목/날짜 컬럼 인덱스 추정.
        
        [근거] 공공기관 게시판 헤더는 보통:
        번호 | 제목 | 부서 | 등록일(작성일/생성일) | 조회수
        """
        col_map = {'title_idx': None, 'date_idx': None}
        if not header_row:
            return col_map
        
        headers = header_row.find_all(['th', 'td'])
        for i, h in enumerate(headers):
            text = h.get_text(strip=True)
            if text in ('제목', '정보목록명', '내용', '결정서명칭', '제 목'):
                col_map['title_idx'] = i
            elif text in ('등록일', '작성일', '생성일', '날짜', '일자', '공개일', '게시일'):
                col_map['date_idx'] = i
        
        # 기본값: 제목은 2번째(idx 1), 날짜는 뒤쪽
        if col_map['title_idx'] is None:
            col_map['title_idx'] = 1
        
        return col_map

    def _normalize_date(self, date_str: str) -> str:
        """
        다양한 날짜 형식을 YYYY-MM-DD로 정규화.
        예: 2025.02.10 → 2025-02-10, 2025/2/10 → 2025-02-10
        """
        # 구분자 통일
        normalized = date_str.replace('.', '-').replace('/', '-')
        parts = normalized.split('-')
        if len(parts) == 3:
            year = parts[0]
            month = parts[1].zfill(2)
            day = parts[2].zfill(2)
            return f"{year}-{month}-{day}"
        return date_str

    def run(self, search_keyword: str):
        """메인 실행 로직"""
        target_dates = self.get_target_dates()
        print(f"[실행] 검색어: '{search_keyword}'")
        print(f"[실행] 대상 날짜: {target_dates}")

        # =====================================================================
        # [핵심 수정 3] 1페이지에서 세션 초기화 후 검색
        # 
        # 일부 공공기관 사이트는 메인 페이지 접속 없이 바로 검색하면 
        # 세션/쿠키 미설정으로 빈 결과를 반환함.
        # =====================================================================
        
        # 세션 초기화: 메인 페이지 먼저 접속
        try:
            init_url = f"{self.base_url}?menuIdx={self.menu_idx}"
            self.session.get(init_url, timeout=10, verify=False)
            print("[*] 세션 초기화 완료")
        except Exception as e:
            print(f"[!] 세션 초기화 실패 (계속 진행): {e}")
        
        # 검색 실행
        html = self.fetch_page(search_keyword)
        if not html:
            self.send_telegram_message(
                "⚠️ 성남시 정보목록 페이지 접속에 실패했습니다."
            )
            return

        # =====================================================================
        # [디버깅용] HTML 일부 출력 - 문제 진단에 필수
        # GitHub Actions 로그에서 확인 가능
        # =====================================================================
        print(f"\n[DEBUG] HTML 길이: {len(html)}")
        print(f"[DEBUG] 'table' 태그 수: {html.lower().count('<table')}")
        print(f"[DEBUG] '<tr>' 태그 수: {html.lower().count('<tr')}")
        print(f"[DEBUG] '<td>' 태그 수: {html.lower().count('<td')}")
        
        # 검색어가 HTML에 포함되어 있는지 확인
        if search_keyword in html:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 포함됨 ✓")
        else:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 없음 ✗")
            # 검색이 실제로 안 된 것일 수 있음
            print("[DEBUG] 검색 파라미터가 올바른지 확인 필요")
        
        # 첫 번째 테이블의 처음 5행 출력 (구조 파악용)
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table')
        if table:
            rows = table.find_all('tr')[:6]
            print(f"\n[DEBUG] 첫 번째 테이블 상위 {len(rows)}행:")
            for i, row in enumerate(rows):
                cells = row.find_all(['th', 'td'])
                cell_texts = [c.get_text(strip=True)[:40] for c in cells]
                print(f"  행{i}: {cell_texts}")
        else:
            print("[DEBUG] <table> 태그를 찾을 수 없음!")
            # div/ul 구조 확인
            lists = soup.find_all('ul')
            print(f"[DEBUG] <ul> 태그 수: {len(lists)}")
            for ul in lists[:3]:
                items = ul.find_all('li')[:3]
                for li in items:
                    print(f"  li: {li.get_text(strip=True)[:80]}")
        
        # HTML 앞부분 500자 출력 (전체 구조 파악)
        print(f"\n[DEBUG] HTML 앞부분 500자:\n{html[:500]}")
        print(f"\n[DEBUG] HTML 뒷부분 500자:\n{html[-500:]}")
        
        # 파싱 실행
        results = self.parse_results(html, target_dates)

        if not results:
            print(f"[결과] 대상 날짜 {target_dates} 기준 신규 내역 없음.")
            # 모든 파싱 결과 (날짜 무관) 출력하여 진단
            all_results = self.parse_results(html, 
                [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
            )
            if all_results:
                print(f"[DEBUG] 최근 30일 기준으로는 {len(all_results)}건 발견:")
                for r in all_results[:5]:
                    print(f"  - [{r['date']}] {r['title'][:80]}")
            else:
                print("[DEBUG] 30일 범위로도 결과 없음. HTML 구조 자체가 다를 가능성 높음.")
            return

        # 결과 전송
        found_count = 0
        for item in results:
            message = (
                f"🚨 <b>토지거래허가 신규 내역 감지</b>\n\n"
                f"📅 <b>일자:</b> {item['date']}\n"
                f"📄 <b>내용:</b> {item['title']}\n\n"
                f"🔗 <a href='{self.base_url}?menuIdx={self.menu_idx}'>게시판 바로가기</a>"
            )
            self.send_telegram_message(message)
            found_count += 1

        print(f"[결과] 총 {found_count}건의 알림을 전송했습니다.")


if __name__ == "__main__":
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', "YOUR_BOT_TOKEN_HERE")
    CHAT_ID = os.getenv('CHAT_ID', "YOUR_CHAT_ID_HERE")
    SEARCH_QUERY = "중앙동 3001"

    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("오류: 텔레그램 봇 토큰을 설정해주세요.")
        sys.exit(1)

    scraper = LandTransactionNotifier(TELEGRAM_TOKEN, CHAT_ID)
    scraper.run(SEARCH_QUERY)
