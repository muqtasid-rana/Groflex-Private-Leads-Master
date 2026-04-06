import os
import re
import time
import datetime
from urllib.parse import urlparse, quote_plus
import requests
from bs4 import BeautifulSoup
import pandas as pd
from playwright.sync_api import sync_playwright

def get_today_query_and_location(search_plan_file):
    """
    Reads the 'Search Plan' Excel file based on today's date.
    Returns: query, city, country, niche
    """
    if not os.path.exists(search_plan_file):
        print(f"Error: Could not find {search_plan_file}")
        return None, None, None, None

    try:
        df = pd.read_excel(search_plan_file)
    except Exception as e:
        print(f"Error reading {search_plan_file}: {e}")
        return None, None, None, None
        
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    day_of_month = str(datetime.datetime.now().day)
    
    # Fill NA values with empty strings
    df = df.fillna('')
    
    matched_row = None
    
    # Try finding exact date match:
    for index, row in df.iterrows():
        # Handle datetime objects or strings
        cell_date = str(row.get('Date', '')).split(' ')[0]
        if cell_date == today_str:
            matched_row = row
            break
            
    # Fallback to Day match
    if matched_row is None:
        for index, row in df.iterrows():
            if str(row.get('Day', '')).strip() == day_of_month:
                matched_row = row
                break
                
    # Fallback to first row
    if matched_row is None and not df.empty:
        matched_row = df.iloc[0]
        
    if matched_row is not None:
        return extract_query_info(matched_row)
        
    return None, None, None, None

def extract_query_info(row):
    query = str(row.get('Search Query', '')).strip()
    country = str(row.get('Country', '')).strip()
    city = str(row.get('City', '')).strip()
    niche = str(row.get('Niche', '')).strip()
    
    if not query:
        query = f"{niche} in {city}, {country}".strip(', ')
        
    return query, city, country, niche

