import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import sys
import re
import os
import urllib3
from typing import Optional
from urllib.parse import urljoin

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
        """오늘 + 최근 3일치 날짜를 반환."""
        dates = []
        for i in range(1, 4):
            d = datetime.now() - timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))
        dates.insert(0, datetime.now().strftime("%Y-%m-%d"))
        return dates

    def _discover_form_info(self, html: str) -> dict:
        """HTML에서 검색 폼의 필드명, action URL, AJAX 엔드포인트를 추출한다.

        성남시 정보목록 페이지는 JavaScript로 테이블 데이터를 동적 로딩하므로,
        실제 데이터를 가져오려면 페이지 내 JavaScript가 호출하는 엔드포인트와
        폼 파라미터명을 정확히 파악해야 한다.
        """
        soup = BeautifulSoup(html, 'html.parser')

        info = {
            'forms': [],       # [{action, method, fields: {name: value}}]
            'ajax_urls': [],   # JS에서 발견된 AJAX 엔드포인트
            'js_functions': [],  # JS 함수 중 검색 관련
        }

        # ── 1. 모든 <form> 태그 분석 ──
        for form in soup.find_all('form'):
            action = form.get('action', '')
            method = form.get('method', 'get').lower()
            form_id = form.get('id', '') or form.get('name', '')
            fields = {}

            for inp in form.find_all(['input', 'select', 'textarea']):
                name = inp.get('name')
                if not name:
                    continue
                value = inp.get('value', '')
                # radio/checkbox는 checked인 것만
                inp_type = inp.get('type', 'text').lower()
                if inp_type in ('radio', 'checkbox') and not inp.get('checked'):
                    continue
                fields[name] = value

            info['forms'].append({
                'id': form_id,
                'action': action,
                'method': method,
                'fields': fields,
            })

        # ── 2. <script> 태그에서 URL과 함수 추출 ──
        for script in soup.find_all('script'):
            text = script.string or ''
            if not text.strip():
                continue

            # .do로 끝나는 URL 패턴 추출
            urls = re.findall(
                r'["\']([^"\']*?(?:infoList|list|ajax|search|data)[^"\']*?\.do[^"\']*)["\']',
                text, re.IGNORECASE
            )
            info['ajax_urls'].extend(urls)

            # form.action = '...' 패턴
            action_urls = re.findall(
                r'\.action\s*=\s*["\']([^"\']+)["\']', text
            )
            info['ajax_urls'].extend(action_urls)

            # $.ajax/$.post/$.get 호출의 URL
            jquery_urls = re.findall(
                r'\$\.(?:ajax|post|get)\s*\(\s*["\']([^"\']+)["\']', text
            )
            info['ajax_urls'].extend(jquery_urls)

            # url: '...' (AJAX 설정 내)
            ajax_config_urls = re.findall(
                r'url\s*:\s*["\']([^"\']+\.do[^"\']*)["\']', text
            )
            info['ajax_urls'].extend(ajax_config_urls)

            # 검색 관련 함수 추출 (fnSearch, search, goSearch 등)
            search_funcs = re.findall(
                r'function\s+((?:fn|go|do)?[Ss]earch\w*)\s*\([^)]*\)\s*\{([^}]{0,500})',
                text
            )
            for fname, fbody in search_funcs:
                info['js_functions'].append({
                    'name': fname,
                    'body': fbody.strip()[:300]
                })

            # 목록/페이징 관련 함수
            list_funcs = re.findall(
                r'function\s+((?:fn|go|do)?(?:[Ll]ist|[Pp]age|[Dd]ata)\w*)\s*\([^)]*\)\s*\{([^}]{0,500})',
                text
            )
            for fname, fbody in list_funcs:
                info['js_functions'].append({
                    'name': fname,
                    'body': fbody.strip()[:300]
                })

        # URL 중복 제거
        info['ajax_urls'] = list(set(info['ajax_urls']))
        return info

    def fetch_page(self, search_keyword: str, page_no: int = 1) -> Optional[str]:
        """웹사이트에서 검색 결과 HTML 가져오기.

        1단계: 초기 페이지를 로드하여 폼 구조와 AJAX 엔드포인트를 파악
        2단계: 파악된 정보를 바탕으로 실제 데이터 요청
        """

        try:
            # ── 1단계: 초기 페이지 로드 + 폼/JS 분석 ──
            init_resp = self.session.get(
                self.base_url,
                params={'menuIdx': self.menu_idx},
                timeout=15, verify=False
            )
            init_resp.raise_for_status()
            if init_resp.encoding and init_resp.encoding.lower() != 'utf-8':
                init_resp.encoding = 'utf-8'
            init_html = init_resp.text

            form_info = self._discover_form_info(init_html)

            # 디버깅: 발견된 폼/AJAX 정보 출력
            for i, form in enumerate(form_info['forms']):
                print(f"[DEBUG] 폼#{i} id='{form['id']}' "
                      f"action='{form['action']}' method='{form['method']}'")
                print(f"[DEBUG]   필드: {list(form['fields'].keys())}")
                if form['fields']:
                    for k, v in form['fields'].items():
                        if v:
                            print(f"[DEBUG]     {k} = '{v[:50]}'")
            if form_info['ajax_urls']:
                print(f"[DEBUG] 발견된 AJAX URL: {form_info['ajax_urls']}")
            for fn in form_info['js_functions']:
                print(f"[DEBUG] JS 함수: {fn['name']}() → {fn['body'][:100]}")

            # ── 2단계: 발견된 폼 데이터로 요청 시도 ──

            # 시도할 요청 목록 구성
            attempts = []

            # (A) 폼 기반 요청 - 각 폼의 필드를 사용
            for form in form_info['forms']:
                if not form['fields']:
                    continue
                form_data = dict(form['fields'])

                # 검색어를 올바른 필드에 설정
                # 필드명에서 문서제목/검색어 관련 필드 찾기
                keyword_set = False
                for field_name in form_data:
                    fl = field_name.lower()
                    if any(kw in fl for kw in [
                        'title', 'keyword', 'word', 'query',
                        'doc', 'search', 'nm'
                    ]):
                        # text input 필드에만 검색어 설정
                        form_data[field_name] = search_keyword
                        keyword_set = True

                if not keyword_set:
                    form_data['searchKeyword'] = search_keyword

                form_data['pageIndex'] = str(page_no)

                # 날짜 범위 설정 (최근 30일)
                end_date = datetime.now().strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
                for field_name in form_data:
                    fl = field_name.lower()
                    if 'start' in fl or 'from' in fl or 'begin' in fl:
                        form_data[field_name] = start_date
                    elif 'end' in fl or 'to' in fl:
                        form_data[field_name] = end_date

                action_url = form['action'] or self.base_url
                if not action_url.startswith('http'):
                    action_url = urljoin(self.base_url, action_url)

                method = form['method']
                attempts.append((
                    f"폼#{form['id']} {method.upper()}",
                    action_url, method, form_data
                ))

            # (B) 발견된 AJAX URL에 폼 데이터로 요청
            for ajax_url in form_info['ajax_urls']:
                if not ajax_url.startswith('http'):
                    ajax_url = urljoin(self.base_url, ajax_url)
                # 기본 파라미터 세트
                base_params = {
                    'menuIdx': self.menu_idx,
                    'searchKeyword': search_keyword,
                    'searchCondition': '1',
                    'pageIndex': str(page_no),
                }
                attempts.append((
                    f"AJAX URL: {ajax_url}",
                    ajax_url, 'post', base_params
                ))

            # (C) 기존 폼 기반 fallback (원래 파라미터)
            fallback_params = {
                'menuIdx': self.menu_idx,
                'searchCondition': '1',
                'searchKeyword': search_keyword,
                'pageIndex': str(page_no),
            }
            attempts.append((
                "기본 POST",
                self.base_url, 'post', fallback_params
            ))
            attempts.append((
                "기본 GET",
                self.base_url, 'get', fallback_params
            ))

            # ── 각 시도 실행 ──
            for attempt_name, url, method, data in attempts:
                try:
                    if method == 'post':
                        resp = self.session.post(
                            url, data=data, timeout=15, verify=False
                        )
                    else:
                        resp = self.session.get(
                            url, params=data, timeout=15, verify=False
                        )
                    resp.raise_for_status()
                    if resp.encoding and resp.encoding.lower() != 'utf-8':
                        resp.encoding = 'utf-8'
                    html = resp.text

                    if self._has_results(html):
                        print(f"[*] '{attempt_name}' 방식 성공 (page={page_no})")
                        return html
                    else:
                        tr_count = html.lower().count('<tr')
                        td_count = html.lower().count('<td')
                        print(f"[!] '{attempt_name}': 데이터 없음 "
                              f"(길이={len(html)}, tr={tr_count}, td={td_count})")
                except Exception as e:
                    print(f"[!] '{attempt_name}' 실패: {e}")

            # 모든 방식 실패 시 초기 페이지 반환 (디버깅용)
            print(f"[!] 모든 요청 방식에서 데이터를 찾지 못함.")
            return init_html

        except requests.exceptions.RequestException as e:
            print(f"[오류] 접속 실패: {e}")
            return None

    def _has_results(self, html: str) -> bool:
        """HTML에 실제 게시물 데이터가 있는지 체크"""
        soup = BeautifulSoup(html, 'html.parser')

        # 테이블에 <td>가 있고 날짜 패턴이 포함된 행
        for row in soup.find_all('tr'):
            tds = row.find_all('td')
            if len(tds) >= 3:
                text = row.get_text()
                if re.search(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', text):
                    return True

        # div/ul 기반 목록에 날짜 패턴
        for selector in ['ul li a', 'div.list a', 'div.board a',
                         'dl dt a', 'div a']:
            for item in soup.select(selector):
                parent = item.parent
                if parent and re.search(
                    r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', parent.get_text()
                ):
                    return True

        # HTML fragment: <td>와 날짜가 함께 존재
        has_tds = bool(soup.find('td'))
        has_dates = bool(re.search(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', html))
        has_links = bool(soup.find('a'))
        if has_dates and has_links and has_tds:
            return True

        return False

    def parse_results(self, html: str, target_dates: list) -> list:
        """HTML에서 게시물 제목과 날짜를 추출."""
        soup = BeautifulSoup(html, 'html.parser')
        found_items = []

        # ===== 방법 1: 테이블 기반 파싱 =====
        tables = soup.find_all('table')
        for table in tables:
            header_row = table.find('tr')
            col_map = self._detect_column_mapping(header_row)

            for row in table.find_all('tr'):
                cols = row.find_all('td')
                if not cols:
                    continue

                row_text = row.get_text(strip=True)

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

                date_text = ""
                for col in cols:
                    txt = col.get_text(strip=True)
                    date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', txt)
                    if date_match:
                        date_text = self._normalize_date(date_match.group(1))
                        break

                if title and date_text:
                    found_items.append({
                        'title': title,
                        'date': date_text,
                        'raw': row_text[:200]
                    })

        # ===== 방법 2: ul/li 기반 =====
        if not found_items:
            for selector in [
                'ul.board_list li', 'ul.list_type li', 'ul.info_list li',
                'div.board_list li', 'div.result_list li', 'ul li',
            ]:
                items = soup.select(selector)
                for item in items:
                    link = item.find('a')
                    if not link:
                        continue
                    title = link.get_text(strip=True)
                    item_text = item.get_text()
                    date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', item_text)
                    if date_match and title:
                        found_items.append({
                            'title': title,
                            'date': self._normalize_date(date_match.group(1)),
                            'raw': item_text.strip()[:200]
                        })
                if found_items:
                    break

        # ===== 방법 3: 날짜 텍스트 노드에서 부모 탐색 =====
        if not found_items:
            for elem in soup.find_all(string=re.compile(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}')):
                date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', str(elem))
                if not date_match:
                    continue
                date_text = self._normalize_date(date_match.group(1))
                parent = elem.parent
                for _ in range(5):
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

        # ===== 방법 4: 정규식 fallback =====
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

        # 중복 제거 + 날짜 필터링
        seen = set()
        results = []
        for item in found_items:
            key = (item['title'], item['date'])
            if key in seen:
                continue
            seen.add(key)
            if item['date'] in target_dates:
                results.append(item)

        return results

    def _detect_column_mapping(self, header_row) -> dict:
        """헤더 행에서 제목/날짜 컬럼 인덱스 추정."""
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

        # 세션 초기화
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
        print(f"\n[DEBUG] 최종 HTML 길이: {len(html)}")
        print(f"[DEBUG] '<tr>' 태그 수: {html.lower().count('<tr')}")
        print(f"[DEBUG] '<td>' 태그 수: {html.lower().count('<td')}")

        if search_keyword in html:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 포함됨")
        else:
            print(f"[DEBUG] 검색어 '{search_keyword}'가 응답 HTML에 없음")

        # 테이블 구조 출력
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table')
        if table:
            rows = table.find_all('tr')[:6]
            print(f"\n[DEBUG] 첫 번째 테이블 상위 {len(rows)}행:")
            for i, row in enumerate(rows):
                cells = row.find_all(['th', 'td'])
                cell_texts = [c.get_text(strip=True)[:40] for c in cells]
                print(f"  행{i}: {cell_texts}")

        print(f"\n[DEBUG] HTML 앞부분 500자:\n{html[:500]}")
        print(f"\n[DEBUG] HTML 뒷부분 500자:\n{html[-500:]}")

        # 파싱 실행
        results = self.parse_results(html, target_dates)

        if not results:
            print(f"[결과] 대상 날짜 {target_dates} 기준 신규 내역 없음.")
            all_results = self.parse_results(html,
                [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(30)]
            )
            if all_results:
                print(f"[DEBUG] 최근 30일 기준으로는 {len(all_results)}건 발견:")
                for r in all_results[:5]:
                    print(f"  - [{r['date']}] {r['title'][:80]}")
            else:
                print("[DEBUG] 30일 범위로도 결과 없음.")
            return

        # 결과 전송
        found_count = 0
        for item in results:
            message = (
                f"🚨 <b>토지거래허가 신규 내역 감지</b>\n\n"
                f"📅 <b>일자:</b> {item['date']}\n"
                f"📄 <b>내용:</b> {item['title']}\n\n"
                f"🔗 <a href='{self.base_url}?menuIdx={self.menu_idx}'>"
                f"게시판 바로가기</a>"
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
