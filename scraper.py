import os
import time
import random
import re
import threading
from urllib.parse import urljoin, urlparse, parse_qs
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LocalBusinessScraper:
    def __init__(
        self,
        search_queue=None,
        location=None,
        headless=False,
        phone_prefix="",
        visit_website=True,
        parallel=True,
        concurrency=3,
        max_leads_per_task=0,
        size_filter="all",
        tasks=None
    ):
        if tasks:
            self.search_queue = tasks
        elif search_queue:
            self.search_queue = search_queue
        else:
            loc = location or "Delhi, India"
            self.search_queue = [{"keyword": "zym", "location": loc, "country": "India", "phone_prefix": phone_prefix}]

        self.headless = headless
        self.visit_website = visit_website
        self.parallel = parallel
        self.concurrency = max(1, int(concurrency or 3))
        self.max_leads_per_task = int(max_leads_per_task or 0)
        self.size_filter = (size_filter or "all").strip().lower()
        self.stop_requested = False

        self.results = []
        self.with_website = []
        self.without_website = []
        self.tasks_meta = []

        self.lock = threading.Lock()
        self.semaphore = threading.Semaphore(self.concurrency)

        for idx, t in enumerate(self.search_queue, start=1):
            loc_str = t.get("location", "Delhi, India")
            loc_parts = [p.strip() for p in loc_str.split(',') if p.strip()]
            country_str = t.get("country") or (loc_parts[-1] if loc_parts else "India")

            self.tasks_meta.append({
                "task_id": idx,
                "keyword": t.get("keyword", "zym"),
                "location": loc_str,
                "country": country_str,
                "phone_prefix": t.get("phone_prefix", phone_prefix),
                "max_leads": int(t.get("max_leads", self.max_leads_per_task) or 0),
                "visit_website": t.get("visit_website", self.visit_website),
                "size_filter": t.get("size_filter", self.size_filter),
                "status": "pending",
                "count": 0,
                "with_website_count": 0,
                "without_website_count": 0,
                "results": [],
                "with_website_results": [],
                "without_website_results": []
            })

        self.desktop_user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
        ]

    def request_stop(self):
        print("\n[!] STOP REQUEST RECEIVED! Stopping all tasks...")
        self.stop_requested = True

    def request_stop_task(self, task_id):
        for t in self.tasks_meta:
            if t["task_id"] == int(task_id):
                t["status"] = "stopped"
                print(f"\n[!] Stop requested for Task #{task_id} ({t['keyword']} in {t['location']})")

    def skip_current_Running_task(self):
        for t in self.tasks_meta:
            if t["status"] == "running":
                t["status"] = "stopped"
                print(f"\n[!] Skipping current running Task #{t['task_id']}")
                break

    def get_random_ua(self):
        return random.choice(self.desktop_user_agents)

    def clean_google_url(self, url):
        if not url or url == "N/A":
            return "N/A"
        if "google.com/url?" in url:
            parsed = parse_qs(urlparse(url).query)
            if 'q' in parsed:
                return parsed['q'][0]
        return url

    def get_default_country_code(self, country_str):
        mapping = {
            "india": "+91", "paraguay": "+595", "united states": "+1", "usa": "+1",
            "united kingdom": "+44", "uk": "+44", "united arab emirates": "+971",
            "uae": "+971", "spain": "+34", "bolivia": "+591", "brazil": "+55",
            "canada": "+1", "australia": "+61", "germany": "+49", "france": "+33",
            "italy": "+39", "saudi arabia": "+966", "singapore": "+65", "mexico": "+52",
            "argentina": "+54", "chile": "+56", "colombia": "+57", "peru": "+51"
        }
        key = (country_str or "").strip().lower()
        for k, code in mapping.items():
            if k in key:
                return code
        return ""

    def format_phone_with_country_code(self, phone_str, country_name):
        if not phone_str or phone_str == "N/A":
            return "N/A"
        cleaned = re.sub(r'[^\d\+]', '', phone_str).strip()
        if not cleaned or len(re.sub(r'\D', '', cleaned)) < 7:
            return "N/A"
        if cleaned.startswith('+'):
            return cleaned
        code = self.get_default_country_code(country_name)
        if code:
            if cleaned.startswith('0'):
                cleaned = cleaned.lstrip('0')
            raw_code = code.replace('+', '')
            if not cleaned.startswith(raw_code):
                return f"{code}{cleaned}"
            else:
                return f"+{cleaned}"
        return cleaned

    def matches_phone_prefix(self, phone_str, prefix_filter):
        if not prefix_filter:
            return True
        if not phone_str or phone_str == "N/A":
            return False
        norm_phone = re.sub(r'[^\d\+]', '', phone_str)
        digits_only = re.sub(r'\D', '', phone_str)
        prefixes = [p.strip() for p in prefix_filter.split(',') if p.strip()]
        for pref in prefixes:
            norm_pref = re.sub(r'[^\d\+]', '', pref)
            digits_pref = re.sub(r'\D', '', pref)
            if norm_pref and norm_phone.startswith(norm_pref):
                return True
            if digits_pref and digits_only.startswith(digits_pref):
                return True
        return False

    def classify_business_size(self, name, category, reviews_count, has_website, web_scale_score=0):
        """
        Determines whether a business (e.g., Hospital, Gym, Store, Company) is Large, Medium, or Small
        based on Google Reviews count, Name/Category keywords, Website presence, and Website scale signals.
        """
        text_check = f"{name} {category}".lower()

        large_keywords = [
            'multi-specialty', 'multispecialty', 'super speciality', 'superspeciality',
            'medical center', 'medical centre', 'research centre', 'research center',
            'institute', 'university', 'foundation', 'group', 'chain', 'pvt ltd',
            'private limited', 'limited', 'ltd', 'corp', 'corporation', 'international',
            'global', 'worldwide', 'apollo', 'fortis', 'max ', 'medanta', 'manipal',
            'decathlon', 'gold\'s gym', 'anytime fitness', 'cult.fit', 'resort',
            'palace', 'plaza', 'mall', 'hypermarket', 'supermarket', 'headquarters', 'hq'
        ]
        small_keywords = [
            'clinic', 'dispensary', 'poly clinic', 'polyclinic', 'cabin', 'kiosk',
            'stall', 'corner', 'parlour', 'parlor', 'boutique', 'tailor', 'repair',
            'general store', 'kirana', 'medical store', 'chemist'
        ]

        is_large_kw = any(k in text_check for k in large_keywords)
        is_small_kw = any(k in text_check for k in small_keywords)

        # Scoring logic
        score = 0
        if reviews_count >= 300:
            score += 4
        elif reviews_count >= 120:
            score += 3
        elif reviews_count >= 35:
            score += 2
        elif reviews_count >= 10:
            score += 1

        if has_website:
            score += 1
        score += web_scale_score

        if is_large_kw:
            score += 2
        if is_small_kw and reviews_count < 150:
            score -= 1

        if score >= 4 or reviews_count >= 250:
            return "Large"
        elif score >= 2 or reviews_count >= 40:
            return "Medium"
        else:
            return "Small"

    def matches_size_filter(self, detected_size, size_filter):
        sf = (size_filter or "all").lower()
        ds = (detected_size or "Small").lower()
        if sf in ("all", ""):
            return True
        if sf == "small_medium":
            return ds in ("small", "medium")
        if sf == "medium_large":
            return ds in ("medium", "large")
        return ds == sf

    def dismiss_popups(self, page):
        try:
            page.evaluate('''() => {
                const elements = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
                for (const el of elements) {
                    const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (['go back to web', 'keep using web', 'stay on web', 'use the web version', 'no thanks', 'reject all', 'accept all', 'i agree'].includes(txt)) {
                        el.click();
                        return txt;
                    }
                }
                return null;
            }''')
        except:
            pass

    def extract_contacts_via_new_tab(self, context, url, country_name):
        found_emails = []
        found_phones = []
        found_socials = []
        web_scale_score = 0
        new_tab = None

        try:
            new_tab = context.new_page()
            new_tab.set_default_timeout(18000)

            try:
                new_tab.goto(url, wait_until="domcontentloaded", timeout=12000)
                time.sleep(0.8)
            except:
                try:
                    new_tab.goto(url, timeout=9000)
                except:
                    pass

            # Smoothly slide/scroll down to load lazy footers & contact widgets
            try:
                for _ in range(3):
                    new_tab.evaluate("window.scrollBy(0, Math.max(800, document.body.scrollHeight / 3))")
                    time.sleep(0.5)
            except:
                pass

            html_content = ""
            body_text = ""
            try:
                html_content = new_tab.content()
                body_text = new_tab.evaluate("document.body.innerText || ''")
            except:
                pass

            def parse_text_and_html(h_text, b_text):
                nonlocal web_scale_score
                combined = (h_text or "") + "\n" + (b_text or "")
                low_body = (b_text or "").lower()

                # Check scale indicators on website (beds, branches, doctors, locations)
                scale_terms = ['branches', 'our locations', 'beds', 'icu', '24/7 emergency', 'specialists', 'our doctors', 'franchise', 'global presence', 'iso 9001']
                hits = sum(1 for term in scale_terms if term in low_body)
                if hits >= 2:
                    web_scale_score = max(web_scale_score, 2)
                elif hits == 1:
                    web_scale_score = max(web_scale_score, 1)

                # Extract Clean Emails
                for e in re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', combined):
                    e_low = e.lower()
                    if not e_low.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.woff')) and not any(j in e_low for j in ['sentry.io', 'wixpress', 'example.com', 'domain.com', 'email.com', 'yoursite.com']):
                        if e_low not in found_emails:
                            found_emails.append(e_low)

                # Extract Phone Numbers from text & format with Country Code
                phone_patterns = re.findall(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,5}[-.\s]?\d{4,6}', b_text or "")
                for p in phone_patterns:
                    digits_only = re.sub(r'\D', '', p)
                    if 8 <= len(digits_only) <= 14:
                        formatted_p = self.format_phone_with_country_code(p, country_name)
                        if formatted_p != "N/A" and formatted_p not in found_phones:
                            found_phones.append(formatted_p)

                soup = BeautifulSoup(h_text or "", 'html.parser')
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    href_low = href.lower()

                    if href_low.startswith('mailto:'):
                        mail_val = href[7:].split('?')[0].strip().lower()
                        if mail_val and '@' in mail_val and mail_val not in found_emails:
                            found_emails.append(mail_val)

                    elif href_low.startswith('tel:'):
                        tel_val = self.format_phone_with_country_code(href[4:], country_name)
                        if tel_val != "N/A" and tel_val not in found_phones:
                            found_phones.append(tel_val)

                    elif any(s in href_low for s in ['wa.me/', 'api.whatsapp.com', 'instagram.com/', 'facebook.com/', 'linkedin.com/']):
                        if not any(ignore in href_low for ignore in ['sharer', 'share?', 'intent/']):
                            clean_soc = href.split('?')[0].rstrip('/')
                            if clean_soc not in found_socials:
                                found_socials.append(clean_soc)

                        # Extract WhatsApp number from wa.me link if present
                        wa_match = re.search(r'(?:wa\.me/|phone=)(\+?\d{8,14})', href_low)
                        if wa_match:
                            wa_num = self.format_phone_with_country_code(wa_match.group(1), country_name)
                            if wa_num != "N/A" and wa_num not in found_phones:
                                found_phones.append(wa_num)

            if html_content:
                parse_text_and_html(html_content, body_text)

            # Find and navigate to "Contact Us" or "About Us" pages
            soup = BeautifulSoup(html_content, 'html.parser') if html_content else None
            if soup:
                sub_links = []
                for a in soup.find_all('a', href=True):
                    href = a['href'].strip()
                    href_low = href.lower()
                    text = a.get_text().lower()
                    if any(k in href_low or k in text for k in ['contact', 'about', 'reach', 'support', 'touch', 'contacto', 'nosotros', 'ubicacion']):
                        sub_link = urljoin(url, href)
                        if sub_link.startswith('http') and urlparse(sub_link).netloc == urlparse(url).netloc:
                            if sub_link not in sub_links and sub_link != url:
                                sub_links.append(sub_link)

                for sub_link in sub_links[:2]:
                    try:
                        new_tab.goto(sub_link, wait_until="domcontentloaded", timeout=9000)
                        time.sleep(0.7)
                        new_tab.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                        time.sleep(0.5)
                        sub_html = new_tab.content()
                        sub_text = new_tab.evaluate("document.body.innerText || ''")
                        parse_text_and_html(sub_html, sub_text)
                    except:
                        pass

        except Exception:
            pass
        finally:
            if new_tab:
                try:
                    new_tab.close()
                except:
                    pass

        return {
            "emails": ", ".join(found_emails[:3]) if found_emails else "N/A",
            "phones": ", ".join(found_phones[:3]) if found_phones else "N/A",
            "socials": ", ".join(found_socials[:2]) if found_socials else "N/A",
            "scale_score": web_scale_score
        }

    def extract_single_card(self, page, context, card, idx_num, task_meta):
        try:
            if self.stop_requested or task_meta["status"] == "stopped":
                return

            self.dismiss_popups(page)
            card.scroll_into_view_if_needed()
            time.sleep(0.2)

            card_name = "N/A"
            try:
                card_name = card.locator('.fontHeadlineSmall').first.inner_text(timeout=1500).strip()
            except:
                try:
                    lbl = card.locator('a.hfpxzc').first.get_attribute('aria-label')
                    if lbl:
                        card_name = lbl.strip()
                except:
                    pass

            prev_h1 = ""
            try:
                if page.locator('h1.DUwDvf').count() > 0:
                    prev_h1 = page.locator('h1.DUwDvf').first.inner_text(timeout=400).strip()
            except:
                pass

            link_target = card.locator('a.hfpxzc').first
            if link_target.count() > 0:
                link_target.click(force=True)
            else:
                card.click(force=True)

            for _ in range(12):
                if self.stop_requested or task_meta["status"] == "stopped":
                    return
                time.sleep(0.25)
                try:
                    panel_h1 = page.locator('h1.DUwDvf').first.inner_text(timeout=500).strip()
                    if panel_h1 and (panel_h1 != prev_h1 or panel_h1.lower() == card_name.lower()):
                        break
                except:
                    pass

            details = page.evaluate('''(fallbackName) => {
                let name = fallbackName || "N/A";
                const h1 = document.querySelector('h1.DUwDvf');
                if (h1 && h1.innerText) name = h1.innerText.trim();

                let phone = "N/A";
                const phoneBtn = document.querySelector('[data-item-id^="phone:tel:"]');
                if (phoneBtn) {
                    let attr = phoneBtn.getAttribute('data-item-id') || '';
                    phone = attr.replace('phone:tel:', '').trim();
                }
                if (!phone || phone === "N/A") {
                    const phoneAria = document.querySelector('[aria-label^="Phone:"], [aria-label*="Phone:"]');
                    if (phoneAria) {
                        phone = (phoneAria.getAttribute('aria-label') || '').replace(/Phone:/i, '').trim();
                    }
                }

                let website = "N/A";
                const webBtn = document.querySelector('a[data-item-id="authority"]');
                if (webBtn && webBtn.href) {
                    website = webBtn.href.trim();
                }

                let address = "N/A";
                const addrBtn = document.querySelector('[data-item-id="address"], button[aria-label^="Address:"]');
                if (addrBtn) {
                    let aria = addrBtn.getAttribute('aria-label');
                    if (aria) {
                        address = aria.replace(/Address:/i, '').trim();
                    } else if (addrBtn.innerText) {
                        address = addrBtn.innerText.trim();
                    }
                }

                // Extract Rating and Reviews Count for Business Size Calculation
                let rating = "N/A";
                let reviewsCount = 0;
                const ratingBox = document.querySelector('div.F7nice');
                if (ratingBox && ratingBox.innerText) {
                    const txt = ratingBox.innerText;
                    const rMatch = txt.match(/(\\d+[.,]\\d+)/);
                    if (rMatch) rating = rMatch[1];
                    const revMatch = txt.match(/\\(([\\d,]+)\\)/);
                    if (revMatch) {
                        reviewsCount = parseInt(revMatch[1].replace(/,/g, ''), 10) || 0;
                    }
                }
                if (reviewsCount === 0) {
                    const revSpan = document.querySelector('span[aria-label*="reviews"], button[aria-label*="reviews"]');
                    if (revSpan) {
                        const lbl = revSpan.getAttribute('aria-label') || '';
                        const m = lbl.match(/([\\d,]+)\\s*review/i);
                        if (m) reviewsCount = parseInt(m[1].replace(/,/g, ''), 10) || 0;
                    }
                }

                return { name, phone, website, address, rating, reviewsCount };
            }''', card_name)

            final_name = details.get("name") if details.get("name") != "N/A" else card_name
            raw_maps_phone = details.get("phone", "N/A").strip()
            google_maps_phone = self.format_phone_with_country_code(raw_maps_phone, task_meta["country"])

            if not self.matches_phone_prefix(google_maps_phone, task_meta["phone_prefix"]):
                return

            final_website = self.clean_google_url(details.get("website", "N/A"))
            final_address = re.sub(r'^[^\w\d]+', '', details.get("address", "N/A")).strip() or "N/A"
            rating_val = details.get("rating", "N/A")
            reviews_count = int(details.get("reviewsCount", 0) or 0)

            has_website = (final_website != "N/A" and final_website.startswith("http"))
            website_extracted_phone = "N/A"
            email = "N/A"
            web_scale_score = 0

            visit_web = task_meta.get("visit_website", True)
            if has_website and visit_web and not self.stop_requested and task_meta["status"] != "stopped":
                print(f"    [Task #{task_meta['task_id']}] Visiting Website & Contact Pages: {final_name} -> {final_website[:40]}...")
                web_info = self.extract_contacts_via_new_tab(context, final_website, task_meta["country"])
                email = web_info.get("emails", "N/A")
                website_extracted_phone = web_info.get("phones", "N/A")
                web_scale_score = web_info.get("scale_score", 0)
            else:
                final_website = "N/A" if not has_website else final_website

            # Determine Business Size (Large / Medium / Small)
            business_size = self.classify_business_size(
                final_name,
                task_meta["keyword"],
                reviews_count,
                has_website,
                web_scale_score
            )

            # Filter by size if user selected a specific size filter
            if not self.matches_size_filter(business_size, task_meta.get("size_filter", "all")):
                print(f"    [-] [SKIPPED - Size Mismatch ({business_size})] {final_name}")
                return

            target_label = f"{task_meta['keyword']} in {task_meta['location']}"
            item_data = {
                "Task ID": task_meta["task_id"],
                "Search Target": target_label,
                "Category": task_meta["keyword"],
                "Location": task_meta["location"],
                "Business Name": final_name,
                "Business Size": business_size,
                "Reviews": reviews_count,
                "Rating": rating_val,
                "Google Maps Phone": google_maps_phone,
                "Website Extracted Phone": website_extracted_phone,
                "Email": email,
                "Website": final_website,
                "Has Website": "Yes" if has_website else "No",
                "Address": final_address
            }

            with self.lock:
                self.results.append(item_data)
                task_meta["results"].append(item_data)
                task_meta["count"] += 1
                if has_website:
                    self.with_website.append(item_data)
                    task_meta["with_website_count"] += 1
                    task_meta["with_website_results"].append(item_data)
                else:
                    self.without_website.append(item_data)
                    task_meta["without_website_count"] += 1
                    task_meta["without_website_results"].append(item_data)

                print(f"    [+] [{business_size.upper()} | {reviews_count} rev] {final_name} | Maps: {google_maps_phone} | Web: {website_extracted_phone}")

        except Exception:
            pass

    def scrape_single_task(self, task_meta):
        with self.semaphore:
            if self.stop_requested or task_meta["status"] == "stopped":
                task_meta["status"] = "stopped"
                return

            task_meta["status"] = "running"
            query = f"{task_meta['keyword']} in {task_meta['location']}"
            max_leads = task_meta.get("max_leads", 0)

            with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = browser.new_context(user_agent=self.get_random_ua(), viewport={'width': 1280, 'height': 720})
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()

                processed_hrefs = set()
                search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}?hl=en"

                try:
                    page.goto(search_url, timeout=45000)
                except:
                    task_meta["status"] = "completed"
                    return

                time.sleep(2.0)
                self.dismiss_popups(page)

                try:
                    page.wait_for_selector('div[role="feed"]', timeout=8000)
                except:
                    pass

                stagnant = 0
                while not self.stop_requested and task_meta["status"] == "running":
                    self.dismiss_popups(page)
                    cards = page.locator('div.Nv2PK').all()
                    new_found = 0

                    for card in cards:
                        if self.stop_requested or task_meta["status"] == "stopped":
                            break
                        if max_leads > 0 and task_meta["count"] >= max_leads:
                            task_meta["status"] = "completed"
                            break

                        card_id = None
                        try:
                            link_el = card.locator('a.hfpxzc').first
                            if link_el.count() > 0:
                                card_id = link_el.get_attribute('href') or link_el.get_attribute('aria-label')
                        except:
                            pass

                        if not card_id:
                            try:
                                card_id = card.locator('.fontHeadlineSmall').first.inner_text(timeout=800).strip()
                            except:
                                continue

                        if not card_id or card_id in processed_hrefs:
                            continue

                        processed_hrefs.add(card_id)
                        new_found += 1
                        self.extract_single_card(page, context, card, task_meta["count"] + 1, task_meta)

                    if max_leads > 0 and task_meta["count"] >= max_leads:
                        break

                    try:
                        page.evaluate('const feed = document.querySelector(\'div[role="feed"]\'); if (feed) feed.scrollBy(0, 3500);')
                    except:
                        pass

                    time.sleep(1.5)
                    if new_found == 0:
                        stagnant += 1
                        if stagnant >= 6:
                            break
                    else:
                        stagnant = 0

                try:
                    browser.close()
                except:
                    pass

            if task_meta["status"] != "stopped":
                task_meta["status"] = "completed"

    def search_google_maps(self):
        threads = []
        for t in self.tasks_meta:
            if self.parallel:
                th = threading.Thread(target=self.scrape_single_task, args=(t,))
                threads.append(th)
                th.start()
            else:
                self.scrape_single_task(t)

        if self.parallel:
            for th in threads:
                th.join()

        return {
            "all": self.results,
            "with_website": self.with_website,
            "without_website": self.without_website,
            "stopped": self.stop_requested,
            "tasks_meta": self.tasks_meta
        }

    def get_tasks_meta_progress(self):
        running_count = sum(1 for t in self.tasks_meta if t["status"] == "running")
        return {
            "running": running_count > 0 or any(t["status"] == "pending" for t in self.tasks_meta),
            "tasks": self.tasks_meta,
            "active_tasks_count": running_count
        }

    def _export_excel_and_pdf(self, rows, base_filepath, report_title, header_color):
        clean_rows = rows if rows else [{
            "Search Target": "N/A",
            "Business Name": "No Leads Found",
            "Business Size": "N/A",
            "Reviews": 0,
            "Google Maps Phone": "N/A",
            "Website Extracted Phone": "N/A",
            "Email": "N/A",
            "Website": "N/A",
            "Address": "N/A"
        }]

        # Save Excel (.xlsx)
        pd.DataFrame(clean_rows).to_excel(f"{base_filepath}.xlsx", index=False)

        # Save PDF (.pdf)
        pdf_path = f"{base_filepath}.pdf"
        doc = SimpleDocTemplate(pdf_path, pagesize=landscape(letter), leftMargin=15, rightMargin=15, topMargin=18, bottomMargin=18)
        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=12, textColor=colors.HexColor('#1e293b'), spaceAfter=8)
        cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontSize=7, leading=9, wordWrap='CJK')

        elements.append(Paragraph(f"{report_title} ({len(rows)} Leads)", title_style))
        elements.append(Spacer(1, 4))

        table_data = [["#", "Target", "Business Name", "Size (Reviews)", "Maps Phone", "Web Phone", "Email", "Website"]]
        for idx, r in enumerate(clean_rows, start=1):
            size_str = f"{r.get('Business Size', 'N/A')} ({r.get('Reviews', 0)})"
            table_data.append([
                str(idx),
                Paragraph(str(r.get("Search Target", "N/A")), cell_style),
                Paragraph(str(r.get("Business Name", "N/A")), cell_style),
                Paragraph(size_str, cell_style),
                Paragraph(str(r.get("Google Maps Phone", "N/A")), cell_style),
                Paragraph(str(r.get("Website Extracted Phone", "N/A")), cell_style),
                Paragraph(str(r.get("Email", "N/A")), cell_style),
                Paragraph(str(r.get("Website", "N/A")), cell_style),
            ])

        t = Table(table_data, colWidths=[20, 95, 135, 65, 80, 85, 115, 155], repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(header_color)),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 7.5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ]))
        elements.append(t)
        doc.build(elements)

    def generate_reports(self, base_dir):
        # 1. Master Combined Reports (Excel + PDF)
        self._export_excel_and_pdf(self.results, os.path.join(base_dir, "leads_all"), "Master Report: All Scraped Businesses", "#4f46e5")
        self._export_excel_and_pdf(self.with_website, os.path.join(base_dir, "leads_with_website"), "Master Report: Businesses WITH Website", "#059669")
        self._export_excel_and_pdf(self.without_website, os.path.join(base_dir, "leads_without_website"), "Master Report: Businesses WITHOUT Website", "#e11d48")

        # 2. Individual Per-Task Reports (Excel + PDF for every single queued business & country)
        for t in self.tasks_meta:
            tid = t["task_id"]
            label = f"{t['keyword']} in {t['location']}"
            self._export_excel_and_pdf(
                t["results"],
                os.path.join(base_dir, f"task_{tid}_all"),
                f"Target #{tid} All Leads: {label}",
                "#4f46e5"
            )
            self._export_excel_and_pdf(
                t["with_website_results"],
                os.path.join(base_dir, f"task_{tid}_with_website"),
                f"Target #{tid} WITH Website: {label}",
                "#059669"
            )
            self._export_excel_and_pdf(
                t["without_website_results"],
                os.path.join(base_dir, f"task_{tid}_without_website"),
                f"Target #{tid} WITHOUT Website: {label}",
                "#e11d48"
            )
