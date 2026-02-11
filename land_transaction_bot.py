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
        """웹사이트에서 HTML 가져오기

        성남시 정보목록 사이트는 테이블 데이터를 AJAX로 동적 로딩함.
        X-Requested-With: XMLHttpRequest 헤더를 포함해야 데이터가 포함된
        HTML 응답을 받을 수 있음.
        """

        params = {
            'menuIdx': self.menu_idx,
            'searchCondition': '1',
            'searchKeyword': search_keyword,
            'pageIndex': str(page_no),
        }

        # AJAX 요청 헤더 (핵심: 서버가 이 헤더를 보고 데이터 포함 여부 결정)
        ajax_headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'text/html, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }

        try:
            # 1차: AJAX POST (가장 유력한 방식)
            resp = self.session.post(
                self.base_url, data=params,
                headers=ajax_headers,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            html = resp.text
            if self._has_results(html):
                print(f"[*] AJAX POST 방식 성공 (page={page_no})")
                return html
            print(f"[!] AJAX POST: 데이터 없음 (응답 길이={len(html)})")

            # 2차: AJAX GET
            resp = self.session.get(
                self.base_url, params=params,
                headers=ajax_headers,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            html = resp.text
            if self._has_results(html):
                print(f"[*] AJAX GET 방식 성공 (page={page_no})")
                return html
            print(f"[!] AJAX GET: 데이터 없음 (응답 길이={len(html)})")

            # 3차: 일반 POST (AJAX 헤더 없이)
            resp = self.session.post(
                self.base_url, data=params,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            html = resp.text
            if self._has_results(html):
                print(f"[*] 일반 POST 방식 성공 (page={page_no})")
                return html

            # 4차: 일반 GET
            resp = self.session.get(
                self.base_url, params=params,
                timeout=15, verify=False
            )
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() != 'utf-8':
                resp.encoding = 'utf-8'
            html = resp.text
            if self._has_results(html):
                print(f"[*] 일반 GET 방식 성공 (page={page_no})")
                return html

            # 5차: 검색 파라미터명 변형 (searchWord)
            alt_params = {
                'menuIdx': self.menu_idx,
                'searchSelect': 'title',
                'searchWord': search_keyword,
                'pageIndex': str(page_no),
            }
            for method_name, req_func, req_kwargs in [
                ('대체파라미터 AJAX POST', self.session.post,
                 {'data': alt_params, 'headers': ajax_headers}),
                ('대체파라미터 GET', self.session.get,
                 {'params': alt_params}),
            ]:
                resp = req_func(
                    self.base_url, timeout=15, verify=False, **req_kwargs
                )
                resp.raise_for_status()
                if resp.encoding and resp.encoding.lower() != 'utf-8':
                    resp.encoding = 'utf-8'
                html = resp.text
                if self._has_results(html):
                    print(f"[*] {method_name} 방식 성공 (page={page_no})")
                    return html

            # 모든 방식 실패 시 마지막 응답 반환 (디버깅용)
            print(f"[!] 모든 요청 방식에서 데이터를 찾지 못함.")
            return resp.text

        except requests.exceptions.RequestException as e:
            print(f"[오류] 접속 실패: {e}")
            return None

    def _has_results(self, html: str) -> bool:
        """HTML에 실제 게시물 데이터가 있는지 체크"""
        # 날짜 패턴이 포함된 <td> 또는 날짜+제목이 있는 구조 탐지
        soup = BeautifulSoup(html, 'html.parser')

        # 방법 1: 테이블에 <td>가 있고 날짜 패턴이 포함된 행
        for row in soup.find_all('tr'):
            tds = row.find_all('td')
            if len(tds) >= 3:
                text = row.get_text()
                if re.search(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', text):
                    return True

        # 방법 2: div/span/dl 기반 목록에 날짜 패턴
        for selector in ['ul li a', 'div.list a', 'div.board a',
                         'dl dt a', 'div a']:
            for item in soup.select(selector):
                parent = item.parent
                if parent and re.search(
                    r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', parent.get_text()
                ):
                    return True

        # 방법 3: 전체 HTML에서 날짜 패턴 + <a> 태그 공존 여부
        # (AJAX 응답이 HTML fragment일 수 있음)
        has_dates = bool(re.search(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', html))
        has_links = bool(soup.find('a'))
        has_tds = bool(soup.find('td'))
        if has_dates and has_links and has_tds:
            return True

        return False

    def parse_results(self, html: str, target_dates: list) -> list:
        """HTML에서 게시물 제목과 날짜를 추출."""
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
                    continue

                row_text = row.get_text(strip=True)

                # 제목 추출
                title = ""
                link_tag = row.find('a')
                if link_tag:
                    title = link_tag.get_text(strip=True)
                elif col_map.get('title_idx') is not None and col_map['title_idx'] < len(cols):
                    title = cols[col_map['title_idx']].get_text(strip=True)
                else:
                    title = max(
                        [td.get_text(strip=True) for td in cols],
                        key=len, default=""
                    )

                # 날짜 추출
                date_text = ""
                for col in cols:
                    txt = col.get_text(strip=True)
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
            'ul li',
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
                break

        # ===== 방법 3: div 기반 파싱 (AJAX 응답 fragment 대응) =====
        if not found_items:
            # 날짜 패턴을 포함하는 모든 요소를 찾아서 근처 <a> 태그와 매칭
            for elem in soup.find_all(string=re.compile(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}')):
                date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', str(elem))
                if not date_match:
                    continue
                date_text = self._normalize_date(date_match.group(1))

                # 부모 요소에서 <a> 태그 검색
                parent = elem.parent
                for _ in range(5):  # 최대 5단계 상위까지 탐색
                    if parent is None:
                        break
                    link = parent.find('a')
                    if link:
                        title = link.get_text(strip=True)
                        if title and len(title) > 2:
                            found_items.append({
                                'title': title,
                                'date': date_text,
                                'raw': parent.get_text(strip=True)[:200]
                            })
                            break
                    parent = parent.parent

        # ===== 방법 4: 전체 HTML에서 정규식 추출 (최종 fallback) =====
        if not found_items:
            print("[!] 구조화된 파싱 실패. 전체 텍스트에서 정규식 추출 시도.")
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

        # ===== 중복 제거 =====
        seen = set()
        unique_items = []
        for item in found_items:
            key = (item['title'], item['date'])
            if key not in seen:
                seen.add(key)
                unique_items.append(item)

        # ===== 필터링: 날짜 매칭 =====
        results = []
        for item in unique_items:
            if item['date'] in target_dates:
                results.append(item)

        return results

    def _detect_column_mapping(self, header_row) -> dict:
        """헤더 행의 th 텍스트를 분석하여 제목/날짜 컬럼 인덱스 추정."""
        col_map = {'title_idx': None, 'date_idx': None}
        if not header_row:
            return col_map

        headers = header_row.find_all(['th', 'td'])
        for i, h in enumerate(headers):
            text = h.get_text(strip=True)
            if text in ('제목', '정보목록명', '내용', '결정서명칭', '제 목', '문서제목'):
                col_map['title_idx'] = i
            elif text in ('등록일', '작성일', '생성일', '날짜', '일자', '공개일', '게시일'):
                col_map['date_idx'] = i

        if col_map['title_idx'] is None:
            col_map['title_idx'] = 1

        return col_map

    def _normalize_date(self, date_str: str) -> str:
        """다양한 날짜 형식을 YYYY-MM-DD로 정규화."""
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

        # 세션 초기화: 메인 페이지 먼저 접속하여 쿠키/세션 설정
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

        # 디버깅 출력
        print(f"\n[DEBUG] HTML 길이: {len(html)}")
        print(f"[DEBUG] 'table' 태그 수: {html.lower().count('<table')}")
        print(f"[DEBUG] '<tr>' 태그 수: {html.lower().count('<tr')}")
        print(f"[DEBUG] '<td>' 태그 수: {html.lower().count('<td')}")

        if search_keyword in html:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 포함됨")
        else:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 없음")

        # 첫 번째 테이블 구조 출력
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table')
        if table:
            rows = table.find_all('tr')[:6]
            print(f"\n[DEBUG] 첫 번째 테이블 상위 {len(rows)}행:")
            for i, row in enumerate(rows):
                cells = row.find_all(['th', 'td'])
                cell_texts = [c.get_text(strip=True)[:40] for c in cells]
                print(f"  행{i}: {cell_texts}")

        # HTML 앞/뒷부분 출력
        print(f"\n[DEBUG] HTML 앞부분 500자:\n{html[:500]}")
        print(f"\n[DEBUG] HTML 뒷부분 500자:\n{html[-500:]}")

        # 파싱 실행
        results = self.parse_results(html, target_dates)

        if not results:
            print(f"[결과] 대상 날짜 {target_dates} 기준 신규 내역 없음.")
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
