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

# ---------------------- Helpers ----------------------

async def auto_delete_message(msg, delay: int = 15):
    """Delete a Telegram message after delay seconds (non-blocking)."""
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await msg.delete()

async def auto_cleanup_directory(directory: str, delay: int = 2):
    """Delete a directory after delay seconds without blocking the bot."""
    await asyncio.sleep(delay)
    if directory and os.path.exists(directory):
        cleanup_directory(directory)

async def delayed_retry_download(tg_file, file_path, delay: int = 3, timeout: int = 180):
    """Retry downloading a Telegram file after delay without blocking."""
    await asyncio.sleep(delay)
    await tg_file.download_to_drive(file_path, read_timeout=timeout)
    print(f"📥 File saved on retry: {file_path}")
    return file_path

# ---------------------- Globals ----------------------

active_processes: Dict[str, Dict] = {}
global_stop_flag = False

# ---------------------- Utility ----------------------

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
        return 'fast'  # Default

async def save_uploaded_file(tg_file, file_unique_id: str, file_name: str) -> str:
    """Save file from Telegram with retry logic."""
    temp_dir = tempfile.gettempdir()
    safe_filename = f"{file_unique_id}_{file_name}"
    file_path = os.path.join(temp_dir, safe_filename)
    try:
        await tg_file.download_to_drive(file_path, read_timeout=500)
        print(f"📥 File saved: {file_path}")
        return file_path
    except Exception as e:
        print(f"⚠️ Download failed once, retrying... Error: {e}")
        return await delayed_retry_download(tg_file, file_path, delay=3, timeout=180)

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
    """Format progress bar text"""
    if total > 0:
        progress_percent = (checked / total) * 100
        progress_bar = "█" * int(progress_percent // 10) + "░" * (10 - int(progress_percent // 10))
    else:
        progress_percent, progress_bar = 0, "░" * 10
    return (
        f"🔄 **{mode.upper()} Check in Progress**\n\n"
        f"📊 Progress: {progress_bar} {progress_percent:.1f}%\n"
        f"📈 Checked: {checked:,}/{total:,}\n"
        f"⏱️ Time: {elapsed:.1f}s\n\n"
        f"_Processing... Please wait._"
    )

def debug_directory_contents(directory: str, level: int = 0) -> None:
    """Recursively print directory contents (for debugging)."""
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
                if level < 3:
                    debug_directory_contents(item_path, level + 1)
            else:
                size = os.path.getsize(item_path)
                print(f"{indent}📄 [FILE] {item} ({size} bytes)")
    except Exception as e:
        print(f"{indent}❌ Error exploring directory {directory}: {str(e)}")

def collect_txt_files_from_directory(directory: str) -> List[str]:
    """Collect all .txt files from directory with debugging info"""
    print(f"\n🔍 DEBUGGING: Starting to collect .txt files from: {directory}")
    debug_directory_contents(directory)
    txt_files = []
    try:
        for root, dirs, files in os.walk(directory):
            print(f"🔍 Walking through: {root}")
            print(f"   📁 Subdirectories: {dirs}")
            print(f"   📄 Files: {files}")
            for file in files:
                if file.lower().endswith('.txt') and not file.startswith('.'):
                    file_path = os.path.join(root, file)
                    file_size = os.path.getsize(file_path)
                    if file_size > 0:
                        txt_files.append(file_path)
                        print(f"   ✅ Added to processing list: {file} ({file_size} bytes)")
                    else:
                        print(f"   ❌ Skipping empty file: {file}")
    except Exception as e:
        print(f"❌ Error collecting txt files from {directory}: {str(e)}")
    return txt_files
async def process_file_with_mode(update: Update, cleaned_txt_files: List[str], original_filename: str,
                                mode: str, clean_format: str = None, reply_to_message=None,
                                cleaned_temp_dir_for_cleanup: str = None):
    """Process cleaned .txt files with the specified checking mode"""
    global global_stop_flag
    print(f"\n🔄 Starting {mode.upper()} check for: {original_filename}")
    print(f"🧹 Cleaning format: {clean_format}")
    print(f"📁 {len(cleaned_txt_files)} files to check")

    global_stop_flag = False
    process_id = str(uuid4())[:8]
    active_processes[process_id] = {
        "stop_flag": False,
        "file_name": original_filename,
        "mode": mode,
        "clean_format": clean_format,
        "cleaned_files_count": len(cleaned_txt_files)
    }

    # Status message
    status_msg = await (reply_to_message or update.message).reply_text(
        f"🔄 **Starting {mode.upper()} check...**\n"
        f"📁 File: `{original_filename}`\n"
        f"🧹 Format: `{clean_format}`\n"
        f"📄 {len(cleaned_txt_files)} cleaned files\n"
        f"⏳ Initializing...",
        parse_mode='Markdown',
        reply_markup=create_status_keyboard(0, 0, process_id)
    )

    results_dir = None
    start_time = asyncio.get_event_loop().time()
    loop = asyncio.get_event_loop()
    progress = {"checked": 0, "total": 0, "valid": 0, "invalid": 0}

    async def update_status():
        last_update_time = 0
        while True:
            if active_processes.get(process_id, {}).get("stop_flag", False) or global_stop_flag:
                break
            current_time = asyncio.get_event_loop().time()
            if current_time - last_update_time >= 2:
                try:
                    await status_msg.edit_text(
                        format_processing_status(progress["checked"], progress["total"],
                                                 progress["valid"], progress["invalid"],
                                                 current_time - start_time, mode),
                        parse_mode='Markdown',
                        reply_markup=create_status_keyboard(progress["valid"], progress["invalid"], process_id)
                    )
                    last_update_time = current_time
                except Exception as e:
                    print(f"⚠️ Status update error: {e}")
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
                else:
                    checked, total, valid, invalid = step
                progress.update({"checked": checked, "total": total,
                                 "valid": valid, "invalid": invalid})
            if last_step and len(last_step) == 5:
                results_dir = last_step[4]
        except Exception as e:
            print(f"❌ Error in run_check: {e}")
            raise
        return results_dir

    # Run chosen check
    if mode == 'fast':
        results_dir = await loop.run_in_executor(None, run_check, fast_check, cleaned_txt_files)
    elif mode == 'slow':
        results_dir = await loop.run_in_executor(None, run_check, slow_check, cleaned_txt_files)
    elif mode == 'logout':
        results_dir = await loop.run_in_executor(None, run_check, logout_check, cleaned_txt_files)

    # Stop updater
    status_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await status_task

    # Final update to 100%
    if progress["total"] > 0:
        progress["checked"] = progress["total"]
        elapsed = asyncio.get_event_loop().time() - start_time
        try:
            await status_msg.edit_text(
                format_processing_status(progress["checked"], progress["total"],
                                         progress["valid"], progress["invalid"],
                                         elapsed, mode),
                parse_mode='Markdown',
                reply_markup=create_status_keyboard(progress["valid"], progress["invalid"], process_id)
            )
        except Exception as e:
            print(f"⚠️ Final status update error: {e}")

    # Delete status msg
    with contextlib.suppress(Exception):
        await status_msg.delete()

    # Check stop flag
    if active_processes.get(process_id, {}).get("stop_flag", False) or global_stop_flag:
        await update.message.reply_text(
            f"🛑 **Process Stopped**\n\n"
            f"📁 File: `{original_filename}`\n"
            f"⏹️ Stopped by user.",
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
    asyncio.create_task(auto_delete_message(summary_msg, delay=5))

    # Collect results
    valid_files, invalid_files = [], []
    if results_dir and os.path.exists(results_dir):
        for root, _, files in os.walk(results_dir):
            for file in files:
                path = os.path.join(root, file)
                if os.path.basename(root) == "valid_cookies":
                    valid_files.append(path)
                elif os.path.basename(root) == "invalid_cookies":
                    invalid_files.append(path)

    # Send valid results
    if progress['valid'] > 0 and valid_files:
        if len(valid_files) == 1:
            msg = await update.message.reply_document(open(valid_files[0], "rb"), caption="✅ Valid Results")
        else:
            zip_path = os.path.join(results_dir, "valid_results.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in valid_files:
                    zf.write(f, os.path.basename(f))
            msg = await update.message.reply_document(open(zip_path, "rb"), caption="✅ Valid Results (ZIP)")
    else:
        msg = await update.message.reply_text("⚠️ No ✅ Valid cookies found.")
        asyncio.create_task(auto_delete_message(msg, delay=5))

    # Send invalid results
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
        asyncio.create_task(auto_delete_message(msg, delay=5))

    # Cleanup results dir after send
    asyncio.create_task(auto_cleanup_directory(results_dir, delay=2))

    # Remove from active
    active_processes.pop(process_id, None)
    print(f"✅ Process {process_id} completed and cleaned up")
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded files"""
    message = update.message
    doc = message.document

    if not doc:
        await message.reply_text(
            "❌ **No file detected!**\n\n"
            "Please upload a valid file (TXT, ZIP, or RAR).",
            parse_mode='Markdown'
        )
        return

    # File size limit
    if doc.file_size and doc.file_size > 500 * 1024 * 1024:
        await message.reply_text(
            "❌ **File too large!**\n\n"
            "Please upload files smaller than 100MB.",
            parse_mode='Markdown'
        )
        return

    # RAR file support check
    if doc.file_name.lower().endswith('.rar'):
        try:
            import rarfile
        except ImportError:
            await message.reply_text(
                "❌ **RAR Support Not Available**\n\n"
                "RAR files require extra setup. Please use ZIP instead.",
                parse_mode='Markdown'
            )
            return

    print(f"\n📁 Processing file: {doc.file_name} ({doc.file_size} bytes)")

    original_file_path, cleaned_output_dir = None, None
    cleaned_txt_files = []

    try:
        # Detect cleaning & checking modes
        clean_format = detect_cleaning_mode(doc.file_name)
        mode = detect_check_mode(doc.file_name)
        print(f"🧹 Format: {clean_format}")
        print(f"🔍 Mode: {mode}")

        # Save uploaded file
        tg_file = await doc.get_file()
        original_file_path = await save_uploaded_file(tg_file, doc.file_unique_id, doc.file_name)

        # Clean file
        print(f"🧼 Cleaning cookies from: {original_file_path}")
        cleaned_output_dir = await asyncio.get_event_loop().run_in_executor(None, universal_clean_input, original_file_path)
        print(f"✅ Cleaning done. Output: {cleaned_output_dir}")

        # Collect .txt files
        cleaned_txt_files = collect_txt_files_from_directory(cleaned_output_dir)

        if not cleaned_txt_files:
            print(f"❌ No valid .txt files found after cleaning")
            temp_msg = await update.message.reply_text(
                f"❌ **No Valid Cookies Found**\n\n"
                f"File `{doc.file_name}` was processed but no valid cookies were found.\n"
                f"Supported formats: Netscape, JSON, NetflixId\n\n"
                f"Please ensure the file contains valid cookie data.",
                parse_mode='Markdown'
            )
            asyncio.create_task(auto_delete_message(temp_msg, delay=15))
        else:
            # Process file
            await process_file_with_mode(
                update,
                cleaned_txt_files,
                doc.file_name,
                mode,
                clean_format,
                cleaned_temp_dir_for_cleanup=cleaned_output_dir
            )

    except Exception as e:
        print(f"❌ Error processing file {doc.file_name}: {e}")
        error_text = html.escape(str(e)[:150] + ('...' if len(str(e)) > 150 else ''))
        safe_filename = html.escape(doc.file_name)
        error_msg = (
            f"❌ <b>Processing Error</b>\n\n"
            f"📁 File: <code>{safe_filename}</code>\n"
            f"🚫 Error: <code>{error_text}</code>\n\n"
            f"Please try again with a different file."
        )
        temp_msg = await update.message.reply_text(error_msg, parse_mode="HTML")
        asyncio.create_task(auto_delete_message(temp_msg, delay=15))

    finally:
        # Cleanup: remove original file
        if original_file_path and os.path.exists(original_file_path):
            try:
                os.remove(original_file_path)
                print(f"🗑️ Cleaned up original file: {original_file_path}")
            except Exception as e:
                print(f"⚠️ Could not remove original file {original_file_path}: {e}")

        # Cleanup: remove cleaned output if unused
        if cleaned_output_dir and os.path.exists(cleaned_output_dir) and not cleaned_txt_files:
            cleanup_directory(cleaned_output_dir)
async def handle_command_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle commands that are replies to files (/fastcheck, /slowcheck, /logout)"""

    # Extract mode
    mode = update.message.text.strip().lstrip("/").lower()
    if mode == "fastcheck":
        mode = "fast"
    elif mode == "slowcheck":
        mode = "slow"
    elif mode == "logout":
        mode = "logout"

    # Ensure user replied to a file
    if not update.message.reply_to_message:
        mode_descriptions = {
            'fast': 'Fast checking (recommended)',
            'slow': 'Detailed checking (thorough)',
            'logout': 'Logout testing (specialized)'
        }
        await update.message.reply_text(
            f"❌ <b>Reply Required</b><br><br>"
            f"Please reply to a file with <code>/{mode}</code> to run {mode_descriptions.get(mode, mode)}.<br><br>"
            f"<b>Usage:</b> Reply to an uploaded file with <code>/{mode}</code>",
            parse_mode='HTML'
        )
        return

    replied_message = update.message.reply_to_message

    if not replied_message.document:
        await update.message.reply_text(
            f"❌ <b>No file found!</b><br><br>"
            f"Please reply to a message with an attached file to use <code>/{mode}</code> mode.",
            parse_mode='HTML'
        )
        return

    print(f"🔄 Reply command: {mode} for {replied_message.document.file_name}")

    original_file_path, cleaned_output_dir, cleaned_txt_files = None, None, []

    try:
        # Download file
        tg_file = await replied_message.document.get_file()
        original_file_path = await save_uploaded_file(
            tg_file,
            replied_message.document.file_unique_id,
            replied_message.document.file_name
        )

        # Clean file
        print(f"🧼 Cleaning cookies from replied file: {original_file_path}")
        cleaned_output_dir = await asyncio.get_event_loop().run_in_executor(None, universal_clean_input, original_file_path)
        print(f"✅ Cleaning completed. Output: {cleaned_output_dir}")

        # Collect .txt files
        cleaned_txt_files = collect_txt_files_from_directory(cleaned_output_dir)

        if not cleaned_txt_files:
            temp_msg = await update.message.reply_text(
                f"❌ <b>No Valid Cookies Found</b><br><br>"
                f"File <code>{replied_message.document.file_name}</code> was processed but no valid cookies were found.",
                parse_mode='HTML'
            )
            asyncio.create_task(auto_delete_message(temp_msg, delay=30))
        else:
            # Delete the command message to keep chat clean
            with contextlib.suppress(Exception):
                await update.message.delete()

            # Process file
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
        print(f"❌ Error in handle_command_reply: {e}")
        error_text = html.escape(str(e)[:150] + ('...' if len(str(e)) > 150 else ''))
        safe_filename = html.escape(replied_message.document.file_name)
        error_msg = (
            f"❌ <b>File Processing Error</b>\n\n"
            f"📁 File: <code>{safe_filename}</code>\n"
            f"🚫 Error: <code>{error_text}</code>\n\n"
            f"Please try again with another file."
        )
        temp_msg = await update.message.reply_text(error_msg, parse_mode="HTML")
        asyncio.create_task(auto_delete_message(temp_msg, delay=15))

    finally:
        # Cleanup
        if original_file_path and os.path.exists(original_file_path):
            with contextlib.suppress(Exception):
                os.remove(original_file_path)
        if cleaned_output_dir and os.path.exists(cleaned_output_dir) and not cleaned_txt_files:
            cleanup_directory(cleaned_output_dir)
# ---------------------- Command Handlers ----------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_text = (
        "🤖 <b>RavenNF Bot</b>\n\n"
        "How to use:\n"
        "• /fast - Quick check (recommended)\n"
        "• /slow - Detailed recheck\n"
        "• /logout - Sign out test\n\n"
        "📂 Send a cookie file (TXT, ZIP, RAR) to begin."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "ℹ️ <b>Help - RavenNF Bot</b>\n\n"
        "Available commands:\n"
        "• /fast - Quick check\n"
        "• /slow - Recheck thoroughly\n"
        "• /logout - Logout testing\n"
        "• /stop - Stop current checks\n\n"
        "📂 Supported file formats: TXT, ZIP, RAR"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fast command (reply mode)"""
    await handle_command_reply(update, context)

async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /slow command (reply mode)"""
    await handle_command_reply(update, context)

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logout command (reply mode)"""
    await handle_command_reply(update, context)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command - stop all processes"""
    global global_stop_flag
    if active_processes:
        global_stop_flag = True
        for process_id in active_processes:
            active_processes[process_id]["stop_flag"] = True
        count = len(active_processes)
        await update.message.reply_text(
            f"🛑 **Stopping {count} active process(es)...**",
            parse_mode='Markdown'
        )

        # Confirm stop in background
        async def confirm_stop():
            await asyncio.sleep(2)
            await update.message.reply_text(
                "✅ **All processes stopped!**\n\n"
                "You can now upload new files or start new checks.",
                parse_mode='Markdown'
            )
        asyncio.create_task(confirm_stop())

    else:
        await update.message.reply_text(
            "ℹ️ **No active processes**\n\n"
            "There are no running checks to stop.",
            parse_mode='Markdown'
        )
# ---------------------- Callback Query Handler ----------------------

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses (Stop, No-op)"""
    query = update.callback_query
    await query.answer()
    callback_data = query.data

    if callback_data.startswith('stop_'):
        process_id = callback_data.split('_', 1)[1]
        if process_id in active_processes:
            # Set stop flag
            active_processes[process_id]["stop_flag"] = True
            process_info = active_processes[process_id]
            await query.edit_message_text(
                f"🛑 **Process Stopped**\n\n"
                f"📁 File: `{process_info['file_name']}`\n"
                f"🔧 Mode: `{process_info['mode']}`\n"
                f"⏹️ Stopping safely...",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "ℹ️ **Process Already Completed**\n\n"
                "This process has already finished or was stopped.",
                parse_mode='Markdown'
            )

    elif callback_data.startswith('noop_'):
        # No operation - just for display
        pass

# ---------------------- Main Entrypoint ----------------------

def main():
    """Main function to run the bot"""
    # Replace with your bot token
    TOKEN = "8270743184:AAFAKRM6lgN_PMA2wJLCMjaTcmhTG065wWw"

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

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Add file handler
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    # Add reply-to-file command handlers
    application.add_handler(CommandHandler("fastcheck", handle_command_reply))
    application.add_handler(CommandHandler("slowcheck", handle_command_reply))
    application.add_handler(CommandHandler("logout", handle_command_reply))

    # Add text handler for plain messages
    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 **Hi there!**\n\n"
            "I'm a cookie checker bot. Please upload a file to get started!\n\n"
            "Use `/help` to see all available commands.",
            parse_mode='Markdown'
        )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Run the bot
    print("🤖 RavenNF Bot starting...")
    print("📋 Commands: /start, /help, /fast, /slow, /logout, /stop")
    print("📁 Supported files: TXT, ZIP, RAR")
    print("🚀 Bot is ready to process files!")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()






