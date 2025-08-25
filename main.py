import os
import re
import asyncio
import shutil
import tempfile
import zipfile
import contextlib
import html
from uuid import uuid4
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from Cleaner import universal_clean_input, cleanup_directory
from Fastcheck import run_check_on_file_list as fast_check
from Slowcheck import run_check_on_file_list as slow_check
from Logout import run_check_on_file_list as logout_check

# Global variables
active_processes: Dict[str, Dict] = {}
global_stop_flag = False

def detect_cleaning_mode(filename: str) -> str:
    """Detect cleaning mode based on filename patterns"""
    filename_lower = filename.lower()
    
    if any(keyword in filename_lower for keyword in ['netscape', '.cookies', 'cookie.txt']):
        return 'netscape'
    elif any(keyword in filename_lower for keyword in ['json', '.json']):
        return 'json'
    elif any(keyword in filename_lower for keyword in ['netflix', 'netflixid', 'id']):
        return 'netflix_id'
    else:
        return 'auto-detect'

def detect_check_mode(filename: str) -> str:
    """Detect check mode based on filename patterns"""
    filename_lower = filename.lower()
    
    if any(keyword in filename_lower for keyword in ['fast', 'quick']):
        return 'fast'
    elif any(keyword in filename_lower for keyword in ['slow', 'detailed', 'full']):
        return 'slow' 
    elif any(keyword in filename_lower for keyword in ['logout', 'log']):
        return 'logout'
    else:
        return 'fast'  # Default to fast

async def save_uploaded_file(tg_file, file_unique_id: str, file_name: str) -> str:
    temp_dir = tempfile.gettempdir()
    safe_filename = f"{file_unique_id}_{file_name}"
    file_path = os.path.join(temp_dir, safe_filename)

    try:
        await tg_file.download_to_drive(file_path, read_timeout=500)
        print(f"📥 File saved: {file_path}")
        return file_path
    except Exception as e:
        print(f"⚠️ Download failed once, retrying... Error: {e}")
        await asyncio.sleep(3)
        await tg_file.download_to_drive(file_path, read_timeout=180)
        print(f"📥 File saved on retry: {file_path}")
        return file_path

        
    except Exception as e:
        print(f"❌ Error saving file {file_name}: {str(e)}")
        raise