def extract_emails_and_location(url, fallback_city, fallback_country):
    """
    Connects to the website using requests and bs4 to find emails.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return set(), fallback_city, fallback_country
            
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text(separator=' ')
        
        email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
        emails = set(re.findall(email_pattern, page_text))
        
        phone_pattern = r'\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}'
        raw_phones = re.findall(phone_pattern, page_text)
        phones = set()
        for p in raw_phones:
            clean_p = re.sub(r'[^\d+]', '', p)
            if 10 <= len(clean_p) <= 15:
                phones.add(p.strip())
        
        for a in soup.find_all('a', href=True):
            if a['href'].startswith('mailto:'):
                email = a['href'].replace('mailto:', '').split('?')[0].strip()
                if re.match(email_pattern, email):
                    emails.add(email)
                    
        valid_emails = set()
        for e in emails:
            e_lower = e.lower().strip()
            if any(e_lower.startswith(prefix) for prefix in ['noreply', 'no-reply', 'donotreply']):
                continue
            if e_lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg', '.webp')):
                continue
            valid_emails.add(e_lower)
            
        found_city = fallback_city
        found_country = fallback_country
        
        return valid_emails, phones, found_city, found_country
        
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return set(), set(), fallback_city, fallback_country

def run_scraper():
    source_file = os.environ.get('SOURCE_SHEET_NAME', 'SearchPlan.xlsx')
    target_file = os.environ.get('TARGET_SHEET_NAME', 'Leads.xlsx')

    query, q_city, q_country, q_niche = get_today_query_and_location(source_file)
    if not query:
        print("No search query found.")
        return
        
    print(f"Executing search for: {query}")
        
    leads_found = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='en-US')
        page = context.new_page()
        
        # Go directly to search URL
        safe_query = quote_plus(query)
        page.goto(f"https://www.google.com/maps/search/{safe_query}", timeout=60000)
        
        # Try a quick bypass if Google shows a GDPR consent screen
        try:
            reject_btn = page.locator('button:has-text("Reject all")')
            accept_btn = page.locator('button:has-text("Accept all")')
            if reject_btn.is_visible(timeout=3000):
                reject_btn.click()
            elif accept_btn.is_visible(timeout=1000):
                accept_btn.click()
        except:
            pass
        
        # Wait for either results list or a single result
        try:
            page.wait_for_selector('a[href^="https://www.google.com/maps/place/"]', timeout=30000)
        except Exception:
            print("No results found or page took too long.")
            browser.close()
            return
            
        time.sleep(3)
        
        businesses = []
        previous_count = 0
        scroll_attempts = 0
        
        while len(businesses) < 150 and scroll_attempts < 15:
            elements = page.query_selector_all('a[href^="https://www.google.com/maps/place/"]')
            
            for el in elements:
                name = el.get_attribute('aria-label')
                href = el.get_attribute('href')
                if name and not any(b['href'] == href for b in businesses):
                    businesses.append({"name": name, "href": href})
            
            if len(businesses) == previous_count:
                page.mouse.wheel(0, 5000)
                time.sleep(2)
                scroll_attempts += 1
            else:
                previous_count = len(businesses)
                scroll_attempts = 0
                
        print(f"Found {len(businesses)} potential businesses on maps.")
        
        for i, b in enumerate(businesses):
            if len(leads_found) >= 50:
                print("Reached 50 leads. Stopping.")
                break
                
            name = b['name']
            print(f"[{i+1}/{len(businesses)}] Checking {name}...")
            
            try:
                page.goto(b['href'])
                page.wait_for_selector('h1', timeout=8000)
                time.sleep(1.5)
            except Exception:
                continue
                
            website_href = None
            try:
                web_link = page.query_selector('a[data-item-id="authority"]')
                if web_link:
                    website_href = web_link.get_attribute('href')
            except:
                pass
                
            if not website_href:
                try:
                    links = page.query_selector_all('a[href^="http"]')
                    for link in links:
                        h = link.get_attribute('href')
                        if 'google.com' not in h:
                            website_href = h
                            break
                except:
                    pass
                    
            if not website_href:
                print(f"   -> No website found, skipping.")
                continue
                
            city = q_city
            country = q_country
            try:
                addr_btn = page.query_selector('button[data-item-id="address"]')
                if addr_btn:
                    addr_text = addr_btn.inner_text()
                    parts = [p.strip() for p in addr_text.split(',')]
                    if len(parts) >= 2:
                        if not country:
                            country = parts[-1]
                        if not city:
                           city_state_zip = parts[-2]
                           city = re.sub(r'[\d]+', '', city_state_zip).strip()
            except:
                pass
               
            emails, phones, city, country = extract_emails_and_location(website_href, city, country)
            
            if not emails:
                print(f"   -> No emails found on {website_href}, skipping.")
                continue
                
            email_list = ", ".join(list(emails))
            phone_list = ", ".join(list(phones)[:3])
            city = city if city else "Unknown"
            country = country if country else "Unknown"
            niche = q_niche if q_niche else "Unknown"
            
            print(f"   => Lead Found! [{email_list}] | Phones: [{phone_list}]")
            lead_data = {
                "Business Name": name, 
                "Website": website_href, 
                "Email": email_list, 
                "Phone": phone_list,
                "City": city, 
                "Country": country, 
                "Niche": niche,
                "Date Found": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            leads_found.append(lead_data)
                
        browser.close()
        
    print(f"Total leads gathered today: {len(leads_found)}")
    
    if leads_found:
        new_leads_df = pd.DataFrame(leads_found)
        
        # Append to Excel file
        if os.path.exists(target_file):
            try:
                existing_df = pd.read_excel(target_file)
                combined_df = pd.concat([existing_df, new_leads_df], ignore_index=True)
            except Exception as e:
                print(f"Could not read existing file, making a new one: {e}")
                combined_df = new_leads_df
        else:
            combined_df = new_leads_df
            
        combined_df.to_excel(target_file, index=False)
        print(f"Successfully written to {target_file}.")

if __name__ == "__main__":
    run_scraper()
