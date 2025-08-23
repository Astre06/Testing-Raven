import os
import re
import json
import zipfile
import tempfile
import shutil

# Try to import rarfile, if not available, skip RAR support
try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False
    print("rarfile not installed. RAR support disabled. Install with: pip install rarfile")


# --- HELPER FUNCTION FOR ROBUST JSON FINDING ---
def find_all_json_objects(text: str) -> list:
    """
    Finds and decodes all valid JSON objects (dictionaries) from a string,
    even if they are not in a list or are surrounded by other text.
    """
    found_objects = []
    pos = 0
    while pos < len(text):
        start_brace = text.find('{', pos)
        if start_brace == -1:
            break

        brace_level = 1
        end_brace = -1
        for i in range(start_brace + 1, len(text)):
            char = text[i]
            if char == '{':
                brace_level += 1
            elif char == '}':
                brace_level -= 1
                if brace_level == 0:
                    end_brace = i
                    break
        
        if end_brace != -1:
            potential_json = text[start_brace : end_brace + 1]
            try:
                decoded_obj = json.loads(potential_json)
                if isinstance(decoded_obj, dict):
                    found_objects.append(decoded_obj)
            except json.JSONDecodeError:
                pass
            pos = end_brace + 1
        else:
            break
            
    return found_objects


# --- ARCHIVE EXTRACTION ---
def extract_archive(archive_path, extract_to):
    """
    Extracts ZIP or RAR files to a temporary directory.
    Returns the extraction directory path or None if failed.
    """
    try:
        file_ext = os.path.splitext(archive_path)[1].lower()
        
        if file_ext == '.zip':
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                zip_ref.extractall(extract_to)
            return extract_to
        elif file_ext == '.rar' and RAR_SUPPORT:
            with rarfile.RarFile(archive_path, 'r') as rar_ref:
                rar_ref.extractall(extract_to)
            return extract_to
        else:
            return None
    except Exception as e:
        print(f"Error extracting {archive_path}: {e}")
        return None


def find_all_txt_files(directory):
    """
    Recursively finds all .txt files in a directory and subdirectories.
    """
    txt_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.lower().endswith('.txt'):
                txt_files.append(os.path.join(root, file))
    return txt_files


# --- DETECTION ---
def detect_cookie_type(file_path: str) -> str:
    """
    Detects cookie type by scanning the whole file.
    Priority:
        1. Netscape if any line starts with '.'
        2. JSON if any line starts with '[' or '{'
        3. NetflixId if any line contains 'NetflixId='
        4. Unknown otherwise
    """
    try:
        has_netscape = False
        has_json = False
        has_netflixid = False

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if line.startswith("."):      # strongest rule
                    return "netscape"
                elif line.startswith("[") or line.startswith("{"):
                    has_json = True
                elif "NetflixId=" in line:
                    has_netflixid = True

        if has_json:
            return "json"
        if has_netflixid:
            return "netflix_id"
    except Exception as e:
        print(f"Error detecting cookie type for {file_path}: {e}")

    return "unknown"


# --- CLEANING FUNCTIONS ---
def clean_netflix_id(input_path, output_dir):
    """Create 1 folder with 1 notepad.txt, each line = NetflixId cookie"""
    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, "notepad.txt")

    count = 0
    with open(input_path, 'r', encoding='utf-8', errors='ignore') as infile, \
         open(out_file, 'w', encoding='utf-8') as outfile:
        for line in infile:
            if "NetflixId=" in line:
                cookie_parts = []
                netflix_id = re.search(r'NetflixId=([^;]+)', line)
                secure_id = re.search(r'SecureNetflixId=([^;]+)', line)
                nfvdid = re.search(r'nfvdid=([^;]+)', line)

                if netflix_id: cookie_parts.append(netflix_id.group(0))
                if secure_id: cookie_parts.append(secure_id.group(0))
                if nfvdid: cookie_parts.append(nfvdid.group(0))

                if cookie_parts:
                    outfile.write("; ".join(cookie_parts) + "\n")
                    count += 1
    return count


def clean_netscape(input_path, output_dir):
    """Create subfolder and split each .cookie group into separate file"""
    os.makedirs(output_dir, exist_ok=True)

    count = 0
    with open(input_path, 'r', encoding='utf-8', errors='ignore') as infile:
        lines = [l.strip() for l in infile]

    group = []
    for line in lines:
        if line.startswith("."):
            group.append(line)
        else:
            if group:
                count += 1
                out_file = os.path.join(output_dir, f"cookie{count}.txt")
                with open(out_file, 'w', encoding='utf-8') as f:
                    f.write("# Netscape HTTP Cookie File\n")
                    f.write("\n".join(group) + "\n")
                group = []
    if group:
        count += 1
        out_file = os.path.join(output_dir, f"cookie{count}.txt")
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("\n".join(group) + "\n")
    return count


def clean_json(input_path, output_dir):
    """Create subfolder and split JSON objects into cookie1.txt, cookie2.txt..."""
    os.makedirs(output_dir, exist_ok=True)

    count = 0
    with open(input_path, 'r', encoding='utf-8', errors='ignore') as infile:
        content = infile.read()

    objects = find_all_json_objects(content)

    for obj in objects:
        if "netflix.com" in obj.get("domain", ""):
            count += 1
            out_file = os.path.join(output_dir, f"cookie{count}.txt")
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(obj, f, indent=4)
    return count


# --- CLEAN WRAPPER PER FILE ---
def clean_file_by_type(input_path, root_output_dir):
    """
    Detects cookie type and cleans accordingly into structured subfolders.
    Returns path of subfolder created.
    """
    ctype = detect_cookie_type(input_path)
    print(f"Detected cleaning format: {ctype}")  # helpful debug

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    sub_output = os.path.join(root_output_dir, base_name)
    os.makedirs(sub_output, exist_ok=True)

    if ctype == "netflix_id":
        clean_netflix_id(input_path, sub_output)
    elif ctype == "netscape":
        clean_netscape(input_path, sub_output)
    elif ctype == "json":
        clean_json(input_path, sub_output)
    else:
        clean_netflix_id(input_path, sub_output)

    return sub_output


# --- UNIVERSAL ENTRY POINT ---
def universal_clean_input(input_path: str) -> str:
    """
    Cleans a single file or archive into root folder with structured subfolders.
    Returns the root folder path containing all cleaned files.
    """
    input_dir = os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    root_output_dir = os.path.join(input_dir, f"{base_name}_CLEANED")
    os.makedirs(root_output_dir, exist_ok=True)

    file_ext = os.path.splitext(input_path)[1].lower()

    if file_ext in [".zip", ".rar"]:
        temp_dir = tempfile.mkdtemp()
        extracted_dir = extract_archive(input_path, temp_dir)
        if extracted_dir:
            txt_files = find_all_txt_files(extracted_dir)
            for f in txt_files:
                clean_file_by_type(f, root_output_dir)
        shutil.rmtree(temp_dir)
    else:
        clean_file_by_type(input_path, root_output_dir)

    return root_output_dir


# --- MAIN (for testing standalone) ---
if __name__ == "__main__":
    test_file = input("Enter file path to clean: ").strip()
    cleaned = universal_clean_input(test_file)
    print(f"✅ Cleaned output saved in: {cleaned}")