def create_status_keyboard(valid_count: int, invalid_count: int, process_id: str) -> InlineKeyboardMarkup:
    """Create inline keyboard for process status"""
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Valid: {valid_count}", callback_data=f"noop_{process_id}"),
            InlineKeyboardButton(f"❌ Invalid: {invalid_count}", callback_data=f"noop_{process_id}")
        ],
        [
            InlineKeyboardButton("🛑 Stop Process", callback_data=f"stop_{process_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_processing_status(checked: int, total: int, valid: int, invalid: int, elapsed: float, mode: str) -> str:
    """Format the processing status message"""
    if total > 0:
        progress_percent = (checked / total) * 100
        progress_bar = "█" * int(progress_percent // 10) + "░" * (10 - int(progress_percent // 10))
    else:
        progress_percent = 0
        progress_bar = "░" * 10
    
    status = f"🔄 **{mode.upper()} Check in Progress**\n\n"
    status += f"📊 Progress: {progress_bar} {progress_percent:.1f}%\n"
    status += f"📈 Checked: {checked:,}/{total:,}\n"
    status += f"⏱️ Time: {elapsed:.1f}s\n\n"
    status += f"_Processing... Please wait._"
    
    return status

def debug_directory_contents(directory: str, level: int = 0) -> None:
    """Debug function to print all contents of a directory recursively"""
    indent = "  " * level
    try:
        print(f"{indent}📁 Exploring directory: {directory}")
        if not os.path.exists(directory):
            print(f"{indent}❌ Directory does not exist!")
            return
            
        items = os.listdir(directory)
        print(f"{indent}📋 Found {len(items)} items:")
        
        for item in items:
            item_path = os.path.join(directory, item)
            if os.path.isdir(item_path):
                print(f"{indent}📁 [DIR]  {item}/")
                if level < 3:  # Limit recursion depth
                    debug_directory_contents(item_path, level + 1)
            else:
                size = os.path.getsize(item_path)
                print(f"{indent}📄 [FILE] {item} ({size} bytes)")
                
    except Exception as e:
        print(f"{indent}❌ Error exploring directory {directory}: {str(e)}")

def collect_txt_files_from_directory(directory: str) -> List[str]:
    """Collect all .txt files from a directory and its subdirectories with enhanced debugging"""
    print(f"\n🔍 DEBUGGING: Starting to collect .txt files from: {directory}")
    
    # First, let's see what's actually in the directory
    debug_directory_contents(directory)
    
    txt_files = []
    try:
        for root, dirs, files in os.walk(directory):
            print(f"🔍 Walking through: {root}")
            print(f"   📁 Subdirectories: {dirs}")
            print(f"   📄 Files: {files}")
            
            for file in files:
                print(f"   🔎 Examining file: {file}")
                
                if file.lower().endswith('.txt') and not file.startswith('.'):
                    file_path = os.path.join(root, file)
                    file_size = os.path.getsize(file_path)
                    print(f"   📄 Found .txt file: {file} (size: {file_size} bytes)")
                    
                    skip_patterns = [
                        'cleaning_summary', 'no_cookies_found', 'cleaning_error', 
                        'archive_info', 'unsupported_format'
                    ]
                    
                    should_skip = any(pattern in file.lower() for pattern in skip_patterns)
                    
                    if should_skip:
                        print(f"   ⏭️  Skipping info file: {file}")
                        continue
                        
                    if file_size > 0:
                        txt_files.append(file_path)
                        print(f"   ✅ Added to processing list: {file}")
                        
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                first_lines = [f.readline().strip() for _ in range(3)]
                                print(f"   👀 First few lines of {file}:")
                                for i, line in enumerate(first_lines, 1):
                                    if line:
                                        print(f"      {i}: {line[:100]}{'...' if len(line) > 100 else ''}")
                        except Exception as e:
                            print(f"   ⚠️  Could not read file content: {e}")
                    else:
                        print(f"   ❌ Skipping empty file: {file}")
                else:
                    print(f"   ⏭️  Skipping non-txt file: {file}")
        
        print(f"\n📋 SUMMARY: Found {len(txt_files)} valid .txt files total:")
        for i, file_path in enumerate(txt_files, 1):
            print(f"   {i}. {os.path.basename(file_path)} ({os.path.getsize(file_path)} bytes)")
            
        return txt_files
        
    except Exception as e:
        print(f"❌ Error collecting txt files from {directory}: {str(e)}")
        return []

async def process_file_with_mode(update: Update, cleaned_txt_files: List[str], original_filename: str, 
                                mode: str, clean_format: str = None, reply_to_message=None, 
                                cleaned_temp_dir_for_cleanup: str = None):
    """Process cleaned .txt files with the specified checking mode"""
    global global_stop_flag

    print(f"\n🔄 Starting {mode.upper()} check for: {original_filename}")
    print(f"🧹 Original cleaning format: {clean_format}")
    print(f"📁 Processing {len(cleaned_txt_files)} cleaned files")
    
    for i, file_path in enumerate(cleaned_txt_files, 1):
        size = os.path.getsize(file_path)
        print(f"   {i}. {os.path.basename(file_path)} ({size} bytes)")

    global_stop_flag = False
    process_id = str(uuid4())[:8]

    active_processes[process_id] = {
        "stop_flag": False,
        "file_name": original_filename,
        "mode": mode,
        "clean_format": clean_format,
        "cleaned_files_count": len(cleaned_txt_files)
    }

    initial_keyboard = create_status_keyboard(0, 0, process_id)

    if reply_to_message:
        status_msg = await reply_to_message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 Original file: `{original_filename}`\n"
            f"🧹 Detected format: `{clean_format}`\n"
            f"📄 Processing {len(cleaned_txt_files)} cleaned files\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )
    else:
        status_msg = await update.message.reply_text(
            f"🔄 **Starting {mode.upper()} check...**\n"
            f"📁 Original file: `{original_filename}`\n"
            f"🧹 Detected format: `{clean_format}`\n"
            f"📄 Processing {len(cleaned_txt_files)} cleaned files\n"
            f"⏳ Initializing...",
            parse_mode='Markdown',
            reply_markup=initial_keyboard
        )

    results_dir = None
    try:
        start_time = asyncio.get_event_loop().time()
        loop = asyncio.get_event_loop()
        progress = {"checked": 0, "total": 0, "valid": 0, "invalid": 0}

        async def update_status():
            last_update_time = 0
            while True:
                if (active_processes.get(process_id, {}).get("stop_flag", False) or global_stop_flag):
                    break
                current_time = asyncio.get_event_loop().time()
                if current_time - last_update_time >= 2:
                    elapsed = current_time - start_time
                    status_text = format_processing_status(
                        progress["checked"], progress["total"],
                        progress["valid"], progress["invalid"],
                        elapsed, mode
                    )
                    keyboard = create_status_keyboard(progress["valid"], progress["invalid"], process_id)
                    try:
                        await status_msg.edit_text(status_text, parse_mode='Markdown', reply_markup=keyboard)
                        last_update_time = current_time
                    except Exception as edit_error:
                        print(f"⚠️ Status update error: {edit_error}")
                await asyncio.sleep(1)

        status_task = asyncio.create_task(update_status())

        def run_check(func, list_of_files):
            nonlocal results_dir
            last_step = None
            try:
                for step in func(list_of_files, live=True):
                    last_step = step
                    if len(step) == 5:
                        checked, total, valid, invalid, temp_results_dir = step
                        results_dir = temp_results_dir
                    elif len(step) == 4:
                        checked, total, valid, invalid = step
                    progress.update({"checked": checked, "total": total, "valid": valid, "invalid": invalid})
                # ensure we capture last yield with results_dir
                if last_step and len(last_step) == 5:
                    results_dir = last_step[4]
            except Exception as e:
                print(f"❌ Error in run_check: {str(e)}")
                raise
            return results_dir

        # Run the actual check
        if mode == 'fast':
            results_dir = await loop.run_in_executor(None, run_check, fast_check, cleaned_txt_files)
        elif mode == 'slow':
            results_dir = await loop.run_in_executor(None, run_check, slow_check, cleaned_txt_files)
        elif mode == 'logout':
            results_dir = await loop.run_in_executor(None, run_check, logout_check, cleaned_txt_files)

        # Stop updater
        # Force final progress update to 100%
        if progress["total"] > 0:
            progress["checked"] = progress["total"]
            elapsed = asyncio.get_event_loop().time() - start_time
            final_status = format_processing_status(
                progress["checked"], progress["total"],
                progress["valid"], progress["invalid"],
                elapsed, mode
            )
            try:
                await status_msg.edit_text(
                    final_status,
                    parse_mode='Markdown',
                    reply_markup=create_status_keyboard(progress["valid"], progress["invalid"], process_id)
                )
            except Exception as e:
                print(f"⚠️ Final status update failed: {e}")

        # Stop updater cleanly
        status_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await status_task


        if (active_processes.get(process_id, {}).get("stop_flag", False) or global_stop_flag):
            await status_msg.edit_text(
                f"🛑 **Process Stopped**\n\n"
                f"📁 File: `{original_filename}`\n"
                f"⏹️ Process was stopped by user request.",
                parse_mode='Markdown'
            )
            return

        # Final summary
        final_elapsed = asyncio.get_event_loop().time() - start_time
        completion_text = (
            f"✅ <b>{mode.upper()} Check Completed!</b>\n\n"
            f"📁 File: <code>{original_filename}</code>\n"
            f"✅ Valid: {progress['valid']}\n"
            f"❌ Invalid: {progress['invalid']}\n"
        )
        if final_elapsed > 0:
            completion_text += f"\n📉 Speed: {progress['total']/final_elapsed:.1f} checks/sec"
        summary_msg = await (reply_to_message or update.message).reply_text(completion_text, parse_mode='HTML')
        await asyncio.sleep(5)
        with contextlib.suppress(Exception):
            await summary_msg.delete()

        # Collect results before cleanup
        valid_files, invalid_files = [], []
        if results_dir and os.path.exists(results_dir):
            for root, _, files in os.walk(results_dir):
                for file in files:
                    path = os.path.join(root, file)
                    if os.path.basename(root) == "valid_cookies":
                        valid_files.append(path)
                    elif os.path.basename(root) == "invalid_cookies":
                        invalid_files.append(path)
        # Delete the live progress/status message BEFORE sending files
        with contextlib.suppress(Exception):
            await status_msg.delete()

        # Send results
        if progress['valid'] > 0 and valid_files:
            if len(valid_files) == 1:
                await update.message.reply_document(open(valid_files[0], "rb"), caption="✅ Valid Results")
            else:
                zip_path = os.path.join(results_dir, "valid_results.zip")
                with zipfile.ZipFile(zip_path, "w") as zf:
                    for f in valid_files:
                        zf.write(f, os.path.basename(f))
                await update.message.reply_document(open(zip_path, "rb"), caption="✅ Valid Results (ZIP)")
        else:
            msg = await update.message.reply_text("⚠️ No ✅ Valid cookies found.")
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await msg.delete()

        # --- Send invalid results ---
        if progress['invalid'] > 0 and invalid_files:
            if len(invalid_files) == 1:
                msg = await update.message.reply_document(open(invalid_files[0], "rb"), caption="❌ Invalid Results")
            else:
                zip_path = os.path.join(results_dir, "invalid_results.zip")
                with zipfile.ZipFile(zip_path, "w") as zf:
                    for f in invalid_files:
                        zf.write(f, os.path.basename(f))
                msg = await update.message.reply_document(open(zip_path, "rb"), caption="❌ Invalid Results (ZIP)")
        else:
            msg = await update.message.reply_text("⚠️ No ❌ Invalid cookies found.")
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await msg.delete()


        # Delay cleanup slightly to ensure Telegram reads files
        with contextlib.suppress(Exception):
            await status_msg.delete()

        # Delay cleanup slightly to ensure Telegram reads files
        await asyncio.sleep(2)
        if results_dir and os.path.exists(results_dir):
            cleanup_directory(results_dir)
            print(f"🗑️ Cleaned up results directory: {results_dir}")


    finally:
        print(f"🧹 Cleaning up process {process_id}...")
        with contextlib.suppress(Exception):
            status_task.cancel()
            await status_task
        if cleaned_temp_dir_for_cleanup and os.path.exists(cleaned_temp_dir_for_cleanup):
            cleanup_directory(cleaned_temp_dir_for_cleanup)
        active_processes.pop(process_id, None)
        print(f"✅ Process {process_id} completed and cleaned up")




# --- The rest of your original main.py remains unchanged (handlers, commands, main() entrypoint) ---
# (I’ll stop here to not overflow, but all command handlers stay the same; only the check functions and cleanup were fixed.)



async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded files"""
    message = update.message
    doc = message.document

    if doc:
        # Check file size (limit to 100MB)
        if doc.file_size and doc.file_size > 100 * 1024 * 1024:
            await message.reply_text(
                "❌ **File too large!**\n\n"
                "Please upload files smaller than 100MB.",
                parse_mode='Markdown'
            )
            return

        # Check for RAR files and warn about rarfile dependency
        if doc.file_name.lower().endswith('.rar'):
            try:
                import rarfile
            except ImportError:
                await message.reply_text(
                    "❌ **RAR Support Not Available**\n\n"
                    "RAR files require additional setup. Please use ZIP files instead.",
                    parse_mode='Markdown'
                )
                return

        print(f"\n📁 Processing file: {doc.file_name} ({doc.file_size} bytes)")

        # Initialize file paths for cleanup
        original_file_path = None
        cleaned_output_dir = None

        try:
            # Detect cleaning and checking modes based on filename
            clean_format = detect_cleaning_mode(doc.file_name)
            mode = detect_check_mode(doc.file_name)
            
            print(f"🧹 Detected cleaning format: {clean_format}")
            print(f"🔍 Detected check mode: {mode}")

            # Save the uploaded file
            tg_file = await doc.get_file()
            original_file_path = await save_uploaded_file(
                tg_file, doc.file_unique_id, doc.file_name
            )

            # Step 1: Clean the input file using universal_clean_input
            print(f"🧼 Cleaning cookies from: {original_file_path}")
            cleaned_output_dir = await asyncio.get_event_loop().run_in_executor(
                None, universal_clean_input, original_file_path
            )
            print(f"✅ Cleaning completed. Output directory: {cleaned_output_dir}")

            # Step 2: Collect all .txt files from the cleaned output directory
            cleaned_txt_files = collect_txt_files_from_directory(cleaned_output_dir)

            if not cleaned_txt_files:
                print(f"❌ No valid .txt files found after cleaning")
                temp_msg = await update.message.reply_text(
                    f"❌ **No Valid Cookies Found**\n\n"
                    f"File `{doc.file_name}` was processed but no valid cookies were found.\n"
                    f"Supported formats: Netscape, JSON, NetflixId\n\n"
                    f"Please ensure your file contains valid cookie data.",
                    parse_mode='Markdown'
                )
                
                # Auto-delete message after 15 seconds
                await asyncio.sleep(15)
                try:
                    await temp_msg.delete()
                except Exception:
                    pass
                return

            # Step 3: Process the cleaned .txt files
            await process_file_with_mode(
                update,
                cleaned_txt_files,
                doc.file_name,
                mode,
                clean_format,
                cleaned_temp_dir_for_cleanup=cleaned_output_dir
            )

        except Exception as e:
            print(f"❌ Error processing file {doc.file_name}: {str(e)}")
            

            error_text = html.escape(str(e)[:150] + ('...' if len(str(e)) > 150 else ''))
            safe_filename = html.escape(update.message.document.file_name)

            error_msg = (
                f"❌ <b>Reply Processing Error</b>\n\n"
                f"📁 File: <code>{safe_filename}</code>\n"
                f"🚫 Error: <code>{error_text}</code>\n\n"
                f"Please try again with a different file."
            )
            temp_msg = await update.message.reply_text(error_msg, parse_mode="HTML")
            try:
                await temp_msg.delete()
            except Exception:
                pass


            
        finally:
            # Cleanup: Remove original uploaded file
            if original_file_path and os.path.exists(original_file_path):
                try:
                    os.remove(original_file_path)
                    print(f"🗑️ Cleaned up original file: {original_file_path}")
                except Exception as e:
                    print(f"⚠️ Could not clean up original file {original_file_path}: {e}")
            
            # Cleanup: Remove cleaned output directory (will be done in process_file_with_mode)
            # But ensure it's cleaned if process_file_with_mode wasn't called
            if (cleaned_output_dir and os.path.exists(cleaned_output_dir) and 
                not cleaned_txt_files):  # Only clean if no files were found
                cleanup_directory(cleaned_output_dir)

    else:
        await message.reply_text(
            "❌ **No file detected!**\n\n"
            "Please upload a valid file (TXT, ZIP, or RAR).",
            parse_mode='Markdown'
        )

async def handle_command_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle commands that are replies to files"""

    # Extract mode directly from the command text
    mode = update.message.text.strip().lstrip("/").lower()

    # Normalize modes so they match process_file_with_mode
    if mode == "fastcheck":
        mode = "fast"
    elif mode == "slowcheck":
        mode = "slow"
    elif mode == "logout":
        mode = "logout"

    if not update.message.reply_to_message:
        mode_descriptions = {
            'fast': 'Fast checking (recommended)',
            'slow': 'Detailed checking (thorough)',
            'logout': 'Logout testing (specialized)'
        }

        await update.message.reply_text(
            f"❌ <b>Reply Required</b><br><br>"
            f"Please reply to a file with <code>/{mode}</code> to run {mode_descriptions.get(mode, mode)} mode.<br><br>"
            f"<b>Usage:</b> Reply to any uploaded file with <code>/{mode}</code>",
            parse_mode='HTML'
        )
        return

    replied_message = update.message.reply_to_message

    # Check if the replied message has a document
    if replied_message.document:
        print(f"🔄 Processing reply command: {mode} for {replied_message.document.file_name}")

        original_file_path = None
        cleaned_output_dir = None
        cleaned_txt_files = []

        try:
            # Download the file
            tg_file = await replied_message.document.get_file()
            original_file_path = await save_uploaded_file(
                tg_file,
                replied_message.document.file_unique_id,
                replied_message.document.file_name
            )

            # Clean file
            print(f"🧼 Cleaning cookies from replied file: {original_file_path}")
            cleaned_output_dir = await asyncio.get_event_loop().run_in_executor(
                None, universal_clean_input, original_file_path
            )
            print(f"✅ Cleaning completed. Output: {cleaned_output_dir}")

            # Collect .txt files
            cleaned_txt_files = collect_txt_files_from_directory(cleaned_output_dir)

            if not cleaned_txt_files:
                temp_msg = await update.message.reply_text(
                    f"❌ <b>No Valid Cookies Found</b><br><br>"
                    f"File <code>{original_filename}</code> was processed but no valid cookies were found.",
                    parse_mode='HTML'
                )
                await asyncio.sleep(5)
                try:
                    await temp_msg.delete()
                except Exception:
                    pass
                return

            # Delete the command message to keep chat clean
            try:
                await update.message.delete()
            except Exception:
                pass

            # Process the file(s) in chosen mode
            await process_file_with_mode(
                update,
                cleaned_txt_files,
                replied_message.document.file_name,
                mode,
                detect_cleaning_mode(replied_message.document.file_name),
                reply_to_message=replied_message,
                cleaned_temp_dir_for_cleanup=cleaned_output_dir
            )

        except Exception as e:
            print(f"❌ Error in handle_command_reply: {str(e)}")

            error_text = html.escape(str(e)[:150] + ('...' if len(str(e)) > 150 else ''))
            safe_filename = html.escape(replied_message.document.file_name)


            error_msg = (
                f"❌ <b>File Processing Error</b>\n\n"
                f"📁 File: <code>{safe_filename}</code>\n"
                f"🚫 Error: <code>{error_text}</code>\n\n"
                f"Please try again with a different file."
            )
            temp_msg = await update.message.reply_text(error_msg, parse_mode="HTML")
            try:
                await temp_msg.delete()
            except Exception:
                pass


        finally:
            # Cleanup
            if original_file_path and os.path.exists(original_file_path):
                try:
                    os.remove(original_file_path)
                except Exception:
                    pass

            if cleaned_output_dir and os.path.exists(cleaned_output_dir) and not cleaned_txt_files:
                cleanup_directory(cleaned_output_dir)

    else:
        await update.message.reply_text(
            f"❌ <b>No file found!</b><br><br>"
            f"Please reply to a message with an attached file to use <code>/{mode}</code> mode.",
            parse_mode='HTML'
        )


# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_text = (
        " RavenNF_bot\n\n"
        " How to use:\n"
        " /fast - Quick \n"
        " /slow - Recheck \n"
        " /logout - Sign out\n\n"
        " Send cookie file (zip,rar,txt)"
    )
    
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "RavenNF_bot\n\n"
        "How to use:\n"
        "/fast - Quick \n"
        "/slow - Recheck \n"
        "/logout - Sign out\n\n"
        " Send cookie file (zip,rar,txt)"
    )
    
    await update.message.reply_text(help_text, parse_mode='HTML')

async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fast command"""
    await handle_command_reply(update, context, 'fast')

async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /slow command"""
    await handle_command_reply(update, context, 'slow')

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logout command"""
    await handle_command_reply(update, context, 'logout')

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command - stop all processes"""
    global global_stop_flag
    
    if active_processes:
        global_stop_flag = True
        
        # Set stop flag for all active processes
        for process_id in active_processes:
            active_processes[process_id]["stop_flag"] = True
        
        count = len(active_processes)
        await update.message.reply_text(
            f"🛑 **Stopping {count} active process(es)...**\n\n"
            f"Please wait while processes are terminated safely.",
            parse_mode='Markdown'
        )
        
        # Wait a bit for processes to stop
        await asyncio.sleep(2)
        
        await update.message.reply_text(
            f"✅ **All processes stopped!**\n\n"
            f"You can now upload new files or start new checks.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "ℹ️ **No active processes**\n\n"
            "There are currently no running checks to stop.",
            parse_mode='Markdown'
        )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data.startswith('stop_'):
        process_id = callback_data.split('_', 1)[1]
        
        if process_id in active_processes:
            # Set stop flag for this specific process
            active_processes[process_id]["stop_flag"] = True
            
            process_info = active_processes[process_id]
            await query.edit_message_text(
                f"🛑 **Process Stopped**\n\n"
                f"📁 File: `{process_info['file_name']}`\n"
                f"🔧 Mode: `{process_info['mode']}`\n"
                f"⏹️ Stopping process safely...",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"ℹ️ **Process Already Completed**\n\n"
                f"The process has already finished or been stopped.",
                parse_mode='Markdown'
            )
    
    elif callback_data.startswith('noop_'):
        # No operation - just for display
        pass

def main():
    """Main function to run the bot"""
    # Replace with your bot token
    TOKEN = "8340045274:AAHLEaAExw9cy5ot8FqVfGFwIpGwoXxjLQg"
    
    if TOKEN == "TOKEN":
        print("❌ Please set your bot token in the main() function")
        return
    
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("fast", fast_command))
    application.add_handler(CommandHandler("slow", slow_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # Add callback query handler for inline keyboards
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Add file handler
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    # Command handlers for reply-to-file mode
    application.add_handler(CommandHandler("fastcheck", handle_command_reply))
    application.add_handler(CommandHandler("slowcheck", handle_command_reply))
    application.add_handler(CommandHandler("logout", handle_command_reply))



    # Add handler for text messages (for error handling)
    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 **Hi there!**\n\n"
            "I'm a cookie checker bot. Please upload a file to get started!\n\n"
            "Use `/help` to see all available commands.",
            parse_mode='Markdown'
        )
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Run the bot
    print("🤖 Netflix Cookie Checker Bot starting...")
    print("📋 Available commands: /start, /help, /fast, /slow, /logout, /stop")
    print("📁 Supported files: TXT, ZIP, RAR")
    print("🚀 Bot is ready to process files!")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

