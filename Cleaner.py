import os
import re
import json
import tempfile
import zipfile
import rarfile
import logging

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# NetflixId regex
pattern = re.compile(r"NetflixId\s*=\s*[^\s|]+")


# ----------------- Netscape Processor -----------------
def process_netscape_format(input_path, output_dir):
    try:
        with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()

        cookie_groups = []
        current_group = []

        for line in all_lines:
            line = line.strip()
            if line.startswith('.'):
                current_group.append(line)
            else:
                if current_group:
                    cookie_groups.append(current_group[:])
                    current_group = []

        if current_group:
            cookie_groups.append(current_group)

        if not cookie_groups:
            return []

        created_files = []
        base_name = os.path.splitext(os.path.basename(input_path))[0]

        for idx, group_lines in enumerate(cookie_groups):
            output_filename = os.path.join(output_dir, f"{base_name}_netscape_{idx+1}.txt")
            with open(output_filename, 'w', encoding='utf-8') as outfile:
                outfile.write("# Netscape HTTP Cookie File\n")
                for cookie_line in group_lines:
                    outfile.write(cookie_line + '\n')

            created_files.append(output_filename)
            logging.info(f"Created {output_filename} with {len(group_lines)} Netscape lines")

        return created_files

    except Exception as e:
        logging.error(f"Error processing Netscape file {input_path}: {e}")
        return []


# ----------------- JSON Processor -----------------
def find_all_json_objects(content):
    """Helper: Extracts all JSON objects from a raw string safely."""
    decoder = json.JSONDecoder()
    pos = 0
    results = []
    while True:
        match = re.search(r"\{", content[pos:])
        if not match:
            break
        try:
            obj, idx = decoder.raw_decode(content[pos + match.start():])
            results.append(obj)
            pos += match.start() + idx
        except Exception:
            pos += match.start() + 1
    return results


def process_json_format(input_path, output_dir):
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_filename = os.path.join(output_dir, f"{base_name}_JSON_Cleaned.txt")

    try:
        with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        all_found_cookies = find_all_json_objects(content)

        if not all_found_cookies:
            return []

        netflix_cookies = [c for c in all_found_cookies if 'netflix.com' in c.get('domain', '')]

        if not netflix_cookies:
            return []

        with open(output_filename, 'w', encoding='utf-8') as outfile:
            json.dump(netflix_cookies, outfile, indent=4)

        logging.info(f"Created {output_filename} with {len(netflix_cookies)} JSON cookies")
        return [output_filename]

    except Exception as e:
        logging.error(f"Error processing JSON file {input_path}: {e}")
        return []


# ----------------- NetflixId Regex Processor -----------------
def process_netflixid_format(path, outdir):
    base = os.path.splitext(os.path.basename(path))[0]
    output_file = os.path.join(outdir, f"{base}_netflixid.txt")

    with open(path, "r", encoding="utf-8", errors="ignore") as infile, \
         open(output_file, "w", encoding="utf-8") as outfile:
        for line in infile:
            # ✅ normalize spaces around =
            clean_line = re.sub(r"\s*=\s*", "=", line.strip())
            match = pattern.search(clean_line)
            if match:
                outfile.write(match.group(0) + "\n")

    logging.info(f"Created {output_file} with NetflixId matches")
    return [output_file]

# ----------------- File Router -----------------
def process_text_file(path, outdir):
    """Strict priority: Netscape > JSON > NetflixId (search in all lines)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]

        if any(line.startswith(".") for line in lines):
            logging.info(f"Detected Netscape format in {path}")
            return process_netscape_format(path, outdir)

        if any(line.startswith("[") for line in lines):
            logging.info(f"Detected JSON format in {path}")
            return process_json_format(path, outdir)

        logging.info(f"Fallback: NetflixId regex for {path}")
        return process_netflixid_format(path, outdir)

    except Exception as e:
        logging.error(f"Router failed for {path}: {e}")
        return []


# ----------------- Universal Cleaner (entrypoint for main.py) -----------------
def universal_clean_input(input_file: str) -> str:
    """
    Cleans the uploaded file and returns a temp directory
    containing processed .txt cookie files.
    """
    temp_dir = tempfile.mkdtemp(prefix="cleaned_")
    processed_files = []

    try:
        if input_file.endswith(".txt"):
            processed_files.extend(process_text_file(input_file, temp_dir))

        elif input_file.endswith(".zip"):
            with zipfile.ZipFile(input_file, "r") as archive:
                archive.extractall(temp_dir)
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith(".txt"):
                        file_path = os.path.join(root, f)
                        processed = process_text_file(file_path, temp_dir)
        
                        # ✅ rename raw file to avoid re-processing
                        new_name = file_path + ".raw"
                        os.rename(file_path, new_name)
        
                        processed_files.extend(processed)

        elif input_file.endswith(".rar"):
            with rarfile.RarFile(input_file, "r") as archive:
                archive.extractall(temp_dir)
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith(".txt"):
                        file_path = os.path.join(root, f)
                        processed = process_text_file(file_path, temp_dir)
        
                        # ✅ rename raw file to avoid re-processing
                        new_name = file_path + ".raw"
                        os.rename(file_path, new_name)
        
                        processed_files.extend(processed)

        logging.info(f"✅ Cleaning finished. Created {len(processed_files)} file(s).")
        return temp_dir

    except Exception as e:
        logging.error(f"Critical error in universal_clean_input: {e}")
        return temp_dir


# ----------------- Cleanup Helper -----------------
def cleanup_directory(directory: str):
    """Delete a directory and all its contents."""
    import shutil
    try:
        shutil.rmtree(directory, ignore_errors=True)
        logging.info(f"🗑️ Cleaned up directory: {directory}")
    except Exception as e:
        logging.error(f"⚠️ Failed to clean directory {directory}: {e}")
def cleanup_raw_files(directory: str):
    """Remove all .raw files in a directory tree."""
    try:
        for root, _, files in os.walk(directory):
            for f in files:
                if f.endswith(".raw"):
                    file_path = os.path.join(root, f)
                    os.remove(file_path)
                    logging.info(f"🗑️ Removed leftover raw file: {file_path}")
    except Exception as e:
        logging.error(f"⚠️ Failed to cleanup raw files in {directory}: {e}")




